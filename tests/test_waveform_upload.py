"""Waveform upload, firmware capability probe and output tracking for CV/DPV.

Background (bench-diagnosed 2026-09-23): CV/DPV uploads used 66-byte legacy
buffer writes, which travel as a 64-byte and a 2-byte USB packet. Older
firmware could run its parser between the two, store 62 bytes, and leave
every later waveform point 2 bytes off its float boundary; the stepper then
turned garbage floats into rail-to-rail DAC output until the ring wrapped.

These tests drive the real driver code against ``FakeFirmware``, a byte-level
model of the firmware's parser, ring buffer and batch stepper with a dummy
resistor as the cell, so a fault injected on the wire shows up in the
measured potential exactly as it did on the bench.
"""

import math
import threading

import numpy as np
import pytest

from moles.driver import ps4_ref
from moles.driver.ps4_ref import (Commands, Potentiostat, WaveformTracker,
                                  WaveformTrackingError, WAVEFORM_MSG_DATA_BYTES)
from moles.methods.cv import LivePotentiostat

RING = 24000
KDAC12 = np.float32(4095 / 3.3)
USB_PACKET = 64


def _dac_volts(setpoint):
    """ADC-scale volts the DAC produces for a stored setpoint float, including
    the firmware's lrintf conversion and the 12-bit register truncation."""
    x = float(np.float32(setpoint) * KDAC12)
    if math.isnan(x):
        code = 0
    elif abs(x) >= 2 ** 31:
        code = (2 ** 31 - 1) if x > 0 else -(2 ** 31)
    else:
        code = int(np.rint(x))
    return (code & 0xFFF) * 3.3 / 4095


class FakeFirmware:
    """Byte-level stand-in for the potentiostat firmware, as a serial port.

    ``positional``: firmware has CMD_BUFFER_WRITE_AT (July 2026+).
    ``new_framing``: firmware from this fix (partial-float writes dropped,
    corrupted setpoints stop the batch, five-counter diagnostics).
    ``split_messages``: indices of host messages whose first USB packet gets
    parsed on its own, the way the old firmware's frame-silence race did.
    ``merge_messages``: indices of host messages parsed in one frame together
    with the message after them, as when the parser runs late.
    ``timing``: firmware with absolute step timing, the end-of-step sampling
    and starvation fail-safe batch options, and the ten-counter diagnostics
    (late September 2026).
    ``inject``: per-run counters (e.g. ``{'late_steps': 4}``) the next batch
    will report, standing in for events during the run.
    """

    def __init__(self, positional=True, new_framing=True,
                 split_messages=(), merge_messages=(), dummy_ohm=1000.0,
                 timing=False):
        self.positional = positional
        self.new_framing = new_framing
        self.timing = timing
        self.late_steps = self.max_late_ms = self.resyncs = self.starve_aborts = 0
        self.inject = {}
        self.batch_flags = []       # option flags each batch was started with
        self.bad_gain_at = None     # batch point whose gain byte arrives corrupted
        self.split_messages = set(split_messages)
        self.merge_messages = set(merge_messages)
        self.dummy_ohm = dummy_ohm
        self.timeout = 10
        self.out = bytearray()
        self.writes = []            # every host write, as sent
        self.ring = bytearray(RING)
        self.buffer_ind = 0
        self.bytes_written = 0
        self.dac = np.float32(1.6336)
        self.switch = 1
        self.auto_gain = False
        self.batch_count = 0
        self.batch_ind = 0
        self.batch_byte = 0
        self.starved = 0
        self.rejected = 0
        self.rx_overflow = 0
        self.batches = []           # setpoints each batch applied, per batch
        self._held = None           # message waiting to merge with the next

    @property
    def applied(self):
        """Setpoints applied by the most recent batch (the probe runs its own)."""
        return self.batches[-1] if self.batches else []

    # -- serial.Serial surface -------------------------------------------
    @property
    def in_waiting(self):
        return len(self.out)

    def flush(self):
        pass

    def reset_input_buffer(self):
        self.out.clear()

    def close(self):
        pass

    def write(self, data):
        data = bytes(data)
        index = len(self.writes)
        self.writes.append(data)
        if self._held is not None:
            data, self._held = self._held + data, None
        elif index in self.merge_messages:
            self._held = data
            return len(data)
        if index in self.split_messages and len(data) > USB_PACKET:
            self._parse(data[:USB_PACKET])
            self._parse(data[USB_PACKET:])
        else:
            self._parse(data)
        return len(data)

    def read(self, n):
        while len(self.out) < n and self._step():
            pass
        chunk, self.out = bytes(self.out[:n]), self.out[n:]
        return chunk

    def readline(self):
        end = self.out.find(b"\n")
        if end < 0:
            chunk, self.out = bytes(self.out), bytearray()
            return chunk
        chunk, self.out = bytes(self.out[:end + 1]), self.out[end + 1:]
        return chunk

    # -- firmware model -------------------------------------------------
    def _ok(self):
        self.out += b"OK\r\n"

    def _parse(self, frame):
        pos = 0
        while pos + 2 <= len(frame):
            cmd = frame[pos + 1]
            if cmd == Commands._WRITE_BUFFER_AT and self.positional:
                if pos + 4 > len(frame):
                    self.rx_overflow += 1
                    return
                dlen = int.from_bytes(frame[pos + 2:pos + 4], "little")
                if pos + 6 + dlen > len(frame):
                    self.rx_overflow += 1
                    return
                wpos = int.from_bytes(frame[pos + 4:pos + 6], "little")
                payload = frame[pos + 6:pos + 6 + dlen]
                aligned = (wpos % 4 == 0 and dlen % 4 == 0) or not self.new_framing
                if wpos + dlen <= RING and aligned:
                    self.ring[wpos:wpos + dlen] = payload
                    self.bytes_written += dlen
                pos += 6 + dlen
                continue
            self._legacy(cmd, frame[pos + 2:])
            return

    def _legacy(self, cmd, p):
        if cmd == Commands._WRITE_BUFFER:
            if self.new_framing and len(p) % 4:
                self.rx_overflow += 1
                return
            if self.buffer_ind + len(p) > RING:
                self.buffer_ind = 0
            self.ring[self.buffer_ind:self.buffer_ind + len(p)] = p
            self.buffer_ind += len(p)
            self.bytes_written += len(p)
        elif cmd == Commands._RESET_BUFFER:
            self.buffer_ind = self.bytes_written = int.from_bytes(p[:4], "little")
            self.starved = self.rejected = 0
            self.late_steps = self.max_late_ms = self.resyncs = self.starve_aborts = 0
            self._ok()
        elif cmd == Commands.EXECUTE_VOLTAGE_BATCH:
            words = np.frombuffer(p[:12] if len(p) >= 12 else p[:8], np.uint32)
            self.batch_count = words[1]
            self.batch_ind = self.batch_byte = 0
            if self.batch_count:
                self.batches.append([])
                self.batch_flags.append(int(words[2]) if (self.timing and len(words) > 2) else 0)
                for name, value in self.inject.items():
                    setattr(self, name, value)
            self._ok()
        elif cmd == Commands.READ_DAC:
            self.out += np.float32(self.dac).tobytes()
        elif cmd == Commands.WRITE_DAC:
            n = len(p) // 5
            for channel, value in zip(p[:n], np.frombuffer(p[n:5 * n], np.float32)):
                if channel == 0:
                    self.dac = np.float32(value)
            self._ok()
        elif cmd == Commands.STOP_CURRENT_HOLD:
            self._ok()
        elif cmd == Commands.WRITE_SWITCH:
            self.switch = p[0]
            self._ok()
        elif cmd == Commands.READ_DIAGNOSTICS:
            vals = [0x0C000000, 0, self.rx_overflow, self.starved]
            if self.new_framing:
                vals.append(self.rejected)
            if self.timing:
                vals += [self.late_steps, self.max_late_ms, self.resyncs,
                         self.starve_aborts, 0x7]
            self.out += np.uint32(vals).tobytes()

    def _step(self):
        """Run one batch step; False when no sample can be produced."""
        if self.batch_ind >= self.batch_count:
            return False
        if self.bytes_written - self.batch_ind * 4 < 4:
            self.starved += 1
            return False
        setpoint = np.frombuffer(bytes(self.ring[self.batch_byte:self.batch_byte + 4]),
                                 np.float32)[0]
        if self.new_framing and not (0.0 <= setpoint <= np.float32(3.3)):
            self.rejected += 1
            self.batch_count = 0
            self.switch = 0
            return False
        self.dac = setpoint
        self.batches[-1].append(setpoint)
        # Dummy resistor between the electrodes: the sensed potential follows
        # the DAC exactly and the current follows Ohm's law.
        re_out = np.float32(_dac_volts(setpoint))
        potential = (float(re_out) / 3.3) * 10 - 5
        bipolar_we = -(potential / self.dummy_ohm) * 100.0
        we_out = np.float32((bipolar_we + 5) / 10 * 3.3)
        self.out += np.float32([we_out, re_out]).tobytes()
        if self.auto_gain:
            bad = (len(self.batches[-1]) - 1) == self.bad_gain_at
            self.out += bytes([200 if bad else 0])
        self.batch_ind += 1
        self.batch_byte += 4
        if self.batch_byte + 4 > RING:
            self.batch_byte = 0
        return True


def _make(cls=Potentiostat, **fw_kwargs):
    fw = FakeFirmware(**fw_kwargs)
    ps = cls.__new__(cls)
    Potentiostat.__init__(ps, serial_port="FAKE")
    ps.serial = fw
    ps.ResistorVal = 100.0
    ps.Resistor = 0
    return ps, fw


def _sweep(n_points=5400, lo=-0.5, hi=0.5):
    """A triangular CV-like waveform, long enough to need steady-state feeding
    after the ~16 s pre-fill."""
    half = n_points // 2
    return np.concatenate([np.linspace(lo, hi, half), np.linspace(hi, lo, n_points - half)])


def _prefill_message_index(fw, write_index):
    """Pre-fill message number of a host write (the probe sends its own
    commands first, and the upload starts after the second buffer reset)."""
    resets = [i for i, w in enumerate(fw.writes) if w[1] == Commands._RESET_BUFFER]
    return write_index - (resets[1] + 1)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(ps4_ref.time, "sleep", lambda s: None)


class TestUploadFraming:
    def test_every_waveform_message_fits_one_usb_packet(self):
        for positional in (True, False):
            ps, fw = _make(positional=positional)
            ps.write_voltage_batch(_sweep(), 4)
            waveform_msgs = [w for w in fw.writes
                             if w[1] in (Commands._WRITE_BUFFER, Commands._WRITE_BUFFER_AT)]
            assert waveform_msgs
            assert max(len(w) for w in waveform_msgs) <= USB_PACKET

    def test_probe_selects_positional_writes_on_new_firmware(self):
        ps, fw = _make(positional=True)
        ps.write_voltage_batch(_sweep(), 4)
        assert ps._positional_writes_supported is True
        after_probe = fw.writes[6:]
        assert not any(w[1] == Commands._WRITE_BUFFER for w in after_probe)

    def test_probe_falls_back_to_legacy_on_old_firmware(self):
        ps, fw = _make(positional=False, new_framing=False)
        out = ps.write_voltage_batch(_sweep(), 4)
        assert ps._positional_writes_supported is False
        np.testing.assert_allclose(np.asarray(fw.applied, float),
                                   ps._scale_input_voltages(_sweep()), atol=1e-6)
        assert out.shape == (5400, 2)

    def test_probe_never_moves_the_output(self):
        ps, fw = _make(positional=True)
        before = np.float32(1.2345)
        fw.dac = before
        ps._probe_positional_writes()
        assert _dac_volts(fw.dac) == _dac_volts(before)
        assert abs(float(fw.dac) - float(before)) <= float(np.spacing(before))

    def test_positional_upload_survives_merged_messages(self):
        # Merge pre-fill messages and steady-state feeds pairwise
        ps, fw = _make(positional=True, merge_messages={20, 21, 150, 400, 420, 430})
        ps.write_voltage_batch(_sweep(), 4)
        np.testing.assert_allclose(np.asarray(fw.applied, float),
                                   ps._scale_input_voltages(_sweep()), atol=1e-6)


class TestHistoricalFault:
    """The Aug 16 failure: a 66-byte legacy message split after 64 bytes."""

    def test_old_chunking_reproduces_the_rail_to_rail_output(self, monkeypatch):
        monkeypatch.setattr(ps4_ref, "WAVEFORM_MSG_DATA_BYTES", 64)
        ps, fw = _make(positional=False, new_framing=False, split_messages={100})

        class _Disabled:
            def __init__(self, programmed):
                pass

            def update(self, first_index, measured):
                return None

        # Guard off, to see the raw fault the way the bench saw it
        monkeypatch.setattr(ps4_ref, "WaveformTracker", _Disabled)
        # The cut left the ring 2 bytes short, so the final point starves
        with pytest.raises(ps4_ref.SerialReadTimeout):
            ps.write_voltage_batch(_sweep(), 4)
        applied = np.asarray(fw.applied, float)
        drift = np.abs(applied - ps._scale_input_voltages(_sweep())[:len(applied)])
        first_bad = int(np.flatnonzero(drift > 1e-3)[0])
        # Message k carries floats 16k..16k+15; the one straddling the cut is
        # the first read off its boundary (the Aug 16 onset was 16*232+15)
        k = _prefill_message_index(fw, 100)
        assert first_bad == 16 * k + 15
        assert np.mean(drift[first_bad:] > 0.1) > 0.5

    def test_tracker_stops_the_historical_fault_within_two_chunks(self, monkeypatch):
        monkeypatch.setattr(ps4_ref, "WAVEFORM_MSG_DATA_BYTES", 64)
        ps, fw = _make(positional=False, new_framing=False, split_messages={100})
        with pytest.raises(WaveformTrackingError, match="stopped following"):
            ps.write_voltage_batch(_sweep(), 4)
        onset = 16 * _prefill_message_index(fw, 100) + 15
        assert len(fw.applied) - onset <= 9, "stopped within the chunk after the fault"
        assert fw.switch == 0
        assert fw.batch_count == 0

    def test_single_packet_messages_cannot_be_split(self):
        # The same wire fault on today's message size has nothing to cut
        ps, fw = _make(positional=False, new_framing=False,
                       split_messages=set(range(0, 800)))
        ps.write_voltage_batch(_sweep(), 4)
        np.testing.assert_allclose(np.asarray(fw.applied, float),
                                   ps._scale_input_voltages(_sweep()), atol=1e-6)

    def test_new_firmware_drops_a_cut_legacy_message_instead_of_misaligning(self, monkeypatch):
        monkeypatch.setattr(ps4_ref, "WAVEFORM_MSG_DATA_BYTES", 64)
        ps, fw = _make(positional=False, new_framing=True, split_messages={100})
        ps._positional_writes_supported = False
        try:
            ps.write_voltage_batch(_sweep(), 4)
        except Exception:
            pass
        applied = np.asarray(fw.applied, float)
        assert np.all((applied >= 0) & (applied <= 3.3)), "no garbage setpoint applied"


class TestFirmwareRejection:
    def test_rejected_setpoint_is_reported_clearly(self):
        ps, fw = _make(positional=True, new_framing=True)
        start = ps._start_waveform_upload
        def corrupt_after_upload(prefill):
            pos = start(prefill)
            fw.ring[400:404] = np.float32(np.nan).tobytes()
            return pos
        ps._start_waveform_upload = corrupt_after_upload
        with pytest.raises(WaveformTrackingError, match="refused a corrupted"):
            ps.write_voltage_batch(_sweep(), 4)
        assert fw.switch == 0
        assert fw.rejected == 1

    def test_live_path_reports_rejection_too(self):
        ps, fw = _make(cls=LivePotentiostat, positional=True, new_framing=True)
        ps.data_callback = lambda v, i: None
        ps.stop_event = threading.Event()
        start = ps._start_waveform_upload
        def corrupt_after_upload(prefill):
            pos = start(prefill)
            fw.ring[800:804] = np.float32(7.5).tobytes()
            return pos
        ps._start_waveform_upload = corrupt_after_upload
        with pytest.raises(WaveformTrackingError):
            ps.write_voltage_batch(_sweep(), 4)
        assert fw.switch == 0


class TestLivePath:
    def test_live_run_trips_and_plots_the_faulty_chunk(self, monkeypatch):
        monkeypatch.setattr(ps4_ref, "WAVEFORM_MSG_DATA_BYTES", 64)
        ps, fw = _make(cls=LivePotentiostat, positional=False, new_framing=False,
                       split_messages={120})
        chunks = []
        ps.data_callback = lambda v, i: chunks.append(np.asarray(v))
        ps.stop_event = threading.Event()
        with pytest.raises(WaveformTrackingError):
            ps.write_voltage_batch(_sweep(), 4)
        plotted = np.concatenate(chunks)
        assert np.max(np.abs(np.diff(plotted))) > 1.0, "fault visible in the plot"
        assert fw.switch == 0

    def test_clean_live_run_completes(self):
        ps, fw = _make(cls=LivePotentiostat, positional=True)
        chunks = []
        ps.data_callback = lambda v, i: chunks.append(np.asarray(v))
        ps.stop_event = threading.Event()
        out = ps.write_voltage_batch(_sweep(), 4)
        assert out.shape == (5400, 2)
        assert sum(len(c) for c in chunks) == 5400
        assert ps.serial.timeout == 10, "batch read timeout restored"


class TestTracker:
    def _run(self, programmed, measured):
        tracker = WaveformTracker(programmed)
        for i in range(0, len(measured), 6):
            fault = tracker.update(i, measured[i:i + 6])
            if fault:
                return i, fault
        return None

    def test_tracking_sweep_passes(self):
        prog = _sweep(1200)
        noise = np.random.default_rng(0).normal(0, 0.002, prog.size)
        assert self._run(prog, prog + 0.03 + noise) is None

    def test_lagging_cell_on_a_big_step_passes(self):
        prog = np.r_[np.zeros(60), np.ones(60), np.zeros(60)] * 2.0
        measured = np.empty_like(prog)
        v = 0.0
        for k, target in enumerate(prog):   # slew-limited to 0.1 V per sample
            v += np.clip(target - v, -0.1, 0.1)
            measured[k] = v
        assert self._run(prog, measured) is None

    def test_cell_following_big_programmed_steps_passes(self):
        prog = np.tile(np.r_[np.zeros(3), np.ones(3)], 40) * 1.5   # square wave
        assert self._run(prog, prog.copy()) is None

    def test_dpv_pulses_pass(self):
        steps = np.repeat(np.arange(-0.5, 0.5, 0.005), 62)
        pulses = np.tile(np.r_[np.zeros(50), np.full(12, 0.05)], steps.size // 62)
        prog = steps + pulses
        assert self._run(prog, prog + 0.02) is None

    def test_rail_to_rail_garbage_trips_immediately(self):
        prog = _sweep(1200)
        measured = prog.copy()
        rng = np.random.default_rng(1)
        measured[600:] = rng.choice([-4.9, 4.9, 1.2, -2.0], size=600)
        hit = self._run(prog, measured)
        assert hit is not None and hit[0] <= 606

    def test_output_parked_at_a_rail_trips(self):
        # Slow scans misalign into long runs at one rail rather than chatter
        prog = _sweep(1200)
        measured = prog.copy()
        measured[500:] = -4.9
        hit = self._run(prog, measured)
        assert hit is not None and hit[0] <= 504

    def test_output_drifting_out_of_compliance_passes(self):
        prog = np.linspace(0, -4.0, 2000)
        measured = np.maximum(prog, -2.5)          # saturates at -2.5 V
        measured += np.random.default_rng(2).normal(0, 0.002, prog.size)
        assert self._run(prog, measured) is None

    def test_single_glitch_is_tolerated(self):
        prog = _sweep(1200)
        measured = prog.copy()
        measured[300] += 1.0
        assert self._run(prog, measured) is None


class TestDiagnostics:
    def test_old_firmware_report_has_four_counters(self):
        ps, fw = _make(new_framing=False)
        report = ps.read_diagnostics()
        assert 'rejected_setpoints' not in report
        assert report['starved_steps'] == 0

    def test_new_firmware_report_includes_rejected_setpoints(self):
        ps, fw = _make(new_framing=True)
        fw.rejected = 3
        assert ps.read_diagnostics()['rejected_setpoints'] == 3

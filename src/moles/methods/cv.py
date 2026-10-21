"""Hardware adapter for live-streaming CV and DPV experiments.

``LivePotentiostat`` subclasses the base ``Potentiostat`` and overrides
``write_voltage_batch`` so that each chunk of data is processed and emitted
to a callback as it arrives from the hardware, enabling live plot updates.

``ExperimentStopped`` is the exception raised when the user requests an
early stop — it is caught by ``CVWorker`` to trigger a clean shutdown.
``HostLagError`` is raised when this computer cannot keep up with the
device's data stream; the run is aborted safely before Windows would cut
off communication mid-stream. ``WaveformTrackingError`` (from the driver) is
raised when the output stops following the programmed waveform; the cell is
disconnected before it propagates.
"""

import logging
import time

import numpy as np

from ..driver.ps4_ref import Commands, Potentiostat, WaveformTracker

logger = logging.getLogger(__name__)

# Unread bytes allowed to pile up in the operating system's receive buffer
# before we react ("backlog"). The host loop normally consumes data as fast as
# the device produces it, so the backlog stays near zero. If the computer falls
# behind (heavy plotting, antivirus, sleep), Windows silently stops reading
# from the device once roughly 12 kB accumulates — which stalls the device and,
# on unpatched firmware, ends in a watchdog reset and a dead COM port. Warn
# well before that, and abort the run cleanly while communication still works.
BACKLOG_WARN_BYTES = 4096
BACKLOG_ABORT_BYTES = 8192

# How often (in loop iterations) to log the loop rate and backlog. At 250 Hz
# sampling one iteration covers 6 samples, so 500 iterations ≈ 12 s.
TELEMETRY_EVERY_ITERS = 500


class ExperimentStopped(Exception):
    """Raised when the user requests a stop mid-experiment."""
    pass


class HostLagError(Exception):
    """Raised when the computer falls too far behind the device's data stream.

    The experiment is aborted (cell switched off) before the operating system
    cuts off USB communication, which would leave the device streaming into
    nowhere and require a power-cycle on unpatched firmware.
    """
    pass


class LivePotentiostat(Potentiostat):
    """Potentiostat subclass that emits live data chunks during a voltage sweep.

    Overrides ``write_voltage_batch`` to intercept each 6-point chunk as it
    is read back from the hardware and forward it to ``data_callback``. This
    lets the UI update the plot in real time rather than waiting for the full
    sweep to complete.

    Args:
        serial_port: USB/COM port string for this device.
        device_ID: hardware device ID (default 1).
        data_callback: called with ``(potential_V, current_uA)`` numpy arrays
            after each chunk is validated and converted to real units.
        stop_event: a ``threading.Event``; when set, the sweep is aborted
            after the current chunk and ``ExperimentStopped`` is raised.
    """

    def __init__(self, serial_port, device_ID=1, data_callback=None, stop_event=None):
        super().__init__(serial_port=serial_port, device_ID=device_ID)
        self.data_callback = data_callback
        self.stop_event = stop_event

    def _halt_batch_stream(self):
        """Stop the device's batch stream and drain the data already in flight.

        Order matters: the device is told to stop *while this side keeps
        reading*. If the host simply stops reading (or closes the port) while
        the device is still streaming, the device's next transmission has
        nowhere to go and unpatched firmware hangs until its watchdog resets
        it — the "needs a power-cycle" failure. A batch-execute with a count
        of 0 halts the stream; we then read until the line goes quiet and
        discard the leftovers.
        """
        try:
            self._write_cmd(Commands.EXECUTE_VOLTAGE_BATCH, np.uint32([0, 0]), False)
        except Exception as e:
            logger.warning("[PS %s] Could not send batch halt: %s", self.device_ID, e)
        old_timeout = self.serial.timeout
        drain_deadline = time.time() + 2.0  # overall cap on the drain
        try:
            self.serial.timeout = 0.2
            while time.time() < drain_deadline:
                if not self.serial.read(4096):
                    break  # stream has gone quiet
        except Exception as e:
            logger.warning("[PS %s] Drain after batch halt failed: %s", self.device_ID, e)
        finally:
            self.serial.timeout = old_timeout
            try:
                self.serial.reset_input_buffer()
            except Exception:
                pass

    def write_voltage_batch(self, voltages, delay, before_start=None,
                            auto_range=False, range_period=1, lead_in=0,
                            ir=None):
        """Run a voltage sweep and emit live data chunks as they arrive.

        Identical to the parent ``write_voltage_batch`` except that after
        parsing each 6-point chunk it validates the readings, converts them
        to real units (V, µA), and calls ``self.data_callback``. Corrupted
        chunks (frame-shift artifacts) are marked invalid in the returned
        data and left off the live plot rather than crashing the sweep. Like
        the parent, it stops the run and disconnects the cell
        (``WaveformTrackingError``) if the output leaves the programmed
        waveform, holds the port lock for the whole batch, and leaves the
        run's report in ``last_batch_report``.
        """
        with self._port_lock:
            return self._write_voltage_batch_live(voltages, delay, before_start,
                                                  auto_range, range_period, lead_in,
                                                  ir)

    def _write_voltage_batch_live(self, voltages, delay, before_start,
                                  auto_range=False, range_period=1, lead_in=0,
                                  ir=None):
        v_per_chunk = 6       # potentials written to hardware per transaction
        bytes_per_v = 4       # each potential is a float32
        tx_len = v_per_chunk * bytes_per_v

        new_voltages = self._scale_input_voltages(voltages)
        v_count = int(len(new_voltages))

        # Pad to a multiple of v_per_chunk by repeating the last potential
        remainder = v_count % v_per_chunk
        pad_count = 0
        if remainder > 0:
            pad_count = v_per_chunk - remainder
            for i in range(pad_count):
                new_voltages = np.append(new_voltages, new_voltages[-1])
            v_count += pad_count

        v_bytes = np.float32(new_voltages).tobytes()
        v_byte_count = len(v_bytes)

        # Dynamic pre-fill: give the device ~16 s of waveform up front so a slow
        # or briefly-stalled host never starves it (a starved device replays
        # stale buffer contents on unpatched firmware). Clamped to 700 chunks
        # (16.8 kB) to leave headroom in the 24 kB hardware buffer. Uploading
        # the pre-fill takes ~2.5 s at the paced write rate, which delays the
        # start of the sweep by the same amount.
        safe_delay = max(1.0, float(delay))
        target_prefill_chunks = int(np.ceil((16000.0 / safe_delay) / v_per_chunk))
        pre_fill_chunks = max(20, min(target_prefill_chunks, 700))
        first_tx_bytes = pre_fill_chunks * tx_len

        if first_tx_bytes > v_byte_count:
            first_tx_bytes = v_byte_count
        first_tx_bytes -= first_tx_bytes % tx_len  # keep chunk-aligned

        tx_ind = 0
        report = self._begin_batch_report(delay, auto_range, range_period, ir)
        ring_pos = self._start_waveform_upload(v_bytes[0:first_tx_bytes])
        time.sleep(0.01)  # allow hardware to receive the pre-fill before starting
        tx_ind = first_tx_bytes

        # Response size per point: float32 current + float32 potential [+ uint8 gain]
        rx_len = 9 if self._auto_gain else 8

        temp_adc = np.zeros((v_count, 2), np.float32)
        # The range each point was read on: reported per point when readings
        # carry it, else the range set before the run
        temp_range = np.full(v_count, self._range_index(), dtype=int)
        res_table = self._res_table()
        tracker = WaveformTracker(voltages)

        if before_start is not None:
            before_start()
        # A stop during the quiet time ends the run before the batch starts
        if self.stop_event and self.stop_event.is_set():
            raise ExperimentStopped("Stop requested by user")
        self._execute_batch(delay, v_count, report)

        backlog_warned = False
        telemetry_t0 = time.time()

        with self._batch_read_timeout(delay):
            for i in range(0, v_count, v_per_chunk):
                # Check for a user stop before blocking on the next hardware read.
                # The stream must be halted and drained BEFORE we stop reading,
                # otherwise the device is left transmitting into a closed pipe.
                if self.stop_event and self.stop_event.is_set():
                    self._halt_batch_stream()
                    raise ExperimentStopped("Stop requested by user")

                # Backlog guard: if unread data piles up, this computer is falling
                # behind the device. Warn early; abort safely well before Windows
                # would cut off USB reads mid-stream (~12 kB of backlog).
                backlog = self.serial.in_waiting
                if backlog >= BACKLOG_ABORT_BYTES:
                    logger.error(
                        "[PS %s] Host fell %d bytes behind the data stream — "
                        "aborting run before communication is lost.",
                        self.device_ID, backlog,
                    )
                    self._halt_batch_stream()
                    self.write_switch(False)  # fail-safe: cut cell power before raising
                    raise HostLagError(
                        f"This computer could not keep up with the device "
                        f"({backlog} bytes behind). The run was stopped safely. "
                        f"Close other programs or reduce the plotted data before retrying."
                    )
                if backlog >= BACKLOG_WARN_BYTES and not backlog_warned:
                    backlog_warned = True
                    logger.warning(
                        "[PS %s] Host is lagging the data stream (%d bytes unread).",
                        self.device_ID, backlog,
                    )
                elif backlog < BACKLOG_WARN_BYTES // 2:
                    backlog_warned = False  # recovered; re-arm the warning

                # Periodic loop-rate log: makes a slow host visible long before it fails.
                if i and (i // v_per_chunk) % TELEMETRY_EVERY_ITERS == 0:
                    now = time.time()
                    rate = TELEMETRY_EVERY_ITERS / max(now - telemetry_t0, 1e-9)
                    telemetry_t0 = now
                    logger.info(
                        "[PS %s] Batch loop at %.1f it/s (need %.1f), backlog %d B",
                        self.device_ID, rate, 1000.0 / max(float(delay), 1.0) / v_per_chunk,
                        backlog,
                    )

                data = self._read_batch_chunk(rx_len * v_per_chunk)
                report['chunk_times_s'].append(time.monotonic())
                if (tx_ind + tx_len) <= v_byte_count:
                    ring_pos = self._write_waveform(v_bytes[tx_ind:tx_ind + tx_len], ring_pos)
                    tx_ind += tx_len

                chunk_adc = np.zeros((v_per_chunk, 2), np.float32)
                temp_ind = 0

                for j in range(v_per_chunk):
                    temp_adc[i + j, :] = np.frombuffer(data, np.float32, 2, temp_ind)
                    temp_ind += 8
                    # Raw floats parsed off-register are usually huge numbers
                    if np.any(np.abs(temp_adc[i + j, :]) > 15.0):
                        logger.warning("[PS %s] Frame shift detected! Invalid "
                                       "potential/current raw: %s",
                                       self.device_ID, temp_adc[i + j, :])
                        temp_adc[i + j, :] = np.nan
                    if self._auto_gain:
                        temp_gain = int(np.frombuffer(data, np.uint8, 1, temp_ind)[0])
                        temp_ind += 1
                        # An impossible gain index means the frame is shifted:
                        # invalidate the point rather than let a KeyError
                        # abandon the stream mid-run
                        if temp_gain in self.res_vals:
                            temp_range[i + j] = temp_gain
                        else:
                            logger.warning("[PS %s] Frame shift detected! Invalid "
                                           "gain index: %s", self.device_ID, temp_gain)
                            temp_range[i + j] = 0
                            temp_adc[i + j, :] = np.nan
                    chunk_adc[j, :] = temp_adc[i + j, :]
                chunk_range = temp_range[i:i + v_per_chunk]

                # Live emission: validate, convert to real units, call callback.
                # Lead-in and padding points are not part of the result, so
                # not plotted either.
                first = max(0, lead_in - i)
                last = min(v_per_chunk, v_count - pad_count - i)
                if self.data_callback and first < last:
                    try:
                        proc_chunk = self._scale_output_voltages(chunk_adc[first:last].copy())
                        # IUPAC sign convention
                        proc_chunk[:, 0] = -proc_chunk[:, 0] / res_table[chunk_range[first:last]]

                        # Potential sanity check — hardware max is ~±5 V; >13 V signals a frame shift
                        if np.any(np.abs(proc_chunk[:, 1]) > 13.0):
                            raise ValueError(f"Potential out of range: {proc_chunk[:, 1]}")
                        if not np.all(np.isfinite(proc_chunk)):
                            raise ValueError("chunk holds invalid samples")

                        # Convert with the board's read calibrations, so the
                        # live plot matches the saved perform_CV data exactly
                        current_uA = np.round(
                            self.calibrate_batch_current_uA(proc_chunk[:, 0],
                                                            chunk_range[first:last]), 5)
                        potential_V = np.round(
                            self.calibrate_batch_potential_V(proc_chunk[:, 1]), 5)

                        self.data_callback(potential_V, current_uA)

                    except ValueError as ve:
                        logger.warning("[PS %s] Skipping corrupted chunk: %s", self.device_ID, ve)
                    except Exception as e:
                        logger.warning("[PS %s] Live callback error: %s", self.device_ID, e)

                # Checked after the chunk is plotted, so the moment it went
                # wrong stays visible on screen
                fault = tracker.update(i, self._scale_output_voltages(chunk_adc[:, 1]))
                if fault:
                    self._abort_waveform_run(fault)

        # Same final processing as the parent: raw currents and potentials,
        # the range of each point, and the run report
        return self._finish_batch_currents(temp_adc, temp_range, report,
                                           v_count - pad_count, v_per_chunk, lead_in)

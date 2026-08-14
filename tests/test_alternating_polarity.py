"""Tests for the alternating-polarity method's failure handling and guards.

Covers the July 2026 crash fixes and the continuous-streaming rework:

  * Degenerate waveforms (sample rate too low to draw the requested shape)
    are refused before any hardware is energized.
  * The waveform runs as one continuous stream; a stream failure halts the
    batch before the stream is re-armed, so a retry cannot race the
    firmware's stepper, and data delivered between failures resets the
    abort counter.
  * The shutdown path halts the stream and opens the switch via the
    spike-free ``_safe_open_switch``.

Everything runs against the mock potentiostat, so no hardware is required.
"""

import queue
import threading

import pytest

from moles.driver.mock import MockPotentiostat
from moles.driver.ps4_ref import SerialReadTimeout
from moles.methods.alternating_polarity import run_alternating_polarity


def _mock_ps():
    ps = MockPotentiostat(serial_port="MOCK_AP")
    ps.connect()
    return ps


def _run(ps, params, output_queue=None):
    return run_alternating_polarity(
        ps, params, output_queue if output_queue is not None else queue.Queue(),
        "T", threading.Event(),
    )


class TestWaveformGuard:
    def test_undersampled_sine_is_refused(self):
        # 50 Hz sine at 50 Hz sampling = 2 points/cycle, both on the sine's
        # zero crossings — the run that silently output 0 V on the bench.
        params = {
            'ap_waveform': 'Sine', 'ap_frequency_hz': 50.0, 'ap_sample_hz': 50.0,
            'total_charge_C': 1.0,
        }
        with pytest.raises(ValueError, match="points per cycle"):
            _run(_mock_ps(), params)

    def test_one_sided_square_is_refused(self):
        # 2 points/cycle at 90% duty: both samples land in the positive phase,
        # so the negative level is never output at all.
        params = {
            'ap_waveform': 'Square', 'ap_frequency_hz': 25.0, 'ap_sample_hz': 50.0,
            'ap_duty_cycle': 0.9, 'total_charge_C': 1.0,
        }
        with pytest.raises(ValueError, match="duty"):
            _run(_mock_ps(), params)

    def test_guard_rejects_before_energizing(self):
        ps = _mock_ps()
        params = {
            'ap_waveform': 'Sine', 'ap_frequency_hz': 50.0, 'ap_sample_hz': 50.0,
            'total_charge_C': 1.0,
        }
        with pytest.raises(ValueError):
            _run(ps, params)
        assert ps._switch_state is False


class TestStreaming:
    PARAMS = {
        'ap_waveform': 'Square', 'ap_frequency_hz': 1.0, 'ap_sample_hz': 10.0,
        'ap_pos_peak_V': 1.0, 'ap_neg_peak_V': -1.0, 'ap_duty_cycle': 0.5,
        'total_charge_C': 0.001,
    }

    def test_run_completes_with_continuous_data(self):
        ps = _mock_ps()
        out = queue.Queue()
        assert _run(ps, dict(self.PARAMS), out) == "completed"
        assert ps._switch_state is False

        # Streamed points must be continuous: strictly increasing time and
        # non-decreasing cumulative charge, with no gaps between blocks.
        points = []
        while not out.empty():
            points.append(out.get())
        assert len(points) >= 2
        times = [p[0] for p in points]
        charges = [p[3] for p in points]
        assert all(t2 > t1 for t1, t2 in zip(times, times[1:]))
        assert all(q2 >= q1 for q1, q2 in zip(charges, charges[1:]))

    def test_50hz_sine_at_500hz_sampling_runs(self):
        # The 500 Hz sample ceiling exists to allow 50 Hz sines at exactly
        # the 10-points-per-cycle minimum.
        ps = _mock_ps()
        params = {
            'ap_waveform': 'Sine', 'ap_frequency_hz': 50.0, 'ap_sample_hz': 500.0,
            'ap_pos_peak_V': 1.0, 'ap_neg_peak_V': -1.0, 'ap_duty_cycle': 0.5,
            'total_charge_C': 0.001,
        }
        assert _run(ps, params) == "completed"

    def test_failed_stream_halts_before_restart(self):
        ps = _mock_ps()
        halts = []
        ps.halt_batch_stream = lambda: halts.append(True)

        real_batch = ps.write_voltage_batch
        calls = {'n': 0}

        def flaky_batch(voltages, delay):
            calls['n'] += 1
            if calls['n'] == 1:
                raise SerialReadTimeout("mock mid-stream failure")
            return real_batch(voltages, delay)

        ps.write_voltage_batch = flaky_batch

        assert _run(ps, dict(self.PARAMS)) == "completed"
        assert calls['n'] >= 2
        # One halt ending the failed attempt, one ending the successful
        # stream, one in the shutdown steps.
        assert len(halts) == 3

    def test_data_between_failures_resets_abort_counter(self):
        # Two failures, a good block, then two more failures: four failures
        # total, but never three in a row without data — the run must survive
        # because the abort counter resets once data flows. (The charge target
        # needs two good blocks, so the run spans the whole pattern.)
        ps = _mock_ps()
        real_batch = ps.write_voltage_batch
        calls = {'n': 0}

        def pattern_batch(voltages, delay):
            calls['n'] += 1
            if calls['n'] in (1, 2, 4, 5):
                raise SerialReadTimeout("mock intermittent failure")
            return real_batch(voltages, delay)

        ps.write_voltage_batch = pattern_batch
        assert _run(ps, dict(self.PARAMS, total_charge_C=0.010)) == "completed"

    def test_persistent_failure_aborts_with_comm_outcome(self):
        ps = _mock_ps()

        def dead_batch(voltages, delay):
            raise SerialReadTimeout("mock persistent failure")

        ps.write_voltage_batch = dead_batch
        assert _run(ps, dict(self.PARAMS)) == "comm_failure"
        # The shutdown must still have opened the switch.
        assert ps._switch_state is False

"""Background worker that runs one CV, DPV, or OCP experiment off the main thread.

``VoltammetryWorker`` is a ``QRunnable`` that drives either a real
``LivePotentiostat`` or, in Test Mode, a ``MockLivePotentiostat``. It streams
live data chunks to the UI as they arrive and, on completion, saves the result
and reports it back through Qt signals.

For DPV the raw per-sample staircase is streamed live, then converted into the
differential curve (delta-i vs step potential) once the sweep finishes — the
form a chemist expects to read peaks from.

An OCP run drives nothing at all: the cell stays disconnected and the worker
streams (time, potential) pairs instead of (potential, current) ones.
"""

import logging
import threading
from datetime import datetime

import numpy as np
from PyQt5 import QtCore

from ... import claims
from ...settings import get_cv_data_folder
from ...calibration import load_calibrations
from ...logging_setup import LOG_DIR
from ...driver.mock import MockLivePotentiostat, MockPotentiostat
from ...driver.ps4_ref import (DEFAULT_QUIET_S, FW_FEATURE_IR_COMP, IR_MAX_FRACTION,
                                RANGE_LABELS, Potentiostat, SwitchState)
from ...methods.cv import ExperimentStopped, LivePotentiostat
from ...methods.dpv import (applied_dpv_settings, build_dpv_waveform,
                            cyclic_sweeps, process_dpv, samples_per_segment)
from ...methods.ocp import DEFAULT_SAMPLE_HZ as OCP_SAMPLE_HZ, monitor_ocp
from ...session import connect_with_retry

logger = logging.getLogger(__name__)

# Acquisition sampling rates. Kept here so the worker, the differential
# processing, and the saved data all use one consistent value.
CV_STEP_HZ = 250
DPV_SAMPLE_HZ = 250

# Point rate for an iR-compensated CV. Every point costs one serial round trip
# (set the potential, read it back), so this sweep cannot keep up with the
# batched CV path above.
IR_STEP_HZ = 150

# Potential step used to measure R_u. Larger steps give a cleaner reading —
# the current grows with the step while the noise floor does not — but must
# stay small enough not to drive a reaction: at 100 mV a step landed on the
# ferrocene wave and sustained faradaic current polluted the estimate
# (bench 2026-08-14). The firmware step capture has signal to spare at 50 mV.
RU_STEP_MV = 50.0

# One R_u measurement on its own cannot be trusted: on the bench, single
# values scattered from -8 % to +35 % around the true resistance (2026-09-21:
# 621, 496, 427 ohm on a ~460 ohm cell), and the high one pushed a "70 %"
# live compensation to ~95 % — the edge of stability. So each click takes
# several independent measurements and refuses them if they disagree.
RU_REPEATS = 3
RU_MAX_SPREAD = 0.10

# Extra columns an iR-compensated CV file carries after the standard six, so
# the potential axis can be rebuilt later from a better R_u. No commas inside
# a name, and nothing matching the loader's 'potential'/'current' keywords can
# come before the standard columns it finds by name.
IR_EXTRA_COLUMNS = "Potential measured (V),iR drop subtracted (V)"

# Last column of CV and DPV raw files: the current range each point was read
# on, 0 (+/-50 mA) to 4 (+/-5 uA). The name must not match the loader's
# 'potential'/'current' keywords.
RANGE_COLUMN = "Range"


def combine_ru_repeats(results, max_spread=RU_MAX_SPREAD):
    """Combine repeated ``measure_R_u`` results into one value, or refuse.

    Every repeat must have succeeded, and the spread (largest minus smallest,
    relative to the median) must be within ``max_spread``. The median is
    reported so one stray repeat cannot drag the value.

    Returns:
        tuple: ``(ok, R_u_ohm, message)`` — ``R_u_ohm`` is None when refused.
    """
    values = []
    for n, result in enumerate(results, start=1):
        R_u = float(result.get('R_u_ohm', float('inf')))
        if not result.get('fit_ok', np.isfinite(R_u)) or not np.isfinite(R_u):
            reason = (result.get('message')
                      or "no current flowed — check the cell connections")
            if n > 1:
                reason = f"repeat {n} was refused: {reason}"
            return False, None, reason[0].upper() + reason[1:]
        values.append(R_u)
    if not values:
        return False, None, "No R_u measurement was made."

    median = float(np.median(values))
    spread = (max(values) - min(values)) / median
    listing = ", ".join(f"{v:.0f}" for v in values)
    if spread > max_spread:
        return False, None, (
            f"Repeats disagree by {spread:.0%} ({listing} ohm), more than the "
            f"{max_spread:.0%} allowed. Let the cell rest and measure again; "
            f"if it keeps disagreeing, take R_u from uncompensated CVs "
            f"instead (peak separation vs. peak current across scan rates).")
    return True, median, f"median of {listing} ohm, spread {spread:.0%}"


class RuWorkerSignals(QtCore.QObject):
    """Signals emitted by ``RuWorker``."""
    # pot_id, success flag, message, measured R_u in ohms (None on failure)
    finished = QtCore.pyqtSignal(str, bool, str, object)
    status = QtCore.pyqtSignal(str, str)  # pot_id, status message


class RuWorker(QtCore.QRunnable):
    """Measure one board's solution resistance, off the main thread.

    Runs outside an experiment, so it takes and releases the board's claim
    itself. The cell is energized only for the moment of the step, and power is
    always cut before the worker exits.
    """

    def __init__(self, pot_id, port, use_mock=False, step_mV=RU_STEP_MV,
                 repeats=RU_REPEATS):
        super().__init__()
        self.pot_id = pot_id
        self.port = port
        self.use_mock = use_mock
        self.step_mV = step_mV
        self.repeats = max(1, int(repeats))
        self.signals = RuWorkerSignals()
        self.ps = None
        self.setAutoDelete(True)

    def run(self):
        claim_key = None
        if not self.use_mock:
            claim_key = claims.claim_key_for(self.pot_id, self.port)
            try:
                claims.acquire(claim_key, app="electroanalysis",
                               experiment="R_u measurement", port=self.port)
            except claims.ClaimBusyError as e:
                self.signals.finished.emit(
                    self.pot_id, False, f"Busy — {e.claim.describe()}", None)
                return

        try:
            self.signals.status.emit(self.pot_id, "Measuring R_u...")

            if self.use_mock:
                self.ps = MockPotentiostat(serial_port=self.port)
            else:
                self.ps = Potentiostat(serial_port=self.port, device_ID=1)
                # R_u is derived from a calibrated current reading, so the
                # board's own calibration has to be loaded first.
                cal = load_calibrations().get(self.pot_id)
                if cal:
                    self.ps.set_calibration_params(
                        cal['slope'], cal['intercept'],
                        cal.get('read_slope'), cal.get('read_intercept'))
                    if cal.get('v_slope') is not None:
                        self.ps.set_voltage_calibration_params(
                            cal['v_slope'], cal['v_intercept'],
                            cal.get('v_read_slope'), cal.get('v_read_intercept'))

            connect_with_retry(self.ps, self.pot_id)
            results = []
            for n in range(1, self.repeats + 1):
                if self.repeats > 1:
                    self.signals.status.emit(
                        self.pot_id, f"Measuring R_u ({n} of {self.repeats})...")
                result = self.ps.measure_R_u(step_mV=self.step_mV)
                results.append(result)

                # Keep the raw decay traces on disk — success or refusal — so
                # a suspect value can be diagnosed from what the fit actually
                # saw instead of from one scalar.
                self._save_traces(result, repeat=n if self.repeats > 1 else None)

                # A refused repeat already sinks the measurement; stop here
                # rather than keep stepping the cell.
                if not result.get('fit_ok', np.isfinite(result['R_u_ohm'])):
                    break

            # The driver refuses estimates its decay fit cannot back up, and
            # repeats that disagree are refused here; either way the reason
            # is passed through rather than a number the data cannot support.
            ok, R_u, note = combine_ru_repeats(results)
            if not ok:
                self.signals.finished.emit(self.pot_id, False, note, None)
                return

            msg = f"R_u = {R_u:.1f} ohm"
            if self.repeats > 1:
                msg += f" ({note})"
            driver_notes = sorted({r['message'] for r in results if r.get('message')})
            if driver_notes:
                msg += " — " + "; ".join(driver_notes)
            self.signals.finished.emit(self.pot_id, True, msg, R_u)

        except Exception as e:
            self.signals.finished.emit(self.pot_id, False, str(e), None)

        finally:
            # Always cut power before disconnecting (hardware safety requirement).
            if self.ps is not None:
                try:
                    self.ps.write_switch(SwitchState.Off)
                except Exception:
                    pass
                try:
                    self.ps.disconnect()
                except Exception:
                    pass
            if claim_key is not None:
                claims.release(claim_key)

    def _save_traces(self, result, repeat=None):
        """Write the measurement's decay traces to ~/.moles/logs/ru_traces/.

        One CSV per measurement (per repeat, suffixed ``_r<n>``), long format
        (one row per sample, labeled by its step), with the fit's verdicts in
        comment lines up top. Saving must never break the measurement, so any
        failure is only logged.
        """
        polarities = result.get('polarities') or []
        if not polarities:
            return  # mock or legacy result without traces
        try:
            folder = LOG_DIR / "ru_traces"
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            suffix = f"_r{repeat}" if repeat is not None else ""
            path = folder / f"RU_PS-{self.pot_id}_{stamp}{suffix}.csv"

            header = [
                f"R_u_ohm: {result.get('R_u_ohm')}",
                f"fit_ok: {result.get('fit_ok')}",
                f"message: {result.get('message', '')}",
            ]
            rows = []
            # The zero-step control (instrument artifact template) is saved
            # as step 0, before the two step traces it was subtracted from.
            control = result.get('control')
            if control is not None:
                header.append(
                    f"control 0 mV: source={control.get('source')} "
                    f"ocp={control.get('ocp'):.4f} V "
                    f"baseline={control.get('baseline_A', 0.0) * 1e6:.1f} uA")
                for t, i in zip(control['times_ms'], control['currents_A']):
                    rows.append((0.0, t, i * 1e3))
            for p in polarities:
                header.append(
                    f"step {p['step_V'] * 1e3:+.0f} mV: source={p.get('source')} "
                    f"ocp={p.get('ocp'):.4f} V baseline={p.get('baseline_A', 0.0) * 1e6:.1f} uA")
                for t, i in zip(p['times_ms'], p['currents_A']):
                    rows.append((p['step_V'] * 1e3, t, i * 1e3))
            fit = result.get('fit')
            if fit:
                header.append(
                    f"fit (difference of the two steps): from_ms={fit.get('blank_ms')} "
                    f"to_ms={fit.get('window_ms')} tau_ms={fit.get('tau_ms')} "
                    f"I_at_0_A={fit.get('I_at_0_A')} rise_A={fit.get('rise_A')} "
                    f"fall_A={fit.get('fall_A')}")

            np.savetxt(
                path, np.asarray(rows), delimiter=",", fmt='%.6f',
                header="\n".join(header)
                       + "\nStep (mV),Time (ms),Current baseline-corrected (mA)",
            )
        except Exception:
            logger.exception("Could not save R_u traces for %s", self.pot_id)


class VoltammetryWorkerSignals(QtCore.QObject):
    """Signals emitted by ``VoltammetryWorker`` for its tab to consume."""
    # pot_id, success flag, status message, result dict, primary file path
    finished = QtCore.pyqtSignal(str, bool, str, object, object)
    status = QtCore.pyqtSignal(str, str)                 # pot_id, status message
    data_chunk = QtCore.pyqtSignal(str, object, object)  # pot_id, V array, I array


class VoltammetryWorker(QtCore.QRunnable):
    """Run one CV, DPV, or OCP experiment in a thread-pool worker.

    Connects to the potentiostat (real or mock), configures auto-gain, runs the
    selected method while streaming live chunks, saves the data, and emits
    progress signals throughout. Power to the cell is always cut before the
    worker exits, on success or failure alike.
    """

    def __init__(self, pot_id, port, params, use_mock=False):
        super().__init__()
        self.pot_id = pot_id
        self.port = port
        self.params = params
        self.use_mock = use_mock
        self.signals = VoltammetryWorkerSignals()
        self.stop_event = threading.Event()
        self.ps = None
        # Set when the device reports the run did not follow its waveform
        # cleanly (see _record_run_report)
        self._degraded = False
        self._scan_started = False
        self.setAutoDelete(True)

    def stop(self):
        """Request a clean stop after the current data chunk completes."""
        self.stop_event.set()

    def force_abort(self):
        """Hard abort for a device that no longer responds: closing the port
        makes any blocked serial call raise immediately, freeing the worker.

        The switch-off command cannot reach a wedged device, so the cell may
        still be energized — power-cycle the potentiostat before reconnecting.
        """
        self.stop_event.set()
        if self.ps is not None:
            self.ps.force_close()

    def _live_callback(self, v_chunk, i_chunk):
        """Forward a chunk to the main thread for live plotting. The pair is
        whatever the running method streams: (potential, current) for CV and
        DPV, (time, potential) for OCP. The first chunk marks the end of the
        preparation (connecting, zeroing, uploading, quiet time)."""
        if not self._scan_started:
            self._scan_started = True
            self.signals.status.emit(self.pot_id, "Running...")
        self.signals.data_chunk.emit(self.pot_id, v_chunk, i_chunk)

    def run(self):
        # Claim the board before touching its port, so a board mid-electrolysis
        # in another app is refused with a message naming the current holder.
        # Mock devices are process-local and need no cross-app coordination.
        claim_key = None
        if not self.use_mock:
            claim_key = claims.claim_key_for(self.pot_id, self.port)
            try:
                claims.acquire(
                    claim_key,
                    app="electroanalysis",
                    experiment=self.params.get('name', ''),
                    port=self.port,
                )
            except claims.ClaimBusyError as e:
                self.signals.status.emit(self.pot_id, f"Busy — {e.claim.describe()}")
                self.signals.finished.emit(self.pot_id, False, str(e), None, None)
                return

        try:
            # Until the scan itself starts (see _live_callback)
            self._scan_started = False
            self.signals.status.emit(self.pot_id, "Preparing...")

            # An iR run streams the corrected potential to the live plot, so
            # the axis on screen matches the file that gets saved. (The live
            # loop run from this computer hands its own callback to the
            # driver.)
            live_cb = self._live_callback
            if (self.params.get('method') == 'CV'
                    and self.params.get('ir_enabled')
                    and float(self.params.get('R_u_ohm', 0.0)) > 0):
                live_cb = self._ir_live_callback(float(self.params['R_u_ohm']))

            ps_class = MockLivePotentiostat if self.use_mock else LivePotentiostat
            self.ps = ps_class(
                serial_port=self.port,
                device_ID=1,
                data_callback=live_cb,
                stop_event=self.stop_event,
            )

            # Load this board's own calibration before any current is read —
            # without it the driver falls back to class-default constants and
            # every current (and the iR feedback built on it) is off by the
            # board's read-fit slope and intercept.
            if not self.use_mock:
                cal = load_calibrations().get(self.pot_id)
                if cal:
                    self.ps.set_calibration_params(
                        cal['slope'], cal['intercept'],
                        cal.get('read_slope'), cal.get('read_intercept'))
                    if cal.get('v_slope') is not None:
                        self.ps.set_voltage_calibration_params(
                            cal['v_slope'], cal['v_intercept'],
                            cal.get('v_read_slope'), cal.get('v_read_intercept'))

            connect_with_retry(self.ps, self.pot_id)
            self._ir_on_board = self._live_ir() and bool(
                getattr(self.ps, 'firmware_features', lambda: 0)() & FW_FEATURE_IR_COMP)

            # The switch stays OPEN through setup — perform_CV/perform_DPV
            # close it only after parking the DAC at the sweep's start
            # potential, so the cell never sits at a stale setpoint while the
            # batch buffer pre-fills (used to put ~3.3 V on the cell for ~2 s).
            # An OCP run never closes it at all — that is the measurement.

            method = self.params.get('method', 'CV')
            try:
                ocp = float(self.ps.read_ocp())
                if method in ('CV', 'DPV'):
                    self.params['ocp_V'] = round(ocp, 4)
            except Exception:
                pass

            if method in ('CV', 'DPV'):
                # Before the tare below, so the zero is taken on the range the
                # run starts on
                self._start_range = self.ps.set_current_range(self._requested_range())
                # Re-zero the current at open circuit, straight after the OCP
                # read, while the cell is still disconnected and no current
                # can flow, so neither the saved currents nor the iR
                # correction built from them carry the board's zero error
                # (bench 2026-09-21: +42 uA on board P). Recorded in the
                # params file so the subtraction is never invisible.
                zero_mA = self.ps.tare_current()
                self.params['current_zero_uA'] = round(zero_mA * 1000.0, 2)
            if method == 'CV':
                result, filepath = self._run_cv()
            elif method == 'OCP':
                result, filepath = self._run_ocp()
            else:
                result, filepath = self._run_dpv()

            self.ps.write_switch(SwitchState.Off)

            outcome = ("Finished — the device reported problems during the run; "
                       "see run_degraded in the params file"
                       if self._degraded else "Finished")
            self.signals.status.emit(self.pot_id, outcome)
            self.signals.finished.emit(self.pot_id, True, outcome, result, filepath)

        except ExperimentStopped:
            # Clean user-requested stop — partial data is already on the plot.
            self.signals.status.emit(self.pot_id, "Stopped")
            self.signals.finished.emit(self.pot_id, False, "Stopped", None, None)

        except Exception as e:
            self.signals.status.emit(self.pot_id, f"Error: {e}")
            self.signals.finished.emit(self.pot_id, False, str(e), None, None)

        finally:
            # Always cut power before disconnecting (hardware safety requirement).
            if self.ps:
                try:
                    self.ps.write_switch(SwitchState.Off)
                except Exception:
                    pass
                try:
                    self.ps.disconnect()
                except Exception:
                    pass
            # Only after the port is closed is the board safe for another app.
            if claim_key is not None:
                claims.release(claim_key)

    # ------------------------------------------------------------------
    # Method runners
    # ------------------------------------------------------------------

    def _run_cv(self):
        """Run a CV sweep and save the full 6-column trace (8 with iR
        compensation — see ``_correct_ir_axis``).

        Returns ``(result_dict, filepath)`` where the result carries the raw
        streamed potential/current arrays for the live plot.
        """
        if self.params.get('ir_enabled'):
            if self._live_ir() and getattr(self, '_ir_on_board', False):
                data = self._run_cv_board_ir()
            elif self._live_ir():
                data = self._run_cv_ir()
            else:
                data = self._run_cv_posthoc_ir()
        else:
            data = self.ps.perform_CV(
                min_V=self.params['min_V'],
                max_V=self.params['max_V'],
                cycles=self.params['cycles'],
                mV_s=self.params['mV_s'],
                step_hz=CV_STEP_HZ,
                start_V=self.params.get('start_V', 0.0),
                last_V=self.params['last_V'],
                scan_direction=self.params.get('scan_direction', 'Negative'),
                quiet_s=self._quiet_s(),
                current_range=self._requested_range(),
            )
        self._record_run_report(scan_rate_mV_s=self.params['mV_s'])

        self.signals.status.emit(self.pot_id, "Saving...")
        filename = self._make_filename()
        if data is not None:
            data = np.round(data, 5)
            # Name the potential column for what it holds, so a file reloaded
            # months later cannot be mistaken for an uncorrected sweep. No
            # comma in the label: these headers are split on commas, and one
            # inside a name shifts every column after it.
            potential_label = ("Potential iR-corrected (V)"
                               if self.params.get('ir_enabled')
                               else "Potential (V)")
            header = f"Time (s),{potential_label},Current (uA),Cycle,Index,Target V"
            if self.params.get('ir_enabled'):
                header += "," + IR_EXTRA_COLUMNS
            header += "," + RANGE_COLUMN
            saved = np.column_stack((data, self._range_column(len(data))))
            np.savetxt(
                filename, saved, delimiter=",", header=header, comments='',
                fmt=['%.5f'] * data.shape[1] + ['%d'],
            )
        self._save_params(filename)

        result = {
            'method': 'CV',
            'raw_v': data[:, 1] if data is not None else np.array([]),
            'raw_i': data[:, 2] if data is not None else np.array([]),
            'processed': None,
        }
        return result, str(filename)

    @staticmethod
    def _interfacial_potential(measured_V, current_uA, R_u):
        """Convert sensed potential to the potential the electrode actually saw.

        The driver reports what it measures between working and reference
        electrodes, which still contains the ohmic drop through the solution —
        and during a compensated run it is deliberately overshooting the
        target. Subtracting I*R_u gives the potential at the electrode itself,
        which is the axis a voltammogram should be read against.

        Uses the full R_u, not the fraction that was applied: the fraction sets
        how much of the drop was corrected for during the run, while the drop
        that physically occurred is I*R_u either way.
        """
        return (np.asarray(measured_V, dtype=float)
                - np.asarray(current_uA, dtype=float) * 1e-6 * R_u)

    @classmethod
    def _correct_ir_axis(cls, data, R_u):
        """Put the interfacial potential in the potential column, and keep the
        sensed potential and the subtracted drop as two extra columns.

        The corrected axis is only as good as the R_u it was built with. With
        the sensed potential saved alongside, a file can be re-corrected later
        from a better R_u (sensed potential minus current x R_u) instead of
        being stuck with the first estimate — bench 2026-09-21, a 621 ohm
        measurement on a ~460 ohm cell collapsed the peak separation to zero.
        In live mode the sensed potential also includes the loop's applied
        correction, so sensed minus ``Target V`` is what the cell really got.
        """
        if data is None:
            return None
        measured_V = data[:, 1].copy()
        data[:, 1] = cls._interfacial_potential(measured_V, data[:, 2], R_u)
        return np.column_stack((data, measured_V, measured_V - data[:, 1]))

    def _ir_live_callback(self, R_u):
        """Live callback that plots interfacial rather than applied potential."""
        def _callback(v_chunk, i_chunk):
            self._live_callback(
                self._interfacial_potential(v_chunk, i_chunk, R_u), i_chunk)
        return _callback

    def _live_ir(self):
        """Whether this run is a CV with live iR compensation."""
        return bool(self.params.get('method', 'CV') == 'CV'
                    and self.params.get('ir_enabled')
                    and self.params.get('ir_mode', 'posthoc') == 'live')

    def _requested_range(self):
        """The current range the run asks for: 'auto' or a fixed range index.
        The live iR loop run from this computer reads point by point and
        cannot auto-range, so there 'auto' means the least sensitive range;
        the board's own loop auto-ranges like any batched CV."""
        requested = self.params.get('current_range', 'auto')
        host_loop = self._live_ir() and not getattr(self, '_ir_on_board', False)
        return 0 if (host_loop and requested == 'auto') else requested

    def _range_column(self, n_points):
        """The current range of each saved point: reported per point by a
        batched run, else the fixed range the run started on."""
        ranges = getattr(self.ps, 'last_batch_ranges', None)
        if ranges is not None and len(ranges) == n_points:
            return np.asarray(ranges, dtype=int)
        start = getattr(self, '_start_range', 0)
        return np.full(n_points, 0 if start == 'auto' else int(start), dtype=int)

    def _quiet_s(self):
        """Seconds the connected cell is held at the first potential before
        the waveform starts."""
        return max(0.0, float(self.params.get('quiet_time_s', DEFAULT_QUIET_S)))

    def _record_run_report(self, scan_rate_mV_s=None):
        """Write how a batched run went into the params file: when each point
        was sampled, the step period programmed and measured, the time basis
        of the saved Time column, and the firmware's health counters.

        A run the firmware reports as degraded (see the driver's
        ``_finish_batch_report``) is also flagged for the status line.
        """
        report = getattr(self.ps, 'last_batch_report', None)
        if not report:
            return
        self.params['sampling'] = (
            'end of each step' if report['sample_at_end']
            else 'start of each step (firmware did not offer end-of-step sampling)')
        self.params['step_ms_nominal'] = report['step_ms_nominal']
        if report['step_ms_measured'] is not None:
            self.params['step_ms_measured'] = round(report['step_ms_measured'], 5)
        self.params['time_axis'] = report['time_axis']
        if scan_rate_mV_s and report['time_axis'] == 'measured':
            self.params['achieved_mV_s'] = round(
                scan_rate_mV_s * report['step_ms_nominal'] / report['step_ms_measured'], 3)
        used = report.get('ranges_used')
        if used:
            self.params['current_ranges_used'] = ", ".join(RANGE_LABELS[r] for r in used)
            if report.get('auto_range'):
                self.params['current_range_changes'] = report['range_changes']
        if report.get('lead_in_points'):
            # Held at the first potential while Auto found its range; read but not saved
            self.params['auto_range_lead_in_points'] = report['lead_in_points']
        if self.params.get('current_range') == 'auto' and not report.get('auto_range'):
            self.params['current_range_note'] = (
                "auto-ranging needs firmware 1.5 or later; this run stayed on "
                f"{RANGE_LABELS[0]}")
        ir = report.get('ir')
        if ir:
            self.params['ir_compensated_ohm'] = round(report['ir_ohm'], 1)
            self.params['ir_max_correction_mV'] = round(ir['max_correction_V'] * 1000.0, 1)
            if 'limit_V' in ir:
                self.params['ir_correction_limit_mV'] = round(ir['limit_V'] * 1000.0, 1)
            self.params['ir_limited_steps'] = ir['limited_steps']
            self.params['ir_lagging_steps'] = ir['slewed_steps']
        for key, value in report['health'].items():
            self.params[f'device_{key}'] = value
        if report['degraded']:
            self.params['run_degraded'] = "; ".join(report['degraded'])
            self._degraded = True

    def _require_r_u(self):
        """Return the R_u the user provided, refusing a run without one."""
        R_u = float(self.params.get('R_u_ohm', 0.0))
        if R_u <= 0:
            raise ValueError(
                "iR compensation needs a solution resistance. Measure R_u "
                "first, or switch the compensation off."
            )
        return R_u

    def _run_cv_posthoc_ir(self):
        """Run a normal batched CV, then subtract the ohmic drop from the axis.

        The default compensation mode: the sweep itself is the proven 250 Hz
        batch path, and the correction is arithmetic on the recorded trace.
        This fixes the potential axis (peak positions, E-half) but the
        electrode itself still experienced the full ohmic drop during the
        run — the UI says so where the mode is chosen.
        """
        R_u = self._require_r_u()
        data = self.ps.perform_CV(
            min_V=self.params['min_V'],
            max_V=self.params['max_V'],
            cycles=self.params['cycles'],
            mV_s=self.params['mV_s'],
            step_hz=CV_STEP_HZ,
            start_V=self.params.get('start_V', 0.0),
            last_V=self.params['last_V'],
            scan_direction=self.params.get('scan_direction', 'Negative'),
            quiet_s=self._quiet_s(),
            current_range=self._requested_range(),
        )
        return self._correct_ir_axis(data, R_u)

    def _run_cv_board_ir(self):
        """Run a CV with the ohmic drop added back live by the board itself
        (firmware 1.8+): a batched sweep, on the board's clock with end-of-step
        readings and any current range, where each setpoint gets the
        compensated share of R_u times the previous step's current added.

        The saved axis is then corrected by the full R_u, as for post-hoc,
        and the sensed potential column shows what the cell really got.
        """
        R_u = self._require_r_u()
        fraction = min(max(float(self.params.get('compensation_fraction', 0.7)), 0.0),
                       IR_MAX_FRACTION)
        self.params['ir_loop'] = 'on the board'
        data = self.ps.perform_CV(
            min_V=self.params['min_V'],
            max_V=self.params['max_V'],
            cycles=self.params['cycles'],
            mV_s=self.params['mV_s'],
            step_hz=CV_STEP_HZ,
            start_V=self.params.get('start_V', 0.0),
            last_V=self.params['last_V'],
            scan_direction=self.params.get('scan_direction', 'Negative'),
            quiet_s=self._quiet_s(),
            current_range=self._requested_range(),
            ir_compensation_ohm=fraction * R_u,
        )
        return self._correct_ir_axis(data, R_u)

    def _run_cv_ir(self):
        """Run a CV with the ohmic drop corrected live, point by point.

        The fallback for firmware before 1.8. Slower than the batched sweep,
        because every point is its own serial exchange, so it runs at its own
        point rate. An early stop comes back as a short trace, which is
        reported the same way a stopped plain CV is.
        """
        R_u = self._require_r_u()
        self.params['ir_loop'] = 'this computer (board firmware before 1.8)'

        data = self.ps.perform_CV_iR(
            min_V=self.params['min_V'],
            max_V=self.params['max_V'],
            cycles=self.params['cycles'],
            mV_s=self.params['mV_s'],
            step_hz=IR_STEP_HZ,
            R_u_ohm=R_u,
            start_V=self.params.get('start_V', 0.0),
            last_V=self.params['last_V'],
            scan_direction=self.params.get('scan_direction', 'Negative'),
            compensation_fraction=self.params.get('compensation_fraction', 0.7),
            data_callback=self._ir_live_callback(R_u),
            stop_event=self.stop_event,
            quiet_s=self._quiet_s(),
            current_range=self._requested_range(),
        )

        # Saved and plotted traces carry the interfacial potential, so the
        # applied overshoot never reaches the peak-separation measurement.
        data = self._correct_ir_axis(data, R_u)

        # The point loop only holds its pace while a serial round trip fits in
        # the step period; record the rate the scan really ran at, so the
        # params file tells the truth even when the link fell behind.
        if data is not None and len(data) > 1 and data[-1, 0] > 0:
            achieved_hz = len(data) / float(data[-1, 0])
            achieved_mV_s = self.params['mV_s'] * achieved_hz / IR_STEP_HZ
            self.params['achieved_mV_s'] = round(float(achieved_mV_s), 2)

        # The driver returns partial data rather than raising, so translate a
        # stop into the same signal the batched CV path produces.
        if self.stop_event.is_set():
            raise ExperimentStopped("Stop requested by user")
        return data

    def _run_dpv(self):
        """Run a DPV sweep, save the raw staircase, and compute + save the
        differential curve.

        Returns ``(result_dict, filepath)`` where ``filepath`` points at the
        differential CSV (the primary result) and the result dict also carries
        the raw stream and the processed Nx2 ``[V, delta-i]`` array.
        """
        # Build the target waveform for the chosen DPV type: a single staircase
        # from Initial to Final V, or a CV-shaped cyclic run (Start V -> cycle
        # between the vertices -> End V).
        if self.params.get('dpv_type') == 'cyclic':
            sweeps = cyclic_sweeps(
                self.params['min_V'], self.params['max_V'],
                self.params['cycles'],
                direction=self.params.get('direction', 'Positive'),
                start_V=self.params.get('start_V'),
                end_V=self.params.get('end_V'),
            )
        else:
            sweeps = [(self.params['start_V'], self.params['end_V'])]

        # Every level on a potential the DAC can really produce, so all pulses
        # (and steps) are the same height; the params file records the heights
        # and hold times as applied, not as typed.
        grid = self.ps.dac_grid()
        dpv_timing = dict(
            potential_hold_ms=self.params['p_hold'],
            pulse_hold_ms=self.params['pulse_duration'],
            sample_hz=DPV_SAMPLE_HZ,
        )
        waveform = build_dpv_waveform(
            sweeps, pulse_V=self.params['pulse_V'], step_V=self.params['step_V'],
            dac_grid=grid, **dpv_timing)
        applied = applied_dpv_settings(
            self.params['pulse_V'], self.params['step_V'], dac_grid=grid,
            **dpv_timing)
        self.params['applied_pulse_mV'] = round(applied['pulse_V'] * 1000.0, 3)
        self.params['applied_step_mV'] = round(applied['step_V'] * 1000.0, 3)
        self.params['applied_pulse_ms'] = applied['pulse_ms']
        self.params['applied_step_hold_ms'] = applied['step_hold_ms']

        # Built from the driver's full, index-aligned result rather than the
        # live plot's stream: a chunk the plot skips would shift every later
        # sample against the step/pulse pattern the processing assumes.
        n_step, n_pulse, _ = samples_per_segment(**dpv_timing)
        data = self.ps.perform_DPV(waveform=waveform, sample_hz=DPV_SAMPLE_HZ,
                                   quiet_s=self._quiet_s(),
                                   current_range=self._requested_range(),
                                   range_period=n_step + n_pulse)
        self._record_run_report()

        self.signals.status.emit(self.pot_id, "Saving...")
        raw_v = data[:, 1]
        raw_i = data[:, 2]

        differential = process_dpv(raw_v, raw_i, **dpv_timing)

        # Save the raw staircase (for re-processing later) and the differential
        # curve (the primary result a chemist reads peaks from).
        base = self._make_filename()
        raw_path = base.with_name(base.stem + "_RAW.csv")
        np.savetxt(
            raw_path, np.column_stack((data, self._range_column(len(data)))),
            delimiter=",", header="Time (s),Potential (V),Current (uA),Target V,"
                                  + RANGE_COLUMN,
            comments='', fmt=['%.5f'] * data.shape[1] + ['%d'],
        )
        np.savetxt(
            base, differential, delimiter=",",
            header="Potential (V),Delta-i (uA)", comments='', fmt='%.5f',
        )
        self._save_params(base)

        result = {
            'method': 'DPV',
            'raw_v': raw_v,
            'raw_i': raw_i,
            'processed': differential,
        }
        return result, str(base)

    def _run_ocp(self):
        """Monitor the cell's open-circuit potential and save the trace.

        Nothing is driven: the cell stays disconnected for the whole run, so
        the data is a time series of resting potential rather than a
        voltammogram. ``raw_v``/``raw_i`` in the result therefore carry time
        and potential — the same (x, y) pair the tab has been plotting live.
        """
        data = monitor_ocp(
            self.ps,
            duration_s=self.params['duration_s'],
            sample_hz=self.params.get('sample_hz', OCP_SAMPLE_HZ),
            data_callback=self._live_callback,
            stop_event=self.stop_event,
        )

        self.signals.status.emit(self.pot_id, "Saving...")
        filename = self._make_filename()
        np.savetxt(
            filename, np.round(data, 5), delimiter=",",
            header="Time (s),Potential (V)", comments='', fmt='%.5f',
        )
        self._save_params(filename)

        result = {
            'method': 'OCP',
            'raw_v': data[:, 0],
            'raw_i': data[:, 1],
            'processed': None,
        }
        return result, str(filename)

    # ------------------------------------------------------------------
    # Saving helpers
    # ------------------------------------------------------------------

    def _make_filename(self):
        """Build a timestamped CSV path for this run in the CV results folder."""
        data_folder = get_cv_data_folder()
        data_folder.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        name = self.params.get('name', 'Experiment')
        return data_folder / f"{name}_PS-{self.pot_id}_{timestamp}.csv"

    def _save_params(self, data_path):
        """Write the experiment parameters next to the data file."""
        param_file = data_path.with_suffix('.params.txt')
        with open(param_file, 'w') as f:
            for k, v in self.params.items():
                f.write(f"{k}: {v}\n")

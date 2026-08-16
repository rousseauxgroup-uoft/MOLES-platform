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
import time
from datetime import datetime

import numpy as np
from PyQt5 import QtCore

from ... import claims
from ...settings import get_cv_data_folder
from ...calibration import load_calibrations
from ...logging_setup import LOG_DIR
from ...driver.mock import MockLivePotentiostat, MockPotentiostat
from ...driver.ps4_ref import Potentiostat, Resistors, SwitchState
from ...methods.cv import ExperimentStopped, LivePotentiostat
from ...methods.dpv import build_dpv_waveform, cyclic_sweeps, process_dpv
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

    def __init__(self, pot_id, port, use_mock=False, step_mV=RU_STEP_MV):
        super().__init__()
        self.pot_id = pot_id
        self.port = port
        self.use_mock = use_mock
        self.step_mV = step_mV
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
            result = self.ps.measure_R_u(step_mV=self.step_mV)
            R_u = float(result['R_u_ohm'])

            # Keep the raw decay traces on disk — success or refusal — so a
            # suspect value can be diagnosed from what the fit actually saw
            # instead of from one scalar.
            self._save_traces(result)

            # The driver refuses estimates its decay fit cannot back up;
            # pass its reason through rather than inventing one here.
            fit_ok = bool(result.get('fit_ok', np.isfinite(R_u)))
            note = result.get('message', '')
            if not fit_ok or not np.isfinite(R_u):
                self.signals.finished.emit(
                    self.pot_id, False,
                    note or "No current flowed — check the cell connections.",
                    None)
                return

            msg = f"R_u = {R_u:.1f} ohm"
            pols = [p for p in result.get('polarities', []) if p.get('ok')]
            if len(pols) == 2:
                msg += (f" (steps {pols[0]['step_V'] * 1e3:+.0f}/"
                        f"{pols[1]['step_V'] * 1e3:+.0f} mV: "
                        f"{pols[0]['R_u_ohm']:.0f}/{pols[1]['R_u_ohm']:.0f} ohm)")
            if note:
                msg += f" — {note}"
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

    def _save_traces(self, result):
        """Write the measurement's decay traces to ~/.moles/logs/ru_traces/.

        One CSV per measurement, long format (one row per sample, labeled by
        its step), with the fit's verdicts in comment lines up top. Saving
        must never break the measurement, so any failure is only logged.
        """
        polarities = result.get('polarities') or []
        if not polarities:
            return  # mock or legacy result without traces
        try:
            folder = LOG_DIR / "ru_traces"
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            path = folder / f"RU_PS-{self.pot_id}_{stamp}.csv"

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
                    f"ocp={p.get('ocp'):.4f} V baseline={p.get('baseline_A', 0.0) * 1e6:.1f} uA "
                    f"R_u={p['R_u_ohm']:.1f} ohm tau_ms={p['tau_ms']} "
                    f"ok={p['ok']} {p['message']}")
                for t, i in zip(p['times_ms'], p['currents_A']):
                    rows.append((p['step_V'] * 1e3, t, i * 1e3))

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
        # Accumulated live stream, used to build the DPV differential curve.
        # The pair is whatever the running method streams: (potential, current)
        # for CV and DPV, (time, potential) for OCP.
        self._acc_v = []
        self._acc_i = []
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
        """Store and forward a chunk to the main thread for live plotting."""
        self._acc_v.extend(np.asarray(v_chunk).tolist())
        self._acc_i.extend(np.asarray(i_chunk).tolist())
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
            self.signals.status.emit(self.pot_id, "Connecting...")

            # A post-hoc iR run streams the corrected potential to the live
            # plot, so the axis on screen matches the file that gets saved.
            live_cb = self._live_callback
            if (self.params.get('method') == 'CV'
                    and self.params.get('ir_enabled')
                    and self.params.get('ir_mode', 'posthoc') == 'posthoc'
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

            self.signals.status.emit(self.pot_id, "Running...")
            # The switch stays OPEN through setup — perform_CV/perform_DPV
            # close it only after parking the DAC at the sweep's start
            # potential, so the cell never sits at a stale setpoint while the
            # batch buffer pre-fills (used to put ~3.3 V on the cell for ~2 s).
            # An OCP run never closes it at all — that is the measurement.

            try:
                self.ps.read_ocp()
            except Exception:
                pass

            # Start at low gain for safety, then enable auto-gain if requested.
            self.ps.write_gain(Resistors.R_100)
            time.sleep(0.2)
            self.ps.write_auto_gain(self.params.get('auto_gain', True))
            time.sleep(0.2)

            method = self.params.get('method', 'CV')
            if method == 'CV':
                result, filepath = self._run_cv()
            elif method == 'OCP':
                result, filepath = self._run_ocp()
            else:
                result, filepath = self._run_dpv()

            self.ps.write_switch(SwitchState.Off)

            self.signals.status.emit(self.pot_id, "Finished")
            self.signals.finished.emit(self.pot_id, True, "Done", result, filepath)

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
        """Run a CV sweep and save the full 6-column trace.

        Returns ``(result_dict, filepath)`` where the result carries the raw
        streamed potential/current arrays for the live plot.
        """
        if self.params.get('ir_enabled'):
            if self.params.get('ir_mode', 'posthoc') == 'live':
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
            )

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
            np.savetxt(
                filename, data, delimiter=",",
                header=f"Time (s),{potential_label},Current (uA),Cycle,Index,Target V",
                comments='', fmt='%.5f',
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

    def _ir_live_callback(self, R_u):
        """Live callback that plots interfacial rather than applied potential."""
        def _callback(v_chunk, i_chunk):
            self._live_callback(
                self._interfacial_potential(v_chunk, i_chunk, R_u), i_chunk)
        return _callback

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
        )
        if data is not None and len(data):
            data[:, 1] = self._interfacial_potential(data[:, 1], data[:, 2], R_u)
        return data

    def _run_cv_ir(self):
        """Run a CV with the ohmic drop corrected live, point by point.

        Slower than the batched sweep, because every point is its own serial
        exchange, so it runs at its own point rate. An early stop comes back as
        a short trace, which is reported the same way a stopped plain CV is.
        """
        R_u = self._require_r_u()

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
        )

        # Saved and plotted traces carry the interfacial potential, so the
        # applied overshoot never reaches the peak-separation measurement.
        if data is not None and len(data):
            data[:, 1] = self._interfacial_potential(data[:, 1], data[:, 2], R_u)

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

        waveform = build_dpv_waveform(
            sweeps,
            pulse_V=self.params['pulse_V'],
            step_V=self.params['step_V'],
            potential_hold_ms=self.params['p_hold'],
            pulse_hold_ms=self.params['pulse_duration'],
            sample_hz=DPV_SAMPLE_HZ,
        )

        # The driver's perform_DPV return value mixes up units, so we ignore it
        # and rebuild everything from the live stream we accumulated instead.
        self.ps.perform_DPV(
            waveform=waveform,
            start_V=sweeps[0][0],  # park the DAC at the first sweep's start
            sample_hz=DPV_SAMPLE_HZ,
        )

        self.signals.status.emit(self.pot_id, "Saving...")
        raw_v = np.asarray(self._acc_v, dtype=float)
        raw_i = np.asarray(self._acc_i, dtype=float)

        differential = process_dpv(
            raw_v, raw_i,
            potential_hold_ms=self.params['p_hold'],
            pulse_hold_ms=self.params['pulse_duration'],
            sample_hz=DPV_SAMPLE_HZ,
        )

        # Save the raw staircase (for re-processing later) and the differential
        # curve (the primary result a chemist reads peaks from).
        base = self._make_filename()
        raw_path = base.with_name(base.stem + "_RAW.csv")
        np.savetxt(
            raw_path, np.column_stack([raw_v, raw_i]), delimiter=",",
            header="Potential (V),Current (uA)", comments='', fmt='%.5f',
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

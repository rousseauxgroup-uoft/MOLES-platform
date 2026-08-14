"""Background worker that runs one CV or DPV experiment off the main thread.

``VoltammetryWorker`` is a ``QRunnable`` that drives either a real
``LivePotentiostat`` or, in Test Mode, a ``MockLivePotentiostat``. It streams
live data chunks to the UI as they arrive and, on completion, saves the result
and reports it back through Qt signals.

For DPV the raw per-sample staircase is streamed live, then converted into the
differential curve (delta-i vs step potential) once the sweep finishes — the
form a chemist expects to read peaks from.
"""

import threading
import time
from datetime import datetime

import numpy as np
from PyQt5 import QtCore

from ... import claims
from ...settings import get_cv_data_folder
from ...driver.mock import MockLivePotentiostat
from ...driver.ps4_ref import Resistors, SwitchState
from ...methods.cv import ExperimentStopped, LivePotentiostat
from ...methods.dpv import build_dpv_waveform, cyclic_sweeps, process_dpv
from ...session import connect_with_retry

# Acquisition sampling rates. Kept here so the worker, the differential
# processing, and the saved data all use one consistent value.
CV_STEP_HZ = 250
DPV_SAMPLE_HZ = 250


class VoltammetryWorkerSignals(QtCore.QObject):
    """Signals emitted by ``VoltammetryWorker`` for its tab to consume."""
    # pot_id, success flag, status message, result dict, primary file path
    finished = QtCore.pyqtSignal(str, bool, str, object, object)
    status = QtCore.pyqtSignal(str, str)                 # pot_id, status message
    data_chunk = QtCore.pyqtSignal(str, object, object)  # pot_id, V array, I array


class VoltammetryWorker(QtCore.QRunnable):
    """Run one CV or DPV experiment in a thread-pool worker.

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

            ps_class = MockLivePotentiostat if self.use_mock else LivePotentiostat
            self.ps = ps_class(
                serial_port=self.port,
                device_ID=1,
                data_callback=self._live_callback,
                stop_event=self.stop_event,
            )
            connect_with_retry(self.ps, self.pot_id)

            self.signals.status.emit(self.pot_id, "Running...")
            # The switch stays OPEN through setup — perform_CV/perform_DPV
            # close it only after parking the DAC at the sweep's start
            # potential, so the cell never sits at a stale setpoint while the
            # batch buffer pre-fills (used to put ~3.3 V on the cell for ~2 s).

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
            np.savetxt(
                filename, data, delimiter=",",
                header="Time (s),Potential (V),Current (uA),Cycle,Index,Target V",
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

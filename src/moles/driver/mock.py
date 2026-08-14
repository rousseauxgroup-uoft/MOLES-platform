import logging
import time
import numpy as np
import random
import threading

from .ps4_ref import SerialReadTimeout

logger = logging.getLogger(__name__)


class MockPotentiostat:
    def __init__(self, serial_port=None):
        self.serial_port = serial_port
        self.device_ID = 1
        self._switch_state = False

        # Internal State
        self._set_current_mA = 0.0
        self._set_potential_V = 0.0
        self._mode = 'idle' # 'cc' or 'cv'

        # Physics Parameters
        self._base_resistance = 100.0 # Ohms
        self._resistance = self._base_resistance
        self._compliance_voltage = 5.0 # Volts
        self._compliance_current_mA = 30.0 # mA

        # Fault Injection States
        self._forced_compliance = False
        self._is_disconnected = False
        # Number of upcoming serial operations that should raise
        # SerialReadTimeout (set via trigger_serial_timeout).
        self._timeout_countdown = 0

        # Calibration attrs mirror the real driver: alternating_polarity's
        # _calibrate_batch reads them directly off the ps object. The mock's
        # physics model is already in true units, so identity values are used.
        self._calib_slope_correction = 1.0
        self._calib_intercept = 0.0
        self._calib_read_slope = 1.0
        self._calib_read_intercept = 0.0
        self._calib_v_slope = 1.0
        self._calib_v_intercept = 0.0
        self._calib_v_read_slope = 1.0
        self._calib_v_read_intercept = 0.0

    def connect(self):
        logger.info("Mock PS connected on %s", self.serial_port)

    def disconnect(self):
        # Matches the real driver so ReconnectWorker can cycle a mock device.
        pass

    def force_close(self):
        pass

    def set_calibration_params(self, slope, intercept, read_slope=None, read_intercept=None):
        self._calib_slope_correction = slope
        self._calib_intercept = intercept
        self._calib_read_slope = 1.0 if read_slope is None else read_slope
        self._calib_read_intercept = 0.0 if read_intercept is None else read_intercept

    def set_voltage_calibration_params(self, slope, intercept, read_slope=None, read_intercept=None):
        self._calib_v_slope = slope
        self._calib_v_intercept = intercept
        self._calib_v_read_slope = 1.0 if read_slope is None else read_slope
        self._calib_v_read_intercept = 0.0 if read_intercept is None else read_intercept

    def read_current(self):
        """Raw current read (A), matching the real driver's interface.

        The onboarding sweep samples this for the read-calibration fit; the
        mock reports its true simulated current, so a mock sweep fits a read
        slope of ~1 and intercept of ~0.
        """
        self._maybe_timeout()
        raw_A = (self._present_current_mA() + np.random.normal(0, 0.005)) / 1000.0
        return np.array([raw_A])

    def _maybe_timeout(self):
        """Raise SerialReadTimeout if a timeout fault has been armed."""
        if self._timeout_countdown > 0:
            self._timeout_countdown -= 1
            raise SerialReadTimeout(
                f"Mock injected read timeout ({self._timeout_countdown} left)"
            )

    def write_switch(self, state):
        self._switch_state = bool(state)

    def _safe_open_switch(self):
        """Match the real driver's spike-free disconnect: stop any hold, park
        the setpoint at the sensed potential, then open the switch."""
        self.write_current_hold_stop()
        self._set_potential_V = self._present_potential_V()
        self._switch_state = False

    def halt_batch_stream(self):
        """Match the real driver's batch halt; the mock has no stream to stop."""
        pass

    def write_gain(self, gain):
        # Mock gain setting (doesn't affect physics much in simple constant R model)
        pass

    def write_dac(self, channels, voltages):
        pass

    def read_ocp(self, restore_switch_state=True):
        # Return a random OCP or one based on "chemistry"
        return 1.234 + np.random.normal(0, 0.005)

    def write_current_hold(self, target_current_mA=0.1, **kwargs):
        # Uncalibrated entry point used by emergency_cleanup.
        self.write_current_hold_calibrated(target_current_mA, **kwargs)

    def write_current_hold_calibrated(self, current_mA, **kwargs):
        self._set_current_mA = current_mA
        self._mode = 'cc'
        # in the original script, learning_rate is passed in kwargs

    def write_current_hold_stop(self):
        self._set_current_mA = 0.0
        self._mode = 'idle'

    def write_potential_calibrated(self, potential_V):
        self._set_potential_V = potential_V
        self._mode = 'cv'

    def write_potential(self, potential_V):
        """Raw potential write, matching the real driver's interface.

        The onboarding voltage sweep commands through this path; the mock
        applies it directly, so a mock sweep fits ~identity in both directions.
        """
        self._set_potential_V = potential_V
        self._mode = 'cv'

    def read_potential(self):
        """Raw potential read (V), matching the real driver's interface."""
        self._maybe_timeout()
        return np.array([self._present_potential_V() + np.random.normal(0, 0.005)])

    def read_current_calibrated(self):
        self._maybe_timeout()
        return np.array([self._present_current_mA() + np.random.normal(0, 0.005)])

    def _present_current_mA(self):
        """Physics model: the current the cell would show right now (no noise)."""
        if not self._switch_state:
            return 0.0

        if self._is_disconnected:
            # Circuit open -> Current is 0
            return 0.0

        current = 0.0

        if self._mode == 'cc':
            # Constant Current Mode
            # If Forced Compliance, we might not reach target current if R is too high
            # V = I*R. If I*R > 5V, then I = 5V/R
            
            req_voltage = self._set_current_mA * 1e-3 * self._resistance # V = A * Ohm
            
            if self._forced_compliance or abs(req_voltage) > self._compliance_voltage:
                # Voltage Clamped
                eff_voltage = np.copysign(self._compliance_voltage, req_voltage)
                current = (eff_voltage / self._resistance) * 1e3 # mA
            else:
                current = self._set_current_mA
                
        elif self._mode == 'cv':
             # Constant Voltage Mode
             # I = V/R
             current = (self._set_potential_V / self._resistance) * 1e3 # mA

             # Current Compliance
             if abs(current) > self._compliance_current_mA:
                 current = np.copysign(self._compliance_current_mA, current)

        return float(current)

    def read_potential_calibrated(self):
        self._maybe_timeout()
        return np.array([self._present_potential_V() + np.random.normal(0, 0.005)])

    def _present_potential_V(self):
        """Physics model: the WE-RE potential the cell would show right now."""
        if not self._switch_state:
            # Maybe show OCP?
            return 1.234

        voltage = 0.0

        if self._is_disconnected:
            if self._mode == 'cc':
                # Driving current into open circuit -> Rail to compliance
                voltage = np.copysign(self._compliance_voltage, self._set_current_mA) if self._set_current_mA != 0 else 0
            elif self._mode == 'cv':
                voltage = self._set_potential_V
            return float(voltage)

        if self._mode == 'cc':
            # V = I * R
            # Calculate actual current first (handling compliance)
            req_voltage = self._set_current_mA * 1e-3 * self._resistance

            if self._forced_compliance or abs(req_voltage) > self._compliance_voltage:
                 voltage = np.copysign(self._compliance_voltage, req_voltage)
            else:
                 voltage = req_voltage

        elif self._mode == 'cv':
            voltage = self._set_potential_V

        return float(voltage)

    def read_potential_current_calibrated(self):
        """Combined read matching the real driver, so Test Mode exercises the
        same single-transaction code path the hardware does."""
        self._maybe_timeout()
        return [
            self._present_potential_V() + np.random.normal(0, 0.005),
            self._present_current_mA() + np.random.normal(0, 0.005),
        ]

    def write_voltage_batch(self, voltages, delay):
        """Simulate the firmware batch protocol used by alternating polarity.

        Sleeps for the batch's real duration (so chunk pacing and stop
        responsiveness behave like hardware) and returns raw-scale data the
        same way the real driver does: col 0 raw current (A), col 1 raw
        potential (V), inverting the calibration that _calibrate_batch will
        re-apply on top.
        """
        self._maybe_timeout()
        voltages = np.asarray(voltages, dtype=float)
        time.sleep(min(len(voltages) * float(delay) / 1000.0, 10.0))

        applied_V = voltages  # driver-side calibration offset already applied
        if self._switch_state and not self._is_disconnected:
            current_mA = (applied_V / self._resistance) * 1e3
            current_mA = np.clip(current_mA, -self._compliance_current_mA,
                                 self._compliance_current_mA)
        else:
            current_mA = np.zeros_like(applied_V)
        current_mA = current_mA + np.random.normal(0, 0.005, len(applied_V))

        # Invert read_potential_current_calibrated's scaling: cal_i = raw*976.9 - 0.6
        raw_i = (current_mA + 0.6) / 976.9
        raw_v = applied_V + np.random.normal(0, 0.002, len(applied_V))
        self._mode = 'cv'
        self._set_potential_V = float(applied_V[-1])
        return np.column_stack((raw_i, raw_v))

    def stream_voltage_batch(self, voltages, delay, on_block, block_points=None):
        """Continuously repeat ``voltages``, matching the real driver: raw
        measurement blocks go to ``on_block`` until it returns False or a
        fault raises, and the stream is halted on the way out either way."""
        tile = np.asarray(voltages, dtype=float)
        if block_points is None:
            block_points = int(round((500.0 / max(1.0, float(delay))) / 6)) * 6
        block_points = max(6, block_points)
        try:
            cursor = 0
            while True:
                idx = (cursor + np.arange(block_points)) % len(tile)
                # Reuses the batch simulation for physics, pacing and fault
                # injection, one block at a time.
                raw = self.write_voltage_batch(tile[idx], delay)
                cursor = int((cursor + block_points) % len(tile))
                if not on_block(raw):
                    return
        finally:
            self.halt_batch_stream()

    # --- Fault Injection Methods ---

    def trigger_serial_timeout(self, count=3):
        """Make the next ``count`` serial operations raise SerialReadTimeout,
        exercising the methods' skip-and-retry tolerance paths."""
        logger.info("[Mock %s] Event: injecting %d serial timeouts", self.serial_port, count)
        self._timeout_countdown = count

    def trigger_short_circuit(self):
        """Simulate a short circuit (low resistance)."""
        logger.info("[Mock %s] Event: Short Circuit", self.serial_port)
        self._resistance = 0.1 # Very low resistance
        self._is_disconnected = False
        self._forced_compliance = False

    def trigger_disconnect(self):
        """Simulate a cell disconnection (infinite resistance)."""
        logger.info("[Mock %s] Event: Disconnected", self.serial_port)
        self._resistance = float('inf')
        self._is_disconnected = True

    def trigger_param_compliance(self):
        """Simulate hitting 5V limit (high resistance/bad contact)."""
        logger.info("[Mock %s] Event: Voltage Compliance", self.serial_port)
        # Increase resistance substantially to force voltage rail and current drop
        self._resistance = 50000.0 # 50 kOhms
        self._forced_compliance = False # Let physics handle it
        self._is_disconnected = False

    def reset_normal(self):
        """Reset to normal operation (100 Ohm)."""
        logger.info("[Mock %s] Event: Normal Operation", self.serial_port)
        self._resistance = self._base_resistance
        self._is_disconnected = False
        self._forced_compliance = False


class MockLivePotentiostat:
    """Drop-in stand-in for ``LivePotentiostat`` used by the voltammetry UI.

    Generates believable synthetic CV and DPV data so the electroanalysis
    interface can be exercised (live plotting, saving, analysis hand-off) on a
    laptop with no potentiostat attached — the "Test Mode" path. It exposes the
    same methods the worker calls on a real ``LivePotentiostat`` and, like the
    real device, streams data back in small chunks through ``data_callback`` so
    the plot fills in progressively.

    Args mirror ``LivePotentiostat``. ``chunk_delay_s`` controls how long to
    pause between streamed chunks (set to 0 in automated tests for speed; a
    small non-zero value gives a lifelike "watch it draw" feel on screen).
    """

    def __init__(self, serial_port=None, device_ID=1, data_callback=None,
                 stop_event=None, chunk_delay_s=0.004):
        self.serial_port = serial_port
        self.device_ID = device_ID
        self.data_callback = data_callback
        self.stop_event = stop_event
        self.chunk_delay_s = chunk_delay_s
        self._auto_gain = True
        self._switch_state = False
        # Synthetic redox couple the fake chemistry is centred on.
        self._redox_E = 0.20  # V

    # --- Connection / configuration no-ops (match the real interface) ---

    def connect(self):
        print(f"Mock live PS connected on {self.serial_port}")

    def disconnect(self):
        pass

    def write_switch(self, state):
        self._switch_state = bool(state)

    def write_gain(self, resistor=0):
        pass

    def write_auto_gain(self, state=False):
        self._auto_gain = bool(state)
        return self._auto_gain

    def read_auto_gain(self):
        return self._auto_gain

    def read_ocp(self, restore_switch_state=True):
        return float(np.random.normal(0.0, 0.005))

    def set_calibration_params(self, slope, intercept, read_slope=None, read_intercept=None):
        pass

    def set_voltage_calibration_params(self, slope, intercept, read_slope=None, read_intercept=None):
        pass

    # --- Streaming helper ---

    def _stream(self, potentials, currents_uA):
        """Emit the (potential, current) arrays in 6-point chunks.

        Honors ``stop_event`` between chunks so a user "Stop" ends the run
        promptly, raising the same exception the real device path raises.
        """
        from ..methods.cv import ExperimentStopped  # local import avoids a cycle

        chunk = 6
        for i in range(0, len(potentials), chunk):
            if self.stop_event is not None and self.stop_event.is_set():
                raise ExperimentStopped("Stop requested by user")
            v_chunk = potentials[i:i + chunk]
            i_chunk = currents_uA[i:i + chunk]
            if self.data_callback:
                self.data_callback(np.round(v_chunk, 5), np.round(i_chunk, 5))
            if self.chunk_delay_s:
                time.sleep(self.chunk_delay_s)

    # --- Synthetic experiments ---

    def perform_CV(self, min_V, max_V, cycles, mV_s, step_hz, start_V=None,
                   last_V=None, scan_direction='Negative'):
        """Generate and stream a synthetic cyclic voltammogram.

        Builds the same triangular potential sweep the real driver would, then
        fakes a current response: a capacitive baseline plus an oxidation peak
        on the forward sweep and a reduction peak on the reverse, so the trace
        has the familiar "duck" shape.
        """
        if start_V is None:
            start_V = 0.0
        if last_V is None:
            last_V = 0.0

        V_s = mV_s / 1000.0
        step_V = max(V_s / step_hz, 1e-4)

        sweep = []
        if scan_direction == 'Positive':
            first, turn_a, turn_b = max_V, min_V, max_V
        else:
            first, turn_a, turn_b = min_V, max_V, min_V

        r1 = abs(first - start_V)
        if r1 > 0:
            sweep = np.append(sweep, np.linspace(start_V, first, int(np.round(r1 / step_V))))
        leg_a = np.linspace(first, turn_a, max(2, int(np.round(abs(turn_a - first) / step_V))))
        leg_b = np.linspace(turn_a, turn_b, max(2, int(np.round(abs(turn_b - turn_a) / step_V))))
        for _ in range(cycles):
            sweep = np.append(sweep, leg_a)
            sweep = np.append(sweep, leg_b)
        r4 = abs(last_V - turn_b)
        if r4 > 0:
            sweep = np.append(sweep, np.linspace(turn_b, last_V, int(np.round(r4 / step_V))))

        applied = np.asarray(sweep, dtype=float)
        n = len(applied)

        # Fake current: capacitive offset that flips with sweep direction, plus
        # gaussian faradaic peaks near the redox potential.
        dE = np.gradient(applied)
        direction = np.sign(dE)
        cap = 0.4 * direction
        E0 = self._redox_E
        ox = 3.0 * np.exp(-((applied - (E0 + 0.03)) / 0.05) ** 2) * (direction > 0)
        red = -2.6 * np.exp(-((applied - (E0 - 0.03)) / 0.05) ** 2) * (direction < 0)
        noise = np.random.normal(0, 0.03, n)
        current_uA = cap + ox + red + noise
        measured_V = applied + np.random.normal(0, 0.002, n)

        self._stream(measured_V, current_uA)

        duration_s = n / step_hz
        time_stamps = np.linspace(0, duration_s, n)
        cycle_points = max(1, n // max(cycles, 1))
        cycle_nums = np.zeros(n)
        for c in range(cycles):
            cycle_nums[c * cycle_points:(c + 1) * cycle_points] = c
        return np.column_stack(
            (time_stamps, measured_V, current_uA, cycle_nums, np.zeros(n), applied)
        )

    def perform_DPV(self, waveform, start_V, sample_hz):
        """Stream a synthetic DPV response for a pre-built target waveform.

        Mirrors the slim real ``perform_DPV``: the staircase-plus-pulse shape is
        built upstream by ``build_dpv_waveform`` and handed in as ``waveform``.
        The faradaic current is modelled as a smooth redox wave (sigmoid) of the
        applied potential, so the pulse-minus-step difference computed
        downstream produces a clean peak near the redox potential — for both
        single-sweep and cyclic runs.
        """
        applied = np.asarray(waveform, dtype=float)
        n = len(applied)
        if n == 0:
            return np.zeros((0, 2))

        # Centre the fake redox wave in the middle of the scanned window so a
        # peak lands wherever the sweep passes through it.
        E0 = (float(applied.min()) + float(applied.max())) / 2.0
        wave = 5.0 / (1.0 + np.exp(-(applied - E0) / 0.04))
        cap = 0.2
        noise = np.random.normal(0, 0.05, n)
        current_uA = wave + cap + noise
        measured_V = applied + np.random.normal(0, 0.002, n)

        self._stream(measured_V, current_uA)

        # Real perform_DPV returns raw [current(A), potential]; match that shape.
        return np.column_stack((current_uA * 1e-6, measured_V))

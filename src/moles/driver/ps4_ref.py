#!/usr/bin/env python3
"""Serial driver for the STM32G473-based open-source potentiostat.

Original work by Han Hao, Sergio Pablo-García, and Gun Deniz Akkoc, from the
open-source potentiostat platform described in Device 2025, 3, 100567
(doi: 10.1016/j.device.2024.100567). Licensed under the Apache License,
Version 2.0.

This file has been modified for the MOLES project. The
changes harden the original command set:
  - logging via the standard library in place of print statements
  - SerialReadTimeout / SerialAckError and retry helpers, so a short or
    unacknowledged serial read is retried and reported rather than raising
    an opaque IndexError partway through an experiment
  - _safe_open_switch(), which matches the DAC to the measured cell
    potential before opening the CE-WE switch, suppressing the output spike
  - stream_voltage_batch(), for gapless upload of a precomputed waveform

Original header:
    2023-08-30
    deniz
"""
import logging
import serial
import threading
import time
import numpy as np
from collections import deque
from contextlib import contextmanager
from enum import IntEnum
from pathlib import Path
# import tomllib
import sys
from typing import Union
from .proc_echem import *
from typing import List
from ..onboarding import firmware as firmware_notes

logger = logging.getLogger(__name__)


class SerialReadTimeout(Exception):
    """Raised when a serial read returns fewer bytes than expected, even after a retry."""


class SerialAckError(Exception):
    """Raised when the device does not acknowledge a command with "OK", even
    after the command has been re-sent once. Means the command may not have
    taken effect on the hardware."""


class WaveformTrackingError(Exception):
    """Raised when a batch run's output stops following the programmed
    waveform, or the firmware rejects a corrupted waveform point. The run has
    already been stopped and the cell disconnected when this is raised."""


class Commands(IntEnum):
    """Contains index for each serial command

    """
    READ_ADC = 1
    WRITE_DAC = 2
    READ_DAC = 3
    WRITE_SWITCH = 4
    WRITE_GAIN = 5
    EXECUTE_VOLTAGE_BATCH = 7
    _WRITE_BUFFER = 8
    _READ_BUFFER = 9
    WRITE_CURRENT_HOLD = 10
    _RESET_BUFFER = 11
    WRITE_SAMPLE_COUNT = 12
    WRITE_AUTO_GAIN = 13
    READ_ANALOG_GAIN = 14
    READ_AUTO_GAIN = 15
    READ_GAIN = 16
    READ_SWITCH = 17
    STOP_CURRENT_HOLD = 18
    READ_DIAGNOSTICS = 19
    # Self-framing positional buffer write ([u16 len][u16 pos] + data);
    # requires July 2026 firmware. See stream_voltage_batch.
    _WRITE_BUFFER_AT = 20
    # Firmware-timed step-response capture for the R_u measurement
    # ([u32 n_samples][u32 period_us]); requires Aug 2026 firmware. The
    # driver probes for it once and falls back to the serial burst.
    STEP_CAPTURE = 21
    # The same capture with the step made by the DAC while the cell stays
    # connected ([u32 n_samples][u32 period_us][u32 n_pre][f32 dac_V]);
    # requires firmware 1.7 (FW_FEATURE_DAC_STEP).
    DAC_STEP_CAPTURE = 22
    # On-board iR compensation for batches started with BATCH_FLAG_IR_COMP:
    # its configuration, and its run report; firmware 1.8 (FW_FEATURE_IR_COMP).
    IR_CONFIG = 23
    IR_REPORT = 24

    
class Resistors():
    """Contains index information of resistor values along with the resistance values for gain.
    This information is then used for V to I conversion (simple V=IR)
    """
    R_100 = 0
    R_1K = 1
    R_10K = 2
    R_100K = 3
    R_1M = 4
    #R_10M = 5

class ADC():
    WE_OUT = 0 #WE-CE potential for conversion to current with resistor multiplier.
    RE_OUT = 1 #WE-RE potential
    VREF = 2
    TEMP = 3 #Temperature
    HUMID = 4 #Humidity

class DAC():
    CE_IN = 0 #RE-WE
    A_REF = 1
    V_AN = 2


class SwitchState(IntEnum):
    On = 1 #CE and WE connected
    Off = 0 #CE and WE disconnected

# Bench-measured output limits (July 2026, dummy-cell tests): the output
# amplifier saturates near +/-4.6 V (-4.60/+4.62 V on a 450-ohm dummy cell)
# and the USB supply tops out near 33 mA (3.35 V on 100 ohm). Setpoints beyond
# these cannot be reached even in an ideal cell; real cells saturate earlier
# because solution resistance and counter-electrode overpotential consume part
# of the headroom. The software input range (+/-5 V) is wider than this, so
# the UIs warn before starting a run whose setpoints exceed the threshold.
HW_VOLTAGE_COMPLIANCE_V = 4.6
VOLTAGE_WARN_THRESHOLD_V = 4.5

# Firmware step-capture (CMD_STEP_CAPTURE) defaults for the R_u measurement:
# 25 us sampling (the firmware's floor) over 150 ms. Only the first
# millisecond or so feeds the fit; the rest is kept in the saved traces to
# show what the cell did afterwards.
RU_CAPTURE_PERIOD_US = 25
RU_CAPTURE_MS = 150.0

# While the switch is open the control amplifier winds up against its rail,
# so for the first few samples after the switch closes the current is its
# recovery (~0.5 mA for ~50 us on the bench, 2026-09-27), not the cell's
# response. Its sign follows the setpoint error, so it flips with the step
# direction and cannot be subtracted as a fixed shape; those samples are
# skipped instead. RU_MIN_BLANK_MS skips the first three 25 us samples, which
# covers the recovery on every trace so far. The zero-step control capture
# extends it on a board that recovers more slowly: the last sample within
# RU_ARTIFACT_WINDOW_MS still off the control's settled level by more than
# RU_ARTIFACT_SIGMAS noise widths and RU_ARTIFACT_FRACTION of its first
# sample's excursion, plus one sample of margin. (Without the fraction,
# ~30 uA wiggles after the spike stretched the skip, and the answer moved
# with it: 346 vs 407 ohm on repeats, bench 2026-08-15.) A recovery longer
# than RU_MAX_BLANK_MS would hide the transient the fit needs, so it is
# refused.
RU_ARTIFACT_WINDOW_MS = 2.0
RU_ARTIFACT_SIGMAS = 6.0
RU_ARTIFACT_FRACTION = 0.1
RU_MIN_BLANK_MS = 0.075
RU_MAX_BLANK_MS = 0.5

# The fit covers this long after the skipped samples (at least
# RU_FIT_MIN_SAMPLES samples, for the slow serial fallback). A real cell
# decays on several time scales, and a longer window lets the slower ones
# drag the extrapolated current down and R_u up: on a ferrocene cell 1, 2
# and 5 ms windows gave 368-389, 384-397 and 411-429 ohm (bench 2026-09-27).
RU_FIT_WINDOW_MS = 1.0
RU_FIT_MIN_SAMPLES = 8

# Ohmic current is symmetric in the step direction. When one step's early
# current (relative to the zero-step control) exceeds the other's by more than
# this factor, something besides R_u dominates, e.g. a step that landed on a
# redox wave, and the measurement is refused. The ferrocene cell above read
# ~1.6, from ferrocene oxidizing on the positive step.
RU_MAX_ASYMMETRY = 3.0

# Firmware 1.7's DAC-step capture moves the potential with the cell already
# connected, so there is no recovery to skip and no charge pushed into the
# double layer before the fit starts (the switch-close spike did both, and
# read R_u high: 335-384 ohm on a ferrocene cell whose CVs put it at
# ~225-320, bench 2026-09-27). What the board reads is then the cell's own
# current through the board's filters, fixed by the as-built parts (PCBWay
# BOM, 2026-05-02): the potential translator (R6 300k x C14 56 pF, 16.8 us),
# the current-to-voltage converter on range 0 (93.4 ohm x ~16.8 nF, 1.6 us)
# and the WE_OUT translator (R21 100k x C24 150 pF, 15 us). Each reading
# also sums 16 ADC conversions spread over ~9.4 us. The fit models all of
# this. On a 0.3 ms cell, a plain exponential fitted from 30 us reads R_u
# 16 % high (the rise drags it), and one fitted from 100 us, past the rise,
# reads it 10 % low (the filters delay the whole decay). The fit starts
# RU_DAC_FIT_START_MS after the step, past the steepest part of the rise, and
# RU_PRE_SAMPLES readings before the step give the cell's zero.
RU_FILTER_TAUS_MS = (0.0168, 0.0016, 0.0150)
RU_ADC_WINDOW_MS = 0.0094
RU_DAC_FIT_START_MS = 0.03
RU_PRE_SAMPLES = 40

# A DAC-step fit whose current at t=0 is uncertain by more than this fraction
# (its own standard error) is refused: the step current is too small against
# the noise, as for a 50 mV step on a ~10 kohm cell, which on range 0 draws
# only about the ~4 uA noise of a single reading.
RU_MAX_I0_ERROR = 0.05

# The firmware's KADC16: raw 16-bit ADC counts to ADC volts (3.3 V full scale)
ADC16_COUNTS_TO_V = 3.3 / 65535.0

# Waveform bytes per upload message. USB full-speed packets carry at most 64
# bytes, and a message that fits in one packet always reaches the firmware
# whole; a longer one arrives in pieces, and a piece boundary is exactly where
# older firmware's frame-silence race cut 66-byte CV/DPV uploads short,
# shifting every later waveform point 2 bytes off its float boundary and
# swinging the output rail to rail until the ring wrapped (bench-diagnosed
# 2026-09-23). 48 leaves room for the 6-byte positional header and is a whole
# number of 6-point (24-byte) feed chunks.
WAVEFORM_MSG_DATA_BYTES = 48

# Read timeout while a batch streams. Samples arrive every few milliseconds, so
# a silence this long means the stream has stopped (for example the firmware
# ended it on a corrupted setpoint), and the run should fail fast instead of
# waiting out the port's 10 s default with the plot frozen.
BATCH_READ_TIMEOUT_S = 3.0

# Per-batch options (the optional third EXECUTE word, late September 2026
# firmware; older firmware ignores it, and two-word hosts get neither option).
# SAMPLE_AT_END reads the cell at the end of each setpoint's dwell, as
# staircase and pulse voltammetry expect, instead of ~0.1 ms after the setpoint
# is written, where every 2.4 mV DAC step's charging spike lands in the reading
# (bench 2026-09-23: +/-3.3 uA on a ferrocene CV). STARVE_FAILSAFE makes the
# firmware end the batch with the cell disconnected if waveform data stops
# arriving for 2 s. Only CV/DPV ask for it: the alternating-polarity loop
# restarts a failed stream without reconnecting the cell.
BATCH_FLAG_SAMPLE_AT_END = 1 << 0
BATCH_FLAG_STARVE_FAILSAFE = 1 << 1
# AUTO_RANGE lets the firmware change the current range during the batch as
# the current changes (firmware 1.5+). It only changes range as a step begins,
# so each reading at the end of a step is on the range it settled on, and only
# at points that are a multiple of the period in the flag word's top 16 bits:
# a DPV passes its step-plus-pulse length, so both readings of a pulse pair
# share a range.
BATCH_FLAG_AUTO_RANGE = 1 << 2
BATCH_RANGE_PERIOD_SHIFT = 16
# Add the ohmic drop back onto each setpoint on the board, as configured by
# Commands.IR_CONFIG (firmware 1.8+; see configure_ir_compensation)
BATCH_FLAG_IR_COMP = 1 << 3

# Capability flags in the firmware's diagnostics report (September 2026+)
FW_FEATURE_SAMPLE_AT_END = 1 << 0
FW_FEATURE_ABSOLUTE_TIMING = 1 << 1
FW_FEATURE_STARVE_FAILSAFE = 1 << 2
# Current ranges 1-4 connect one feedback resistor each (firmware 1.4+); older
# firmware's switch table connected the wrong combinations on these boards
FW_FEATURE_TRUE_RANGES = 1 << 3
# The batch auto-range option above (firmware 1.5+)
FW_FEATURE_AUTO_RANGE = 1 << 4
# Commands.DAC_STEP_CAPTURE, the R_u step made with the cell connected
# (firmware 1.7+)
FW_FEATURE_DAC_STEP = 1 << 5
# On-board iR compensation (firmware 1.8+)
FW_FEATURE_IR_COMP = 1 << 6

# Limits of the on-board iR loop. The correction moves at most IR_MAX_STEP_V
# per step (2.5 V/s at 4 ms steps; the ferrocene CVs of 2026-09-27 needed
# under 1 mV per step), and never further than the output can go: the
# compliance limit less the sweep's largest programmed potential, so the
# window of a scan is never what limits it. The board ends the run with the
# cell disconnected on:
#  - a runaway, the signature of an overestimated R_u: the correction climbs
#    at its walking limit and still falls further behind the readings, for
#    IR_RUNAWAY_MS. That bounds how far past the program it can drive the
#    cell to about 25 steps' worth, ~0.25 V. (Catching up with a large but
#    steady drop also walks at the limit, but gains every step.)
#  - the correction held at that cap, or the output at the DAC's end, for
#    IR_LIMIT_MS: the board cannot deliver the compensated sweep.
# Each new reading moves the smoothed correction IR_FEEDBACK_ALPHA of the
# way, which halves the reading noise fed back at one step's lag.
IR_MAX_STEP_V = 0.010
IR_RUNAWAY_MS = 100.0
IR_LIMIT_MS = 250.0
IR_MIN_HEADROOM_V = 0.05
IR_FEEDBACK_ALPHA = 0.5
# Largest share of R_u the live correction may add back. The board's loop is
# one step behind, so it stays stable up to the TRUE R_u; this keeps a margin
# for R_u measured ~5 % high.
IR_MAX_FRACTION = 0.9

# Current ranges (gain index 0-4) on firmware 1.4+: nominal feedback
# resistance in ohms, bench-confirmed on board TEST within 1 % (2026-09-27).
# Range 0 is three resistors together and is the range every board's current
# calibration was made on; ranges 1-3 are 1k, 10k and 100k, each with the 1M
# base in parallel, and range 4 is the 1M base alone. Each switched resistor
# carries ~4 ohm of switch resistance.
RANGE_OHMS = (93.4, 1003.0, 9905.0, 90912.0, 1.0e6)
RANGE_LABELS = ('+/-50 mA', '+/-5 mA', '+/-500 uA', '+/-50 uA', '+/-5 uA')
# The amplifier output swings about +/-4.9 V before a range's reading clips.
# A reading beyond AMP_SATURATION_V sits at its range's limit: the current was
# larger than the range can read, and while it was, the cell was not held at
# the programmed potential.
AMP_FULL_SCALE_V = 4.9
AMP_SATURATION_V = 4.75
# An auto-ranged run starts on the least sensitive range and the firmware
# moves one range more sensitive per this many points at most, so the run is
# held at its first potential long enough to climb all the way before its
# saved data begins. Those lead-in points are read but not kept: on the least
# sensitive ranges a small current is noisy and carries the zero correction's
# residual error (bench 2026-09-27: a few uA jump over the first 16 points).
AUTO_RANGE_UP_POINTS = 8

# The diagnostics report's counters, in order. Older firmware sends a prefix
# of this list: four counters before September 2026, then five.
DIAGNOSTIC_FIELDS = ('reset_flags', 'tx_dropped', 'rx_overflow', 'starved_steps',
                     'rejected_setpoints', 'late_steps', 'max_late_ms',
                     'resyncs', 'starve_aborts', 'features')

# Time the cell is held, connected, at a scan's first potential before the
# waveform starts: the conventional "quiet time", which lets the double layer
# charge so the first points are not a switch-on transient.
DEFAULT_QUIET_S = 2.0

# The CE DAC is 12-bit: the output takes one of 4096 levels across the +/-5 V
# span, about 2.42 mV apart at the cell.
DAC_FULL_SCALE_CODES = 4095
# That spacing for a board without a stored voltage calibration: the 10 V span
# times the legacy x0.9919 scale inside _scale_input_voltages, over the codes.
# A board's own spacing (dac_grid) differs by its calibration slope, ~1 %.
NOMINAL_DAC_STEP_V = 10.0 * 0.9919 / DAC_FULL_SCALE_CODES

# A run's true step period is measured from when its data arrive. It replaces
# the nominal period on the time axis only when the run is long enough for the
# measurement to beat the device clock's own ~0.2 % error, and it is plausible.
TIMING_FIT_MIN_S = 10.0
TIMING_FIT_MAX_DEVIATION = 0.01
TIMING_FIT_MAX_RESIDUAL_S = 0.05


class WaveformTracker:
    """Spots a batch output that has stopped following its programmed waveform.

    Between consecutive samples of a healthy run the measured potential moves
    by the programmed step (millivolts for a sweep, one pulse edge for DPV),
    or by less when the cell cannot slew that fast, and it stays close to the
    programmed potential. Corrupted setpoints, such as a waveform ring read
    off its float boundaries, throw the output volts away in one sample, in a
    direction the program never asked for, and it then sits at a rail or
    keeps jumping. A sample counts as a jump when the measured change is
    large AND disagrees with the programmed change by as much: a lagging cell
    fails the first test, a cell following a big programmed step the second,
    and output that drifts out of compliance never jumps at all.

    The run stops when, after a jump, the output stays away from where it was
    tracking for ``HOLD_SAMPLES`` samples, or when ``TRIP_JUMPS`` jumps land
    within ``WINDOW`` samples. A one-sample glitch jumps out and straight
    back, and passes.
    """
    JUMP_V = 0.5
    HOLD_SAMPLES = 3
    TRIP_JUMPS = 3
    WINDOW = 12

    def __init__(self, programmed_V):
        self._programmed = np.asarray(programmed_V, dtype=float)
        self._prev_index = None
        self._prev_measured = None
        self._prev_offset = 0.0
        self._jumps = deque()
        self._away_since = None     # (index, offset tracked before the jump)
        self._away_count = 0

    def update(self, first_index, measured_V):
        """Check measured potentials for points ``first_index`` onwards.

        Both arrays are in the driver's raw potential units (before the
        board's read calibration). Returns a description of the fault once
        the guard trips, otherwise None.
        """
        for k, measured in enumerate(np.asarray(measured_V, dtype=float)):
            i = first_index + k
            if i >= len(self._programmed) or not np.isfinite(measured):
                continue
            offset = measured - self._programmed[i]
            if self._prev_index == i - 1:
                moved = measured - self._prev_measured
                asked = self._programmed[i] - self._programmed[i - 1]
                if abs(moved) > self.JUMP_V and abs(moved - asked) > self.JUMP_V:
                    self._jumps.append((i, moved))
                    if self._away_since is None:
                        self._away_since = (i, self._prev_offset)
                        self._away_count = 0
            if self._away_since is not None:
                if abs(offset - self._away_since[1]) > self.JUMP_V:
                    self._away_count += 1
                    if self._away_count >= self.HOLD_SAMPLES:
                        return (f"the measured potential jumped "
                                f"{offset - self._away_since[1]:+.2f} V away "
                                f"from the programmed waveform at point "
                                f"{self._away_since[0]} and did not come back")
                else:
                    self._away_since = None    # back on track: a glitch
            self._prev_index, self._prev_measured = i, measured
            self._prev_offset = offset
            while self._jumps and self._jumps[0][0] <= i - self.WINDOW:
                self._jumps.popleft()
            if len(self._jumps) >= self.TRIP_JUMPS:
                worst = max(abs(m) for _, m in self._jumps)
                return (f"the measured potential jumped by up to {worst:.2f} V "
                        f"between samples {len(self._jumps)} times within "
                        f"{self.WINDOW} samples, near point {i}")
        return None


def cv_cycle_numbers(waveform, min_V, max_V, cycles):
    """Label each point of a CV target waveform with its cycle (0-based).

    Cycles are counted by how far the scan has swept since it began: every
    full round trip across the window (2 x |max_V - min_V| of travel) is one
    cycle. This follows the real sweep wherever Start sits, where splitting
    the points evenly drifts whenever a lead-in or lead-out is present. The
    lead-out to the final potential stays with the last cycle, and the point
    that closes a round trip stays with the cycle it closes.
    """
    waveform = np.asarray(waveform, dtype=float)
    n_cycles = max(int(cycles), 1)
    period = 2.0 * abs(max_V - min_V)
    if waveform.size == 0 or period == 0:
        return np.zeros(waveform.size)
    travel = np.concatenate(([0.0], np.cumsum(np.abs(np.diff(waveform)))))
    # The small tolerance absorbs float round-off in the running sum, so a
    # point landing exactly on the end of a round trip is not bumped forward
    labels = np.ceil(travel / period - 1e-6) - 1
    return np.clip(labels, 0, n_cycles - 1)


def build_cv_waveform(min_V, max_V, cycles, step_V, start_V, last_V,
                      scan_direction='Negative'):
    """The sequence of target potentials for a CV sweep.

    A 'Negative' scan starts by going down to min_V; a 'Positive' scan starts
    by going up to max_V. One cycle visits each vertex once: Start -> first
    vertex -> second vertex, the vertex-to-vertex sweeps repeat for the
    remaining cycles, then the scan leaves the second vertex for Last.

    Every leg moves in equal steps as close to ``step_V`` as its length allows
    and lands exactly on its end vertex, and each vertex appears once, so the
    sweep neither pauses at a turn nor runs fast on a leg (the old legs were
    ``linspace`` with one point too few, and repeated every vertex).
    """
    if scan_direction == 'Positive':
        first, second = max_V, min_V
    else:
        first, second = min_V, max_V

    # Each cycle sweeps first -> second; the return to the first vertex only
    # runs between cycles, so the lead-out to Last cannot add a cycle
    vertices = [start_V, first, second]
    for _ in range(max(1, int(cycles)) - 1):
        vertices += [first, second]
    vertices.append(last_V)

    cv = [float(start_V)]
    for v_from, v_to in zip(vertices[:-1], vertices[1:]):
        span = abs(v_to - v_from)
        if span == 0:
            continue
        n_steps = max(1, int(np.round(span / step_V)))
        cv.extend(np.linspace(v_from, v_to, n_steps + 1)[1:])
    return np.asarray(cv, dtype=float)


def measure_step_period_ms(chunk_times_s, points_per_chunk, nominal_ms):
    """Measure a batch's real step period from when its chunks arrived.

    The device clocks points on its own oscillator, which runs ~0.2 % fast
    (bench 2026-09-23), and the host blocks on every chunk, so the arrival
    times follow the device clock plus a little USB and scheduling jitter; a
    straight-line fit through all of them averages the jitter out.

    Returns:
        tuple: ``(period_ms, trusted)``. ``trusted`` is False when the run was
            too short, the arrivals too irregular (the host fell behind), or
            the result implausible; the nominal period should be used then.
    """
    t = np.asarray(chunk_times_s, dtype=float)
    # The first arrivals include the stream's start-up latency
    t = t[min(5, len(t) // 10):]
    if len(t) < 10:
        return None, False
    k = np.arange(len(t))
    slope, intercept = np.polyfit(k, t, 1)
    period_ms = float(slope / points_per_chunk * 1000.0)
    scatter_s = float(np.std(t - (slope * k + intercept)))
    trusted = ((t[-1] - t[0]) >= TIMING_FIT_MIN_S
               and abs(period_ms / nominal_ms - 1.0) <= TIMING_FIT_MAX_DEVIATION
               and scatter_s <= TIMING_FIT_MAX_RESIDUAL_S)
    return period_ms, bool(trusted)


def _format_ms(value_ms):
    """A duration for a message: microseconds below 1 ms, where one decimal
    of milliseconds would print a 25 us sample interval as '0.0 ms'."""
    if value_ms is None or not np.isfinite(value_ms):
        return str(value_ms)
    if abs(value_ms) < 1.0:
        return f"{value_ms * 1000.0:.0f} \u00b5s"
    return f"{value_ms:.1f} ms"


class Potentiostat():
    serial = None
    serial_port = None
    device_ID = 0
    baudrate = 115200
    _chunk_size = 254 #number of bytes that can be communicated at once minus 2 (for ID and command bytes) !!!CONFIG
    _vin_lims = [-5,5]
    _vout_lims = [0,3.3]
    _auto_gain = False
    _switch_state = False
    _rx_buffer_size = 24000 #rx buffer size of the pstat
    _tx_buffer_size = 64000 #tx buffer size of the pstat
    # Whether the connected firmware has CMD_STEP_CAPTURE: None until the
    # first R_u measurement probes for it, then cached for the connection.
    _step_capture_supported = None
    # Whether the connected firmware has CMD_BUFFER_WRITE_AT: None until the
    # first CV/DPV upload probes for it, then cached for the connection.
    _positional_writes_supported = None
    # Whether the connected firmware answers the diagnostics query: None until
    # the first batch run asks, then cached for the connection.
    _diagnostics_supported = None
    # What the last CV/DPV batch run reported about itself: sampling mode,
    # nominal and measured step period, and the firmware's health counters
    # for the run (see _finish_batch_report). None before the first run.
    last_batch_report = None

    # Resistance the driver converts each range's readings with. Range 0
    # keeps the historical 100 ohm that every board's read calibration was
    # fitted against (the range is really ~93.4 ohm; the calibration slope
    # carries the difference); ranges 1-4 are the real ones of firmware 1.4+.
    res_vals    = {Resistors.R_100: 1e2,
                Resistors.R_1K: RANGE_OHMS[1],
                Resistors.R_10K: RANGE_OHMS[2],
                Resistors.R_100K: RANGE_OHMS[3],
                Resistors.R_1M: RANGE_OHMS[4],
    }
    # Per-board feedback resistances, for calibration code to replace with
    # measured values; only their ratios to range 0 are used
    range_ohms = RANGE_OHMS
    # The connected firmware's capability flags: None until first asked for
    _features = None
    # The connected firmware's release (e.g. "1.8"): None until known
    firmware_version = None
    # Which current range each sample of the last batch was taken on
    last_batch_ranges = None
    
    def _select_resistor(self, current):
        """_summary_

        Args:
            current (float): Target Ampere

        Returns:
            Resistors: Best resistance for highest gain without saturation of the ADC
        """
        max_currents = [0.05, 0.005, 0.0005, 0.00005, 0.000005, 0.0000005] #in Amps
        #max_currents = [0.05, 0.005, 0.0005, 0.00005, 0.000005, 0.0000005, 0.00000005] #in Amps (with 10M resistor)
        for i in range(len(max_currents)):
            if abs(current) > (max_currents[i] * 0.9):
                if i < 1: return 0
                return i-1
        return Resistors.R_1M

    def _select_100ohm_resistor(self, current):
        """_summary_

        Args:
            current (float): Target Ampere

        Returns:
            Resistors: R_100 ohm resistor for electrosynthesis-scale currents (mA level)
        """
        # Hardware appears stuck on R_100, force selection.
        return Resistors.R_100


    class Errors(IntEnum):
        NO_ERROR = 0
        ERROR = 1

    def __init__(self,serial_port='/dev/ttyACM1',baudrate=115200,device_ID = 1):
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.device_ID = device_ID
        # Serializes every command/response transaction on this port. Multiple
        # threads can legitimately reach for the same device (experiment loop,
        # emergency cleanup at shutdown, reconnect) and interleaved commands
        # desync the request/response protocol. RLock so composite operations
        # (e.g. a whole voltage batch) can hold it across nested calls.
        self._port_lock = threading.RLock()

    def connect(self):
        with self._port_lock:
            # timeout=10 ensures readline() gives up after 10 seconds instead of blocking forever.
            # write_timeout=2 ensures a wedged USB device makes writes fail instead of blocking
            # the calling thread forever (a hung write used to lock a channel until app restart).
            # dsrdtr=False prevents pyserial from toggling the DTR line on open, which can trigger
            # an unintended STM32 reset and cause the device to be unresponsive to the first commands.
            # exclusive=True makes the OS refuse a second open of this port by any other process
            # (flock on POSIX; Windows COM ports are always exclusive), so two MOLES apps can
            # never interleave commands to one board.
            self.serial = serial.Serial(self.serial_port, self.baudrate, timeout=10,
                                        write_timeout=2, dsrdtr=False, exclusive=True)
            # Allow time for the device UART stack to initialize after the USB connection is established.
            # 1.0s is conservative but avoids the PermissionError that occurs when commands arrive
            # before the firmware is ready.
            time.sleep(1.0)
            # Ask Windows for a larger receive buffer. NOTE: measured on the
            # bench (July 2026), the USB-serial driver caps its willingness to
            # buffer unread data at ~12 kB regardless of this request — it then
            # simply stops reading from the device. So this call is kept as a
            # harmless hint, but the real protections are the batch-loop
            # backlog guard and the device-side pre-fill (see methods/cv.py).
            try:
                self.serial.set_buffer_size(rx_size=1_048_576)
            except Exception:
                pass
            logger.info("[PS %s] serial dev created on %s", self.device_ID, self.serial_port)
            # The board may have been reflashed since a previous connection
            self._positional_writes_supported = None
            self._diagnostics_supported = None
            self._features = None
            self.firmware_version = None
            try:
                # A session that died mid-experiment can leave the device still
                # streaming batch data, which would garble the handshake below.
                self.halt_batch_stream()

                self.write_switch(False)
                # Park at the least-sensitive shunt: safest default if the switch is later
                # turned on without an experiment script having set its own gain.
                self.write_gain(Resistors.R_100)
                self.write_auto_gain(False)
                #Sync current status of the pstat to the class by read commands
                logger.info("[PS %s] switch is %s, gain is %s, auto_gain is %s",
                            self.device_ID, self.read_switch(), self.read_gain(),
                            self.read_auto_gain())
                self.write_dac(channels=[DAC.A_REF,DAC.V_AN], voltages=[-5,0])
            except Exception:
                # Cut potentiostat output before tearing down the connection.
                try:
                    self.write_switch(0)
                except Exception:
                    pass
                self.serial.close()
                self.serial = None
                raise
            self._note_firmware()

    def _note_firmware(self):
        """Learn which firmware release the board runs and note it where the
        launcher can show it. Never fails the connection."""
        report = self._read_diagnostics_quietly()
        if report is None and self._diagnostics_supported is not False:
            return
        self._features = int(report.get('features', 0)) if report else 0
        self.firmware_version = firmware_notes.version_from_diagnostics(report)
        if self.firmware_version is None:
            return
        logger.info("[PS %s] firmware %s", self.device_ID, self.firmware_version)
        firmware_notes.record(firmware_notes.usb_serial_for_port(self.serial_port),
                              self.firmware_version)

    def disconnect(self):
        with self._port_lock:
            self.serial.close()

    def force_close(self):
        """Close the underlying port WITHOUT taking the port lock.

        Recovery hatch for watchdogs: if a thread is hung inside a serial
        operation (holding the lock), closing the handle from another thread
        makes the pending read/write raise, which unblocks the hung thread
        into its normal error path. Never use this in normal control flow.
        """
        try:
            if self.serial is not None:
                self.serial.close()
        except Exception:
            logger.exception("[PS %s] force_close failed", self.device_ID)

    def _scale_input_voltages(self,voltages):
        """Map (and limit) to be inputted actual potential(V) values to pstat operation range
        """
        #changes!
        # return (np.interp(voltages,self._vin_lims,self._vout_lims) - 0.0494)/0.9919
        # return np.interp(voltages,self._vin_lims,self._vout_lims)
        voltages_calibrated = (np.array(voltages)- 0.0494)/0.9919
        return np.interp(voltages_calibrated,self._vin_lims,self._vout_lims)
    
    def _scale_output_voltages(self,voltages):
        """Map (and limit) outputted potential(V) values to actual values
        """
        # return np.interp(voltages,self._vout_lims,self._vin_lims)
        voltages = np.interp(voltages,self._vout_lims,self._vin_lims)
        return voltages * 0.9919 + 0.0494
    
    def _current_to_voltage(self,currents):
        """Converts target current value(s) to DAC voltage value to be written.

        Args:
            currents (np.float32): current(I) value(s)

        Returns:
            np.float32: DAC voltage value
        """
        # Derived from empirical data: (0.1V, 0.3mA), (1.0V, 8.9mA) -> Refined with 4-point fit
        # V = 113.0 * I_A + 0.060
        OFFSET = 0.060
        GAIN = 113.0
        
        # Fix for Asymmetry:
        # Previously used logic: V = -(GAIN*|I| + OFFSET) for I>0, V = +(GAIN*|I| + OFFSET) for I<0.
        # This created a discontinuity (Jump from -0.06 to +0.06) and over-drove negative currents.
        # User reported Correct Positive (+1mA) but Excess Negative (-2.25mA for -1mA target).
        # Positive worked implies: V_pos = -113*I - 0.06 is correct.
        # Extending this linearly to negative: V_neg = -113*(-1) - 0.06 = 113 - 0.06 = 53 mV.
        # (Previous logic sent 113 + 0.06 = 173 mV, causing the overshoot).
        
        voltages = (currents * GAIN * -1.0) - OFFSET
        
        voltages = self._scale_input_voltages(voltages)
        return voltages

    def _check_response(self,timeout_s=10):
        """Check whether the response to a command is an OK line.
        Logs a warning if the hardware returns an unexpected response.
        """
        resp = self.serial.readline()
        resp = resp.strip()
        if resp == b"OK":
            return True
        else:
            logger.warning("[PS %s] Unexpected hardware response: %r", self.device_ID, resp)
            return False

    def _write_cmd(self,cmd,data,check_response=True,retry=True):
        """Write command by its data. Adds device ID to the beginning of the data package.

        When ``check_response`` is set, a missing/garbled "OK" acknowledgement
        re-sends the command once (all simple commands are idempotent), and a
        second failure raises ``SerialAckError`` — a command that silently
        never took effect is worse than a loud failure, especially for safety
        commands like opening the cell switch. Pass ``retry=False`` for
        commands that must not be re-sent (e.g. EXECUTE_VOLTAGE_BATCH, where a
        duplicate would corrupt the stream protocol).

        Args:
            cmd (Commands): command index
            data (any): command data
            check_response (bool, optional): Whether to check for OK. Defaults to True.
            retry (bool, optional): Re-send once on a failed acknowledgement.
        """
        msg = bytearray()
        msg.extend(np.uint8(self.device_ID).tobytes()) #device ID
        msg.extend(np.uint8(cmd).tobytes()) #Command
        if not (data is None):
            if str(type(data)) == "<class 'bytes'>" or str(type(data)) == "<class 'bytearray'>":
                msg.extend(data)
            else:
                msg.extend(data.tobytes())

        with self._port_lock:
            self.serial.write(msg)
            if not check_response:
                return None
            if self._check_response():
                return True
            if retry:
                logger.warning("[PS %s] Re-sending unacknowledged command %s",
                               self.device_ID, cmd)
                self.serial.reset_input_buffer()
                self.serial.write(msg)
                if self._check_response():
                    return True
            raise SerialAckError(
                f"Device {self.device_ID} did not acknowledge command {cmd!r}"
            )

    def _read_with_retry(self, n_bytes, cmd, data):
        """Read exactly n_bytes from the serial port. On a short read (transient
        USB latency or firmware-busy window), flush the input buffer to discard
        any late-arriving stragglers, re-issue the command, and retry once.
        Raises SerialReadTimeout if the second attempt also returns short.
        """
        with self._port_lock:
            result = self.serial.read(n_bytes)
            if len(result) < n_bytes:
                self.serial.reset_input_buffer()
                self._write_cmd(cmd, data, False)
                result = self.serial.read(n_bytes)
                if len(result) < n_bytes:
                    self.serial.reset_input_buffer()
                    raise SerialReadTimeout(
                        f"Expected {n_bytes} bytes, received {len(result)} after one retry"
                    )
        return result

    def _reset_buffer(self, pos=0):
        self._write_cmd(Commands._RESET_BUFFER,np.uint32(pos))

    def _write_to_buffer(self, data):
        """Append waveform bytes to the device ring with the legacy write
        (CMD_BUFFER_WRITE), in messages that each fit one USB packet."""
        # Small, paced messages: every message must stay inside one USB packet
        # (see WAVEFORM_MSG_DATA_BYTES), and the gap keeps each in its own frame
        tx_delay = 0.01 # 10ms delay to allow hardware interrupt usage

        ind = 0
        total_len = len(data)

        while ind < total_len:
            end = min(ind + WAVEFORM_MSG_DATA_BYTES, total_len)
            chunk = data[ind:end]

            # Write chunk
            self._write_cmd(Commands._WRITE_BUFFER, chunk, False)
            self.serial.flush() # Force OS to send
            time.sleep(tx_delay) # Wait for hardware to ingest

            ind += WAVEFORM_MSG_DATA_BYTES

        return True

    def _read_from_buffer(self, data_len):
        """Read a batch-stream chunk. A short read here means the firmware
        stalled mid-batch; raise cleanly instead of letting np.frombuffer
        parse a truncated buffer into garbage downstream."""
        result = self.serial.read(data_len)
        if len(result) < data_len:
            self.serial.reset_input_buffer()
            raise SerialReadTimeout(
                f"Batch stream returned {len(result)} of {data_len} bytes"
            )
        return result

    def _read_dac_setpoint(self):
        """The CE DAC setpoint exactly as the firmware holds it (raw float32,
        ADC-scale volts)."""
        with self._port_lock:
            self._write_cmd(Commands.READ_DAC, np.uint8([DAC.CE_IN]), False)
            raw = self._read_with_retry(4, Commands.READ_DAC, np.uint8([DAC.CE_IN]))
        return np.frombuffer(raw, np.float32)[0]

    def _probe_positional_writes(self):
        """Find out whether the firmware has CMD_BUFFER_WRITE_AT (July 2026+).

        Older firmware silently ignores the command, so a positional upload
        to such a board would leave the ring holding whatever an earlier run
        left there; the answer has to be known before the upload is trusted.
        The probe fills ring slot 0 twice, first with a legacy write and then
        with a positional write of the neighbouring float, runs a one-step
        batch, and reads the DAC setpoint back: it shows which write the
        stepper saw. Both values equal the setpoint already applied to within
        one float step, far below one DAC count, so the output never moves
        and the probe is safe with the cell connected.
        """
        with self._port_lock:
            legacy_val = np.float32(self._read_dac_setpoint())
            toward = np.float32(0.0) if legacy_val > 1.0 else np.float32(3.3)
            positional_val = np.nextafter(legacy_val, toward)
            rx_len = 9 if self._auto_gain else 8

            self._reset_buffer(0)
            self._write_cmd(Commands._WRITE_BUFFER, legacy_val.tobytes(), False)
            self.serial.flush()
            time.sleep(0.01)
            header = np.uint16([4, 0]).tobytes()
            self._write_cmd(Commands._WRITE_BUFFER_AT,
                            header + positional_val.tobytes(), False)
            self.serial.flush()
            time.sleep(0.01)
            self._write_cmd(Commands.EXECUTE_VOLTAGE_BATCH, np.uint32([1, 1]),
                            True, retry=False)
            if len(self.serial.read(rx_len)) < rx_len:
                self.halt_batch_stream()
                raise SerialReadTimeout("No sample from the one-step firmware probe")
            seen = np.float32(self._read_dac_setpoint())

        supported = bool(seen == positional_val)
        if not supported and seen != legacy_val:
            logger.warning("[PS %s] Upload probe read back an unexpected setpoint "
                           "(%r); using legacy uploads.", self.device_ID, seen)
        self._positional_writes_supported = supported
        logger.info("[PS %s] Waveform uploads: %s", self.device_ID,
                    "positional (CMD_BUFFER_WRITE_AT)" if supported
                    else "legacy append (firmware predates CMD_BUFFER_WRITE_AT)")
        return supported

    def _start_waveform_upload(self, prefill):
        """Clear the device's waveform ring and load a batch's pre-fill.

        Returns the ring position where the next waveform bytes belong, for
        ``_write_waveform``.
        """
        if self._positional_writes_supported is None:
            self._probe_positional_writes()
        self._reset_buffer(0)
        return self._write_waveform(prefill, 0)

    def _write_waveform(self, data, ring_pos):
        """Write waveform bytes into the device ring starting at ``ring_pos``.

        Each message fits one USB packet, so the firmware never receives part
        of one. On firmware with CMD_BUFFER_WRITE_AT every message also names
        its ring position: a lost or merged message can then never move later
        points off their float boundaries (merges are what railed the
        alternating-polarity output in July 2026). Older firmware gets the
        legacy append. Returns the ring position after the data.
        """
        if not self._positional_writes_supported:
            self._write_to_buffer(data)
            return (ring_pos + len(data)) % self._rx_buffer_size
        ind = 0
        while ind < len(data):
            # A message may not run past the ring's end; the firmware refuses it
            n = min(WAVEFORM_MSG_DATA_BYTES, len(data) - ind,
                    self._rx_buffer_size - ring_pos)
            header = np.uint16([n, ring_pos]).tobytes()
            self._write_cmd(Commands._WRITE_BUFFER_AT,
                            header + bytes(data[ind:ind + n]), False)
            self.serial.flush()
            time.sleep(0.01)  # keep each message in its own frame
            ind += n
            ring_pos = (ring_pos + n) % self._rx_buffer_size
        return ring_pos

    @contextmanager
    def _batch_read_timeout(self, delay_ms):
        """Use a short read timeout while a batch streams (see
        BATCH_READ_TIMEOUT_S), scaled up for slow sample rates."""
        old_timeout = self.serial.timeout
        chunk_s = 6 * max(1.0, float(delay_ms)) / 1000.0
        self.serial.timeout = max(BATCH_READ_TIMEOUT_S, 10 * chunk_s)
        try:
            yield
        finally:
            self.serial.timeout = old_timeout

    def _read_batch_chunk(self, data_len):
        """Read one chunk of a running batch, explaining a stopped stream."""
        try:
            return self._read_from_buffer(data_len)
        except SerialReadTimeout as exc:
            self._explain_stream_stop(exc)

    def _explain_stream_stop(self, exc):
        """Handle a batch stream that stopped delivering samples.

        Raises WaveformTrackingError when the firmware ended the batch itself
        (it has already disconnected the cell): because its iR compensation
        ran away, because it caught a corrupted waveform point, or because
        waveform data stopped arriving for longer than its starvation limit.
        Otherwise re-raises ``exc``.
        """
        report = {}
        ir = {}
        try:
            self.halt_batch_stream()
            report = self.read_diagnostics()
            if report.get('features', 0) & FW_FEATURE_IR_COMP:
                ir = self.read_ir_report()
        except Exception:
            logger.warning("[PS %s] Could not read diagnostics after the stream "
                           "stopped", self.device_ID, exc_info=True)
        if ir.get('runaway_aborts', 0):
            self._switch_state = False
            raise WaveformTrackingError(
                "The iR compensation ran away (each increase of its "
                "correction raised the current by more), so the potentiostat "
                "stopped the run and disconnected the cell. R_u, or the share "
                "of it compensated, is too high: re-measure R_u or lower the "
                "percentage. The data up to that point is valid.") from exc
        if ir.get('limit_aborts', 0):
            self._switch_state = False
            raise WaveformTrackingError(
                "The iR-compensated potential would have gone beyond what the "
                "potentiostat can output, so it stopped the run and "
                "disconnected the cell: the ohmic drop there is larger than "
                "the room left beyond the sweep. Lower the compensation "
                "percentage or narrow the potential window. The data up to "
                "that point is valid.") from exc
        if report.get('rejected_setpoints', 0):
            self._switch_state = False
            raise WaveformTrackingError(
                "The potentiostat refused a corrupted waveform point and "
                "disconnected the cell to protect it. The data up to that "
                "point is valid; please repeat the run.") from exc
        if report.get('starve_aborts', 0):
            self._switch_state = False
            raise WaveformTrackingError(
                "The potentiostat stopped the run and disconnected the cell "
                "because waveform data stopped arriving from this computer. "
                "The data up to that point is valid; please repeat the run.") from exc
        raise exc

    def _abort_waveform_run(self, reason):
        """Stop a batch whose output has left the programmed waveform,
        disconnect the cell, and raise WaveformTrackingError."""
        logger.error("[PS %s] Output left the programmed waveform: %s. "
                     "Stopping the run and disconnecting the cell.",
                     self.device_ID, reason)
        try:
            # Halt the stepper, then open the switch at once; the pause keeps
            # the two commands in separate frames, since the firmware runs
            # only the first legacy command of a frame.
            self._write_cmd(Commands.EXECUTE_VOLTAGE_BATCH, np.uint32([0, 0]), False)
            self.serial.flush()
            time.sleep(0.01)
            self._write_cmd(Commands.WRITE_SWITCH, np.uint8(SwitchState.Off), False)
            self.serial.flush()
            self._switch_state = False
            time.sleep(0.3)  # let in-flight samples and acknowledgements land
            self.serial.reset_input_buffer()
            self.write_switch(SwitchState.Off)  # confirmed this time
        except Exception:
            logger.warning("[PS %s] Could not confirm the switch opened after the "
                           "fault", self.device_ID, exc_info=True)
        raise WaveformTrackingError(
            f"The potentiostat's output stopped following the programmed "
            f"waveform ({reason}). The run was stopped and the cell "
            f"disconnected to protect it.")

    def _read_diagnostics_quietly(self):
        """The diagnostics report, or None when it cannot be had.

        Bookkeeping around a run must never cost the run: a short timeout
        keeps a board whose firmware predates the diagnostics query (July
        2026) from stalling the start, and that answer is cached for the
        connection.
        """
        if self._diagnostics_supported is False:
            return None
        old_timeout = self.serial.timeout
        try:
            self.serial.timeout = 0.5
            report = self.read_diagnostics()
            self._diagnostics_supported = True
            return report
        except SerialReadTimeout:
            self._diagnostics_supported = False
            logger.info("[PS %s] Firmware has no diagnostics query; runs will "
                        "not report device health.", self.device_ID)
            return None
        except Exception:
            logger.warning("[PS %s] Diagnostics read failed", self.device_ID,
                           exc_info=True)
            return None
        finally:
            self.serial.timeout = old_timeout

    def _begin_batch_report(self, delay_ms, auto_range=False, range_period=1,
                            ir=None):
        """Open the run report for a batch, before its upload: snapshot the
        firmware's cumulative counters and learn which batch options (end-of-
        step sampling, starvation fail-safe, auto-ranging) it offers.

        ``auto_range`` asks for auto-ranging, changing range only at points
        that are a multiple of ``range_period``; it is honoured only when the
        firmware can sample at the end of each step and auto-range.

        ``ir``, a dict of ``ohm`` and ``max_correction_V``, asks the board to
        add that resistance times the current back onto each setpoint
        (``configure_ir_compensation``). Unlike the other options this one is
        never quietly dropped: firmware that cannot do it raises RuntimeError
        before anything is uploaded."""
        self.last_batch_report = None
        self.last_batch_ranges = None
        before = self._read_diagnostics_quietly()
        features = before.get('features', 0) if before else 0
        if before is not None:
            self._features = features
        sample_at_end = bool(features & FW_FEATURE_SAMPLE_AT_END)
        ir_ohm = float(ir['ohm']) if ir else 0.0
        if ir_ohm > 0:
            if not (sample_at_end and features & FW_FEATURE_IR_COMP):
                raise RuntimeError("On-board iR compensation needs firmware 1.8 "
                                   "or later on this board.")
            self.configure_ir_compensation(ir_ohm, delay_ms, ir['max_correction_V'])
        return {
            'ir_ohm': ir_ohm if ir_ohm > 0 else 0.0,
            'ir_max_correction_V': (float(ir['max_correction_V'])
                                    if ir_ohm > 0 else 0.0),
            'step_ms_nominal': float(delay_ms),
            'sample_at_end': sample_at_end,
            'starve_failsafe': bool(features & FW_FEATURE_STARVE_FAILSAFE),
            'auto_range': bool(auto_range and sample_at_end
                               and features & FW_FEATURE_AUTO_RANGE),
            'range_period': max(1, min(int(range_period), 0xFFFF)),
            'diagnostics_before': before,
            'chunk_times_s': [],
        }

    def _execute_batch(self, delay, count, report):
        """Start the uploaded batch with every option the firmware offers:
        sampling at the end of each step, the starvation fail-safe, and
        auto-ranging and iR compensation when the run asked for them."""
        flags = ((BATCH_FLAG_SAMPLE_AT_END if report['sample_at_end'] else 0)
                 | (BATCH_FLAG_STARVE_FAILSAFE if report['starve_failsafe'] else 0))
        if report.get('auto_range'):
            flags |= (BATCH_FLAG_AUTO_RANGE
                      | report['range_period'] << BATCH_RANGE_PERIOD_SHIFT)
        if report.get('ir_ohm'):
            flags |= BATCH_FLAG_IR_COMP
        words = [delay, count] + ([flags] if flags else [])
        # retry=False: re-sending EXECUTE while the firmware is already
        # streaming would corrupt the batch protocol; a failed ack must abort.
        self._write_cmd(Commands.EXECUTE_VOLTAGE_BATCH, np.uint32(words), True,
                        retry=False)

    def _finish_batch_report(self, report, n_points, points_per_chunk,
                             ranges=None, amp_V=None):
        """Close a completed batch's run report and keep it as
        ``last_batch_report``.

        ``health`` holds the firmware's per-run counters for this upload
        (starved, rejected and late steps, re-anchors, starvation aborts) and
        the growth of its cumulative ones (dropped and discarded messages)
        since the run began. ``degraded`` lists, in plain words, anything that
        means the cell may not have followed the programmed waveform exactly;
        an empty list is a clean run.

        ``ranges`` and ``amp_V`` — each sample's current range and amplifier
        output — add which ranges the run used, how often it changed range,
        and how many readings sat at their range's limit; the ranges are
        kept as ``last_batch_ranges``. A run with on-board iR compensation
        adds the board's iR report (``read_ir_report``) as ``ir``.
        """
        nominal_ms = report['step_ms_nominal']
        period_ms, trusted = measure_step_period_ms(
            report['chunk_times_s'], points_per_chunk, nominal_ms)
        before = report['diagnostics_before']
        after = self._read_diagnostics_quietly() if before is not None else None
        health = {}
        if after is not None:
            for key in ('starved_steps', 'rejected_setpoints', 'late_steps',
                        'max_late_ms', 'resyncs', 'starve_aborts'):
                if key in after:
                    health[key] = after[key]
            for key in ('tx_dropped', 'rx_overflow'):
                health[key] = max(0, after[key] - before.get(key, 0))

        degraded = []
        if health.get('starved_steps'):
            degraded.append(f"the potential was held for {health['starved_steps']} "
                            f"steps while waveform data was late")
        if health.get('tx_dropped'):
            degraded.append(f"{health['tx_dropped']} data messages were dropped "
                            f"because this computer read them too slowly")
        if health.get('rx_overflow'):
            degraded.append(f"{health['rx_overflow']} damaged command frames "
                            f"were discarded")
        if health.get('resyncs'):
            degraded.append(f"the step timing was re-anchored "
                            f"{health['resyncs']} times after long stalls")
        if health.get('max_late_ms', 0) >= 2 * nominal_ms:
            degraded.append(f"a step ran up to {health['max_late_ms']} ms late")

        ir = None
        if report.get('ir_ohm'):
            try:
                ir = self.read_ir_report()
            except Exception:
                logger.warning("[PS %s] Could not read the iR compensation "
                               "report", self.device_ID, exc_info=True)
            if ir is None:
                degraded.append("the board's iR compensation report could not "
                                "be read, so whether it compensated is unknown")
            elif ir['runs'] < 1:
                degraded.append("the board did not apply the iR compensation: "
                                "this run was not compensated")
            else:
                ir['limit_V'] = report['ir_max_correction_V']
                if ir['limited_steps']:
                    degraded.append(f"the iR correction was held at the output's "
                                    f"limit for {ir['limited_steps']} steps, so "
                                    f"part of the ohmic drop there was not "
                                    f"compensated")
                if ir['slewed_steps']:
                    degraded.append(f"the current changed faster than the iR "
                                    f"correction may follow for "
                                    f"{ir['slewed_steps']} steps, so the "
                                    f"compensation lagged there")

        ranges_used, range_changes, saturated = [], 0, 0
        if ranges is not None:
            ranges = np.asarray(ranges, dtype=int)
            valid = np.isfinite(np.asarray(amp_V, dtype=float))
            ranges_used = sorted(int(r) for r in np.unique(ranges[valid]))
            range_changes = int(np.count_nonzero(np.diff(ranges[valid])))
            saturated = int(np.count_nonzero(
                np.abs(np.asarray(amp_V, dtype=float)[valid]) > AMP_SATURATION_V))
            self.last_batch_ranges = ranges
        if saturated:
            degraded.append(f"{saturated} readings were at the limit of their "
                            f"current range: the current there was larger than "
                            f"recorded, and the cell was not held at the "
                            f"programmed potential")

        self.last_batch_report = {
            'points': int(n_points),
            'sample_at_end': report['sample_at_end'],
            'step_ms_nominal': nominal_ms,
            'step_ms_measured': period_ms,
            'time_axis': 'measured' if trusted else 'nominal',
            'auto_range': bool(report.get('auto_range')),
            'lead_in_points': int(report.get('lead_in', 0)),
            'ranges_used': ranges_used,
            'range_changes': range_changes,
            'saturated_points': saturated,
            'ir_ohm': float(report.get('ir_ohm', 0.0)),
            'ir': ir,
            'health': health,
            'degraded': degraded,
        }
        if degraded:
            logger.warning("[PS %s] Batch run degraded: %s", self.device_ID,
                           "; ".join(degraded))
        return self.last_batch_report

    def _batch_time_axis(self, n_points):
        """When each sample of the last batch was taken, in seconds from the
        moment its first setpoint was applied.

        Uses the step period measured over the run when that measurement can
        be trusted (the device clock runs ~0.2 % fast), else the nominal one.
        A sample taken at the end of its step belongs one period after that
        step began.
        """
        report = self.last_batch_report or {}
        period_ms = report.get('step_ms_nominal', 0.0)
        if report.get('time_axis') == 'measured':
            period_ms = report['step_ms_measured']
        offset = 1 if report.get('sample_at_end') else 0
        return (np.arange(n_points) + offset) * period_ms / 1000.0

    def _quiet_wait(self, seconds, stop_event=None):
        """Wait out a quiet time, returning early if a stop is requested."""
        seconds = max(0.0, float(seconds))
        if stop_event is None:
            stop_event = getattr(self, 'stop_event', None)
        if stop_event is not None:
            stop_event.wait(seconds)
        else:
            time.sleep(seconds)

    def _connect_cell_for_batch(self, quiet_s):
        """Connect the cell once its waveform is loaded and hold it at the
        parked first potential for the quiet time.

        Closing the switch only now (not before the upload) makes the hold at
        the first potential exactly ``quiet_s``: it used to be however long
        the pre-fill upload took, up to ~2.7 s, and unrecorded.
        """
        self.write_switch(SwitchState.On)
        self._quiet_wait(quiet_s)

    def dac_grid(self):
        """The potentials the CE DAC can actually produce, as ``(spacing,
        offset)``: level k is ``offset + k * spacing``, in the same calibrated
        units as ``write_potential_calibrated``. Waveform points between two
        levels are rounded to the nearer one by the firmware."""
        slope = self._calib_v_slope
        # Avoid div by zero on a malformed calibration row
        if abs(slope) < 1e-6:
            slope = 1.0
        lo, hi = self._scale_output_voltages(np.array([0.0, 3.3]))
        spacing = (hi - lo) * slope / DAC_FULL_SCALE_CODES
        return float(spacing), float(lo * slope + self._calib_v_intercept)

    def firmware_features(self):
        """The connected firmware's capability flags (``FW_FEATURE_*``), or 0
        when it does not report them. Asked once per connection."""
        if self._features is None:
            report = self._read_diagnostics_quietly()
            self._features = int(report.get('features', 0)) if report else 0
        return self._features

    def set_current_range(self, current_range):
        """Choose the current range for the next measurement.

        ``current_range`` is ``'auto'`` or a range index 0-4 (``RANGE_OHMS``,
        ``RANGE_LABELS``). ``'auto'`` starts on the least sensitive range
        with every reading tagged with its range, and a batch run with
        ``auto_range`` then moves between ranges as the current changes; on
        firmware that cannot auto-range it is range 0 for the whole run. A
        fixed range above 0 needs firmware 1.4 or later, because older
        firmware's ranges 1-4 are not the ones they claim to be.

        Returns what the run will use: ``'auto'`` or the fixed range index.
        """
        features = self.firmware_features()
        if current_range == 'auto':
            self._force_gain(Resistors.R_100)
            auto = bool(features & FW_FEATURE_AUTO_RANGE)
            self.write_auto_gain(auto)
            return 'auto' if auto else Resistors.R_100
        index = int(current_range)
        if not 0 <= index < len(RANGE_OHMS):
            raise ValueError(f"No current range {index}; ranges are 0-{len(RANGE_OHMS) - 1}")
        if index and not features & FW_FEATURE_TRUE_RANGES:
            raise ValueError(
                f"This board's firmware predates the corrected current ranges "
                f"(firmware 1.4), so only the {RANGE_LABELS[0]} range can be "
                f"used. Flash the image in firmware/prebuilt/ to use "
                f"{RANGE_LABELS[index]}.")
        self._force_gain(index)
        self.write_auto_gain(False)
        return index

    def _force_gain(self, index):
        """Set the current range even if the driver believes it is already
        set: after an auto-ranged run the board may have moved on."""
        self.Resistor = None
        self.write_gain(index)

    def _range_index(self):
        """The current range the driver last set or read (0 before any)."""
        return getattr(self, 'Resistor', None) or 0

    def _res_table(self):
        """``res_vals`` as an array indexed by range."""
        return np.array([self.res_vals[i] for i in range(len(self.res_vals))], dtype=float)

    def _calibrated_mA(self, raw_A, ranges):
        """Board-calibrated current (mA) from raw readings, each taken on the
        range given for it.

        The board's read calibration was fitted on range 0, and both of its
        parts belong to the amplifier, not the range: the slope to the
        measurement chain's gain, the intercept to a fixed ~57 mV offset at
        the amplifier output (bench 2026-09-27: the same offset on every
        range). So a reading on any range is the range-0 calibration applied
        to its amplifier voltage, scaled by the ratio of the two ranges'
        resistances. On range 0 this is exactly the historical calibration.
        """
        ranges = np.asarray(ranges, dtype=int)
        amp_V = -np.asarray(raw_A, dtype=float) * self._res_table()[ranges]
        range0_mA = (-amp_V / self.res_vals[0] * 1000.0 * self._calib_read_slope
                     + self._calib_read_intercept)
        return range0_mA * (self.range_ohms[0] / np.asarray(self.range_ohms, dtype=float)[ranges])

    def halt_batch_stream(self):
        """Stop any in-progress batch execution and flush its residue.

        A batch-execute with a count of 0 halts the firmware's stepper; the
        pause lets in-flight data frames land so one purge removes them all.
        Required before re-arming the batch protocol after a failed chunk: a
        chunk can keep executing on the board for many seconds after the host
        gives up on it, and a reset/execute sent mid-batch races the running
        stepper over the ring buffer while the old stream drowns out the new
        commands' acknowledgements (log-confirmed July 2026).
        """
        with self._port_lock:
            self._write_cmd(Commands.EXECUTE_VOLTAGE_BATCH, np.uint32([0, 0]), False)
            time.sleep(0.3)
            self.serial.reset_input_buffer()

    def stream_voltage_batch(self, voltages, delay, on_block, block_points=None):
        """Execute a repeating voltage waveform continuously, with no gaps.

        ``write_voltage_batch`` runs one finite pre-uploaded batch per call, so
        a long experiment built from repeated calls has a dead gap between
        batches while the next one uploads (the DAC holds its last value for
        ~0.5 s every chunk). This method instead starts a single hardware-timed
        batch with an effectively endless step count and keeps the firmware's
        ring buffer topped up while it runs, so the output waveform is seamless
        for the whole experiment.

        The waveform repeats until ``on_block`` returns False or a serial error
        occurs; in both cases the stream is halted (``halt_batch_stream``)
        before this method returns or raises.

        Requires firmware with ``CMD_BUFFER_WRITE_AT`` (July 2026 or later):
        the stream feeds the ring buffer with self-framing positional writes,
        which older firmware silently ignores — the symptom on an un-updated
        board is the stream starving into read timeouts right after start.

        Args:
            voltages: one waveform period, in the same units/scale as
                ``write_voltage_batch``'s input. It is tiled endlessly.
            delay: milliseconds between steps (same as write_voltage_batch).
            on_block: called with each parsed block of measurements as an
                [n, 2] array — column 0 CE-WE current (A), column 1 RE-WE
                potential (V), matching write_voltage_batch's return format.
                Return True to keep streaming, False to stop. It runs while
                the port lock is held mid-stream, so it must NOT issue any
                driver commands.
            block_points: measurements per callback, rounded to a multiple of
                6 (the protocol's transfer granularity). Default ~0.5 s worth.
        """
        v_per_chunk = 6 #number of potentials (V) read back from pstat each time
        bytes_per_v = 4
        tx_len = v_per_chunk * bytes_per_v

        # Feed the ring buffer in large, widely-spaced messages: 60 points per
        # write, nominally once per 10 reads. Large messages lift the feed
        # capacity an order of magnitude above per-read 6-point writes (which
        # cap out near 550 points/s and set the old 250 Hz sample-rate
        # ceiling).
        #
        # Message spacing is the safety-critical part. The firmware frames
        # messages by >2 ms of line silence, and its parser (the main loop)
        # can stall for up to ~50 ms while waiting on a slow host to collect
        # a measurement frame. Two feed messages arriving within one such
        # stall get parsed as ONE buffer-write, injecting the second one's
        # 2 header bytes into the waveform ring — every later sample is then
        # read 2 bytes off-register and the DAC rails until the ring's wrap
        # realigns it (bench-observed at 500 Hz, July 2026). So consecutive
        # feed messages must be separated by MORE than the firmware's worst
        # stall, in wall-clock time, even while the host is bursting through
        # a read backlog: hence the minimum interval below rather than a
        # fixed sleep. 80 ms still feeds 750 points/s, above the 500 Hz cap.
        feed_iterations = 10          #reads per feed message, nominal
        feed_len = feed_iterations * tx_len  #bytes per feed message
        feed_points = feed_iterations * v_per_chunk
        min_feed_interval_s = 0.08    #> firmware's ~50 ms max parser stall

        scaled = self._scale_input_voltages(voltages)
        n_cycle = int(len(scaled))
        if n_cycle < 1:
            raise ValueError("stream_voltage_batch requires at least one waveform point")
        # Tile the cycle so its byte length is a multiple of one feed message:
        # every buffer write then stays aligned with the repeating waveform
        # (and with the firmware ring's wrap point), and feed slices never
        # straddle the tile edge.
        reps = feed_points // int(np.gcd(n_cycle, feed_points))
        tile_bytes = np.float32(np.tile(scaled, reps)).tobytes()

        def _feed(ring_pos, data):
            # One self-framing positional buffer write: [u16 len][u16 pos] +
            # data. The explicit length lets the firmware separate messages
            # that land in one RX frame (a late parser used to merge them,
            # splicing header bytes into the waveform — the 500 Hz railing),
            # and the explicit position means a lost message can never shift
            # the ring alignment. Requires firmware with CMD_BUFFER_WRITE_AT
            # (July 2026); older firmware ignores it and the stream times out.
            # Spacing to the next message is the caller's responsibility
            # (see min_feed_interval_s above).
            header = np.uint16([len(data), ring_pos]).tobytes()
            self._write_cmd(Commands._WRITE_BUFFER_AT, header + bytes(data), False)
            self.serial.flush()

        safe_delay = max(1.0, float(delay))
        if block_points is None:
            block_points = int(round((500.0 / safe_delay) / v_per_chunk)) * v_per_chunk
        block_points = max(v_per_chunk, (int(block_points) // v_per_chunk) * v_per_chunk)

        if self._auto_gain:
            rx_len = 9 #1 float32 current(A) + 1 float32 potential(V) + 1 uint8 resistor index
        else:
            rx_len = 8 #1 float32 current(A) + 1 float32 potential(V)

        with self._port_lock:
            try:
                # Same pre-fill margin policy as write_voltage_batch: ~16 s of
                # points, capped well inside the device's 24 kB ring buffer,
                # rounded to whole feed messages so every producer position
                # stays feed-aligned (the firmware's ring wraps at exactly
                # 24 kB, a multiple of the feed size — misaligned writes would
                # desync producer and consumer at the wrap). The steady-state
                # loop below writes exactly as many bytes as the device
                # consumes, so this lead is preserved for the whole stream and
                # the producer can never lap the consumer.
                pre_fill_chunks = max(20, min(int(np.ceil((16000.0 / safe_delay) / v_per_chunk)), 700))
                first_tx_bytes = pre_fill_chunks * tx_len
                first_tx_bytes -= first_tx_bytes % feed_len
                tile_reps = int(np.ceil(first_tx_bytes / len(tile_bytes)))
                prefill = (tile_bytes * max(1, tile_reps))[:first_tx_bytes]
                tx_cursor = first_tx_bytes % len(tile_bytes)

                self._reset_buffer(0)
                for i in range(0, first_tx_bytes, feed_len):
                    _feed(i, prefill[i:i + feed_len])
                    # During pre-fill the batch is not running yet, so the
                    # firmware's parser has no transmit work to stall on and
                    # a short framing gap between messages is sufficient.
                    time.sleep(0.008)
                # Where the next feed lands in the device's ring buffer. The
                # host names every write position (see _feed), so this must
                # track the firmware's CMD_RX_BUFFER_SIZE ring exactly.
                ring_pos = first_tx_bytes % self._rx_buffer_size
                time.sleep(0.01) #wait for pstat to receive the message

                # The step count only bounds the stream's lifetime (~198 days
                # at 4 ms/step); the real terminator is halt_batch_stream.
                self._write_cmd(Commands.EXECUTE_VOLTAGE_BATCH,
                                np.uint32([delay, 0xFFFFFFFF]), True, retry=False)

                block = np.zeros((block_points, 2), np.float32)
                block_res = np.zeros(block_points, np.float32)
                fill = 0
                owed_bytes = 0
                last_feed_t = time.monotonic()
                while True:
                    data = self._read_from_buffer(rx_len * v_per_chunk)
                    # Each read means the device consumed 6 more points; owe
                    # it that many bytes back. Feeding is deferred while the
                    # minimum message interval hasn't elapsed (see above), so
                    # a read backlog being drained in a burst can never put
                    # two messages inside one firmware parser stall; the owed
                    # balance catches back up afterwards, one message per
                    # interval, well faster than the device consumes.
                    owed_bytes += tx_len
                    if owed_bytes >= feed_len and \
                            (time.monotonic() - last_feed_t) >= min_feed_interval_s:
                        _feed(ring_pos, tile_bytes[tx_cursor:tx_cursor + feed_len])
                        ring_pos = (ring_pos + feed_len) % self._rx_buffer_size
                        tx_cursor = (tx_cursor + feed_len) % len(tile_bytes)
                        owed_bytes -= feed_len
                        last_feed_t = time.monotonic()

                    temp_ind = 0
                    for j in range(v_per_chunk):
                        adc_vals = np.frombuffer(data, np.float32, 2, temp_ind)
                        block[fill + j, :] = adc_vals
                        temp_ind += 8

                        # Frame-shift check, as in write_voltage_batch: raw
                        # floats parsed off-register are usually huge numbers.
                        if np.any(np.abs(adc_vals) > 15.0):
                            logger.warning("[PS %s] Frame shift detected! Invalid potential/current raw: %s",
                                           self.device_ID, adc_vals)
                            block[fill + j, :] = np.nan

                        if self._auto_gain:
                            temp_gain = np.frombuffer(data, np.uint8, 1, temp_ind)[0]
                            temp_ind += 1
                            if temp_gain in self.res_vals:
                                block_res[fill + j] = self.res_vals[temp_gain]
                            else:
                                logger.warning("[PS %s] Frame shift detected! Invalid gain index: %s",
                                               self.device_ID, temp_gain)
                                block_res[fill + j] = np.nan
                                block[fill + j, :] = np.nan
                    fill += v_per_chunk
                    if fill < block_points:
                        continue

                    out = self._scale_output_voltages(block)
                    if self._auto_gain:
                        out[:, 0] = np.divide(out[:, 0], block_res)
                        self.Resistor = temp_gain
                        self.ResistorVal = block_res[-1]
                    else:
                        out[:, 0] /= self.ResistorVal
                    out[:, 0] *= -1
                    fill = 0
                    if not on_block(out):
                        return
            finally:
                # Terminate the endless batch however this method exits; an
                # un-halted stream would garble every later command's ack.
                try:
                    self.halt_batch_stream()
                except Exception:
                    logger.warning("[PS %s] Could not halt stream on exit",
                                   self.device_ID, exc_info=True)

    def write_voltage_batch(self, voltages: List[float], delay: np.uint32,
                            before_start=None, auto_range=False, range_period=1,
                            lead_in=0, ir=None):
        """Write a list of set voltage values to pstat's buffer. Then this buffer is executed.
        Each set V (WE-RE) is applied for ``delay`` ms and the CE-WE current (A) is read along with
        the RE-WE potential: at the end of each step on firmware that can (September 2026+), else
        right after the setpoint is written. If auto-gain mode is active, it will automatically
        return the gain corrected current values.

        Holds the port lock for the whole batch: the stream protocol cannot
        survive another thread's command being interleaved mid-batch.

        Raises WaveformTrackingError, after stopping the batch and
        disconnecting the cell, if the measured potential stops following the
        programmed waveform or the firmware rejects a corrupted waveform point.

        A completed run leaves its sampling mode, step period and the
        firmware's health counters in ``last_batch_report``.

        Args:
            voltages (float): list of voltages to set
            delay (np.uint32): delay between setting voltages (and reading the current and potential)
            before_start (callable, optional): called once the waveform is
                loaded, just before it starts (e.g. to connect the cell).
            auto_range (bool): let the firmware change the current range
                during the run (see ``set_current_range('auto')``).
            range_period (int): with ``auto_range``, only change range at
                points that are a multiple of this.
            lead_in (int): leading points to run but leave out of the result
                (see ``_auto_range_lead_in``).
            ir (dict, optional): ``ohm`` and ``max_correction_V``: the board
                adds that resistance times the current onto each setpoint
                (``configure_ir_compensation``; firmware 1.8+, else
                RuntimeError before anything runs).

        Returns:
            list: 2D array with rows as many as voltages | column 0: CE-WE current (A) | column 1: RE-WE V.
            The range each row was read on is left in ``last_batch_ranges``.
        """
        with self._port_lock:
            return self._write_voltage_batch_unlocked(voltages, delay, before_start,
                                                      auto_range, range_period, lead_in,
                                                      ir)

    def _write_voltage_batch_unlocked(self, voltages: List[float], delay: np.uint32,
                                      before_start=None, auto_range=False, range_period=1,
                                      lead_in=0, ir=None):
        v_per_chunk = 6 #number of potentials (V) written to pstat each time
        bytes_per_v = 4 #number of bytes used for each V
        tx_len = v_per_chunk * bytes_per_v #number of bytes to write at a time
        new_voltages = self._scale_input_voltages(voltages) #scale potentials for pstat
        v_count = int(len(new_voltages)) #number of data points (# of target V's)

        #ensure multiple of v_per_chunk per data point
        remainder = (v_count % v_per_chunk)
        pad_count = 0
        if remainder > 0:
            pad_count = v_per_chunk - remainder
            for i in range(pad_count):
                new_voltages = np.append(new_voltages,new_voltages[-1]) #pad by the last potential
            v_count += pad_count


        v_bytes = np.float32(new_voltages).tobytes() #entire array of target Vs in bytes
        v_byte_count = len(v_bytes)

        # Pre-fill: how much waveform the device holds before the stream starts.
        # The device consumes points on its own clock regardless of how fast this
        # loop supplies them, so the pre-fill is the safety margin against a slow
        # or briefly-stalled host. Target ~16 s of points, capped at 700 chunks
        # (16.8 kB) to stay within the device's 24 kB buffer. The old fixed value
        # of 4 chunks (~100 ms of margin) let any GUI hiccup starve the device.
        safe_delay = max(1.0, float(delay))
        pre_fill_chunks = max(20, min(int(np.ceil((16000.0 / safe_delay) / v_per_chunk)), 700))
        first_tx_bytes = min(pre_fill_chunks * tx_len, v_byte_count)
        first_tx_bytes -= first_tx_bytes % tx_len #keep chunk-aligned
        tx_ind = 0 #index to iterate on v_bytes

        report = self._begin_batch_report(delay, auto_range, range_period, ir)
        ring_pos = self._start_waveform_upload(v_bytes[0:first_tx_bytes]) #clear the ring, write the pre-fill
        time.sleep(0.01) #wait for pstat to receive the message (1ms delay is end of msg for USB)
        tx_ind = first_tx_bytes #move the ind accordingly

        if self._auto_gain:
            rx_len = 9 #1 float32 current(A) + 1 float32 potential(V) + 1 uint8 resistor index (per target V)
        else:
            rx_len = 8 #1 float32 current(A) + 1 float32 potential(V) (per target V)

        temp_adc = np.zeros((v_count,2),np.float32) #array to hold ADC readings
        # The range each point was read on: reported per point when readings
        # carry it, else the range set before the run
        temp_range = np.full(v_count, self._range_index(), dtype=int)
        tracker = WaveformTracker(voltages)

        if before_start is not None:
            before_start()
        self._execute_batch(delay, v_count, report) #start the protocol
        with self._batch_read_timeout(delay):
            for i in range(0,v_count,v_per_chunk): #for each target V
                data = self._read_batch_chunk(rx_len * v_per_chunk) #read
                report['chunk_times_s'].append(time.monotonic())
                if (tx_ind + tx_len) <= v_byte_count: #write if the target V list is not exhausted
                    ring_pos = self._write_waveform(v_bytes[tx_ind:tx_ind+tx_len], ring_pos)
                    tx_ind += tx_len

                temp_ind = 0 #temp index in data
                for j in range(v_per_chunk):
                    # Read raw ADC values (Current, Potential)
                    adc_vals = np.frombuffer(data, np.float32, 2, temp_ind)
                    temp_adc[i+j,:] = adc_vals
                    temp_ind += 8

                    # Check for Frame Shift via Potential Range (Approx +/- 15V limit)
                    # Raw float interpretation of wrong bytes often yields huge numbers
                    if np.any(np.abs(adc_vals) > 15.0):
                         logger.warning("[PS %s] Frame shift detected! Invalid potential/current raw: %s",
                                        self.device_ID, adc_vals)
                         temp_adc[i+j,:] = np.nan

                    if self._auto_gain:
                        temp_gain = int(np.frombuffer(data, np.uint8, 1, temp_ind)[0])
                        temp_ind += 1

                        # Validate Resistor Index
                        if temp_gain in self.res_vals:
                             temp_range[i+j] = temp_gain
                        else:
                             logger.warning("[PS %s] Frame shift detected! Invalid gain index: %s",
                                            self.device_ID, temp_gain)
                             temp_range[i+j] = 0
                             temp_adc[i+j,:] = np.nan # Invalidate data too

                fault = tracker.update(
                    i, self._scale_output_voltages(temp_adc[i:i+v_per_chunk, 1]))
                if fault:
                    self._abort_waveform_run(fault)

        return self._finish_batch_currents(temp_adc, temp_range, report,
                                           v_count - pad_count, v_per_chunk, lead_in)

    def _finish_batch_currents(self, raw, ranges, report, n_points, points_per_chunk,
                               lead_in=0):
        """A finished batch's readings as raw currents (A, column 0) and
        potentials (V, column 1), lead-in and padding dropped; closes its run
        report."""
        out = self._scale_output_voltages(raw[lead_in:n_points])
        amp_V = out[:, 0].copy()
        ranges = np.asarray(ranges[lead_in:n_points], dtype=int)
        out[:, 0] = -amp_V / self._res_table()[ranges]
        report['lead_in'] = lead_in
        self._finish_batch_report(report, n_points - lead_in, points_per_chunk,
                                  ranges=ranges, amp_V=amp_V)
        if len(ranges):
            # The board stays on the range the run ended on
            self.Resistor = int(ranges[-1])
            self.ResistorVal = self.res_vals[self.Resistor]
        return out

    @staticmethod
    def _auto_range_lead_in(range_period=1):
        """Points to hold at the first potential before an auto-ranged run's
        saved data: enough chances to change range to climb from the least
        sensitive range to the most sensitive, as a whole number of periods so
        a DPV's pulse pairs stay aligned with its range changes."""
        period = max(1, int(range_period))
        per_climb = period * int(np.ceil(AUTO_RANGE_UP_POINTS / period))
        return per_climb * (len(RANGE_OHMS) - 1)

    def write_dac(self, channels, voltages):
        """Write Digital to Analog Converter values to potentiostat.
        Consult "DAC" class for channel information.
        Channels and voltages must refer to same index.
        i.e., channel[1]'s target value should be given in voltages[1]

        Args:
            channels (list): list of channel(s)
            voltages (list): list of voltages(s)

        """
        new_voltages = self._scale_input_voltages(voltages)
        data = bytearray()
        data.extend(np.uint8(channels).tobytes())
        data.extend(np.float32(new_voltages).tobytes())
        self._write_cmd(Commands.WRITE_DAC,data)

    def write_switch(self, state:bool):
        """Change the switch state between CE and WE. True for connected, False for disconnected.

        Args:
            state (bool): Whether the switch is on

        """
        self._write_cmd(Commands.WRITE_SWITCH,np.uint8(state))
        self._switch_state = state

    def read_switch(self)-> bool:
        """Returns the switch state between CE and WE. True for connected, False for disconnected.

        Returns:
            bool: Whether the switch is on
        """
        with self._port_lock:
            self._write_cmd(Commands.READ_SWITCH,None,False)
            result = self._read_with_retry(1, Commands.READ_SWITCH, None)
        result = np.frombuffer(result,np.uint8)
        if result == 0:
            self._switch_state = False
        else:
            self._switch_state = True
        return result
    
    def read_diagnostics(self) -> dict:
        """Read the firmware's health report (requires firmware with the
        diagnostics query, July 2026 or later).

        Returns a dict:
            reset_flags: raw reset-cause register captured when the device
                last booted (each bit is one possible cause).
            watchdog_reset: True if the last reboot was forced by the
                device's watchdog — i.e. the firmware froze mid-run.
            tx_dropped: data messages the device discarded because this
                computer stopped reading in time.
            rx_overflow: corrupted/oversized command frames discarded.
            starved_steps: sampling periods the device spent holding its
                potential while waiting for waveform data from this computer.
            rejected_setpoints: corrupted waveform points that made the device
                stop the batch and disconnect the cell (firmware from
                September 2026 onward; absent from older firmware's report).
            late_steps, max_late_ms: batch steps that ran at least 1 ms after
                they were due, and the worst lateness (ms).
            resyncs: times the step schedule was re-anchored after a stall.
            starve_aborts: batches the device ended, disconnecting the cell,
                because waveform data stopped arriving.
            features: capability flags (FW_FEATURE_*).
            The last five come from firmware of late September 2026 onward.

        Every counter except reset_flags, tx_dropped and rx_overflow describes
        the most recent waveform upload. After a clean run everything except
        a normal power-up flag is zero. Non-zero counters mean the run was
        degraded and should be repeated.
        """
        extra_len = 4 * (len(DIAGNOSTIC_FIELDS) - 4)
        with self._port_lock:
            self._write_cmd(Commands.READ_DIAGNOSTICS, None, False)
            result = self._read_with_retry(16, Commands.READ_DIAGNOSTICS, None)
            # Newer firmware appends more counters than the original four; a
            # short read of the rest is how an older one shows where it stops
            old_timeout = self.serial.timeout
            try:
                self.serial.timeout = 0.2
                extra = self.serial.read(extra_len)
            finally:
                self.serial.timeout = old_timeout
        raw = result + extra[:len(extra) - len(extra) % 4]
        vals = np.frombuffer(raw, np.uint32)
        report = {name: int(v) for name, v in zip(DIAGNOSTIC_FIELDS, vals)}
        report['watchdog_reset'] = bool(report['reset_flags'] & (1 << 29))  # IWDG flag bit
        return report

    def _dac_V_per_cell_V(self):
        """How far the DAC moves per volt of calibrated cell potential, along
        the same path a batched waveform takes to the board."""
        E = np.array([0.0, 0.1])
        dac = self._scale_input_voltages(self._waveform_to_raw_V(E))
        return float((dac[1] - dac[0]) / (E[1] - E[0]))

    def _ir_factors(self, R_comp_ohm):
        """The on-board iR loop's factor for each current range: DAC volts to
        add per ADC volt the current channel reads away from its zero.

        Worked out along the driver's own conversions (ADC volts -> amps with
        the board's read calibration on that range; cell volts -> DAC volts
        with its set calibration), so the correction applied on the board is
        exactly ``R_comp_ohm`` times the current this driver records. Both
        conversions are straight lines, so two points each fix them; the
        zero is the board's own switch-open reading.
        """
        adc = np.array([1.0, 2.0])
        amps_per_adc_V = []
        for r in range(len(self.range_ohms)):
            raw_A = -self._scale_output_voltages(adc) / self._res_table()[r]
            I_A = self._calibrated_mA(raw_A, [r, r]) / 1000.0
            amps_per_adc_V.append((I_A[1] - I_A[0]) / (adc[1] - adc[0]))
        return (float(R_comp_ohm) * np.array(amps_per_adc_V)
                * self._dac_V_per_cell_V())

    def configure_ir_compensation(self, R_comp_ohm, step_ms, max_correction_V):
        """Set up the on-board iR loop (firmware 1.8+) for the next batch run
        with ``BATCH_FLAG_IR_COMP``: each setpoint gets ``R_comp_ohm`` times
        the previous step's end-of-step current added, never more than
        ``max_correction_V``, within the limits ``IR_MAX_STEP_V``,
        ``IR_RUNAWAY_MS`` and ``IR_LIMIT_MS``.

        Raises ``SerialAckError`` if the board refuses the configuration.
        """
        dac_per_V = abs(self._dac_V_per_cell_V())
        max_dac = min(max(float(max_correction_V), IR_MIN_HEADROOM_V) * dac_per_V, 3.3)
        step_dac = min(IR_MAX_STEP_V * dac_per_V, max_dac)
        step_ms = max(float(step_ms), 1.0)
        steps = [max(1, int(np.ceil(IR_RUNAWAY_MS / step_ms))),
                 max(1, int(np.ceil(IR_LIMIT_MS / step_ms)))]
        payload = (np.float32(self._ir_factors(R_comp_ohm)).tobytes()
                   + np.float32([max_dac, step_dac, IR_FEEDBACK_ALPHA]).tobytes()
                   + np.uint32(steps).tobytes())
        self._write_cmd(Commands.IR_CONFIG, payload)

    def read_ir_report(self):
        """The on-board iR loop's report for the current waveform upload
        (firmware 1.8+).

        Returns:
            dict: 'runs' (batches that compensated), 'limited_steps' (steps
                the correction was held at its cap or the output at the DAC's
                end), 'slewed_steps' (steps the correction trailed the
                readings at its walking limit), 'runaway_aborts' and
                'limit_aborts' (runs the board ended for each reason), and
                'max_correction_V' / 'correction_V' (the largest correction
                applied and the present one, in cell volts).
        """
        with self._port_lock:
            self._write_cmd(Commands.IR_REPORT, None, False)
            raw = self._read_with_retry(28, Commands.IR_REPORT, None)
        runs, limited, slewed, runaways, limits = (
            int(v) for v in np.frombuffer(raw, np.uint32, 5, 0))
        largest, now = (float(v) for v in np.frombuffer(raw, np.float32, 2, 20))
        dac_per_V = abs(self._dac_V_per_cell_V())
        return {'runs': runs, 'limited_steps': limited, 'slewed_steps': slewed,
                'runaway_aborts': runaways, 'limit_aborts': limits,
                'max_correction_V': largest / dac_per_V,
                'correction_V': now / dac_per_V}

    def write_gain(self, resistor: Resistors = Resistors.R_100):
        """Change gain by switching resistor between CE and WE

        Args:
            resistor (Resistors, optional): Target resistor (gain) index. Defaults to Resistors.R_100.
        """
        # Prevent redundant writes
        if hasattr(self, 'Resistor') and self.Resistor == resistor:
            return

        self._write_cmd(Commands.WRITE_GAIN,np.uint8(resistor))
        self.Resistor = resistor
        self.ResistorVal = self.res_vals[self.Resistor]
    
    def read_gain(self):
        """Returns currently active resistor index (gain) between CE and WE

        Returns:
            Resistor: Current resistor index
        """
        with self._port_lock:
            self._write_cmd(Commands.READ_GAIN,np.uint8(0),False)
            result = self._read_with_retry(1, Commands.READ_GAIN, np.uint8(0))
        result = np.frombuffer(result,np.uint8)
        gain_idx = int(result[0])
        if gain_idx not in self.res_vals:
            # Out-of-range index means the response stream is desynced; treat it
            # like a failed read so callers' timeout tolerance applies.
            self.serial.reset_input_buffer()
            raise SerialReadTimeout(f"Invalid gain index {gain_idx} in READ_GAIN response")
        self.Resistor = gain_idx
        self.ResistorVal = self.res_vals[self.Resistor]
        return self.Resistor
    
    
    # Per-board calibration, measured end-to-end by the onboarding sweep.
    # The set pair maps a requested current to the true delivered current;
    # the read pair maps the board's own current reading (nominal mA) to the
    # true current. Defaults reproduce the historical average-board behavior
    # so an uncalibrated device acts exactly as it did before onboarding
    # existed (write: divide by 1.0989; read: raw_A * 976.9 - 0.6).
    _calib_slope_correction = 1.0989
    _calib_intercept = 0.0
    _calib_read_slope = 0.9769
    _calib_read_intercept = -0.6

    # Zero error removed from the read intercept by the last tare_current()
    # (mA), kept so a repeated tare replaces it instead of stacking on it.
    _current_tare_mA = 0.0

    # A tare larger than this means the calibration or the board is badly
    # off, not a small drift; it is still applied, but logged as a warning.
    CURRENT_TARE_WARN_MA = 0.2

    # Nominal internal chain at the 100-ohm gain: while the firmware's hold
    # loop is converged, the board's own reading (mA) = 1.13 * command + 0.6.
    # Follows from the command model in _current_to_voltage (113 ohm, 0.06 V)
    # and the 100-ohm shunt; used only to derive a read fallback below.
    _NOMINAL_READ_GAIN = 1.13
    _NOMINAL_READ_OFFSET = 0.6

    # Per-board voltage calibration, measured by the onboarding voltage sweep.
    # Set pair maps a requested potential to the true applied potential; read
    # pair maps the board's own potential reading to the true value.
    #
    # Defaults are IDENTITY on purpose: the historical global correction
    # (x0.9919 +/- 0.0494 V) already lives inside _scale_input_voltages /
    # _scale_output_voltages, which every raw write and read passes through,
    # and the onboarding sweep measures its fits through those same raw
    # calls — so the stored pairs are corrections ON TOP of raw. The old
    # defaults of +0.0494 applied the historical offset a second time on
    # every *_calibrated call, shifting OCP reads and parked potentials by
    # ~49 mV each way on boards without a stored voltage calibration
    # (bench-diagnosed 2026-08-14: asymmetric R_u steps, +49 mV live-CV
    # axis).
    _calib_v_slope = 1.0
    _calib_v_intercept = 0.0
    _calib_v_read_slope = 1.0
    _calib_v_read_intercept = 0.0

    def set_voltage_calibration_params(self, slope, intercept, read_slope=None, read_intercept=None):
        """Set per-board voltage calibration from the onboarding registry.

        Unlike the current calibration, no read fallback can be derived from
        the set pair — the drive (DAC -> CE) and sense (RE -> ADC) chains are
        physically independent — so a missing read pair keeps the historical
        default until the board is onboarded with the voltage sweep.
        """
        self._calib_v_slope = slope
        self._calib_v_intercept = intercept
        if read_slope is not None and read_intercept is not None:
            self._calib_v_read_slope = read_slope
            self._calib_v_read_intercept = read_intercept
        else:
            logger.info("[PS %s] No voltage read calibration stored; keeping "
                        "the default. Re-onboard this board to measure it.",
                        self.device_ID)
        logger.info("[PS %s] Voltage calibration set: slope=%s, intercept=%s, "
                    "read_slope=%s, read_intercept=%s",
                    self.device_ID, self._calib_v_slope, self._calib_v_intercept,
                    self._calib_v_read_slope, self._calib_v_read_intercept)

    def set_calibration_params(self, slope, intercept, read_slope=None, read_intercept=None):
        """Set per-board calibration from the onboarding registry.

        ``slope``/``intercept`` are the full set-current correction (no global
        hardware factor is applied on top — the onboarding fit already spans
        the whole signal chain). When the read pair is missing (registry rows
        saved before the read fit existed), it is derived from the set pair
        through the nominal internal chain; re-onboarding replaces this
        approximation with measured values.
        """
        self._calib_slope_correction = slope
        self._calib_intercept = intercept
        if read_slope is None or read_intercept is None:
            self._calib_read_slope = slope / self._NOMINAL_READ_GAIN
            self._calib_read_intercept = (
                intercept - self._NOMINAL_READ_OFFSET * self._calib_read_slope
            )
            logger.info("[PS %s] No read calibration stored; derived from set "
                        "calibration. Re-onboard this board to measure it.",
                        self.device_ID)
        else:
            self._calib_read_slope = read_slope
            self._calib_read_intercept = read_intercept
        # A fresh intercept supersedes any tare folded into the old one
        self._current_tare_mA = 0.0
        logger.info("[PS %s] Calibration set: slope=%s, intercept=%s, "
                    "read_slope=%s, read_intercept=%s",
                    self.device_ID, self._calib_slope_correction,
                    self._calib_intercept, self._calib_read_slope,
                    self._calib_read_intercept)

    def write_current_hold(self, target_current_mA=0.1, initial_step_V = 0.00015, learning_rate = 0.05, force_gain = False, voltage_guess=None):
        # Bench-verified July 2026: boards deliver at least 30 mA, so the legacy
        # 28 mA ceiling was raised to match.
        MAX_CURRENT_MA = 30.0
        if abs(target_current_mA) > MAX_CURRENT_MA:
            logger.warning("[PS %s] Target %.2f mA exceeds hardware compliance limit (%s mA). Clamping.",
                           self.device_ID, target_current_mA, MAX_CURRENT_MA)
            target_current_mA = np.sign(target_current_mA) * MAX_CURRENT_MA

        target_A = target_current_mA * 1e-3 
        if force_gain == False:
            self.write_gain(self._select_100ohm_resistor(target_A)) #guess the best resistor for highest gain without saturation
        
        # Use provided voltage guess if available, otherwise calculate from static model
        if voltage_guess is not None:
             dac_val = self._scale_input_voltages(voltage_guess)
        else:
             dac_val = self._current_to_voltage(target_A)
             
        #ADD "LEARNING RATE" AND REPEAT COUNT PARAMS
        msg = bytearray()
        msg.extend(np.float32([dac_val,initial_step_V,learning_rate]).tobytes())
        self._write_cmd(Commands.WRITE_CURRENT_HOLD,msg)

    def write_current_hold_calibrated(self, target_current_mA: np.float32, **kwargs):
        """Hold ``target_current_mA`` corrected by the board's set calibration.

        The stored slope/intercept span the entire signal chain (measured
        against a multimeter during onboarding), so they are the only
        correction applied — no additional global hardware factor.
        """
        slope = self._calib_slope_correction
        # Avoid div by zero on a malformed calibration row
        if abs(slope) < 1e-6:
            slope = 1.0

        current = (target_current_mA - self._calib_intercept) / slope
        self.write_current_hold(current, force_gain=False, **kwargs)

    def set_alternating_current(self, target_current_mA: float, voltage_guess: float = None):
        """
        Sets the current for an AC waveform step. 
        This is a wrapper around write_current_hold_calibrated to make the intent clear.
        """
        self.write_current_hold_calibrated(target_current_mA, voltage_guess=voltage_guess)

    def write_sample_count(self, sample_count=10):
        """Sets the number of samples to be averaged per channel

        Args:
            sample_count (int, optional): Number of samples to be averaged per channel. Defaults to 10.
        """
        self._write_cmd(Commands.WRITE_SAMPLE_COUNT,np.uint32(sample_count))

    def write_current_hold_stop(self):
        """Stop current hold tasl
        """
        self._write_cmd(Commands.STOP_CURRENT_HOLD,None)

    def read_DAC(self,channels):
        """Read Digital to Analog Converter values from potentiostat.
        Consult "DAC" class for channel information

        Args:
            channels (list): list of channel(s)

        Returns:
            list: List of float32 DAC values from the requested channels
        """
        channels = np.ones(1,np.uint8) * channels
        with self._port_lock:
            self._write_cmd(Commands.READ_DAC,np.uint8(channels),False)
            result = self._read_with_retry(
                np.uint8(channels).size * 4, Commands.READ_DAC, np.uint8(channels)
            ) #channel count x float32
        result = np.frombuffer(result,np.float32).copy()
        result = self._scale_output_voltages(result)
        return result
    
    def read_ADC(self,channels):
        """Read Analog to Digital Converter values from potentiostat.
        Consult "ADC" class for channel information

        Args:
            channels (list): list of channel(s)

        Returns:
            list: List of float32 ADC values from the requested channels
        """
        #read ADC values
        channels = np.ones(1,np.uint8) * channels
        with self._port_lock:
            self._write_cmd(Commands.READ_ADC,np.uint8(channels),False)
            result = self._read_with_retry(
                np.uint8(channels).size * 4, Commands.READ_ADC, np.uint8(channels)
            ) #channel count x float32
        result = np.frombuffer(result,np.float32).copy()
        for i in range(len(channels)):
            if channels[i] == ADC.WE_OUT: #V-I conversion channel, process by resistor value
                result[i] = self._scale_output_voltages(result[i])
                result[i] /= (-1 * self.res_vals[self.Resistor])
            elif (channels[i] == ADC.TEMP) or (channels[i] == ADC.HUMID): #Skip temperature and humidiy
               pass 
            else:  #normal ADC, just scale the range
                result[i] = self._scale_output_voltages(result[i])
        return result
    
    def read_ADC_gain(self,channels):
        """Read Analog to Digital Converter values from potentiostat
        Additionally request for gain information (resistor index).
        Consult "ADC" class for channel information.
        This function automatically updates the resistor value of the class.
        Also processes the current reading accordingly.
        
        Args:
            channels (list): list of channel(s)

        Returns:
            list: List of float32 ADC values from the requested channels
        """
        channels = np.ones(1,np.uint8) * channels
        with self._port_lock:
            self._write_cmd(Commands.READ_ANALOG_GAIN,np.uint8(channels),False)
            result = self._read_with_retry(
                (np.uint8(channels).size * 4) + 1,
                Commands.READ_ANALOG_GAIN,
                np.uint8(channels),
            )
        ch_result = result[0:(len(channels) * 4)]
        ch_result = np.frombuffer(ch_result,np.float32).copy()
        gain_result = result[(len(channels) * 4):]
        gain_result = np.frombuffer(gain_result,np.uint8).copy()
        gain_idx = int(gain_result[0])
        if gain_idx not in self.res_vals:
            # An impossible gain index means the response frame is shifted —
            # raise as a read failure (used to be a KeyError that killed the
            # run) so the measurement loops' timeout tolerance applies.
            self.serial.reset_input_buffer()
            raise SerialReadTimeout(
                f"Invalid gain index {gain_idx} in READ_ANALOG_GAIN response"
            )
        self.Resistor = gain_idx
        self.ResistorVal = self.res_vals[self.Resistor]
        for i in range(len(channels)):
            if channels[i] == ADC.WE_OUT:
                ch_result[i] = self._scale_output_voltages(ch_result[i])
                ch_result[i] /= (-1 * self.res_vals[self.Resistor])
            elif (channels[i] == ADC.TEMP) or (channels[i] == ADC.HUMID):
               pass 
            else:
                ch_result[i] = self._scale_output_voltages(ch_result[i])
        return ch_result
    
    def read_auto_gain(self) -> bool:
        """_summary_

        Returns:
            bool: Whether the auto-gain is turned on.
        """
        with self._port_lock:
            self._write_cmd(Commands.READ_AUTO_GAIN,np.uint8(0),False)
            result = self._read_with_retry(1, Commands.READ_AUTO_GAIN, np.uint8(0)) #single byte bool
        result = np.frombuffer(result,np.uint8) #cast to uint8
        if (result == 0):
            self._auto_gain = False
        else:
            self._auto_gain = True
        return self._auto_gain
    
    def write_auto_gain(self,state:bool=False) -> bool:
        """_summary_

        Args:
            state (bool, optional): Whether the auto-gain is turned on(True). Defaults to False(off).

        Returns:
            bool: _description_
        """
        self._write_cmd(Commands.WRITE_AUTO_GAIN,np.uint8(state)) #single byte state bool
        self._auto_gain = state
        return self._auto_gain

    def read_current(self):
        """Read A current (I) between WE and CE

        Returns:
            np.float32: Current (I) between WE and CE (A)
        """
        return self.read_ADC_gain(channels=[ADC.WE_OUT])
    
    def read_current_calibrated(self):
        """Return the current in mA, corrected by the board's read calibration."""
        raw_A = self.read_current()
        return self._calibrated_mA(raw_A, self._range_index())

    def tare_current(self, n_readings=10):
        """Re-zero the current reading against the open circuit, like taring
        a balance.

        With the switch open no current can flow, so whatever the calibrated
        reading shows is the chain's own zero error at the present gain. The
        onboarding intercept is extrapolated from 0.1-20 mA points, and a
        residual of a few tens of uA that is invisible there becomes a
        baseline offset on uA-scale CVs — and, in iR runs, a phantom I*R_u
        shift of the potential (bench 2026-09-21: +42 uA on board P, ~20 mV).
        The median of ``n_readings`` is folded into the read intercept, so
        every current path is corrected alike: single reads, the live iR
        loop, batch streams, and step captures.

        The zero error is a fixed voltage at the amplifier, so a tare taken
        on one current range corrects every range; take it on the run's own
        range anyway, which also removes that range's small leakage offset.
        A repeated tare replaces the previous one, and
        ``set_calibration_params`` clears it.

        Args:
            n_readings (int): Readings to take the median of. Default 10.

        Returns:
            float: The zero error removed, in mA.
        """
        if self._switch_state:
            raise RuntimeError("tare_current needs the cell disconnected — "
                               "open the switch first.")
        # Undo the previous tare, so this one measures the stored zero afresh
        self._calib_read_intercept += self._current_tare_mA
        self._current_tare_mA = 0.0

        zero_mA = float(np.median([float(self.read_current_calibrated()[0])
                                   for _ in range(n_readings)]))
        # The intercept is in range-0 terms (see _calibrated_mA)
        index = self._range_index()
        zero_range0_mA = zero_mA * self.range_ohms[index] / self.range_ohms[0]
        self._calib_read_intercept -= zero_range0_mA
        self._current_tare_mA = zero_range0_mA

        if abs(zero_range0_mA) > self.CURRENT_TARE_WARN_MA:
            logger.warning("[PS %s] Current tare removed %.1f uA — far more "
                           "than drift; check this board's calibration.",
                           self.device_ID, zero_mA * 1000.0)
        else:
            logger.info("[PS %s] Current tare removed %.1f uA.",
                        self.device_ID, zero_mA * 1000.0)
        return zero_mA

    def calibrate_batch_current_uA(self, raw_A, ranges=None):
        """Convert raw batch-stream current (A) to uA with the board's read
        calibration — the same correction as ``read_current_calibrated``, for
        the array data returned by the batch/streaming protocol.

        ``ranges`` gives the current range of each point (``last_batch_ranges``
        after an auto-ranged run); by default, the range currently set."""
        if ranges is None:
            ranges = self._range_index()
        return self._calibrated_mA(raw_A, ranges) * 1000.0

    def calibrate_batch_potential_V(self, raw_V):
        """Apply the board's voltage READ calibration to batch-stream
        potentials — the same correction as ``read_potential_calibrated``,
        for the array data returned by the batch/streaming protocol. Identity
        for boards without a stored voltage calibration."""
        return (np.asarray(raw_V, dtype=float) * self._calib_v_read_slope
                + self._calib_v_read_intercept)

    def _waveform_to_raw_V(self, voltages):
        """Apply the board's voltage SET calibration to a target waveform, so
        a batched sweep drives the same true potentials the calibrated
        single-point writes do. Identity for boards without a stored voltage
        calibration."""
        slope = self._calib_v_slope
        # Avoid div by zero on a malformed calibration row
        if abs(slope) < 1e-6:
            slope = 1.0
        return (np.asarray(voltages, dtype=float) - self._calib_v_intercept) / slope

    def read_potential(self):
        """Read potential (V) between WE and RE

        Returns:
            np.float32: Potential (V) between WE and RE
        """
        return self.read_ADC_gain(channels=[ADC.RE_OUT])
    
    def read_potential_calibrated(self):
        """Return the potential in V, corrected by the board's read calibration."""
        return self.read_potential() * self._calib_v_read_slope + self._calib_v_read_intercept
        
    def read_potential_current(self):
        """Read potential (V) between WE-RE and current Ampere (I) between WE-CE

        Returns:
            list: Potential (V) between WE-RE and current (I) between WE-CE (A)
        """
        return self.read_ADC_gain(channels=[ADC.RE_OUT,ADC.WE_OUT])

    def read_potential_current_calibrated(self):
        """
        Read calibrated potential (V) and current (mA).
        Returns: [Potential_V, Current_mA]
        """
        raw_vals = self.read_potential_current()
        raw_v = raw_vals[0]
        raw_i = raw_vals[1]

        # Apply calibrations matches read_potential_calibrated and read_current_calibrated
        cal_v = raw_v * self._calib_v_read_slope + self._calib_v_read_intercept
        cal_i = float(self._calibrated_mA(raw_i, self._range_index()))

        return [cal_v, cal_i]
    
    def write_potential(self,potential_V:np.float32):
        """Read potential (V) between WE-RE in Volts

        Args:
            potential_V (np.float32): potential (V) between WE-RE in Volts
        """
        self.write_dac(DAC.CE_IN,potential_V)

    def write_potential_calibrated(self, potential_V:np.float32):
        """Apply ``potential_V`` corrected by the board's set calibration."""
        slope = self._calib_v_slope
        # Avoid div by zero on a malformed calibration row
        if abs(slope) < 1e-6:
            slope = 1.0
        self.write_potential((potential_V - self._calib_v_intercept) / slope)

    def _safe_open_switch(self):
        """Open the CE-WE switch without a voltage transient.

        Opening the switch while the setpoint disagrees with the cell potential
        causes an output spike, and any running current-hold keeps adjusting
        the DAC after the disconnect until it hits its limit (bench-verified
        July 2026). To prevent both: stop the hold, set the DAC to the
        currently sensed potential so almost no current is flowing, then open.
        """
        with self._port_lock:
            try:
                self.write_current_hold_stop()
                sensed_V = float(self.read_potential_calibrated()[0])
                self.write_potential_calibrated(sensed_V)
                time.sleep(0.05) #let residual current decay before disconnecting
            except Exception:
                # Fail-state: cut the output even if matching the DAC failed
                self.write_switch(SwitchState.Off)
                raise
            self.write_switch(SwitchState.Off)

    def read_ocp(self,restore_switch_state = True)->np.float32:
        """Read Open Circuit Potential in Volts. Disconnects CE-WE connection and reads potential.

        Args:
            restore_switch_state (bool, optional): Whether to restore previous switch state. Defaults to True.

        Returns:
            np.float32: OCP in Volts
        """
        prev_switch_state = self._switch_state
        self._safe_open_switch()
        if self._auto_gain:
            for i in range(Resistors.R_1M):
                self.read_potential_calibrated() #Read current multiple times to allow auto-gain
        result = self.read_potential_calibrated()
        if not (prev_switch_state == SwitchState.Off) and restore_switch_state:
            self.write_switch(SwitchState.On) #restore previous switch state
        return result[0]

    def _build_cv_waveform(self, min_V, max_V, cycles, step_V, start_V, last_V,
                           scan_direction='Negative'):
        """The target potentials for a CV sweep, shared by perform_CV and
        perform_CV_iR so both drive an identical sweep shape (see
        ``build_cv_waveform``)."""
        return build_cv_waveform(min_V, max_V, cycles, step_V, start_V, last_V,
                                 scan_direction)

    def perform_CV(self, min_V, max_V, cycles, mV_s, step_hz, start_V=None, last_V=None,
                   scan_direction='Negative', quiet_s=DEFAULT_QUIET_S,
                   current_range=None, ir_compensation_ohm=0.0):
        """Performs CV with given parameters.

        The device steps a whole number of milliseconds per point, so
        ``step_hz`` is rounded to the nearest rate it can run (1000/delay)
        and the potential step is sized for that real rate: the sweep runs at
        ``mV_s`` whatever ``step_hz`` is. The cell is connected once the
        waveform is loaded and held at the first potential for ``quiet_s``
        before the sweep starts. ``current_range`` is ``'auto'`` or a range
        index (see ``set_current_range``); None keeps the range already set.

        ``ir_compensation_ohm`` above zero has the board add that resistance
        times the current back onto each setpoint as the sweep runs (live iR
        compensation, firmware 1.8+; see ``configure_ir_compensation``), as
        far as the output has room beyond the sweep's largest potential. The
        measured potential column then includes the applied correction.

        Returns:
            np.ndarray: columns time (s) of each sample from the sweep's
                start, measured potential (V), current (uA), cycle number,
                experiment number, target potential (V). How the run went
                (sampling, step period, device health) is left in
                ``last_batch_report``.
        """

        #Stop any running current-hold before the OCP read below opens the
        #switch, otherwise the firmware hold loop drives the DAC to its limit
        self.write_current_hold_stop()

        #if start_V and/or last_V is None, start/end with OCP
        if (start_V is None) or (last_V is None):
            ocp = self.read_ocp()
            if start_V is None:
                start_V = ocp
            if last_V is None:
                last_V = ocp

        # Park the DAC at the sweep's first potential while the cell is still
        # disconnected, so the switch closes onto the right setpoint rather
        # than a stale one (bench-observed as ~2 s at an uncontrolled voltage).
        # Uses the same set calibration as the batch points below, so the
        # handoff to the waveform's first point is seamless.
        self.write_potential_calibrated(start_V)

        delay_ms = max(1, int(np.round(1000 / step_hz)))
        step_V = (mV_s / 1000) / (1000.0 / delay_ms)

        cv = self._build_cv_waveform(min_V, max_V, cycles, step_V,
                                     start_V, last_V, scan_direction)

        mode = self.set_current_range(current_range) if current_range is not None else None
        lead_in = self._auto_range_lead_in() if mode == 'auto' else 0
        ir = None
        if ir_compensation_ohm > 0:
            ir = {'ohm': float(ir_compensation_ohm),
                  'max_correction_V': HW_VOLTAGE_COMPLIANCE_V - float(np.max(np.abs(cv)))}
        rtn = self.write_voltage_batch(
            voltages=self._waveform_to_raw_V(np.concatenate((np.full(lead_in, cv[0]), cv))),
            delay=delay_ms,
            before_start=lambda: self._connect_cell_for_batch(quiet_s),
            auto_range=(mode == 'auto'), lead_in=lead_in, ir=ir)
        num_points = rtn.shape[0]
        time_stamps = self._batch_time_axis(num_points)
        cycle_nums = cv_cycle_numbers(cv, min_V, max_V, cycles)
        return np.column_stack((time_stamps,
                                self.calibrate_batch_potential_V(rtn[:, 1]),
                                self.calibrate_batch_current_uA(rtn[:, 0],
                                                                self.last_batch_ranges),
                                cycle_nums, np.zeros(num_points), cv))

    # An exponential fit whose time constant is shorter than this many sample
    # intervals cannot be extrapolated back to t=0 with any confidence — the
    # decay was essentially over before the second reading.
    _RU_MIN_TAU_INTERVALS = 2.0

    # Below this relative decay across the trace, the current is treated as
    # flat: purely resistive behaviour (e.g. a dummy cell) rather than an
    # RC transient.
    _RU_FLAT_DECAY_FRACTION = 0.15

    @staticmethod
    def _fit_ru_decay(times_ms, currents_A, step_V):
        """Estimate R_u from one post-step current transient.

        Fits I(t) = I0*exp(-t/tau) + C and evaluates the fit at t=0 — the
        instant the switch closed, when the double-layer had not yet charged
        and the cell was purely resistive — so R_u = step / I(0). This
        replaces taking the largest raw sample, which by the time the serial
        link delivers it is already well down the decay.

        A trace with no visible decay is treated as purely resistive and
        extrapolated with a straight line (a dummy-cell resistor really
        behaves this way; for a live cell it means the transient was too fast
        or too slow to resolve, which the message flags). A fitted time
        constant shorter than a couple of sample intervals means the
        extrapolation is unconstrained, so the measurement is refused rather
        than returning a number that cannot be trusted.

        Returns:
            dict: 'R_u_ohm', 'tau_ms' (None when no decay was fit), 'ok',
                and 'message' (empty when there is nothing to flag).
        """
        times_ms = np.asarray(times_ms, dtype=float)
        currents_A = np.asarray(currents_A, dtype=float)
        n_tail = max(3, len(currents_A) // 6)

        if np.max(np.abs(currents_A)) < 1e-9:
            return {'R_u_ohm': float('inf'), 'tau_ms': None, 'ok': False,
                    'I_at_0_A': None,
                    'message': ("No current flowed — check the cell "
                                "connections and that the electrolyte "
                                "conducts.")}

        tail_C = float(np.mean(currents_A[-n_tail:]))

        # Spike-aware flatness: compare the largest excursion from the
        # settled tail against the tail itself, on a lightly smoothed trace
        # so one noisy sample cannot mimic a transient. A head-mean check
        # here once diluted a sub-millisecond transient into invisibility
        # over a long capture window (bench 2026-08-14) and called a
        # faradaically active cell "purely resistive".
        if len(currents_A) >= 3:
            # Pad with the edge values: zero-padding would fake an excursion
            # at both ends of a perfectly flat trace.
            padded = np.concatenate(([currents_A[0]], currents_A,
                                     [currents_A[-1]]))
            smoothed = np.convolve(padded, np.ones(3) / 3.0, mode='valid')
        else:
            smoothed = currents_A
        dev = smoothed - tail_C
        peak_idx = int(np.argmax(np.abs(dev)))
        peak_dev = float(dev[peak_idx])

        if abs(peak_dev) < Potentiostat._RU_FLAT_DECAY_FRACTION * abs(tail_C):
            # A straight line's value at t=0 rather than the plain mean, so a
            # decay too slow to register here is still extrapolated back.
            I_flat = (float(np.polyfit(times_ms, currents_A, 1)[1])
                      if len(currents_A) >= 3 else float(np.mean(currents_A)))
            if abs(I_flat) < 1e-9:
                return {'R_u_ohm': float('inf'), 'tau_ms': None, 'ok': False,
                        'I_at_0_A': None,
                        'message': "extrapolated current at t=0 is zero — refused"}
            return {'R_u_ohm': abs(step_V / I_flat), 'tau_ms': None,
                    'ok': True, 'I_at_0_A': I_flat,
                    'message': ("no decay visible — treated as purely "
                                "resistive; on a real cell this means the "
                                "transient was too fast or too slow to "
                                "resolve")}

        from scipy.optimize import curve_fit

        def _model(t, I0, tau, C):
            return I0 * np.exp(-t / tau) + C

        span_ms = max(times_ms[-1] - times_ms[0], 1e-3)
        # Seed the time constant from where the excursion falls to 1/e of
        # its peak, so a transient spanning only a sliver of the capture
        # still starts the fit in the right decade.
        below = np.nonzero(np.abs(dev[peak_idx:]) <= abs(peak_dev) / np.e)[0]
        if len(below):
            tau0 = max(times_ms[peak_idx + below[0]] - times_ms[peak_idx], 1e-3)
        else:
            tau0 = span_ms / 3.0
        p0 = [peak_dev, tau0, tail_C]
        try:
            popt, _ = curve_fit(_model, times_ms, currents_A, p0=p0,
                                bounds=([-np.inf, 1e-6, -np.inf],
                                        [np.inf, np.inf, np.inf]),
                                maxfev=5000)
        except (RuntimeError, ValueError):
            return {'R_u_ohm': float('inf'), 'tau_ms': None, 'ok': False,
                    'I_at_0_A': None,
                    'message': ("the current transient did not fit an "
                                "exponential decay — measurement refused")}

        I0, tau_ms, C = popt
        dt_ms = float(np.median(np.diff(times_ms))) if len(times_ms) > 1 else 0.0
        if tau_ms < Potentiostat._RU_MIN_TAU_INTERVALS * dt_ms:
            return {'R_u_ohm': float('inf'), 'tau_ms': float(tau_ms),
                    'ok': False, 'I_at_0_A': None,
                    'message': (f"decay too fast to extrapolate (tau = "
                                f"{_format_ms(tau_ms)} vs {_format_ms(dt_ms)} "
                                f"between samples) — measurement refused")}

        I_at_0 = I0 + C
        if abs(I_at_0) < 1e-9:
            return {'R_u_ohm': float('inf'), 'tau_ms': float(tau_ms),
                    'ok': False, 'I_at_0_A': None,
                    'message': "fitted current at t=0 is zero — refused"}

        return {'R_u_ohm': abs(step_V / I_at_0), 'tau_ms': float(tau_ms),
                'ok': True, 'I_at_0_A': float(I_at_0), 'message': ''}

    @staticmethod
    def _filtered_step_response(times_ms, A, tau_ms, C):
        """What the board reads after a clean potential step (t=0 at the DAC
        write) when the cell's own current is ``A*exp(-t/tau) + C``: that
        current through the board's filters (``RU_FILTER_TAUS_MS``), each
        reading averaged over its ADC conversions (``RU_ADC_WINDOW_MS``)."""
        from scipy.signal import lfilter

        times_ms = np.asarray(times_ms, dtype=float)
        h = 0.00025  # 0.25 us grid, a sixth of the fastest filter
        half = RU_ADC_WINDOW_MS / 2.0
        grid = np.arange(0.0, float(np.max(times_ms)) + half + 2 * h, h)
        y = A * np.exp(-grid / tau_ms) + C
        for tf in RU_FILTER_TAUS_MS:
            # Exact first-order low-pass for an input that is linear between
            # grid points; the initial state makes the output start from
            # zero, as it was before the step, however far the input jumps.
            a = np.exp(-h / tf)
            k = tf / h * (1.0 - a)
            b0, b1 = 1.0 - k, k - a
            y = lfilter([b0, b1], [1.0, -a], y, zi=[-b0 * y[0]])[0]
        area = np.concatenate(([0.0], np.cumsum((y[1:] + y[:-1]) * 0.5 * h)))
        lo = np.clip(times_ms - half, 0.0, None)
        hi = np.clip(times_ms + half, 0.0, None)
        return (np.interp(hi, grid, area) - np.interp(lo, grid, area)) / (2 * half)

    @staticmethod
    def _fit_ru_step_response(times_ms, currents_A, step_V):
        """Estimate R_u from a DAC-step capture's current (t=0 at the step).

        Fits the cell current ``A*exp(-t/tau) + C`` as the board reads it
        (``_filtered_step_response``) and evaluates the cell current itself
        at t=0, so R_u = step / (A + C). A trace that stays flat once the
        filters have settled is a plain resistance, extrapolated with a
        straight line. Refused: a fitted time constant under two sample
        intervals, which cannot be extrapolated, and a current at t=0
        uncertain by more than RU_MAX_I0_ERROR.

        Returns:
            dict: 'R_u_ohm', 'tau_ms' (None for a plain resistance), 'ok',
                'I_at_0_A', and 'message' (empty when there is nothing to
                flag), as ``_fit_ru_decay`` returns them.
        """
        from scipy.optimize import curve_fit

        times_ms = np.asarray(times_ms, dtype=float)
        currents_A = np.asarray(currents_A, dtype=float)
        refused = {'R_u_ohm': float('inf'), 'tau_ms': None, 'ok': False,
                   'I_at_0_A': None}
        if len(currents_A) < 4 or np.max(np.abs(currents_A)) < 1e-9:
            return dict(refused, message=("No current flowed — check the cell "
                                          "connections and that the "
                                          "electrolyte conducts."))

        n_tail = max(3, len(currents_A) // 6)
        noisy = ("the step current is too small against the noise to "
                 "extrapolate (\u00b1{:.0%} at t=0) — increase the step size "
                 "and re-measure")

        # Flatness is judged once the filters have settled; before that even
        # a plain resistance's current is still rising. A flat trace has no
        # time constant for the fit to pin down, so it gets a straight line.
        settled = times_ms >= 5.0 * max(RU_FILTER_TAUS_MS)
        if settled.sum() >= 5:
            ts, cs = times_ms[settled], currents_A[settled]
            tail = float(np.mean(cs[-max(3, len(cs) // 6):]))
            padded = np.concatenate(([cs[0]], cs, [cs[-1]]))
            smoothed = np.convolve(padded, np.ones(3) / 3.0, mode='valid')
            if (np.max(np.abs(smoothed - tail))
                    < Potentiostat._RU_FLAT_DECAY_FRACTION * abs(tail)):
                coef, cov = np.polyfit(ts, cs, 1, cov=True)
                I_at_0 = float(coef[1])
                if abs(I_at_0) < 1e-9:
                    return dict(refused, message=("extrapolated current at t=0 "
                                                  "is zero — refused"))
                error = float(np.sqrt(cov[1, 1])) / abs(I_at_0)
                if not error <= RU_MAX_I0_ERROR:
                    return dict(refused, message=noisy.format(error))
                return {'R_u_ohm': abs(step_V / I_at_0), 'tau_ms': None,
                        'ok': True, 'I_at_0_A': I_at_0,
                        'message': ("no decay visible — treated as purely "
                                    "resistive; on a real cell this means the "
                                    "transient was too fast or too slow to "
                                    "resolve")}

        C0 = float(np.mean(currents_A[-n_tail:]))
        A0 = float(currents_A[0]) - C0
        span_ms = max(times_ms[-1] - times_ms[0], 1e-3)
        below = np.nonzero(np.abs(currents_A - C0) <= abs(A0) / np.e)[0]
        tau0 = (max(times_ms[below[0]] - times_ms[0], 0.01) if len(below)
                else span_ms)
        try:
            popt, pcov = curve_fit(Potentiostat._filtered_step_response,
                                times_ms, currents_A, p0=[A0, tau0, C0],
                                bounds=([-np.inf, 1e-4, -np.inf],
                                        [np.inf, 1e3, np.inf]),
                                maxfev=5000)
        except (RuntimeError, ValueError):
            return dict(refused, message=("the current transient did not fit "
                                          "an exponential decay — measurement "
                                          "refused"))

        A, tau_ms, C = (float(v) for v in popt)
        I_at_0 = A + C
        if abs(I_at_0) < 1e-9:
            return dict(refused, message="fitted current at t=0 is zero — refused")
        error = float(np.sqrt(pcov[0, 0] + pcov[2, 2] + 2.0 * pcov[0, 2])) / abs(I_at_0)
        if not error <= RU_MAX_I0_ERROR:
            return dict(refused, tau_ms=tau_ms, message=noisy.format(error))
        dt_ms = float(np.median(np.diff(times_ms)))
        if tau_ms < Potentiostat._RU_MIN_TAU_INTERVALS * dt_ms:
            return dict(refused, tau_ms=tau_ms,
                        message=(f"decay too fast to extrapolate (tau = "
                                 f"{_format_ms(tau_ms)} vs {_format_ms(dt_ms)} "
                                 f"between samples) — measurement refused"))
        return {'R_u_ohm': abs(step_V / I_at_0), 'tau_ms': tau_ms, 'ok': True,
                'I_at_0_A': I_at_0, 'message': ''}

    def step_capture(self, n_samples, period_us):
        """Firmware-timed step-response capture (CMD_STEP_CAPTURE).

        The firmware closes the CE switch itself and burst-samples the raw
        current channel at a cycle-counter-timed pace, so the transient is
        recorded from within microseconds of the switch closing — over the
        serial link the first reading lands one round trip (~3 ms) later,
        past most of the decay at typical cell time constants.

        The DAC must already be parked at the step potential and the switch
        open. On success the switch is left CLOSED; follow with
        ``_safe_open_switch()``. Raises ``SerialReadTimeout`` on firmware
        without the command (it sends no acknowledgement), which is how the
        capability probe in ``_acquire_step_trace`` detects old firmware.

        Args:
            n_samples (int): Samples requested. The firmware clamps to its
                buffer (~32k) and to a 1 s total duration.
            period_us (int): Sample period in microseconds (floor 25).

        Returns:
            tuple: ``(counts, duration_us, gain_idx)`` — raw uint16 ADC
                counts, the measured wall-clock duration of the capture, and
                the gain resistor index it ran at.
        """
        n_samples = int(n_samples)
        period_us = int(period_us)
        expected_s = n_samples * period_us / 1e6
        with self._port_lock:
            old_timeout = self.serial.timeout
            try:
                # The acknowledgement only arrives after the whole capture
                self.serial.timeout = expected_s + 2.0
                self._write_cmd(Commands.STEP_CAPTURE,
                                np.uint32([n_samples, period_us]), False)
                ack = self.serial.read(9)
                if len(ack) < 9:
                    raise SerialReadTimeout(
                        "No step-capture acknowledgement — firmware without "
                        "CMD_STEP_CAPTURE?")
                captured = int(np.frombuffer(ack, np.uint32, 1, 0)[0])
                duration_us = int(np.frombuffer(ack, np.uint32, 1, 4)[0])
                gain_idx = int(ack[8])
                if captured == 0:
                    raise SerialReadTimeout("Step capture returned no samples")
                self._write_cmd(Commands._READ_BUFFER,
                                np.uint32([0, captured * 2]), False)
                raw = self.serial.read(captured * 2)
                if len(raw) < captured * 2:
                    raise SerialReadTimeout(
                        f"Step-capture readback short: {len(raw)} of "
                        f"{captured * 2} bytes")
            finally:
                self.serial.timeout = old_timeout
        counts = np.frombuffer(raw, np.uint16).copy()
        return counts, duration_us, gain_idx

    def _capture_counts_to_mA(self, counts, gain_idx):
        """Convert raw step-capture ADC counts to calibrated mA.

        Same chain as ``read_current_calibrated``: counts -> ADC volts ->
        physical volts -> amps through the gain resistor -> the board's read
        calibration.
        """
        v_adc = np.asarray(counts, dtype=float) * ADC16_COUNTS_TO_V
        v = self._scale_output_voltages(v_adc)
        i_A = v / (-1.0 * self.res_vals[gain_idx])
        return self._calibrated_mA(i_A, gain_idx)

    def dac_step_capture(self, n_samples, period_us, n_pre, potential_V):
        """Firmware-timed step capture with the step made by the DAC
        (``Commands.DAC_STEP_CAPTURE``, firmware 1.7+).

        With the cell already connected, the firmware records ``n_pre`` raw
        current readings, moves the DAC to ``potential_V`` (calibrated, as
        ``write_potential_calibrated`` takes it) and keeps sampling, so the
        trace holds the cell's response to a clean step and its zero just
        before it. The DAC is left at ``potential_V`` and the cell connected.

        Returns:
            tuple: ``(counts, duration_us, gain_idx, centre_us)`` — raw uint16
                ADC counts, the capture's measured wall-clock duration, the
                gain resistor index, and the time from the DAC write to the
                middle of the first reading after it.

        Raises:
            RuntimeError: The cell was not connected, so nothing was captured.
            SerialReadTimeout: No acknowledgement or a short readback.
        """
        slope = self._calib_v_slope
        # Avoid div by zero on a malformed calibration row
        if abs(slope) < 1e-6:
            slope = 1.0
        dac_V = float(self._scale_input_voltages(
            (potential_V - self._calib_v_intercept) / slope))
        n_samples = int(n_samples)
        period_us = int(period_us)
        request = (np.uint32([n_samples, period_us, int(n_pre)]).tobytes()
                   + np.float32(dac_V).tobytes())
        expected_s = n_samples * period_us / 1e6
        with self._port_lock:
            old_timeout = self.serial.timeout
            try:
                # The acknowledgement only arrives after the whole capture
                self.serial.timeout = expected_s + 2.0
                self._write_cmd(Commands.DAC_STEP_CAPTURE, request, False)
                ack = self.serial.read(13)
                if len(ack) < 13:
                    raise SerialReadTimeout("No DAC-step capture acknowledgement")
                captured = int(np.frombuffer(ack, np.uint32, 1, 0)[0])
                duration_us = int(np.frombuffer(ack, np.uint32, 1, 4)[0])
                gain_idx = int(ack[8])
                centre_us = int(np.frombuffer(ack, np.uint32, 1, 9)[0]) / 1000.0
                if captured == 0:
                    raise RuntimeError("DAC-step capture refused: the cell is "
                                       "not connected")
                self._write_cmd(Commands._READ_BUFFER,
                                np.uint32([0, captured * 2]), False)
                raw = self.serial.read(captured * 2)
                if len(raw) < captured * 2:
                    raise SerialReadTimeout(
                        f"DAC-step capture readback short: {len(raw)} of "
                        f"{captured * 2} bytes")
            finally:
                self.serial.timeout = old_timeout
        counts = np.frombuffer(raw, np.uint16).copy()
        return counts, duration_us, gain_idx, centre_us

    def _acquire_step_trace(self, n_readings, capture_ms, capture_period_us):
        """Close the switch and record the post-step current transient.

        Prefers the firmware capture; falls back to the serial burst (one
        reading per round trip) on firmware without CMD_STEP_CAPTURE, and
        remembers the answer for the rest of the connection. The DAC must
        already be parked at the step potential with the switch open; the
        switch is left closed either way.

        Returns:
            tuple: ``(times_ms, currents_A, source, baseline_A)`` with t=0 at
                the switch closing, ``source`` either ``'firmware'`` or
                ``'serial'``, and ``baseline_A`` the zero-current reading that
                was subtracted from the whole trace.
        """
        # With the switch still open the true cell current is exactly zero,
        # so whatever the chain reads now is its zero-offset — calibration
        # intercept error plus drift — at this gain, this minute. R_u's
        # transient currents are the same order as the raw offset the
        # calibration cancels (~0.5 mA), so a small residual offset lands
        # directly in R_u unless it is measured and subtracted here.
        baseline_A = float(np.mean([
            float(self.read_current_calibrated()[0]) / 1000.0
            for _ in range(5)]))

        if getattr(self, '_step_capture_supported', False) is not False:
            try:
                n = max(2, int(round(capture_ms * 1000.0 / capture_period_us)))
                counts, duration_us, gain_idx = self.step_capture(
                    n, capture_period_us)
                self._step_capture_supported = True
                dt_ms = (duration_us / 1000.0) / len(counts)
                times_ms = (np.arange(len(counts)) + 0.5) * dt_ms
                currents_A = self._capture_counts_to_mA(counts, gain_idx) / 1000.0
                # Drop railed samples (ADC pinned at either end): they carry
                # no amplitude information and would drag the fit.
                ok = (counts > 100) & (counts < 65435)
                if ok.sum() >= 10:
                    times_ms = times_ms[ok]
                    currents_A = currents_A[ok]
                return times_ms, currents_A - baseline_A, 'firmware', baseline_A
            except SerialReadTimeout as e:
                self._step_capture_supported = False
                logger.info("[PS %s] Firmware step capture unavailable (%s); "
                            "falling back to the serial burst.",
                            self.device_ID, e)
                if getattr(self, 'serial', None) is not None:
                    self.serial.reset_input_buffer()

        times_ms = np.zeros(n_readings)
        currents_A = np.zeros(n_readings)
        # Hold the port for the whole burst so another thread's commands
        # cannot interleave and stretch the decay's timing.
        with self._port_lock:
            self.write_switch(SwitchState.On)
            t0 = time.time()
            for i in range(n_readings):
                currents_A[i] = float(self.read_current_calibrated()[0]) / 1000.0
                times_ms[i] = (time.time() - t0) * 1000.0
        return times_ms, currents_A - baseline_A, 'serial', baseline_A

    @staticmethod
    def _ru_blank_ms(times_ms, currents_A):
        """How long the zero-step control capture shows the control
        amplifier's recovery from the switch closing (see
        RU_ARTIFACT_WINDOW_MS). Samples before this are left out of the fit.
        """
        times_ms = np.asarray(times_ms, dtype=float)
        currents_A = np.asarray(currents_A, dtype=float)
        if len(times_ms) < 2:
            return RU_MIN_BLANK_MS
        tail = currents_A[-max(3, len(currents_A) // 6):]
        excursion = np.abs(currents_A - float(np.mean(tail)))
        threshold = max(RU_ARTIFACT_SIGMAS * float(np.std(tail)),
                        RU_ARTIFACT_FRACTION * float(excursion[0]), 1e-6)
        off = (excursion > threshold) & (times_ms <= RU_ARTIFACT_WINDOW_MS)
        if not off.any():
            return RU_MIN_BLANK_MS
        dt_ms = float(np.median(np.diff(times_ms)))
        # Halfway past the following sample, which is skipped as margin
        last_ms = float(times_ms[np.nonzero(off)[0][-1]])
        return max(RU_MIN_BLANK_MS, last_ms + 1.5 * dt_ms)

    @staticmethod
    def _fit_ru_steps(up, down, control=None):
        """R_u from two opposite step captures.

        Fits the DIFFERENCE of the two step traces, which is the cell's
        response to one step of twice the size: a constant current-reading
        offset rides equally on both traces and cancels exactly, as does
        anything else that does not reverse with the step. The fit covers the
        first RU_FIT_WINDOW_MS of the transient, where the ohmic charging
        current dominates the slower processes.

        With a ``control`` capture the steps were made by closing the switch
        (older firmware): the samples taken while the amplifier was still
        recovering are skipped (the control shows for how long), each step's
        current is measured from the control's level, and the difference is
        fitted with a plain exponential. Without one they were made by the
        DAC with the cell connected: each trace is already zeroed on its
        readings before the step, and the fit models the board's filters
        (``_fit_ru_step_response``) from RU_DAC_FIT_START_MS on.

        Args:
            up, down (dict): The positive and negative step captures, with
                'times_ms' (t=0 at the step), 'currents_A' and 'step_V'; DAC
                steps also carry 'pre_std_A', the noise before the step.
            control (dict, optional): The zero-step switch-close capture, with
                'times_ms', 'currents_A' and 'source' ('firmware' or 'serial').

        Returns:
            dict: 'R_u_ohm' (inf when refused), 'ok', 'message', 'tau_ms'
                (None when no decay was fit), 'I_at_0_A' (of the difference),
                'blank_ms' (where the fit starts), 'window_ms' (where it
                ends), and 'rise_A' / 'fall_A' (each step's mean current over
                the window's first RU_FIT_MIN_SAMPLES readings, from zero).
        """
        result = {'R_u_ohm': float('inf'), 'ok': False, 'message': '',
                  'tau_ms': None, 'I_at_0_A': None, 'blank_ms': None,
                  'window_ms': None, 'rise_A': None, 'fall_A': None}

        t = np.asarray(up['times_ms'], dtype=float)
        i_up = np.asarray(up['currents_A'], dtype=float)
        # One time base for all: rail masking can drop different samples
        # from each capture
        i_down = np.interp(t, down['times_ms'], down['currents_A'])
        if control is None:
            blank_ms = RU_DAC_FIT_START_MS
            i_zero = np.zeros_like(t)
            noise = max(float(up.get('pre_std_A', 0.0)),
                        float(down.get('pre_std_A', 0.0)))
        else:
            # The serial fallback's first reading lands one round trip
            # (~3 ms) after the switch closes, long after the recovery
            if control.get('source', 'firmware') == 'firmware':
                blank_ms = Potentiostat._ru_blank_ms(control['times_ms'],
                                                     control['currents_A'])
            else:
                blank_ms = 0.0
            i_zero = np.interp(t, control['times_ms'], control['currents_A'])
            c_i = np.asarray(control['currents_A'], dtype=float)
            noise = float(np.std(c_i[-max(3, len(c_i) // 6):]))
        result['blank_ms'] = blank_ms
        if blank_ms > RU_MAX_BLANK_MS:
            result['message'] = (
                f"the amplifier took {_format_ms(blank_ms)} to settle after "
                f"the cell was connected, which hides the transient — "
                f"measurement refused")
            return result

        after = np.nonzero(t >= blank_ms)[0]
        if len(after) < 3:
            result['message'] = ("too few samples after the step — "
                                 "measurement refused")
            return result
        end_ms = max(t[after[0]] + RU_FIT_WINDOW_MS,
                     t[after[min(len(after), RU_FIT_MIN_SAMPLES) - 1]])
        win = after[t[after] <= end_ms]
        result['window_ms'] = float(t[win[-1]])

        # Each step's own current, which must clear the noise and point the
        # way its step does. Taken over the window's first readings, where
        # even a fast cell's current has not yet decayed away.
        head = win[:RU_FIT_MIN_SAMPLES]
        rise = float(np.mean(i_up[head] - i_zero[head]))
        fall = float(np.mean(i_zero[head] - i_down[head]))
        result['rise_A'], result['fall_A'] = rise, fall
        noise /= np.sqrt(len(head))
        for drawn, step in ((rise, up), (fall, down)):
            if drawn <= 0:
                result['message'] = (
                    f"the {step['step_V'] * 1e3:+.0f} mV step drew current "
                    f"against its own direction — check the cell connections, "
                    f"and rest the cell away from any redox wave — "
                    f"measurement refused")
                return result
            if drawn <= max(5.0 * noise, 1e-9):
                result['message'] = (
                    f"the {step['step_V'] * 1e3:+.0f} mV step drew only "
                    f"{drawn * 1e6:.1f} \u00b5A, too little to measure against "
                    f"the noise — check the cell connections, or increase the "
                    f"step size — measurement refused")
                return result
        if max(rise, fall) > RU_MAX_ASYMMETRY * min(rise, fall):
            result['message'] = (
                f"the two steps drew very different currents "
                f"({rise * 1e6:.0f} vs {fall * 1e6:.0f} µA), so something "
                f"other than solution resistance dominates, most likely "
                f"faradaic current from a step onto a redox wave — rest the "
                f"cell away from any wave, or reduce the step, and re-measure")
            return result

        fit_fn = (Potentiostat._fit_ru_step_response if control is None
                  else Potentiostat._fit_ru_decay)
        fit = fit_fn(t[win], i_up[win] - i_down[win],
                     up['step_V'] - down['step_V'])
        result.update(R_u_ohm=fit['R_u_ohm'], ok=fit['ok'],
                      message=fit['message'], tau_ms=fit['tau_ms'],
                      I_at_0_A=fit['I_at_0_A'])
        return result

    def _check_step_targets(self, ocp, step_mV):
        """Refuse R_u steps the board cannot reach from ``ocp``."""
        for polarity in (+1.0, -1.0):
            E_step = ocp + polarity * step_mV / 1000.0
            if abs(E_step) > HW_VOLTAGE_COMPLIANCE_V:
                raise ValueError(
                    f"Step target {E_step:.3f} V is beyond the board's "
                    f"output limit ({HW_VOLTAGE_COMPLIANCE_V} V). Reduce "
                    f"step_mV.")

    def _switch_step_traces(self, step_mV, n_readings, settle_s):
        """The two R_u steps made by closing the switch (firmware before
        1.7), plus the zero-step control capture before them.

        Returns:
            tuple: ``(control, [up, down])``, as ``_fit_ru_steps`` takes them.
                The switch is left open.
        """
        # Control capture: park at OCP itself and record the switch closing
        # with zero driving force, which shows how long the instrument takes
        # to recover.
        ocp0 = float(self.read_ocp(restore_switch_state=False))
        self._check_step_targets(ocp0, step_mV)
        self.write_potential_calibrated(ocp0)
        time.sleep(settle_s)
        c_times, c_currents, c_source, c_baseline = \
            self._acquire_step_trace(n_readings, RU_CAPTURE_MS,
                                     RU_CAPTURE_PERIOD_US)
        self._safe_open_switch()
        control = {'times_ms': c_times, 'currents_A': c_currents,
                   'baseline_A': c_baseline, 'ocp': ocp0,
                   'source': c_source}
        logger.info("[PS %s] R_u control (no step, %s): OCP=%.4f V, "
                    "baseline=%.1f uA, first sample=%.1f uA",
                    self.device_ID, c_source, ocp0, c_baseline * 1e6,
                    float(c_currents[0]) * 1e6)

        traces = []
        for polarity in (+1.0, -1.0):
            step_V = polarity * step_mV / 1000.0

            # read_ocp leaves the switch open, which is where the step
            # begins; re-read before each step because OCP drifts while the
            # cell recovers from the previous one.
            ocp = float(self.read_ocp(restore_switch_state=False))
            self._check_step_targets(ocp, step_mV)

            # Park the DAC at the step potential before connecting the cell,
            # so the step is as sharp as the hardware allows.
            self.write_potential_calibrated(ocp + step_V)
            time.sleep(settle_s)

            times_ms, currents_A, source, baseline_A = \
                self._acquire_step_trace(
                    n_readings, RU_CAPTURE_MS, RU_CAPTURE_PERIOD_US)

            self._safe_open_switch()

            traces.append({'step_V': step_V, 'ocp': ocp, 'source': source,
                           'baseline_A': baseline_A, 'times_ms': times_ms,
                           'currents_A': currents_A})
            logger.info("[PS %s] R_u step %+.0f mV (%s): OCP=%.4f V, "
                        "baseline=%.1f uA, first sample=%.1f uA",
                        self.device_ID, step_V * 1e3, source, ocp,
                        baseline_A * 1e6, float(currents_A[0]) * 1e6)
        return control, traces

    def _dac_step_traces(self, step_mV, settle_s):
        """The two R_u steps made by the DAC with the cell connected
        (firmware 1.7+).

        The cell is connected at its open-circuit potential and held there
        for ``settle_s``, long enough for the switch-close transient to die
        away; each step then starts from rest, and the cell goes back to OCP
        for ``settle_s`` before the next one. Each trace is zeroed on its
        own readings from just before its step.

        Returns:
            list: ``[up, down]``, as ``_fit_ru_steps`` takes them. The switch
                is left open.
        """
        ocp = float(self.read_ocp(restore_switch_state=False))
        self._check_step_targets(ocp, step_mV)
        self.write_potential_calibrated(ocp)
        self.write_switch(SwitchState.On)

        period_ms = RU_CAPTURE_PERIOD_US / 1000.0
        n_samples = RU_PRE_SAMPLES + int(round(RU_CAPTURE_MS / period_ms))
        traces = []
        for polarity in (+1.0, -1.0):
            step_V = polarity * step_mV / 1000.0
            time.sleep(settle_s)
            counts, duration_us, gain_idx, centre_us = self.dac_step_capture(
                n_samples, RU_CAPTURE_PERIOD_US, RU_PRE_SAMPLES, ocp + step_V)
            self.write_potential_calibrated(ocp)
            if duration_us > 1.02 * len(counts) * RU_CAPTURE_PERIOD_US:
                logger.warning("[PS %s] R_u capture ran slow (%d us for %d "
                               "samples); its time axis is approximate.",
                               self.device_ID, duration_us, len(counts))

            times_ms = ((np.arange(len(counts)) - RU_PRE_SAMPLES) * period_ms
                        + centre_us / 1000.0)
            currents_A = self._capture_counts_to_mA(counts, gain_idx) / 1000.0
            # Railed samples (ADC pinned at either end) carry no amplitude
            # information and would drag the fit
            ok = (counts > 100) & (counts < 65435)
            pre = currents_A[:RU_PRE_SAMPLES][ok[:RU_PRE_SAMPLES]]
            baseline_A = float(np.mean(pre)) if len(pre) else 0.0
            pre_std_A = float(np.std(pre)) if len(pre) > 1 else 0.0
            if ok.sum() >= 10:
                times_ms, currents_A = times_ms[ok], currents_A[ok]
            traces.append({'step_V': step_V, 'ocp': ocp, 'source': 'dac-step',
                           'baseline_A': baseline_A, 'pre_std_A': pre_std_A,
                           'times_ms': times_ms,
                           'currents_A': currents_A - baseline_A})
            logger.info("[PS %s] R_u step %+.0f mV (DAC, cell connected): "
                        "OCP=%.4f V, zero before step=%.1f uA (noise %.1f uA), "
                        "first reading %s after the step",
                        self.device_ID, step_V * 1e3, ocp, baseline_A * 1e6,
                        pre_std_A * 1e6, _format_ms(centre_us / 1000.0))
        self._safe_open_switch()
        return traces

    def measure_R_u(self, step_mV=50.0, n_readings=30, settle_s=2.0):
        """Measure the cell's uncompensated (solution) resistance, R_u.

        Applies a small, sudden potential step away from OCP and records the
        current decay, sampled by the firmware from within microseconds of
        the step. The decay is fitted with an exponential and extrapolated
        back to the instant of the step, when the cell behaved like a plain
        resistor, giving R_u = step / I(0).

        The step is made twice, once in each direction from OCP, and the fit
        is made to the DIFFERENCE of the two traces (see ``_fit_ru_steps``),
        so any constant current-reading offset — which rides equally on both
        and is the same order as the transient currents themselves — cancels
        exactly. Alternating the direction also cancels the drift a single
        repeated step leaves behind.

        On firmware 1.7+ (``FW_FEATURE_DAC_STEP``) the steps are made by the
        DAC with the cell connected at OCP (``_dac_step_traces``): the trace
        is the cell's response through the board's known filters, from the
        first microseconds. Older firmware makes them by closing the switch
        onto a DAC already parked at the step potential
        (``_switch_step_traces``), whose first ~75 us are the control
        amplifier recovering from the open switch. The recovery also pushes
        charge into the double layer before the fit starts, which reads R_u
        high; and without CMD_STEP_CAPTURE at all, the decay is read over the
        serial link, one round trip (~3 ms) per point.

        Args:
            step_mV (float): Step size in mV. Keep it small (20–100 mV) so the
                step does not drive a reaction. Default 50 mV.
            n_readings (int): Number of current readings per step when the
                serial fallback is used. Default 30.
            settle_s (float): Hold time at rest before each step, to let the
                cell relax. Default 2.0 s.

        Returns:
            dict: 'R_u_ohm' (inf when refused), 'fit_ok', 'message', 'fit'
                (the combined fit's details, see ``_fit_ru_steps``),
                'polarities' (each step's full trace), 'control' (None for
                DAC steps), plus the legacy keys 'step_V', 'I_peak_A',
                'times_ms', 'currents_A' describing the first step.
        """
        # Force the 100 ohm gain: the current spike right after the step would
        # saturate a higher-gain resistor and clip the transient this
        # measurement depends on.
        prev_gain = getattr(self, 'Resistor', Resistors.R_100)
        prev_auto = self._auto_gain
        features = getattr(self, 'firmware_features', lambda: 0)()

        control = None
        try:
            self.write_auto_gain(False)
            self.write_gain(Resistors.R_100)
            if features & FW_FEATURE_DAC_STEP:
                polarities = self._dac_step_traces(step_mV, settle_s)
            else:
                control, polarities = self._switch_step_traces(
                    step_mV, n_readings, settle_s)

        except Exception:
            self.write_switch(SwitchState.Off)  # Fail-state: cut the output before raising
            raise

        finally:
            # Restoring the gain must never mask the original failure
            try:
                self.write_gain(prev_gain)
                self.write_auto_gain(prev_auto)
            except Exception:
                logger.warning("[PS %s] Could not restore the gain setting "
                               "after the R_u measurement.", self.device_ID)

        fit = self._fit_ru_steps(*polarities, control=control)
        if fit['ok']:
            logger.info("[PS %s] R_u = %.1f ohm (%s steps, fit from %s to %s, "
                        "tau %s, step currents %.1f / %.1f uA) %s",
                        self.device_ID, fit['R_u_ohm'],
                        'DAC' if control is None else 'switch',
                        _format_ms(fit['blank_ms']),
                        _format_ms(fit['window_ms']),
                        (_format_ms(fit['tau_ms']) if fit['tau_ms'] is not None else '-'),
                        fit['rise_A'] * 1e6, fit['fall_A'] * 1e6,
                        fit['message'])
        else:
            logger.warning("[PS %s] R_u refused: %s", self.device_ID,
                           fit['message'])

        first = polarities[0]
        return {
            'R_u_ohm':    float(fit['R_u_ohm']),
            'fit_ok':     bool(fit['ok']),
            'message':    fit['message'],
            'fit':        fit,
            'polarities': polarities,
            'control':    control,
            'step_V':     first['step_V'],
            'I_peak_A':   float(first['currents_A'][int(np.argmax(np.abs(first['currents_A'])))]),
            'times_ms':   first['times_ms'],
            'currents_A': first['currents_A'],
        }

    def perform_CV_iR(self, min_V, max_V, cycles, mV_s, step_hz, R_u_ohm,
                      start_V=None, last_V=None, scan_direction='Negative',
                      compensation_fraction=0.7, max_consecutive_timeouts=5,
                      data_callback=None, stop_event=None, callback_block=6,
                      feedback_median=3, feedback_alpha=0.4,
                      correction_slew_V=0.010, freeze_auto_gain=True,
                      quiet_s=DEFAULT_QUIET_S, current_range=None):
        """Run a CV with live software iR compensation (advanced).

        Solution resistance means the electrode never quite reaches the
        potential asked for — it falls short by I times R_u. This method adds
        that missing drop back on, one point at a time, from the current
        measured at the previous points:

            applied = target + (filtered current x R_u x compensation_fraction)

        Unlike perform_CV, which streams the whole waveform to the device at
        once, this loop sets and reads a single point per serial round trip,
        which limits it to roughly 100–170 points per second.

        This is positive feedback, and through a faradaic wave it runs with
        almost no damping: a single bad current sample would echo through
        tens of points as a real potential excursion (bench-diagnosed
        Aug 2026). Three guards keep it in check. Auto-gain is frozen for the
        scan, because a reading taken mid range-change can be wildly
        mis-scaled — the seed of the observed spikes. The feedback current is
        median-filtered then exponentially smoothed, so no single sample
        reaches the setpoint unchecked. And the correction term is
        slew-limited so it can only walk, never jump.

        Only part of the drop is added back, because giving back more than
        the cell actually lost makes the correction feed on itself. The
        default backs off further than analog instruments do (0.7 rather
        than ~0.9) because the one-sample feedback delay eats stability
        margin an analog loop keeps. For a fully corrected axis, prefer the
        post-hoc mode in the UI, which subtracts the drop after a normal
        batched scan.

        As a further guard, every setpoint is capped at the board's output
        limit and the first cap is logged as a warning. If a scan reports
        capping, re-measure R_u before trusting the trace.

        Args:
            min_V (float): Lowest potential of the sweep (V)
            max_V (float): Highest potential of the sweep (V)
            cycles (int): Number of full cycles
            mV_s (float): Scan rate in mV/s
            step_hz (float): Points per second, which sets the step size
            R_u_ohm (float): Uncompensated resistance in ohms, from measure_R_u
            start_V (float, optional): Starting potential. Defaults to OCP.
            last_V (float, optional): Ending potential. Defaults to OCP.
            scan_direction (str): 'Negative' (default) or 'Positive'
            compensation_fraction (float): Portion of the ohmic drop to add
                back, 0 to 1. Default 0.7. Lower it if a scan looks noisy or
                rings; 1.0 fully compensates but has no stability margin.
            max_consecutive_timeouts (int): Dropped readings tolerated in a row
                before the scan is abandoned. Default 5.
            data_callback (callable, optional): Called with
                ``(potential_V, current_uA)`` numpy arrays as the scan runs, so
                a caller can plot it live. Batched into small blocks rather
                than one call per point.
            stop_event (threading.Event, optional): When set, the scan stops
                after the current point, opens the switch safely, and returns
                the points completed so far instead of the full sweep.
            callback_block (int): Points buffered per ``data_callback`` call.
                Default 6, matching the batch path's chunk size.
            feedback_median (int): Window of recent current samples the
                feedback takes its median over. 1 disables the median.
                Default 3.
            feedback_alpha (float): Weight of the newest (median-filtered)
                sample in the exponential smoothing of the feedback current,
                0 to 1. 1.0 disables the smoothing. Default 0.4.
            correction_slew_V (float): Largest change of the correction term
                allowed per point, in volts. ``np.inf`` disables the limit.
                Default 0.010.
            freeze_auto_gain (bool): Disable auto-gain for the scan and
                restore it afterwards, so range changes cannot feed a
                mis-scaled sample into the setpoint. Default True.
            quiet_s (float): Time the connected cell is held at the first
                potential before the scan starts, as in perform_CV.
            current_range: a fixed range index for the scan (see
                ``set_current_range``), or None to keep the range already
                set. Point-by-point control cannot auto-range, so ``'auto'``
                runs the scan on range 0.

        Returns:
            np.ndarray: same columns as perform_CV — time (s), measured
                potential (V), current (uA), cycle number, experiment number,
                target potential (V). Shorter than the requested sweep if
                ``stop_event`` was set part-way through.
        """
        #Stop any running current-hold before the OCP read below opens the
        #switch, otherwise the firmware hold loop drives the DAC to its limit
        self.write_current_hold_stop()

        #if start_V and/or last_V is None, start/end with OCP
        if (start_V is None) or (last_V is None):
            ocp = self.read_ocp()
            if start_V is None:
                start_V = ocp
            if last_V is None:
                last_V = ocp

        V_s = mV_s / 1000
        step_V = V_s / step_hz

        cv = self._build_cv_waveform(min_V, max_V, cycles, step_V,
                                     start_V, last_V, scan_direction)
        num_points = len(cv)
        if num_points == 0:
            raise ValueError("The CV waveform came out empty — check min_V, "
                             "max_V, cycles, and the scan rate.")

        if compensation_fraction > 1.0:
            logger.warning("[PS %s] compensation_fraction=%.2f gives back more "
                           "than the cell lost; the correction may ring or "
                           "amplify noise.", self.device_ID, compensation_fraction)
        R_u_effective = R_u_ohm * compensation_fraction

        delay_s = 1.0 / step_hz
        measured_V = np.zeros(num_points)
        measured_I_mA = np.zeros(num_points)
        actual_times = np.zeros(num_points)

        # Feedback state: the correction is computed from a median-filtered,
        # exponentially smoothed current rather than the last raw sample.
        recent_I_A = deque(maxlen=max(1, int(feedback_median)))
        filtered_I_A = 0.0
        alpha = min(max(float(feedback_alpha), 0.0), 1.0)
        prev_correction_V = 0.0
        consecutive_timeouts = 0
        capped = False

        if current_range is not None:
            self.set_current_range(Resistors.R_100 if current_range == 'auto'
                                   else current_range)

        # A mid-wave gain change can hand the feedback a mis-scaled sample,
        # so the gain is held fixed for the whole scan by default.
        prev_auto_gain = self._auto_gain
        if freeze_auto_gain and prev_auto_gain:
            self.write_auto_gain(False)

        # Park the DAC at the sweep's first potential BEFORE closing the
        # switch, so the cell never sees a stale setpoint as the scan starts.
        self.write_potential_calibrated(cv[0])
        time.sleep(0.05)
        self.write_switch(SwitchState.On)

        points_done = num_points
        block_start = 0

        def _emit_block(upto):
            """Hand the points since the last emission to the live callback."""
            if data_callback is None or upto <= block_start:
                return
            try:
                data_callback(np.round(measured_V[block_start:upto], 5),
                              np.round(measured_I_mA[block_start:upto] * 1000.0, 5))
            except Exception as e:
                logger.warning("[PS %s] Live callback error: %s", self.device_ID, e)

        try:
            self._quiet_wait(quiet_s, stop_event)
            t_start = time.time()
            for i in range(num_points):
                # Check for a stop before driving the next point, so a stopped
                # scan never applies a potential it will not read back
                if stop_event is not None and stop_event.is_set():
                    points_done = i
                    break

                t_step_start = time.time()

                # Add back the ohmic drop, from the filtered current of the
                # points before this one, walking no faster than the slew
                # limit allows.
                correction_V = filtered_I_A * R_u_effective
                if np.isfinite(correction_slew_V):
                    correction_V = float(np.clip(
                        correction_V,
                        prev_correction_V - correction_slew_V,
                        prev_correction_V + correction_slew_V))
                prev_correction_V = correction_V
                E_corrected = cv[i] + correction_V

                # Guard against a runaway from an overestimated R_u
                if abs(E_corrected) > HW_VOLTAGE_COMPLIANCE_V:
                    E_corrected = np.sign(E_corrected) * HW_VOLTAGE_COMPLIANCE_V
                    if not capped:
                        capped = True
                        logger.warning(
                            "[PS %s] iR-corrected setpoint hit the %.1f V output "
                            "limit at point %d — R_u may be overestimated.",
                            self.device_ID, HW_VOLTAGE_COMPLIANCE_V, i)

                self.write_potential_calibrated(E_corrected)

                try:
                    reading = self.read_potential_current_calibrated()
                    consecutive_timeouts = 0
                except SerialReadTimeout:
                    # A dropped reading is survivable: keep the previous values
                    # so the scan holds its timing, and give up only if the
                    # link stays broken.
                    consecutive_timeouts += 1
                    if consecutive_timeouts >= max_consecutive_timeouts:
                        raise
                    logger.warning("[PS %s] Dropped reading at point %d of %d.",
                                   self.device_ID, i, num_points)
                    reading = [measured_V[i - 1] if i else 0.0,
                               measured_I_mA[i - 1] if i else 0.0]

                measured_V[i] = reading[0]
                measured_I_mA[i] = reading[1]
                actual_times[i] = time.time() - t_start

                recent_I_A.append(measured_I_mA[i] / 1000.0)
                median_I_A = float(np.median(recent_I_A))
                filtered_I_A = (median_I_A if i == 0
                                else alpha * median_I_A + (1.0 - alpha) * filtered_I_A)

                if (i + 1 - block_start) >= callback_block:
                    _emit_block(i + 1)
                    block_start = i + 1

                # Sleep only the time left in this step, to hold the scan rate
                remaining = delay_s - (time.time() - t_step_start)
                if remaining > 0:
                    time.sleep(remaining)

            _emit_block(points_done)
            self._safe_open_switch()

        except Exception:
            self.write_switch(SwitchState.Off)  # Fail-state: cut the output before raising
            raise

        finally:
            # Restoring auto-gain must never mask the original failure
            if freeze_auto_gain and prev_auto_gain:
                try:
                    self.write_auto_gain(prev_auto_gain)
                except Exception:
                    logger.warning("[PS %s] Could not restore auto-gain after "
                                   "the iR-compensated scan.", self.device_ID)

        # The loop keeps its own pace only while a serial round trip fits in
        # the step period; past that it silently runs slow, so report the
        # rate actually achieved for the caller to record.
        if points_done > 1 and actual_times[points_done - 1] > 0:
            achieved_hz = points_done / actual_times[points_done - 1]
            if achieved_hz < 0.98 * step_hz:
                logger.warning(
                    "[PS %s] iR scan achieved %.0f points/s of the %.0f "
                    "requested — the true scan rate was %.1f%% of nominal.",
                    self.device_ID, achieved_hz, step_hz,
                    100.0 * achieved_hz / step_hz)

        # A stopped scan returns only the points actually measured
        measured_V = measured_V[:points_done]
        measured_I_mA = measured_I_mA[:points_done]
        actual_times = actual_times[:points_done]
        cv = cv[:points_done]
        cycle_nums = cv_cycle_numbers(cv, min_V, max_V, cycles)

        return np.column_stack((actual_times, measured_V, measured_I_mA * 1000.0,
                                cycle_nums, np.zeros(points_done), cv))

    def perform_DPV(self, waveform, start_V=None, sample_hz=250,
                    quiet_s=DEFAULT_QUIET_S, current_range=None, range_period=1):
        """Run a pre-built DPV target waveform and stream the response.

        The staircase-plus-pulse waveform is built by
        ``moles.methods.dpv.build_dpv_waveform``, which is where the sweep shape
        (single-sweep or cyclic segments) is decided; this method only drives
        the hardware. ``waveform`` is the per-sample target-potential array.
        The DAC is parked at ``start_V`` (default: the waveform's first point,
        which makes the handoff seamless) before the cell is connected, and
        the cell is held there for ``quiet_s`` before the waveform starts.
        ``current_range`` is as for perform_CV; ``range_period`` is the
        waveform's step-plus-pulse length in points, so an auto-ranged run
        changes range only between pulse pairs.

        Returns:
            np.ndarray: one row per waveform point, columns time (s) of each
                sample, measured potential (V), current (uA) and target
                potential (V), calibrated like perform_CV's. How the run went
                is left in ``last_batch_report``.
        """
        waveform = np.asarray(waveform, dtype=float)
        if start_V is None:
            start_V = waveform[0]
        self.write_current_hold_stop()
        # Park the DAC at the waveform's first potential while the cell is
        # still disconnected — same stale-setpoint protection as perform_CV.
        self.write_potential_calibrated(start_V)

        step_ms = int(np.round(np.ceil(1000 / sample_hz)))
        mode = self.set_current_range(current_range) if current_range is not None else None
        lead_in = self._auto_range_lead_in(range_period) if mode == 'auto' else 0
        rtn = self.write_voltage_batch(
            voltages=self._waveform_to_raw_V(
                np.concatenate((np.full(lead_in, waveform[0]), waveform))),
            delay=step_ms,
            before_start=lambda: self._connect_cell_for_batch(quiet_s),
            auto_range=(mode == 'auto'), range_period=range_period, lead_in=lead_in)
        n = rtn.shape[0]
        return np.column_stack((self._batch_time_axis(n),
                                self.calibrate_batch_potential_V(rtn[:, 1]),
                                self.calibrate_batch_current_uA(rtn[:, 0],
                                                                self.last_batch_ranges),
                                waveform[:n]))

    def perform_CDPV(self,
                     min_V: float,
                     pulse_V: float,
                     step_V: float,
                     max_V: float,
                     potential_hold_ms: int,
                     pulse_hold_ms: int,
                     voltage_hold_s: float,
                     start_V: float = None,
                     cycles: int = 1,
                     sample_hz = 1000
                     ):
        """
        performs cyclic DPV experiment from ocp and ends up with ocp
        :param min_V:
        :param pulse_V:
        :param step_V:
        :param max_V:
        :param potential_hold_ms:
        :param pulse_hold_ms:
        :param cycles:
        :param sample_hz:
        :return:
        """
        if start_V is None:
            start_V = self.read_ocp()
        stop_V = start_V

        self.write_current_hold_stop()
        self.write_potential_calibrated(start_V)
        self.write_switch(True)
        time.sleep(voltage_hold_s)

        step_ms = int(np.round(np.ceil(1000 / sample_hz)))

        def hold(v, ms):
            n = int(np.round(ms / step_ms))
            return np.zeros(n, np.float32) + v

        dpv = []

        p1 = np.linspace(start_V, min_V, int(np.round(abs(min_V - start_V) / step_V)))
        p2 = np.linspace(min_V, max_V, int(np.round(abs(max_V - min_V) / step_V)))
        p3 = np.linspace(max_V, stop_V, int(np.round(abs(max_V - stop_V) / step_V)))

        for i in range(cycles):
            for step in p1[:-1]:  # start_V to min
                dpv = np.append(dpv, hold(step, potential_hold_ms))  # potential hold at stair step
                dpv = np.append(dpv, hold(step + pulse_V,
                                          pulse_hold_ms))  # potential hold of the peak before the next stair step
            for step in p2[:-1]:  # min to max
                dpv = np.append(dpv, hold(step, potential_hold_ms))
                dpv = np.append(dpv, hold(step - pulse_V, pulse_hold_ms))

            for step in p3[:-1]:  # max to stop_V
                dpv = np.append(dpv, hold(step, potential_hold_ms))
                dpv = np.append(dpv, hold(step + pulse_V, pulse_hold_ms))

        dpv = np.append(dpv, hold(start_V, potential_hold_ms))
        rtn = self.write_voltage_batch(voltages=self._waveform_to_raw_V(dpv), delay=step_ms)
        ranges = self.last_batch_ranges
        num_points = rtn.shape[0]
        cycle_points = num_points // cycles
        duration_s = num_points/sample_hz
        time_stamps = np.linspace(voltage_hold_s, voltage_hold_s+duration_s, num_points)
        cycle_nums = np.zeros(num_points)
        for i in range(0, cycles):
            cycle_nums[i * cycle_points: (i+1) * cycle_points] = np.full(cycle_points, i)
        return np.column_stack((time_stamps,
                                self.calibrate_batch_potential_V(rtn[:, 1]),
                                self.calibrate_batch_current_uA(rtn[:, 0], ranges),
                                cycle_nums, np.zeros(num_points), dpv))

    def process_CDPV(self, data: np.ndarray, file_basename: Union[str, Path]):
        base = Path(file_basename)
        base_no_ext = base.with_suffix("")
        out_dir = base_no_ext.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        csv_raw = base_no_ext.with_name(f"{base_no_ext.stem}_CDPV_raw.csv")
        png_raw = base_no_ext.with_name(f"{base_no_ext.stem}_CDPV_proc.png")
        csv_proc = base_no_ext.with_name(f"{base_no_ext.stem}_CDPV_proc.csv")
        
        np.savetxt(csv_raw, data, delimiter=",", fmt="%.3E,%.3E,%.3E,%d,%d,%.3E",
                   header="time,voltage,current,cycle_num,exp_num,voltage_applied")
        proc_cdpv = plot_cdpv(data, png_raw, True, log_file=None)[:,1:3]
        np.savetxt(csv_proc, proc_cdpv, delimiter=",", fmt="%.3E,%.3E",
                   header="voltage,current")
        
    def process_CV(self, data: np.ndarray, file_basename: Union[str, Path]):
        base = Path(file_basename)
        base_no_ext = base.with_suffix("")
        out_dir = base_no_ext.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        csv_raw = base_no_ext.with_name(f"{base_no_ext.stem}_CV_raw.csv")
        png_raw = base_no_ext.with_name(f"{base_no_ext.stem}_CV_proc.png")
        csv_proc = base_no_ext.with_name(f"{base_no_ext.stem}_CV_proc.csv")
        np.savetxt(csv_raw, data, delimiter=",", fmt="%.3E,%.3E,%.3E,%d,%d,%.3E",
                   header="time,voltage,current,cycle_num,exp_num,voltage_applied")
        proc_cv = plot_cv(data, png_raw)
        np.savetxt(csv_proc, proc_cv, delimiter=",",fmt="%.3E,%.3E",
                   header="voltage,current")

if __name__ == "__main__":
    poten_name = "/dev/poten_2" #"/dev/potentiostat0" + sys.argv[1]
    file_name = sys.argv[2]

    ps = Potentiostat(serial_port=poten_name)
    print("ps created, connecting...")
    ps.connect()
    print("ps connected")
    ps.write_switch(0)
    ps.write_dac(channels=[DAC.CE_IN,DAC.A_REF,DAC.V_AN], voltages=[0,-5,0]) #initialize like this
    ocp = ps.read_ocp()
    print(f"ocp is, {ocp}")

    # Test CDPV
    print("CDPV")
    rtn = ps.perform_CDPV(0.0, 0.11, 0.01, 0.8, 500, 50, 5, 0)
    #rtn = ps.perform_CDPV(-0.8, 0.11, 0.01, 0.5, 500, 50, 5, 0)
    np.savetxt(f"{file_name}_CDPV_raw_poten{sys.argv[1]}.csv", rtn, delimiter=",",
               fmt="%.3E,%.3E,%.3E,%d,%d,%.3E")
    proc_cdpv = plot_cdpv(rtn, f"{file_name}_CDPV_proc_poten{sys.argv[1]}.png",
            True, f"ref_fit_log_poten{sys.argv[1]}.log")
    np.savetxt(f"{file_name}_CDPV_proc_poten{sys.argv[1]}.csv", proc_cdpv[:,0:5], delimiter=",",
               fmt="%.3E,%.3E,%.3E,%d,%d")

    # Test CV
    print("CV")
    rtn = ps.perform_CV(-0.2, 1, 5, 200, 250)
    # rtn = ps.perform_CV(-1.2, 1.2, 5, 500, 250)
    np.savetxt(f"{file_name}_CV_raw_poten{sys.argv[1]}.csv", rtn, delimiter=",",
               fmt="%.3E,%.3E,%.3E,%d,%d,%.3E")
    proc_cv = plot_cv(rtn, f"{file_name}_CV_proc_poten{sys.argv[1]}.png")
    np.savetxt(f"{file_name}_CV_proc_poten{sys.argv[1]}.csv", proc_cv, delimiter=",",
               fmt="%.3E,%.3E")

    # close poten
    ps.write_switch(0)
    ps.disconnect()

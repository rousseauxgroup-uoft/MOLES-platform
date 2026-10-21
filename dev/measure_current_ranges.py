#!/usr/bin/env python3
"""**Hardware-facing.** Measure what each current range really is, using a test resistor.

The board turns current into a voltage with an amplifier whose feedback
("sense") resistance sets the current range. The firmware's gain index 0-4
chooses which resistors are switched in, and the software converts readings
assuming 100 ohm, 1 kohm, 10 kohm, 100 kohm and 1 Mohm. Firmware before 1.4
has a switch table written for a switch chip with the opposite on/off logic
to the one fitted, so its ranges are about 93, 94, 103, 903 and 93 ohm
(bench-confirmed on board TEST, September 2026). From 1.4 on they should be
about 93 ohm, 1.0 kohm, 9.9 kohm, 91 kohm and 1 Mohm. This script measures
the real values, to check a board's firmware and to calibrate each range.

Wiring: one plain resistor as a dummy cell, with the WE lead on one end and
the RE and CE leads together on the other end. Measure the resistor with a
multimeter first and pass that value; every result scales with it.

How it measures: the board reads the cell potential and the amplifier output
through two identical circuits, so over a small potential sweep

    feedback ohms = test resistor ohms x |change in amplifier output / change in potential|

which needs no stored calibration. On each range the potential is raised in
small steps until the amplifier output is large enough to measure, so it
never sits at its limit for long. If a range saturates even at a few mV, or
barely responds at the largest allowed potential, the script says which
resistor size would measure it properly.

What it reports for each range: the measured feedback resistance and which
explanation it matches, the largest current the range can read, the noise of
a single reading, and what the range reads with no current flowing. Two CSV
files are saved: every reading, and the summary.

Suggested bench session:
  1. A resistor near 1 kohm measures ranges 0-3 (on old firmware, all five).
  2. A resistor near 100 kohm measures ranges 2-4 and any range the first
     run reports as "too sensitive".

Never run automatically. --simulate runs the same code against a simulated
board (no hardware is touched), to try the script out or see the report.

Usage:
    python dev/measure_current_ranges.py --resistor 998.7 --board TEST
    python dev/measure_current_ranges.py --resistor 99870 --board TEST --label after-fix
    python dev/measure_current_ranges.py --resistor 1000 --simulate as-built
"""
import argparse
import atexit
import csv
import math
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

# Add src to path so the script runs from a checkout without installing
current_dir = Path(__file__).resolve().parent
src_dir = current_dir.parent / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from moles import session
from moles.calibration import get_potentiostat_ports, load_calibrations
from moles.driver.ps4_ref import (ADC, DAC, Commands, Potentiostat, SerialReadTimeout,
                                  SwitchState)

# ------------------------------------------------------------------ predictions
# As-built parts (PCBWay assembly order, April/May 2026): four switched
# resistors R10-R13, each in series with one channel of the TMUX6212 switch
# chip and controlled by pins PB9-PB12 (bits 0-3 of the firmware's switch
# code), plus R14, which is always across the amplifier.
SWITCHED_OHMS = (100.0, 1e3, 1e4, 1e5)
BASE_OHMS = 1e6
# TMUX6212 on-resistance with +/-5 V supplies: 3.3 ohm typical, 6.3 max
SWITCH_ON_OHMS = 4.0

# Switch code for each gain index: the table in firmware before 1.4, and the
# corrected table from 1.4 on, which keeps index 0 (every board's calibration
# was made on it) and gives each other index one resistor.
FIRMWARE_TABLE = (0x07, 0x0B, 0x0D, 0x0E, 0x0F)
PROPOSED_TABLE = (0x07, 0x02, 0x04, 0x08, 0x00)


def feedback_ohms(code, on_when_high=True):
    """Feedback resistance for one switch code: the base resistor and every
    switched-in resistor, in parallel."""
    siemens = 1.0 / BASE_OHMS
    for bit, ohms in enumerate(SWITCHED_OHMS):
        if bool(code >> bit & 1) == on_when_high:
            siemens += 1.0 / (ohms + SWITCH_ON_OHMS)
    return 1.0 / siemens


# Each explanation's feedback resistance for gain indices 0-4
MODELS = {
    "as-built": tuple(feedback_ohms(c) for c in FIRMWARE_TABLE),
    "on-when-low": tuple(feedback_ohms(c, on_when_high=False) for c in FIRMWARE_TABLE),
    "software": (100.0, 1e3, 1e4, 1e5, 1e6),
    "proposed-fix": tuple(feedback_ohms(c) for c in PROPOSED_TABLE),
}
MODEL_DESCRIPTIONS = {
    "as-built": "firmware before 1.4, whose switch table assumes the opposite chip logic",
    "on-when-low": "a switch chip that turns on when its pin is LOW, as the firmware table assumes",
    "software": "the decade values the ranges are named after (100 ohm to 1 Mohm)",
    "proposed-fix": "the corrected switch table (firmware 1.4 onward)",
}
VERDICTS = {
    "as-built": ("Every measured range matches firmware with the old switch table, "
                 "written for a switch chip with the opposite logic. Flash firmware 1.4 "
                 "or later (firmware/prebuilt/) to get the real ranges."),
    "on-when-low": ("The ranges match a switch chip that turns on when its pin is LOW. "
                    "That contradicts the parts list (TMUX6212), so check which chip "
                    "is fitted at U1 on this board."),
    "software": ("The ranges match their plain decade names (100 ohm to 1 Mohm), not "
                 "either switch table: send the summary file for a look."),
    "proposed-fix": "The ranges match the corrected switch table (firmware 1.4 onward).",
}
# Resistor tolerance, switch resistance and the multimeter all fit within this
MATCH_TOLERANCE = 0.05
# Diagnostics capability flag of firmware whose ranges 1-4 are the real ones
FEATURE_TRUE_RANGES = 1 << 3

# ------------------------------------------------------------------ measurement
# The amplifier output (driver scale, volts) reaches the ADC's limit near
# +/-4.95 V; readings beyond this count as saturated.
AMP_LIMIT_V = 4.5
# Output the sweep aims for at its extremes, leaving room for offsets and noise
AMP_TARGET_V = 2.5
# Output the full-scale current figure is quoted at
FULL_SCALE_V = 4.9
# Smallest and first potential steps tried when looking for a measurable signal
MIN_PROBE_V = 0.005
FIRST_PROBE_V = 0.01
# Below this sweep size the DAC's 2.4 mV levels make the sweep too coarse
MIN_SWEEP_V = 0.02
# A sweep that moves the amplifier less than this share of its reach, or
# leaves more than this relative uncertainty, gives only an approximate value
WEAK_SPAN_FRACTION = 0.02
WEAK_UNCERTAINTY = 0.005
# Largest resistor for which range 0 must show current (wiring check)
WIRING_CHECK_MAX_OHMS = 20e3


class WiringError(RuntimeError):
    """The board's readings show the resistor is not wired as a dummy cell."""


@dataclass
class Limits:
    max_volts: float = 2.0
    max_amps: float = 0.010


@dataclass
class Timing:
    settle_s: float = 0.2        # after a potential change
    range_settle_s: float = 0.5  # after a range change or connecting the cell


class PotentialMap:
    """Converts a wanted cell potential into the raw command that produces it,
    using readings from this board, so no stored calibration is needed."""

    def __init__(self, commands, measured):
        self.slope, self.offset = np.polyfit(commands, measured, 1)

    def command_for(self, potential_V):
        return (potential_V - self.offset) / self.slope


class SampleLog:
    """Every reading taken, for the samples CSV."""

    HEADER = ("time_s", "phase", "range", "reported_range", "target_V", "command_V",
              "potential_V", "amp_out_V")

    def __init__(self):
        self.rows = []
        self._t0 = time.monotonic()

    def add(self, phase, index, target_V, command_V, P, W, idx):
        t = round(time.monotonic() - self._t0, 3)
        for p, w, g in zip(P, W, idx):
            self.rows.append((t, phase, index, int(g),
                              "" if target_V is None else round(float(target_V), 6),
                              "" if command_V is None else round(float(command_V), 6),
                              float(p), float(w)))

    def save(self, path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(self.HEADER)
            writer.writerows(self.rows)


def select_range(ps, index):
    # write_gain skips an index it believes is already set, and the board's
    # own state is what matters here
    ps.Resistor = None
    ps.write_gain(index)


def read_block(ps, n):
    """n paired readings: the cell potential, and the amplifier output on the
    same volts scale, with the range the board reports for each."""
    P = np.empty(n)
    W = np.empty(n)
    idx = np.empty(n, dtype=int)
    for k in range(n):
        for attempt in range(3):
            try:
                potential, current = ps.read_ADC_gain([ADC.RE_OUT, ADC.WE_OUT])
                break
            except SerialReadTimeout:
                if attempt == 2:
                    raise
        # The driver divided the amplifier output by the resistance it
        # assumes for the reported range; undo that to get the output itself
        reported = ps.Resistor
        idx[k] = reported
        P[k] = potential
        W[k] = -float(current) * ps.res_vals[reported]
    return P, W, idx


def measure_open_circuit(ps, ranges, n, timing, log):
    """What each range reads with the cell disconnected, where no current can
    flow. The potential read here is the one at which the resistor carries
    no current once connected."""
    ps.write_switch(SwitchState.Off)
    out = {}
    potentials = []
    for r in ranges:
        select_range(ps, r)
        time.sleep(timing.range_settle_s)
        P, W, idx = read_block(ps, n)
        log.add("open-circuit", r, None, None, P, W, idx)
        potentials.append(P)
        out[r] = {"open_offset_V": float(W.mean()),
                  "open_noise_V": float(W.std(ddof=1)),
                  "open_reported_range": int(idx[-1])}
    return out, float(np.concatenate(potentials).mean())


def map_potential(ps, zero_V, resistor_ohms, limits, n, timing, log):
    """Relate the raw potential command to the measured potential, on range 0
    with the cell connected, and check the resistor is wired as a dummy cell."""
    select_range(ps, 0)
    # Raw commands land within a few mV of the measured potential, so this
    # connects the cell with almost no current flowing
    ps.write_potential(zero_V)
    ps.write_switch(SwitchState.On)
    time.sleep(timing.range_settle_s)

    amp = min(0.2, limits.max_volts, 0.5 * limits.max_amps * resistor_ohms)
    commands = tuple(zero_V + c for c in (0.0, amp, -amp, 0.0))
    measured, amp_out = [], []
    for c in commands:
        ps.write_potential(c)
        time.sleep(timing.settle_s)
        P, W, idx = read_block(ps, n)
        log.add("potential-map", 0, None, c, P, W, idx)
        measured.append(P.mean())
        amp_out.append(W.mean())

    pmap = PotentialMap(commands, measured)
    if not 0.9 < pmap.slope < 1.1:
        raise WiringError(
            f"The measured potential does not follow the command (it moved "
            f"{pmap.slope:.2f} V per V). Check that the RE and CE leads are both "
            f"on one end of the resistor.")

    # Range 0 is about 100 ohm or less on every board, so a resistor up to
    # WIRING_CHECK_MAX_OHMS must move its output measurably
    if resistor_ohms <= WIRING_CHECK_MAX_OHMS:
        expected = 2 * amp * 50.0 / resistor_ohms
        if abs(amp_out[1] - amp_out[2]) < 0.25 * expected:
            raise WiringError(
                "Almost no current flows through the resistor. Check that the WE "
                "lead is on the other end of it, and that --resistor is right.")
    return pmap


def measure_range(ps, index, pmap, zero_V, resistor_ohms, limits, n, timing, log):
    """Measure one range's feedback resistance with a sweep around zero current.

    zero_V is the potential at which the resistor carries no current; every
    range change happens there, so a sensitive range is never switched in
    with current already flowing."""
    res = {"range": index, "status": "ok", "note": ""}

    def point(target, phase):
        command = pmap.command_for(target)
        ps.write_potential(command)
        time.sleep(timing.settle_s)
        P, W, idx = read_block(ps, n)
        log.add(phase, index, target, command, P, W, idx)
        return P, W, idx

    ps.write_potential(pmap.command_for(zero_V))
    time.sleep(timing.settle_s)
    select_range(ps, index)
    time.sleep(timing.range_settle_s)

    P0, W0, idx0 = point(zero_V, "probe")
    if np.any(idx0 != index):
        res.update(status="not-selected",
                   note=f"the board reports range {int(idx0[-1])} instead")
        return res
    w0 = float(W0.mean())
    if np.max(np.abs(W0)) > AMP_LIMIT_V:
        res.update(status="too-sensitive",
                   note="the amplifier is at its limit even with no current; "
                        "use a much larger resistor")
        return res

    # Step the potential up until the amplifier output moves measurably
    largest = min(limits.max_volts, limits.max_amps * resistor_ohms)
    step = min(FIRST_PROBE_V, largest)
    gain = None
    saturated = False
    for _ in range(12):
        P1, W1, _ = point(zero_V + step, "probe")
        saturated = bool(np.max(np.abs(W1)) > AMP_LIMIT_V)
        if saturated:
            if step <= MIN_PROBE_V:
                break
            step = max(MIN_PROBE_V, step / 4)
            continue
        dP = float(P1.mean() - P0.mean())
        dW = float(W1.mean() - w0)
        resolvable = 5 * (W0.std(ddof=1) + W1.std(ddof=1)) / math.sqrt(n) + 20e-6
        if abs(dW) > resolvable or step >= largest:
            gain = abs(dW / dP) if abs(dP) > 1e-4 else 0.0
            break
        step = min(largest, step * 8)

    if gain is None and not saturated:
        res.update(status="failed", note="the response to small steps was inconsistent")
        return res
    if gain is None:
        least = (AMP_LIMIT_V - abs(w0)) / step * resistor_ohms
        res.update(status="too-sensitive",
                   note=f"saturates even at {step * 1000:.0f} mV; measure it with a "
                        f"resistor of {fmt_ohms(standard_resistor(0.8 * least))} or more")
        return res

    amp = largest
    if gain > 0:
        amp = min(amp, (AMP_TARGET_V - abs(w0)) / gain)
    if amp < MIN_SWEEP_V:
        res.update(status="too-sensitive",
                   note=f"roughly {fmt_ohms(gain * resistor_ohms)}, too sensitive to sweep "
                        f"with this resistor; measure it with about "
                        f"{fmt_ohms(standard_resistor(0.8 * gain * resistor_ohms))}")
        return res

    targets = (0.0, amp / 2, amp, -amp / 2, -amp, 0.0)
    groups = []
    for t in targets:
        P, W, idx = point(zero_V + t, "sweep")
        keep = (np.abs(W) <= AMP_LIMIT_V) & (idx == index)
        groups.append((P[keep], W[keep]))
    P_all = np.concatenate([g[0] for g in groups])
    W_all = np.concatenate([g[1] for g in groups])
    if P_all.size < 3 * n or np.ptp(P_all) < MIN_SWEEP_V:
        res.update(status="failed", note="too few usable readings in the sweep")
        return res

    slope, intercept = np.polyfit(P_all, W_all, 1)
    fit = intercept + slope * P_all
    dof = max(P_all.size - 2, 1)
    slope_se = math.sqrt(float(np.sum((W_all - fit) ** 2)) / dof
                         / float(np.sum((P_all - P_all.mean()) ** 2)))
    measured = abs(slope) * resistor_ohms

    # Scatter within each point is the reading noise; the offset of each
    # point's mean from the line is the nonlinearity
    within = [(len(w) - 1, np.var(w, ddof=1)) for _, w in groups if len(w) > 1]
    noise_V = math.sqrt(sum(d * v for d, v in within) / max(sum(d for d, _ in within), 1))
    span_V = abs(slope) * (np.max(P_all) - np.min(P_all))
    worst = max(abs(float(np.mean(w - (intercept + slope * p)))) for p, w in groups if len(w))
    zeros = [float(np.mean(w)) for (p, w), t in zip(groups, targets) if t == 0.0 and len(w)]

    res.update(
        measured_ohms=measured,
        rel_uncertainty=slope_se / abs(slope),
        nonlinearity_pct=100.0 * worst / span_V if span_V else float("nan"),
        span_fraction=0.5 * span_V / FULL_SCALE_V,
        amplitude_V=amp,
        full_scale_A=FULL_SCALE_V / measured,
        noise_A=noise_V / measured,
        drift_V=zeros[-1] - zeros[0] if len(zeros) == 2 else float("nan"),
        slope_sign=int(np.sign(slope)),
    )
    if res["span_fraction"] < WEAK_SPAN_FRACTION or res["rel_uncertainty"] > WEAK_UNCERTAINTY:
        better = standard_resistor(max(0.8 * measured, limits.max_volts / limits.max_amps))
        res.update(status="weak-signal",
                   note=f"approximate; a resistor near {fmt_ohms(better)} measures it better")
    return res


def finish_safely(ps, pmap, zero_V):
    """Leave the cell disconnected, whatever happened before."""
    try:
        ps.write_potential(pmap.command_for(zero_V) if pmap is not None else 0.0)
    except Exception:
        pass
    try:
        ps.write_switch(SwitchState.Off)
    except Exception as e:
        print(f"\n  !! Could not confirm the cell switch opened ({e}). Unplug the "
              f"resistor leads before touching anything.")
    try:
        select_range(ps, 0)
    except Exception:
        pass


def run_measurement(ps, ranges, resistor_ohms, limits, n, timing, log, progress=None):
    """Open-circuit readings, potential map, then a sweep on every range.
    Always leaves the cell switch open."""
    pmap = None
    zero_V = 0.0
    try:
        ps.write_current_hold_stop()
        ps.write_auto_gain(False)
        # The reference levels every app run uses; the offsets depend on them
        ps.write_dac(channels=[DAC.A_REF, DAC.V_AN], voltages=[-5, 0])
        open_circuit, zero_V = measure_open_circuit(ps, ranges, n, timing, log)
        pmap = map_potential(ps, zero_V, resistor_ohms, limits, n, timing, log)
        results = []
        for r in ranges:
            res = measure_range(ps, r, pmap, zero_V, resistor_ohms, limits, n, timing, log)
            res.update(open_circuit[r])
            add_comparisons(res)
            results.append(res)
            if progress:
                progress(res)
        return results, pmap, zero_V
    finally:
        finish_safely(ps, pmap, zero_V)


# ------------------------------------------------------------------ analysis
def add_comparisons(res):
    """Deviation from each explanation, the best match, and the factor the
    decade name is off by."""
    index = res["range"]
    res["nominal_ohms"] = MODELS["software"][index]
    measured = res.get("measured_ohms")
    if not measured:
        return
    res["deviation"] = {name: measured / values[index] - 1.0 for name, values in MODELS.items()}
    # A weak measurement matches within its own uncertainty as well, so it
    # cannot overturn what the precise ranges show
    window = MATCH_TOLERANCE + 3.0 * res["rel_uncertainty"]
    res["matching_models"] = [name for name, dev in res["deviation"].items()
                              if abs(dev) <= window]
    best = min(res["deviation"], key=lambda name: abs(math.log1p(res["deviation"][name])))
    res["best_model"] = best if best in res["matching_models"] else ""
    res["correction_factor"] = res["nominal_ohms"] / measured
    res["zero_reading_A"] = -res["open_offset_V"] / measured


def consistent_models(results):
    """Explanations that fit every measured range within tolerance."""
    measured = [r for r in results if r.get("measured_ohms")]
    if not measured:
        return []
    return [name for name in MODELS
            if all(name in r["matching_models"] for r in measured)]


E12 = (1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2)


def standard_resistor(ohms):
    """Nearest common (E12) resistor value, for suggestions."""
    decade = 10.0 ** math.floor(math.log10(ohms))
    candidates = [v * decade for v in E12] + [10.0 * decade]
    return min(candidates, key=lambda v: abs(math.log(v / ohms)))


def fmt_ohms(x, digits=3):
    if x >= 1e6:
        return f"{x / 1e6:.{digits}g} Mohm"
    if x >= 1e3:
        return f"{x / 1e3:.{digits}g} kohm"
    return f"{x:.{digits}g} ohm"


def fmt_amps(x):
    for scale, unit in ((1e-3, "mA"), (1e-6, "uA"), (1e-9, "nA")):
        if abs(x) >= scale:
            return f"{x / scale:.3g} {unit}"
    return f"{x / 1e-12:.3g} pA"


def print_report(board, resistor_ohms, label, results, cal):
    print(f"\nCurrent ranges, board {board}, {fmt_ohms(resistor_ohms, 5)} test resistor"
          + (f" ({label})" if label else ""))
    print(f"\n{'range':>5}  {'nominal':>9}  {'measured':>11}  {'matches':<22}"
          f"{'full scale':>11}  {'noise':>9}  {'zero reads':>10}")
    fits = consistent_models(results)
    for r in results:
        head = f"{r['range']:>5}  {fmt_ohms(r['nominal_ohms']):>9}  "
        if not r.get("measured_ohms"):
            print(head + f"{'-':>11}  {r['status']}: {r['note']}")
            continue
        # Several explanations can predict the same value for one range; name
        # the one that fits the whole board when there is one
        shown = next((m for m in fits if m in r["matching_models"]), r["best_model"])
        match = (f"{shown} ({100 * r['deviation'][shown]:+.1f}%)" if shown else "none")
        print(head + f"{fmt_ohms(r['measured_ohms'], 4):>11}  {match:<22}"
              f"{'+/-' + fmt_amps(r['full_scale_A']):>11}  {fmt_amps(r['noise_A']):>9}"
              f"  {fmt_amps(r['zero_reading_A']):>10}")
        if r["status"] != "ok":
            print(f"{'':>7}{r['status']}: {r['note']}")

    print("\n  nominal: the decade the range is named after.  full scale: largest current the "
          "range can read.\n  noise: spread of one reading.  zero reads: the reading "
          "with no current flowing.")

    print()
    if len(fits) == 1:
        print(VERDICTS[fits[0]])
    elif fits:
        print("Consistent with more than one explanation: "
              + "; ".join(MODEL_DESCRIPTIONS[f] for f in fits)
              + ". Measure more ranges to tell them apart.")
    elif any(r.get("measured_ohms") for r in results):
        print("No single explanation fits every range. Send the summary file for a look.")
    unmeasured = [str(r["range"]) for r in results if not r.get("measured_ohms")]
    if unmeasured:
        print(f"Not measured with this resistor: range {', '.join(unmeasured)} "
              f"(see the notes above).")

    r0 = next((r for r in results if r["range"] == 0 and r.get("measured_ohms")), None)
    if r0 is not None and cal.get("read_slope"):
        # The onboarding fit converts readings the driver scaled for 100 ohm,
        # through an output scaling 0.19% steeper than the translator's 1/3
        implied = Potentiostat.res_vals[0] / (cal["read_slope"] * 1.00192)
        dev = 100.0 * (r0["measured_ohms"] / implied - 1.0)
        print(f"\nRange 0 check: the onboarding calibration (read slope "
              f"{cal['read_slope']:.4f}) implies {implied:.1f} ohm; measured "
              f"{r0['measured_ohms']:.1f} ohm ({dev:+.1f}%)." + check_note(dev))


def check_note(dev_pct):
    """What a range-0 disagreement with the onboarding calibration suggests."""
    if abs(dev_pct) <= 2.0:
        return ""
    if abs(dev_pct) <= 20.0:
        return (" A gap this size points at the ADC's absolute scale or the "
                "multimeter reading of the resistor.")
    return ("\n  !! Far too large for a calibration difference. Check the --resistor "
            "value first (a factor of 10 is easy to slip), and that nothing else "
            "touches the resistor.")


def save_summary(path, board, resistor_ohms, label, results, features):
    fields = ("board", "label", "resistor_ohms", "firmware_features", "range", "status",
              "note", "nominal_ohms", "measured_ohms", "rel_uncertainty",
              "nonlinearity_pct", "span_fraction", "amplitude_V", "full_scale_A",
              "noise_A", "open_offset_V", "open_noise_V", "zero_reading_A", "drift_V",
              "best_model", "matching_models", "correction_factor",
              "open_reported_range", "slope_sign")
    fields += tuple(f"dev_{name}" for name in MODELS)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for r in results:
            row = dict(r, board=board, label=label, resistor_ohms=resistor_ohms,
                       firmware_features="" if features is None else features,
                       matching_models=";".join(r.get("matching_models", [])))
            for name, dev in r.get("deviation", {}).items():
                row[f"dev_{name}"] = dev
            writer.writerow(row)


# ------------------------------------------------------------------ simulated board
class SimulatedBoard:
    """A board with a resistor as its dummy cell, standing in for the USB serial port.

    Models what the measurement relies on: the potential loop, the current
    amplifier with a feedback resistance per gain index, the two identical
    circuits that bring the potential and the amplifier output to the ADC,
    the ADC noise seen on the real boards, and the DAC's 12-bit levels. The
    amplifier's zero-current reference sits 50 mV above ground, the likely
    source of the boards' current-reading offset.
    """

    REF_OFFSET_V = 0.05
    NOISE_V = 63e-6         # per reading at the ADC: ~2 uA rms on the ~93 ohm range
    COMPLIANCE_V = 4.6
    CURRENT_LIMIT_A = 0.033
    RAIL_V = 4.95
    OK = b"OK\r\n"

    def __init__(self, resistor_ohms, range_ohms, seed=1):
        self.resistor_ohms = float(resistor_ohms)
        self.range_ohms = [float(r) for r in range_ohms]
        self.rng = np.random.default_rng(seed)
        self.dac = [1.65, 0.0, 1.65]    # CE_IN, A_REF, V_AN, as the ADC-side volts
        self.switch = 0
        self.gain = 0
        self.auto_gain = 1
        self.re_connected = True
        self.fail_after_reads = None    # stop answering reads after this many
        self.reads = 0
        self.timeout = 10
        self.is_open = True
        self.out = bytearray()

    # -- serial.Serial surface used by the driver
    @property
    def in_waiting(self):
        return len(self.out)

    def flush(self):
        pass

    def close(self):
        self.is_open = False

    def reset_input_buffer(self):
        self.out.clear()

    def read(self, n):
        chunk = bytes(self.out[:n])
        del self.out[:n]
        return chunk

    def readline(self):
        end = self.out.find(b"\n")
        return self.read(len(self.out) if end < 0 else end + 1)

    def write(self, msg):
        msg = bytes(msg)
        cmd, data = msg[1], msg[2:]
        if cmd == Commands.WRITE_DAC:
            n = len(data) // 5
            values = np.frombuffer(data[n:n + 4 * n], np.float32)
            for ch, v in zip(data[:n], values):
                if ch < len(self.dac):
                    self.dac[ch] = round(float(v) / 3.3 * 4095) * 3.3 / 4095
            self.out += self.OK
        elif cmd == Commands.WRITE_SWITCH:
            self.switch = 1 if data[0] == 1 else 0
            self.out += self.OK
        elif cmd == Commands.WRITE_GAIN:
            if data[0] < len(self.range_ohms):
                self.gain = data[0]
            self.out += self.OK
        elif cmd == Commands.WRITE_AUTO_GAIN:
            self.auto_gain = data[0]
            self.out += self.OK
        elif cmd in (Commands.STOP_CURRENT_HOLD, Commands.WRITE_SAMPLE_COUNT):
            self.out += self.OK
        elif cmd in (Commands.READ_ADC, Commands.READ_ANALOG_GAIN):
            self.reads += 1
            if self.fail_after_reads is not None and self.reads > self.fail_after_reads:
                return len(msg)
            self.out += np.asarray(self._adc(list(data)), np.float32).tobytes()
            if cmd == Commands.READ_ANALOG_GAIN:
                self.out += bytes([self.gain])
        elif cmd == Commands.READ_SWITCH:
            self.out += bytes([self.switch])
        elif cmd == Commands.READ_GAIN:
            self.out += bytes([self.gain])
        elif cmd == Commands.READ_AUTO_GAIN:
            self.out += bytes([self.auto_gain])
        # Anything else (e.g. the diagnostics query) gets no answer, like old firmware
        return len(msg)

    # -- analog model
    def _analog(self):
        v_mid = self.dac[2]
        node = self.REF_OFFSET_V          # the working electrode, held by the amplifier
        if self.switch and self.re_connected:
            v_re = float(np.clip(-3.0 * (self.dac[0] - v_mid),
                                 -self.COMPLIANCE_V, self.COMPLIANCE_V))
            current = (node - v_re) / self.resistor_ohms
            if abs(current) > self.CURRENT_LIMIT_A:
                current = math.copysign(self.CURRENT_LIMIT_A, current)
                v_re = node - current * self.resistor_ohms
        elif self.re_connected:
            current, v_re = 0.0, node      # through the resistor, with nothing flowing
        else:
            current, v_re = 0.0, 0.0
        v_amp = float(np.clip(node + current * self.range_ohms[self.gain],
                              -self.RAIL_V, self.RAIL_V))
        return v_mid, v_re, v_amp

    def _adc(self, channels):
        v_mid, v_re, v_amp = self._analog()
        step = 3.3 / 65535 / 10
        out = []
        for ch in channels:
            if ch in (ADC.WE_OUT, ADC.RE_OUT):
                v = v_mid - (v_amp if ch == ADC.WE_OUT else v_re) / 3.0
                v = min(max(v + self.rng.normal(0.0, self.NOISE_V), 0.0), 3.3)
                out.append(round(v / step) * step)
            else:
                out.append(1.21 if ch == ADC.VREF else 0.0)
        return out


def simulated_potentiostat(model, resistor_ohms, seed=1):
    ps = Potentiostat(serial_port="SIMULATED")
    ps.serial = SimulatedBoard(resistor_ohms, MODELS[model], seed=seed)
    return ps


# ------------------------------------------------------------------ main
def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--resistor", type=float, required=True,
                   help="Test resistor in ohms, as measured with a multimeter")
    p.add_argument("--board", default=None,
                   help="Board name from the registry (A, TEST, ...). "
                        "Defaults to the only connected board.")
    p.add_argument("--port", default=None, help="Serial port, bypassing the registry")
    p.add_argument("--ranges", type=int, nargs="+", default=[0, 1, 2, 3, 4],
                   choices=range(5), help="Gain indices to measure (default: all)")
    p.add_argument("--max-volts", type=float, default=2.0,
                   help="Largest potential across the resistor (default: 2.0 V)")
    p.add_argument("--max-ma", type=float, default=10.0,
                   help="Largest current through the resistor (default: 10 mA)")
    p.add_argument("--readings", type=int, default=40,
                   help="Readings per point (default: 40)")
    p.add_argument("--label", default="",
                   help="Short note saved with the results, e.g. before-fix")
    p.add_argument("--outdir", default=str(Path.home() / "moles_data" / "range_measurements"),
                   help="Where to save the CSV files")
    p.add_argument("--simulate", choices=sorted(MODELS), default=None,
                   help="Run against a simulated board built to one explanation; "
                        "no hardware is touched")
    args = p.parse_args(argv)
    if args.resistor <= 0:
        p.error("--resistor must be positive")
    if not 0 < args.max_volts <= 4.0:
        p.error("--max-volts must be between 0 and 4 V (the board saturates near 4.6 V)")
    if not 0 < args.max_ma <= 30:
        p.error("--max-ma must be between 0 and 30 mA")
    if args.readings < 5:
        p.error("--readings must be at least 5")
    return args


def resolve_board(args):
    """Pick a serial port and its calibration, from the registry or --port."""
    calibrations = load_calibrations()
    if args.port:
        return args.port, args.board or "board", calibrations.get(args.board or "", {})
    ports = get_potentiostat_ports()
    if not ports:
        raise SystemExit("No potentiostats found. Connect one, or pass --port.")
    if args.board:
        if args.board not in ports:
            raise SystemExit(f"Board {args.board} not found. Connected: "
                             f"{', '.join(sorted(ports))}")
        name = args.board
    elif len(ports) == 1:
        name = next(iter(ports))
    else:
        raise SystemExit(f"Several boards connected ({', '.join(sorted(ports))}). "
                         f"Pick one with --board.")
    return ports[name], name, calibrations.get(name, {})


def apply_calibration(ps, cal):
    """Load the board's stored calibration, which only the rest-potential check uses."""
    if cal:
        ps.set_calibration_params(cal["slope"], cal["intercept"],
                                  cal.get("read_slope"), cal.get("read_intercept"))
        if cal.get("v_slope") is not None:
            ps.set_voltage_calibration_params(cal["v_slope"], cal["v_intercept"],
                                              cal.get("v_read_slope"),
                                              cal.get("v_read_intercept"))


def firmware_features(ps):
    """The firmware's capability flags, or None if it does not report them."""
    old = getattr(ps.serial, "timeout", None)
    try:
        ps.serial.timeout = 0.5
        return ps.read_diagnostics().get("features")
    except Exception:
        return None
    finally:
        ps.serial.timeout = old


def main(argv=None):
    args = parse_args(argv)
    limits = Limits(max_volts=args.max_volts, max_amps=args.max_ma / 1000.0)

    if args.simulate:
        board, cal = f"SIM-{args.simulate}", {}
        ps = simulated_potentiostat(args.simulate, args.resistor)
        timing = Timing(0.0, 0.0)
        print(f"Simulated board ({MODEL_DESCRIPTIONS[args.simulate]}); no hardware is used.")
    else:
        port, board, cal = resolve_board(args)
        timing = Timing()
        ps = Potentiostat(serial_port=port)
        apply_calibration(ps, cal)
        print(f"Board {board} on {port}")
        print(f"Test resistor: {fmt_ohms(args.resistor, 5)} (as measured with a multimeter)")
        print("Wiring: WE lead on one end of the resistor; RE and CE leads together on "
              "the other end. Nothing else connected.")
        try:
            input("Press Enter to start, or Ctrl-C to cancel... ")
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled before connecting.")
            return 1
        try:
            session.connect_with_retry(ps, board)
        except ConnectionError as e:
            raise SystemExit(f"{e}\nIf a MOLES app is using this board, close it first.")
        # Lets the interpreter's exit hook open the switch if this script dies
        session.ACTIVE_POTENTIOSTATS[board] = ps

    outdir = Path(args.outdir)
    stamp = datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
    label = re.sub(r"[^A-Za-z0-9_-]+", "-", args.label).strip("-")
    tag = "_".join(filter(None, ["ranges", board, label, stamp]))
    log = SampleLog()
    results = None
    try:
        ocp = session.initialize_potentiostat(ps, board)
        print(f"Rest potential: {float(ocp):+.3f} V"
              + ("" if abs(ocp) < 0.1 else
                 "  <-- a resistor should rest near 0 V; check the wiring"))
        features = firmware_features(ps)
        if features is not None:
            print("Firmware switch table: "
                  + ("corrected (1.4 or later)" if features & FEATURE_TRUE_RANGES else "old"))

        def progress(r):
            value = fmt_ohms(r["measured_ohms"], 4) if r.get("measured_ohms") else "-"
            print(f"  range {r['range']}: {value}"
                  + ("" if r["status"] == "ok" else f" ({r['status']})"))

        print("Measuring (about a minute)...")
        results, pmap, zero_V = run_measurement(ps, args.ranges, args.resistor, limits,
                                                args.readings, timing, log, progress)
        print("Cell switch open.")
    except WiringError as e:
        print(f"\nStopped: {e}\nCell switch open.")
    except KeyboardInterrupt:
        print("\nCancelled. Cell switch open.")
    finally:
        if not args.simulate:
            # A second Ctrl-C can interrupt the first attempt to open the switch
            try:
                ps.write_switch(SwitchState.Off)
            except Exception:
                pass
            session.ACTIVE_POTENTIOSTATS.pop(board, None)
            try:
                ps.disconnect()
            except Exception:
                pass
        if log.rows:
            outdir.mkdir(parents=True, exist_ok=True)
            log.save(outdir / f"{tag}_samples.csv")

    if not results:
        if log.rows:
            print(f"Readings so far saved to {outdir / (tag + '_samples.csv')}")
        return 1

    print_report(board, args.resistor, args.label, results, cal)
    save_summary(outdir / f"{tag}_summary.csv", board, args.resistor, args.label,
                 results, features)
    print(f"\nSaved to {outdir}:\n  {tag}_summary.csv\n  {tag}_samples.csv")
    return 0


if __name__ == "__main__":
    code = main()
    # The session module's exit hook announces an emergency cleanup even with
    # nothing connected; by now the cell is disconnected and the port closed
    if not session.ACTIVE_POTENTIOSTATS:
        atexit.unregister(session.emergency_cleanup)
    sys.exit(code)

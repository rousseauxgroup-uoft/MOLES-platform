"""Find peaks and shoulders inside a box drawn over one voltammogram.

The user selects a trace and drags a box over part of it. The points of that
trace inside the box (as displayed: offsets applied, within the data range)
are split into runs of consecutive points, and each run is analysed on its
own, so a box catching both sweeps of a CV, or several cycles, reports each
separately.

Each sweep branch is smoothed with a Savitzky-Golay filter, which also gives
the first and second derivatives, taken along point order (the potential only
moves one way within a branch). Smoothing the whole branch rather than only
the boxed points means the box edges do not distort the result.

* **Peaks** are sign changes of the first derivative: + to - is a maximum,
  - to + a minimum. The reported point is the raw data point nearest the
  smoothed extremum. Valleys — the extrema between two peaks of the sweep's
  own direction, such as a current maximum between two reductions on a
  negative-going sweep — are only reported when ``PeakSettings.report_valleys``
  asks for them.
* **Shoulders** are hidden peaks on the flank of a bigger feature, where the
  slope flattens without changing sign and then recovers. The slope at the
  flat spot must fall well below the steepest slope on each side, and between
  those two steep points the curve must first run ahead of a straight line
  and then fall behind it (an S shape), each part by at least half the
  prominence threshold. That rejects the foot of a peak and the knee of a
  plateau, which bend only one way. The reported point
  is the second-derivative extremum of a hidden peak of the sweep's direction
  (oxidations, maxima, on positive-going sweeps; reductions, minima, on
  negative-going ones, IUPAC).

Both use one prominence threshold in µA. By default it is set from the data:
a multiple of the noise left after smoothing, measured on the same sweep.

Runs are also split where the sweep turns around, so a switching potential is
never reported as a peak, and nothing is reported within one smoothing window
of a sweep end or of the data range's ends: there the filter runs out of data
on one side, and the current is still settling from the reversal.
"""

import bisect
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy.signal import peak_prominences, savgol_coeffs, savgol_filter

from .model import EXTREMUM_MAX, EXTREMUM_MIN, LABEL_PEAK, LABEL_SHOULDER, PeakSettings

# Fewest points a cubic Savitzky-Golay window may span and still smooth.
MIN_WINDOW_POINTS = 7

# How far either side of a flat spot to look for the steep flanks that make it
# a shoulder, in smoothing windows.
_SHOULDER_REACH_WINDOWS = 3

# A shoulder's flat spot must slope at most this fraction of its gentler steep
# flank: the slope has to clearly dip towards zero, not just wobble.
_SHOULDER_MAX_SLOPE_RATIO = 0.6


@dataclass(frozen=True)
class Feature:
    """One detected peak or shoulder. ``index`` is the raw data point reported."""

    index: int
    kind: str          # LABEL_PEAK or LABEL_SHOULDER
    extremum: str      # EXTREMUM_MAX or EXTREMUM_MIN (for a shoulder, its hidden peak's)
    prominence_uA: float


@dataclass
class RunReport:
    """How one run of boxed points was analysed, for the status line and tests."""

    first: int
    last: int
    direction: int
    window_points: int = 0
    threshold_uA: float = 0.0
    noise_uA: float = 0.0
    note: str = ""


@dataclass
class Detection:
    features: List[Feature] = field(default_factory=list)
    runs: List[RunReport] = field(default_factory=list)


@dataclass
class _Branch:
    """One smoothed sweep branch (clipped to the data range)."""

    first: int
    last: int
    window: int
    smooth: np.ndarray
    d1: np.ndarray
    d2: np.ndarray
    noise_uA: float = 0.0     # noise left after smoothing
    threshold_uA: float = 0.0
    # Every extremum over the whole branch that passes the threshold, valleys
    # included: shoulders are looked for between them.
    extrema: List[Feature] = field(default_factory=list)
    note: str = ""


def detect_in_box(potential, current, branch, direction, box,
                  settings: Optional[PeakSettings] = None,
                  index_range: Optional[Tuple[int, int]] = None,
                  offsets: Tuple[float, float] = (0.0, 0.0)) -> Detection:
    """Detect peaks and shoulders of one trace inside ``box``.

    ``box`` is ``(x_a, x_b, y_a, y_b)`` in displayed coordinates (V, µA), in
    any order. ``index_range`` is the trace's inclusive data range and
    ``offsets`` its ``(x_offset, y_offset)``. ``branch`` and ``direction`` come
    from the loaded dataset.
    """
    settings = settings or PeakSettings()
    potential = np.asarray(potential, dtype=float)
    current = np.asarray(current, dtype=float)
    branch = np.asarray(branch)
    direction = np.asarray(direction)
    n = len(potential)
    lo, hi = index_range if index_range is not None else (0, n - 1)
    lo, hi = max(0, int(lo)), min(n - 1, int(hi))

    inside = _points_in_box(potential, current, box, lo, hi, offsets)
    runs = _split_runs(inside, potential, branch, settings.smoothing_mV / 1000.0 / 2)

    detection = Detection()
    smoothed: Dict[int, _Branch] = {}
    for first, last in runs:
        b = int(branch[first])
        if b not in smoothed:
            smoothed[b] = _smooth_branch(potential, current, branch, b, lo, hi, settings)
        sb = smoothed[b]
        sweep = int(direction[first])
        report = RunReport(first, last, sweep, sb.window,
                           sb.threshold_uA, sb.noise_uA, sb.note)
        detection.runs.append(report)
        if sb.window == 0:
            continue

        valid_first = max(first, sb.first + sb.window)
        valid_last = min(last, sb.last - sb.window)
        if valid_first > valid_last:
            report.note = report.note or "too close to the end of the sweep"
            continue
        peaks = [f for f in sb.extrema
                 if valid_first <= f.index <= valid_last
                 and (settings.report_valleys or not _is_valley(f, sweep))]
        shoulders = _find_shoulders(sb, valid_first, valid_last, sweep)
        detection.features.extend(peaks + shoulders)

    detection.features.sort(key=lambda f: f.index)
    return detection


def _is_valley(feature, sweep):
    """True for a maximum on a negative-going sweep or a minimum on a positive-going one."""
    if sweep > 0:
        return feature.extremum == EXTREMUM_MIN
    if sweep < 0:
        return feature.extremum == EXTREMUM_MAX
    return False


# ----------------------------------------------------------------------
# Runs
# ----------------------------------------------------------------------

def _points_in_box(potential, current, box, lo, hi, offsets):
    x_a, x_b, y_a, y_b = box
    x0, x1 = sorted((x_a, x_b))
    y0, y1 = sorted((y_a, y_b))
    x = potential[lo:hi + 1] + offsets[0]
    y = current[lo:hi + 1] + offsets[1]
    mask = (x >= x0) & (x <= x1) & (y >= y0) & (y <= y1)
    return np.flatnonzero(mask) + lo


def _split_runs(indices, potential, branch, merge_gap_V):
    """Group boxed point indices into runs of consecutive points on one branch.

    Runs on the same branch separated by less than ``merge_gap_V`` of potential
    are joined: noise flickering across a box edge should not cut one feature
    into several runs.
    """
    if len(indices) == 0:
        return []
    breaks = np.flatnonzero((np.diff(indices) > 1)
                            | (np.diff(branch[indices]) != 0)) + 1
    pieces = [(int(p[0]), int(p[-1])) for p in np.split(indices, breaks)]
    runs = [pieces[0]]
    for first, last in pieces[1:]:
        prev_first, prev_last = runs[-1]
        same_branch = branch[prev_last] == branch[first]
        if same_branch and abs(potential[first] - potential[prev_last]) <= merge_gap_V:
            runs[-1] = (prev_first, last)
        else:
            runs.append((first, last))
    return runs


# ----------------------------------------------------------------------
# Smoothing
# ----------------------------------------------------------------------

def window_points(settings, volts_per_point, available):
    """Savitzky-Golay window in points: the smoothing span, odd, within limits.

    Returns 0 when ``available`` points are too few to smooth at all.
    """
    minimum = max(MIN_WINDOW_POINTS, settings.polyorder + 2)
    if minimum % 2 == 0:
        minimum += 1
    if volts_per_point > 0:
        window = int(round(settings.smoothing_mV / 1000.0 / volts_per_point))
    else:
        window = minimum
    window = max(window, minimum)
    if window % 2 == 0:
        window += 1
    if window > available:
        window = available if available % 2 == 1 else available - 1
    return window if window >= minimum else 0


def _robust_noise(y):
    """Standard deviation of the point-to-point noise, insensitive to the signal.

    Second differences cancel any locally straight signal, and their median
    absolute deviation ignores occasional spikes.
    """
    if len(y) < 5:
        return 0.0
    dd = np.diff(y, 2)
    mad = np.median(np.abs(dd - np.median(dd)))
    return float(1.4826 * mad / np.sqrt(6.0))


def _smooth_branch(potential, current, branch, b, lo, hi, settings):
    first = int(np.searchsorted(branch, b, side="left"))
    last = int(np.searchsorted(branch, b, side="right")) - 1
    first, last = max(first, lo), min(last, hi)
    count = last - first + 1
    span_V = abs(potential[last] - potential[first])
    volts_per_point = span_V / (count - 1) if count > 1 else 0.0
    window = window_points(settings, volts_per_point, count)
    if window == 0:
        empty = np.empty(0)
        return _Branch(first, last, 0, empty, empty, empty,
                       note="too few points to smooth")

    y = current[first:last + 1]
    order = settings.polyorder
    smooth = savgol_filter(y, window, order, mode="interp")
    d1 = savgol_filter(y, window, order, deriv=1, mode="interp")
    d2 = savgol_filter(y, window, order, deriv=2, mode="interp")
    # White noise of standard deviation s leaves s * |coeffs| after the filter.
    residual = _robust_noise(y) * float(np.linalg.norm(savgol_coeffs(window, order)))
    sb = _Branch(first, last, window, smooth, d1, d2, residual,
                 _threshold(residual, settings))
    sb.extrema = _find_extrema(sb, first + window, last - window)
    return sb


def _threshold(noise_uA, settings):
    if settings.prominence_uA is not None:
        return float(settings.prominence_uA)
    return max(settings.auto_floor_uA, settings.auto_noise_multiple * noise_uA)


# ----------------------------------------------------------------------
# Peaks
# ----------------------------------------------------------------------

def _find_extrema(sb, valid_first, valid_last):
    """Sign changes of the first derivative between two point indices.

    The reported point is the raw point nearest where the derivative crosses
    zero: the extremum of the smoothed curve. Its prominence is read at the
    highest smoothed point within half a window, because the smoothed curve
    still carries point-to-point ripple that would otherwise hide the real
    top behind a tiny one. Noise near a flat top can flip the derivative's
    sign several times; flips sharing one top are reported once.
    """
    a, b = valid_first - sb.first, valid_last - sb.first
    if a > b:
        return []
    d1 = sb.d1
    sign = np.sign(d1)
    crossings = np.flatnonzero(sign[:-1] * sign[1:] < 0)
    half = sb.window // 2
    best = {}  # (top, is_max) -> (distance from top, Feature)
    for j in crossings:
        k = int(round(j + d1[j] / (d1[j] - d1[j + 1])))
        if not a <= k <= b:
            continue
        is_max = d1[j] > 0
        signal = sb.smooth if is_max else -sb.smooth
        lo, hi = max(0, k - half), min(len(signal) - 1, k + half)
        top = _climb(signal, lo + int(np.argmax(signal[lo:hi + 1])))
        prominence = _prominence(signal, top)
        if prominence < sb.threshold_uA:
            continue
        feature = Feature(int(sb.first + k), LABEL_PEAK,
                          EXTREMUM_MAX if is_max else EXTREMUM_MIN, prominence)
        key = (top, is_max)
        if key not in best or abs(k - top) < best[key][0]:
            best[key] = (abs(k - top), feature)
    return sorted((feature for _distance, feature in best.values()),
                  key=lambda f: f.index)


def _climb(signal, i):
    """The local top of ``signal`` reached by walking uphill from point i."""
    last = len(signal) - 1
    while True:
        if i > 0 and signal[i - 1] > signal[i]:
            i -= 1
        elif i < last and signal[i + 1] > signal[i]:
            i += 1
        else:
            return i


def _prominence(signal, index):
    """Prominence of the point ``index`` of ``signal``, 0 if it is not a local top."""
    if index <= 0 or index >= len(signal) - 1:
        return 0.0
    if signal[index] < signal[index - 1] or signal[index] < signal[index + 1]:
        return 0.0
    return float(peak_prominences(signal, [index])[0][0])


# ----------------------------------------------------------------------
# Shoulders
# ----------------------------------------------------------------------

def _find_shoulders(sb, valid_first, valid_last, sweep):
    if sweep == 0:
        return []
    hidden = EXTREMUM_MAX if sweep > 0 else EXTREMUM_MIN
    s, d1 = sb.smooth, sb.d1
    steep = np.abs(d1)
    n = len(s)
    half = sb.window // 2
    reach = _SHOULDER_REACH_WINDOWS * sb.window
    extrema = [f.index - sb.first for f in sb.extrema]
    a, b = valid_first - sb.first, valid_last - sb.first

    # Flat spots: local minima of the slope's size.
    inner = np.arange(max(a, 1), min(b, n - 2) + 1)
    flat = inner[(steep[inner] <= steep[inner - 1]) & (steep[inner] < steep[inner + 1])]

    candidates = []  # (slope ratio, flat spot, left flank, right flank, size)
    for f in flat:
        # A true peak bounds the search: beyond it lies another feature's
        # flank, and a flat spot right at one is that peak itself.
        k = bisect.bisect_left(extrema, f)
        left_peak = extrema[k - 1] if k > 0 else None
        right_peak = extrema[k] if k < len(extrema) else None
        if (left_peak is not None and f - left_peak <= half) or \
                (right_peak is not None and right_peak - f <= half):
            continue
        left = max(0, f - reach, left_peak + half if left_peak is not None else 0)
        right = min(n - 1, f + reach, right_peak - half if right_peak is not None else n - 1)
        if left >= f or right <= f:
            continue
        lo = left + int(np.argmax(steep[left:f + 1]))
        hi = f + int(np.argmax(steep[f:right + 1]))
        if lo == f or hi == f or np.sign(d1[lo]) != np.sign(d1[hi]) or d1[lo] == 0:
            continue
        ratio = steep[f] / min(steep[lo], steep[hi])
        if ratio > _SHOULDER_MAX_SLOPE_RATIO:
            continue

        # Against a straight line between the two steep flanks, a shoulder runs
        # ahead and then falls behind; a foot or a knee does only one of them.
        deviation = s[lo:hi + 1] - np.linspace(s[lo], s[hi], hi - lo + 1)
        ahead, behind = float(np.max(deviation)), float(-np.min(deviation))
        if min(ahead, behind) < sb.threshold_uA / 2:
            continue
        candidates.append((ratio, f, lo, hi, ahead + behind))

    # Noise leaves several flat spots inside one shoulder; the deepest speaks
    # for the whole stretch between its steep flanks.
    bend = -sb.d2 if hidden == EXTREMUM_MAX else sb.d2
    taken: List[Tuple[int, int, int]] = []
    found: List[Feature] = []
    for _ratio, f, lo, hi, size in sorted(candidates):
        if any(t_lo <= f <= t_hi or lo <= t_f <= hi for t_f, t_lo, t_hi in taken):
            continue
        taken.append((f, lo, hi))
        # The hidden peak bends the curve its own way on one side of the flat
        # spot: before it when the curve is heading towards that peak's sign.
        heading_down = d1[lo] < 0
        before = heading_down == (hidden == EXTREMUM_MIN)
        side_lo, side_hi = (lo, f) if before else (f, hi)
        e = side_lo + int(np.argmax(bend[side_lo:side_hi + 1]))
        if bend[e] <= 0 or not a <= e <= b:
            continue
        found.append(Feature(int(sb.first + e), LABEL_SHOULDER, hidden, size))
    return found

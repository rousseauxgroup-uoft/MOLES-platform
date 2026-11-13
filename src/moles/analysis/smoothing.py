"""Smoothing of a trace's measured signal, chosen per trace.

Three methods are offered everywhere MOLES smooths a trace:

* ``none`` — the data as measured.
* ``moving_average`` — a centred rolling mean. Cheap and strong, but it
  flattens peaks: a peak narrower than the window loses height and widens.
* ``savgol`` — Savitzky-Golay: a quadratic is fitted by least squares to the
  points in each window and the point is replaced by the fit's value there.
  It removes noise about as well as a moving average of the same window while
  keeping the height, width and position of peaks far better, which is what a
  voltammogram is read for.

Both run along point order (time). At a file's own ends the moving average
shrinks its window to the points that exist rather than averaging in zeros,
and Savitzky-Golay fits its polynomial to the end window, so neither dips
there. Even moving-average windows sit half a point early; Savitzky-Golay
needs an odd window and rounds an even one up.

Smoothed currents are kept per loaded file, method and window, so labels and
redraws do not recompute them.
"""

import weakref

import numpy as np
from scipy.signal import savgol_filter

METHOD_NONE = "none"
METHOD_MOVING_AVERAGE = "moving_average"
METHOD_SAVGOL = "savgol"
METHODS = (METHOD_NONE, METHOD_MOVING_AVERAGE, METHOD_SAVGOL)

# Names shown in the smoothing method menus, in menu order.
METHOD_LABELS = {
    METHOD_NONE: "None",
    METHOD_MOVING_AVERAGE: "Rolling average",
    METHOD_SAVGOL: "Savitzky–Golay",
}

# Polynomial order of the Savitzky-Golay fit. Quadratic follows a peak's
# curvature without chasing the noise.
SAVGOL_ORDER = 2

# Smoothed versions kept per loaded file (the most recently used settings).
_KEEP_WINDOWS = 4

_cache = weakref.WeakKeyDictionary()


def moving_average(y, window):
    """Centred moving average of ``y`` over ``window`` points (1 or less: unchanged)."""
    y = np.asarray(y, dtype=float)
    n = len(y)
    window = int(window)
    if window <= 1 or n == 0:
        return y
    before = window // 2
    after = window - 1 - before
    i = np.arange(n)
    lo = np.maximum(i - before, 0)
    hi = np.minimum(i + after + 1, n)
    finite = np.isfinite(y)
    if finite.all():
        sums = np.concatenate(([0.0], np.cumsum(y)))
        return (sums[hi] - sums[lo]) / (hi - lo)
    # Missing points (NaN) are left out of each average and stay missing, so
    # one gap does not blank the rest of the trace through the running sum.
    sums = np.concatenate(([0.0], np.cumsum(np.where(finite, y, 0.0))))
    counts = np.concatenate(([0], np.cumsum(finite)))
    with np.errstate(invalid="ignore", divide="ignore"):
        out = (sums[hi] - sums[lo]) / (counts[hi] - counts[lo])
    out[~finite] = np.nan
    return out


def savitzky_golay(y, window):
    """Quadratic Savitzky-Golay smoothing of ``y`` over ``window`` points.

    An even window is rounded up to the next odd one, and a window longer than
    the data is cut to fit it. Windows of 3 points or fewer leave the data
    unchanged, since a quadratic passes through 3 points exactly.
    """
    y = np.asarray(y, dtype=float)
    n = len(y)
    window = int(window)
    if window % 2 == 0:
        window += 1
    if window > n:
        window = n if n % 2 else n - 1
    if window <= SAVGOL_ORDER + 1:
        return y
    return savgol_filter(y, window, SAVGOL_ORDER, mode="interp")


def is_smoothing(method, window):
    """True when ``method`` and ``window`` change the data at all."""
    window = int(window)
    if method == METHOD_MOVING_AVERAGE:
        return window > 1
    if method == METHOD_SAVGOL:
        return window > SAVGOL_ORDER + 1
    return False


def smooth(y, method, window):
    """``y`` smoothed by ``method`` over ``window`` points (unknown method: unchanged)."""
    if method == METHOD_MOVING_AVERAGE:
        return moving_average(y, window)
    if method == METHOD_SAVGOL:
        return savitzky_golay(y, window)
    return np.asarray(y, dtype=float)


def describe(method, window):
    """A short human-readable account of a smoothing choice, for export notes."""
    if not is_smoothing(method, window):
        return "off"
    if method == METHOD_SAVGOL:
        return (f"Savitzky-Golay, {int(window)} points "
                f"(polynomial order {SAVGOL_ORDER})")
    return f"rolling average, {int(window)} points"


def smoothed_current(dataset, window, method=METHOD_MOVING_AVERAGE):
    """A loaded file's current (µA) smoothed by ``method`` over ``window`` points, read-only."""
    window = int(window)
    if not is_smoothing(method, window):
        return dataset.current
    kept = _cache.setdefault(dataset, {})
    key = (method, window)
    if key not in kept:
        if len(kept) >= _KEEP_WINDOWS:
            kept.pop(next(iter(kept)))
        smoothed = smooth(dataset.current, method, window)
        smoothed.setflags(write=False)
        kept[key] = smoothed
    return kept[key]

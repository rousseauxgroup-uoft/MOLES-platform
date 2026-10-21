"""Moving-average smoothing of a trace's current, chosen per experiment.

The average is centred on each point and runs along point order (time), over
the whole file before the data range is cut, so a range's ends are smoothed
with the points beyond them. At the file's own ends the window shrinks to the
points that exist, rather than averaging in zeros, so the trace does not dip
there. Even windows sit half a point early.

Smoothed currents are kept per loaded file and window, so labels and redraws
do not recompute them.
"""

import weakref

import numpy as np

# Smoothed versions kept per loaded file (the most recently used windows).
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
    sums = np.concatenate(([0.0], np.cumsum(y)))
    i = np.arange(n)
    lo = np.maximum(i - before, 0)
    hi = np.minimum(i + after + 1, n)
    return (sums[hi] - sums[lo]) / (hi - lo)


def smoothed_current(dataset, window):
    """A loaded file's current (µA) averaged over ``window`` points, read-only."""
    window = int(window)
    if window <= 1:
        return dataset.current
    kept = _cache.setdefault(dataset, {})
    if window not in kept:
        if len(kept) >= _KEEP_WINDOWS:
            kept.pop(next(iter(kept)))
        smoothed = moving_average(dataset.current, window)
        smoothed.setflags(write=False)
        kept[window] = smoothed
    return kept[window]

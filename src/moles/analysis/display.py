"""Thinning long traces for drawing on screen.

A CV can hold hundreds of thousands of points, more than a screen can show
and enough to make dragging a slider sluggish. pyqtgraph's own thinning
assumes the x-axis only increases, which a voltammogram's potential does not,
so traces are thinned here instead, along point order: each block of
consecutive points keeps its lowest and highest current, in their original
order, so no peak is lost from the drawing. Thinning is for display only;
peak picking, labels, and export always use every point.
"""

import numpy as np

# Most points a trace is drawn with.
DISPLAY_POINTS = 50_000

# Most points a dashed or dotted trace is drawn with. A pen's dash pattern runs
# along the drawn path, and on dense, noisy data that path zigzags many times
# per pixel, so the dashes shrink to nothing and the line looks solid. Every
# n-th point keeps the path about as long as the trace looks.
PATTERNED_POINTS = 2_000


def every_nth(x, y, max_points=PATTERNED_POINTS):
    """``(x, y)`` reduced to about ``max_points`` evenly spaced points, ends kept."""
    x = np.asarray(x)
    y = np.asarray(y)
    n = len(x)
    if n <= max_points:
        return x, y
    step = int(np.ceil(n / max_points))
    keep = np.arange(0, n, step)
    if keep[-1] != n - 1:
        keep = np.append(keep, n - 1)
    return x[keep], y[keep]


def thin(x, y, max_points=DISPLAY_POINTS):
    """``(x, y)`` reduced to about ``max_points`` points, keeping each block's extremes."""
    x = np.asarray(x)
    y = np.asarray(y)
    n = len(x)
    if n <= max_points:
        return x, y
    block = int(np.ceil(n / (max_points / 2)))
    whole = (n // block) * block
    rows = np.arange(whole // block) * block
    yb = y[:whole].reshape(-1, block)
    lo = rows + np.argmin(yb, axis=1)
    hi = rows + np.argmax(yb, axis=1)
    keep = np.column_stack([np.minimum(lo, hi), np.maximum(lo, hi)]).ravel()
    # The trace must still start and end where it does.
    keep = np.unique(np.concatenate([[0], keep, np.arange(whole, n), [n - 1]]))
    return x[keep], y[keep]

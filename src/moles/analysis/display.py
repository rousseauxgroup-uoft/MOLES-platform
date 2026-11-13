"""Thinning long traces for drawing on screen.

A CV can hold hundreds of thousands of points, more than a screen can show
and enough to make dragging a slider sluggish. pyqtgraph's own thinning
assumes the x-axis only increases, which a voltammogram's potential does not,
so traces are thinned here instead, along point order: each block of
consecutive points keeps its lowest and highest current, in their original
order, so no peak is lost from the drawing. Thinning is for display only;
peak picking, labels, and export always use every point.

On screen, ``screen_trace`` goes further and fits a trace to the view it is
drawn in. Qt's cost of drawing a wide, antialiased line grows with every
segment it strokes, and with every pixel a segment covers *including those
outside the window*, so a zoomed-in trace whose segments run far past the
edges is drawn very slowly. The trace is therefore cut where it leaves the
view (plus a margin), and each run of consecutive points falling in one pixel
column is cut down to its first, lowest, highest and last point, in order:
the same pixels are lit, with a fraction of the segments.
"""

import numpy as np

# Most points a trace is drawn with.
DISPLAY_POINTS = 50_000

# A run of points in one pixel column shorter than this (in pixels) is drawn
# as a straight line from its first point to its last.
_FLAT_RUN_PX = 0.5

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


def clip_polyline(x, y, rect):
    """Cut the polyline through ``(x, y)`` to ``rect`` = ``(x0, x1, y0, y1)``.

    Returns ``(x, y, connect)``, where ``connect[i]`` says whether point ``i``
    joins point ``i + 1`` (pyqtgraph's ``connect`` array). Segments outside
    the rectangle are dropped; a segment crossing its edge ends on the edge,
    and the line breaks there until it comes back in.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    n = len(x)
    if n < 2:
        return x, y, np.zeros(n, dtype=bool)
    x0, x1, y0, y1 = rect
    if x.min() >= x0 and x.max() <= x1 and y.min() >= y0 and y.max() <= y1:
        return x, y, _all_connected(n)

    # Segments with both ends past the same edge are out; only the rest need
    # the Liang-Barsky test, which finds where each enters (t0) and leaves (t1).
    ax, bx, ay, by = x[:-1], x[1:], y[:-1], y[1:]
    out = (((ax < x0) & (bx < x0)) | ((ax > x1) & (bx > x1))
           | ((ay < y0) & (by < y0)) | ((ay > y1) & (by > y1)))
    seg = np.flatnonzero(~out)
    if len(seg) == 0:
        return x[:0], y[:0], np.zeros(0, dtype=bool)
    sx, sy = ax[seg], ay[seg]
    dx, dy = bx[seg] - sx, by[seg] - sy
    t0 = np.zeros(len(seg))
    t1 = np.ones(len(seg))
    with np.errstate(divide="ignore", invalid="ignore"):
        for p, q in ((-dx, sx - x0), (dx, x1 - sx), (-dy, sy - y0), (dy, y1 - sy)):
            t = q / p
            entering = p < 0
            leaving = p > 0
            t0 = np.where(entering, np.maximum(t0, t), t0)
            t1 = np.where(leaving, np.minimum(t1, t), t1)
            # Parallel to this edge and outside it: never visible.
            t1 = np.where((p == 0) & (q < 0), -1.0, t1)
    shown = t0 <= t1
    seg, t0, t1 = seg[shown], t0[shown], t1[shown]
    sx, sy, dx, dy = sx[shown], sy[shown], dx[shown], dy[shown]
    if len(seg) == 0:
        return x[:0], y[:0], np.zeros(0, dtype=bool)

    # A segment starts a new piece of line unless it carries straight on from
    # the one before: the segment just before it, shown and not cut at its end.
    clipped_start = t0 > 0
    clipped_end = t1 < 1
    starts = np.ones(len(seg), dtype=bool)
    starts[1:] = (seg[1:] != seg[:-1] + 1) | clipped_end[:-1] | clipped_start[1:]

    start_x = np.where(clipped_start, sx + t0 * dx, sx)
    start_y = np.where(clipped_start, sy + t0 * dy, sy)
    end_x = np.where(clipped_end, sx + t1 * dx, x[seg + 1])
    end_y = np.where(clipped_end, sy + t1 * dy, y[seg + 1])

    # Two slots per segment: its start (used only when it starts a piece) and its end.
    m = len(seg)
    px = np.empty(2 * m)
    py = np.empty(2 * m)
    px[0::2], px[1::2] = start_x, end_x
    py[0::2], py[1::2] = start_y, end_y
    connect = np.ones(2 * m, dtype=bool)
    connect[1::2] = ~np.append(starts[1:], True)
    used = np.ones(2 * m, dtype=bool)
    used[0::2] = starts
    return px[used], py[used], connect[used]


def column_extremes(x, y, connect, x_origin, column_width, row_height):
    """Keep, of each run of consecutive points in one pixel column, its first,
    lowest, highest and last point, in their original order.

    Columns are ``column_width`` wide from ``x_origin``; a run never spans a
    break in ``connect``. A run less than half a pixel (``row_height``) tall
    keeps only its ends.
    """
    n = len(x)
    if n < 3:
        return x, y, connect
    col = np.floor((x - x_origin) / column_width)
    begins = np.ones(n, dtype=bool)
    begins[1:] = (col[1:] != col[:-1]) | ~connect[:-1]
    first = np.flatnonzero(begins)
    last = np.append(first[1:], n) - 1
    if len(first) * 2 > n:
        return x, y, connect   # too few points per column to gain anything
    run = np.cumsum(begins) - 1
    lo = np.minimum.reduceat(y, first)
    hi = np.maximum.reduceat(y, first)
    index = np.arange(n)
    i_lo = np.minimum.reduceat(np.where(y == lo[run], index, n), first)
    i_hi = np.minimum.reduceat(np.where(y == hi[run], index, n), first)
    flat = (hi - lo) < _FLAT_RUN_PX * row_height
    i_lo = np.where(flat, first, i_lo)
    i_hi = np.where(flat, last, i_hi)
    keep = np.sort(np.column_stack([first, i_lo, i_hi, last]), axis=1).ravel()
    keep = keep[np.append(True, keep[1:] != keep[:-1])]
    return x[keep], y[keep], connect[keep]


def screen_trace(x, y, rect, pixel_size, max_points=DISPLAY_POINTS, reduce=True):
    """``(x, y, connect)`` to draw a trace in a view: cut to ``rect`` and, if
    ``reduce``, cut down to the extremes of each pixel column.

    ``rect`` = ``(x0, x1, y0, y1)`` is the region drawn (the view plus a
    margin), ``pixel_size`` = ``(width, height)`` of a screen pixel in data
    units. Columns widen as needed to stay within ``max_points``.
    """
    x, y, connect = clip_polyline(x, y, rect)
    if not reduce:
        return x, y, connect
    width, height = pixel_size
    span = rect[1] - rect[0]
    while True:
        rx, ry, rc = column_extremes(x, y, connect, rect[0], width, height)
        if len(rx) <= max_points or width > span:
            return rx, ry, rc
        width *= 2


def _all_connected(n):
    connect = np.ones(n, dtype=bool)
    connect[-1] = False
    return connect

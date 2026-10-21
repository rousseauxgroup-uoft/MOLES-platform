"""What a plot looks like, shared by the screen and the exported figure.

A plot window and its exported figure must look the same, so both read their
fonts, sizes, line widths, dash patterns, and tick positions from here
rather than each choosing their own. Sizes are in points (1/72 inch): Qt
measures fonts in points already, and the screen converts the rest to pixels
with its own resolution, so a figure exported at the plot's on-screen size
matches it.

The text sizes and the legend's line width are the user's to change
(``model.PlotStyle``); the rest is fixed here.
"""

import math
from typing import List, Tuple

from matplotlib.ticker import MaxNLocator

FONT_FAMILIES = ("Arial", "Helvetica", "DejaVu Sans")

FRAME_COLOR = "#a6a6a6"
TEXT_COLOR = "#000000"

FRAME_LINE_PT = 1.0
TRACE_LINE_PT = 2.0
TICK_LENGTH_PT = 5.0
TICK_PAD_PT = 3.0
LABEL_MARKER_PT = 9.0
MARKER_RIM_PT = 1.5
LEADER_LINE_PT = 1.0
LEGEND_SAMPLE_PT = 20.0  # length of a legend's line sample
LEGEND_GAP_PT = 8.0      # gap between a legend's line sample and its name
LEGEND_PAD_PT = 6.0      # space inside the legend's box, all round
LEGEND_ROW_GAP_PT = 3.0  # extra space between the legend's rows
LEGEND_ROW_EM = 1.3      # height of a legend row, in legend font sizes
# A light backing behind the legend, so traces passing under it stay out of its way.
LEGEND_BACKING = (1.0, 1.0, 1.0, 0.82)

# Dash patterns, in multiples of the line width (as Qt and matplotlib both scale them).
DASH_PATTERNS = {
    "dash": (4.0, 2.0),
    "dot": (1.0, 2.0),
    "dashdot": (4.0, 2.0, 1.0, 2.0),
    "dashdotdot": (4.0, 2.0, 1.0, 2.0, 1.0, 2.0),
}

# Room each tick number needs along its axis, in font sizes: numbers sit side
# by side under the x-axis but are stacked up the y-axis.
_TICK_SPACING_FONTS = {"x": 4.5, "y": 2.2}


def _decimals(step):
    """Fewest decimals that write every multiple of ``step`` exactly."""
    for d in range(0, 8):
        scaled = step * 10 ** d
        if abs(scaled - round(scaled)) < 1e-6 * max(1.0, scaled):
            return d
    return 8


def tick_text(value, decimals):
    text = f"{value:.{decimals}f}"
    return text.lstrip("-") if float(text) == 0 else text


def axis_ticks(vmin, vmax, length_pt, font_pt, axis="x") -> List[Tuple[float, str]]:
    """Tick positions and their text for one axis showing ``vmin``..``vmax``.

    ``length_pt`` is the axis's length on the page or screen; the number of
    ticks follows it, so a figure at the plot's size gets the plot's ticks.
    """
    if not (math.isfinite(vmin) and math.isfinite(vmax)) or vmax <= vmin:
        return []
    spacing = _TICK_SPACING_FONTS[axis] * font_pt
    nbins = max(2, int(length_pt / spacing))
    locator = MaxNLocator(nbins=nbins, steps=[1, 2, 2.5, 5, 10])
    span = vmax - vmin
    values = [float(v) for v in locator.tick_values(vmin, vmax)
              if vmin - 1e-9 * span <= v <= vmax + 1e-9 * span]
    if not values:
        return []
    step = values[1] - values[0] if len(values) > 1 else abs(values[0]) or 1.0
    decimals = _decimals(step)
    return [(round(v, decimals + 3), tick_text(v, decimals)) for v in values]

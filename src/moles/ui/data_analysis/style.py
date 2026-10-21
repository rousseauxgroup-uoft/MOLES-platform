"""Look of a Data Analysis plot on screen, after the group's voltammogram figures.

White background, no grid, a light grey frame around the plot area, and
ticks with their labels outside the frame along the left and bottom sides
(not crossing at zero). Axis titles name the quantity in bold with its unit
after it. pyqtgraph's own right-click menu and auto-range button are hidden:
they change the view behind the window's back.

Sizes, fonts and tick positions come from ``analysis.appearance`` and the
window's ``PlotStyle``, the same places the exported figure reads them from,
so the two match.
"""

import pyqtgraph as pg
from PyQt5 import QtCore, QtGui

from ...analysis.appearance import (
    FONT_FAMILIES, FRAME_COLOR, FRAME_LINE_PT, TEXT_COLOR, TICK_LENGTH_PT, TICK_PAD_PT,
    axis_ticks,
)

X_QUANTITY, X_UNIT = "Potential", "V"
Y_QUANTITY, Y_UNIT = "Current", "µA"

BACKGROUND = "w"


def axis_title(quantity, unit):
    return f"<b>{quantity}</b> ({unit})"


def plot_font(size_pt):
    font = QtGui.QFont(FONT_FAMILIES[0])
    font.setPointSizeF(size_pt)
    return font


def style_plot(plot_item, style, px_per_pt):
    """Apply the figure style to a pyqtgraph ``PlotItem``."""
    plot_item.showGrid(x=False, y=False)
    plot_item.setMenuEnabled(False)
    plot_item.hideButtons()
    plot_item.showAxis("top")
    plot_item.showAxis("right")
    frame = pg.mkPen(FRAME_COLOR, width=FRAME_LINE_PT * px_per_pt)
    for side in ("left", "bottom", "top", "right"):
        plot_item.getAxis(side).setPen(frame)
    for side in ("left", "bottom"):
        axis = plot_item.getAxis(side)
        axis.setTextPen(pg.mkPen(TEXT_COLOR))
        # Keep µA as µA: pyqtgraph would otherwise rescale to mA or nA.
        axis.enableAutoSIPrefix(False)
    for side in ("top", "right"):
        plot_item.getAxis(side).setStyle(showValues=False, tickLength=0)
    apply_text_style(plot_item, style, px_per_pt)


def apply_text_style(plot_item, style, px_per_pt):
    """Tick numbers and axis titles at the window's text sizes."""
    for side in ("left", "bottom"):
        plot_item.getAxis(side).setStyle(
            tickLength=int(round(TICK_LENGTH_PT * px_per_pt)),  # positive: out of the frame
            tickTextOffset=int(round(TICK_PAD_PT * px_per_pt)),
            tickFont=plot_font(style.tick_font_pt))
    title = {"color": TEXT_COLOR, "font-size": f"{style.axis_title_pt}pt",
             "font-family": FONT_FAMILIES[0]}
    plot_item.setLabel("bottom", axis_title(X_QUANTITY, X_UNIT), **title)
    plot_item.setLabel("left", axis_title(Y_QUANTITY, Y_UNIT), **title)


class TickSync(QtCore.QObject):
    """Keeps a plot's ticks where the exported figure will put them.

    Ticks are chosen by ``appearance.axis_ticks`` from the view range and the
    axis's length in points, whenever either changes.
    """

    def __init__(self, plot_item, style, px_per_pt, parent=None):
        super().__init__(parent)
        self._plot_item = plot_item
        self._style = style          # callable -> PlotStyle
        self._px_per_pt = px_per_pt  # callable -> float
        view_box = plot_item.getViewBox()
        view_box.sigRangeChanged.connect(self.update)
        view_box.sigResized.connect(self.update)

    def update(self, *_args):
        view_box = self._plot_item.getViewBox()
        (x0, x1), (y0, y1) = view_box.viewRange()
        rect = view_box.boundingRect()
        px = self._px_per_pt()
        font_pt = self._style().tick_font_pt
        self._plot_item.getAxis("bottom").setTicks(
            [axis_ticks(x0, x1, rect.width() / px, font_pt, "x"), []])
        self._plot_item.getAxis("left").setTicks(
            [axis_ticks(y0, y1, rect.height() / px, font_pt, "y"), []])

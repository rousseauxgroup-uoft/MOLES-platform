"""Helpers to make a pyqtgraph live-plot grid vertically scrollable.

With many potentiostats connected (up to 16), the two-column grid of live plots
squeezes every plot too short to read. Wrapping the ``GraphicsLayoutWidget`` in
a ``PlotScrollArea`` keeps each plot at a legible height: enough rows to show
``PLOTS_IN_VIEW`` plots fill the viewport, and any beyond that scroll into view.

Both the electrolysis and the electroanalysis windows use this so their grids
behave identically.
"""

from PyQt5 import QtCore, QtWidgets

# Number of individual plots kept visible without scrolling.
PLOTS_IN_VIEW = 8
# Columns in the plot grid — matches each window's addPlot(row, col) layout.
PLOT_GRID_COLUMNS = 2
# Floor on a single plot row's height (px) so a plot never collapses to nothing
# even in a very short window.
MIN_PLOT_ROW_HEIGHT = 170


class PlotScrollArea(QtWidgets.QScrollArea):
    """A vertical scroll area that announces resizes.

    The inner plot grid's height has to be recomputed whenever the viewport
    changes size (so a fixed number of plots keeps filling it), which needs a
    resize hook the plain ``QScrollArea`` doesn't expose.
    """

    resized = QtCore.pyqtSignal()

    def __init__(self, inner, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setWidget(inner)
        # The grid is exactly viewport-wide (two columns fit), so only ever
        # scroll vertically.
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.resized.emit()


def apply_scroll_height(scroll_area, inner, n_plots, focused=False):
    """Size ``inner`` so ``PLOTS_IN_VIEW`` plots fill the viewport, rest scroll.

    When the grid has few enough plots to fit (or a single plot is focused), the
    inner widget is left to fill the viewport (minimum height 0, no scrollbar).
    Otherwise its minimum height is set so each visible row is
    ``viewport_height / rows_in_view`` tall, which forces the scrollbar and keeps
    every plot the same legible size.
    """
    rows_in_view = max(1, PLOTS_IN_VIEW // PLOT_GRID_COLUMNS)
    if focused:
        total_rows = 1
    else:
        total_rows = (max(0, n_plots) + PLOT_GRID_COLUMNS - 1) // PLOT_GRID_COLUMNS
    viewport_h = scroll_area.viewport().height()
    if total_rows <= rows_in_view or viewport_h <= 0:
        # Everything fits (or nothing to lay out yet): fill the viewport.
        inner.setMinimumHeight(0)
        return
    row_h = max(MIN_PLOT_ROW_HEIGHT, viewport_h / rows_in_view)
    inner.setMinimumHeight(int(row_h * total_rows))

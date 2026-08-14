"""Experiment-table column layout, styling delegate, and naming helpers.

The multichannel UI uses a single QTableWidget with one row per potentiostat.
This module owns:
  - Column index constants and the human-readable header labels.
  - The mapping from electrochemical method to which columns are *relevant*
    (so irrelevant cells get greyed out and locked).
  - Default values seeded into a new row.
  - The selection-aware delegate that preserves cell background colours.
  - The auto-increment helper for "fill below" experiment naming.
"""

import re

from PyQt5 import QtCore, QtGui, QtWidgets


# ----- Column indices -----
# Module-level constants so the many helper methods that touch the table read uniformly.
(
    COL_ID,
    COL_STATUS,
    COL_EXP_NAME,
    COL_PROGRESS,
    COL_METHOD,
    COL_MMOL,
    COL_ELECTRONS,
    COL_TARGET_C,
    COL_TIME_LIMIT_MIN,
    COL_CC_CURRENT,
    COL_CP_POTENTIAL,
    COL_AC_POS,
    COL_AC_NEG,
    COL_AC_FREQ,
    COL_AC_DUTY,
    COL_AC_WAVE,
    COL_AP_POS,
    COL_AP_NEG,
    COL_AP_SAMPLE,
) = range(19)

TABLE_HEADERS = [
    "ID", "Status", "Exp Name", "Progress", "Method", "Scale (mmol)",
    "e\u207b (F/mol)", "Target (C)", "Time Limit (min)", "Current (mA)", "Potential (V)",
    "AC +Peak (mA)", "AC \u2212Peak (mA)", "Freq (Hz)", "Duty (%)", "Waveform",
    "AP +Peak (V)", "AP \u2212Peak (V)", "Sample Rate (Hz)",
]

# Per-method, the columns that are *relevant* and therefore editable.
# Anything not in this set for the row's current method gets greyed out and locked.
METHOD_COLS = {
    "constant_current":    {COL_CC_CURRENT},
    "constant_potential":  {COL_CP_POTENTIAL},
    "alternating_current": {COL_AC_POS, COL_AC_NEG, COL_AC_FREQ, COL_AC_DUTY, COL_AC_WAVE},
    "alternating_polarity": {COL_AP_POS, COL_AP_NEG, COL_AC_FREQ, COL_AC_DUTY, COL_AC_WAVE, COL_AP_SAMPLE},
}
ALL_METHOD_COLS = set().union(*METHOD_COLS.values())

# Default values seeded into a new row.
ROW_DEFAULTS = {
    COL_EXP_NAME: "Experiment",
    COL_MMOL: 0.1,
    COL_ELECTRONS: 2.0,
    COL_CC_CURRENT: 1.0,
    COL_CP_POTENTIAL: 1.0,
    COL_AC_POS: 1.0,
    COL_AC_NEG: -1.0,
    COL_AC_FREQ: 0.1,
    COL_AC_DUTY: 50.0,
    COL_AP_POS: 1.0,
    COL_AP_NEG: -1.0,
    COL_AP_SAMPLE: 250,
}

METHOD_LABELS = {
    "Constant Current": "constant_current",
    "Constant Potential": "constant_potential",
    "Alternating Current": "alternating_current",
    "Alternating Polarity": "alternating_polarity",
}
METHOD_LABELS_REVERSE = {v: k for k, v in METHOD_LABELS.items()}


def _increment_exp_name(name: str) -> str:
    """Auto-increment the trailing suffix of an experiment name.

    Detects two common naming conventions:
      - Numeric suffix (with or without separator):  "Exp-3" → "Exp-4", "Run3" → "Run4"
      - Single alphabetic letter after a separator:  "Exp-A" → "Exp-B"

    Falls back to appending "-1" when no recognised pattern is found, so
    successive calls produce "-1", "-2", etc. (e.g. "Experiment" → "Experiment-1").
    Zero-padding is preserved for numeric suffixes (e.g. "Exp-03" → "Exp-04").
    """
    # Numeric suffix, preceded by at least one non-digit (preserves zero-padding)
    m = re.match(r'^(.*\D)(\d+)$', name)
    if m:
        prefix, num_str = m.group(1), m.group(2)
        return f"{prefix}{int(num_str) + 1:0{len(num_str)}d}"
    # Pure numeric string edge case
    if name.isdigit():
        return str(int(name) + 1)
    # Single alpha letter after a separator: "Exp-A" → "Exp-B"
    m = re.match(r'^(.*[\-_\s])([A-Za-z])$', name)
    if m:
        prefix, letter = m.group(1), m.group(2)
        if letter.upper() == 'Z':
            return prefix + ('AA' if letter.isupper() else 'aa')
        return prefix + chr(ord(letter) + 1)
    # No recognised pattern — start a numeric series from 1
    return name + "-1"


class _BackgroundPreservingDelegate(QtWidgets.QStyledItemDelegate):
    """Keep each cell's background colour when its row is selected.

    Qt's default delegate replaces cell backgrounds with the system highlight
    colour on row selection, which overwrites the grey shading used to mark
    irrelevant parameter cells. This delegate preserves those backgrounds and
    instead draws a coloured border to indicate the selected row.
    """
    _BORDER_COLOR = QtGui.QColor(0, 120, 215)  # Windows-accent blue

    def paint(self, painter, option, index):
        opt = QtWidgets.QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        is_selected = bool(opt.state & QtWidgets.QStyle.State_Selected)
        if is_selected:
            bg_brush = index.data(QtCore.Qt.BackgroundRole)
            bg_color = (bg_brush.color() if bg_brush is not None
                        else opt.widget.palette().color(QtGui.QPalette.Base))
            fg_brush = index.data(QtCore.Qt.ForegroundRole)
            fg_color = (fg_brush.color() if fg_brush is not None
                        else opt.palette.color(QtGui.QPalette.Text))
            opt.palette.setColor(QtGui.QPalette.Highlight, bg_color)
            opt.palette.setColor(QtGui.QPalette.HighlightedText, fg_color)
        super().paint(painter, opt, index)
        if is_selected:
            painter.save()
            painter.setPen(QtGui.QPen(self._BORDER_COLOR, 2))
            painter.drawRect(opt.rect.adjusted(1, 1, -1, -1))
            painter.restore()

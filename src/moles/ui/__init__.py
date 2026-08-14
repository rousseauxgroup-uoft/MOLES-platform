"""PyQt-based user interfaces for the MOLES platform.

Subpackages:
  - ``electrolysis``: multichannel electrosynthesis control UI.
  - ``electroanalysis``: tabbed CV / DPV control and analysis UI.
  - ``plots``: post-experiment data viewers (CSV in, plot out).
"""

import os
import PyQt5

# Fix for macOS "cocoa" plugin error — must run before QApplication is created.
# Placing it here ensures it fires once, automatically, whenever any moles UI
# subpackage is first imported, so individual windows don't each have to repeat it.
_qt_plugin_path = os.path.join(os.path.dirname(PyQt5.__file__), "Qt5", "plugins")
if os.path.exists(_qt_plugin_path):
    os.environ["QT_QPA_PLATFORM_PLUGIN_PATH"] = _qt_plugin_path

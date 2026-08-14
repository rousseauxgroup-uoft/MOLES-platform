"""Multichannel electrolysis control UI (PyQt).

Modules:
  - ``main_window``: the top-level ``MainWindow`` and ``main()`` entry point.
  - ``workers``: ``QRunnable`` workers that run experiments off the GUI thread.
  - ``table``: per-row column constants, styling delegate, and naming helpers.
  - ``widgets``: standalone display widgets (e.g. AC waveform preview).

Run with ``python -m moles.ui.electrolysis`` or, after ``pip install -e .``,
with the ``moles-electrolysis`` console script.
"""

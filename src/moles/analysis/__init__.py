"""Data analysis core for the MOLES Data Analysis app.

Everything here is free of Qt so it can be tested, scripted, and reused on its
own; the windows in ``moles.ui.data_analysis`` are a thin layer on top.

  - ``datasets``: recognise and load saved data files (voltammetry for now).
  - ``model``: what a plot window holds — its experiments, their display
    settings, and their labelled points — and how that is saved in a session.
  - ``peaks``: automatic peak and shoulder detection inside a user's box.
"""

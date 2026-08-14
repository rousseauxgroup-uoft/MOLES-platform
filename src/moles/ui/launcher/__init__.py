"""MOLES launcher — the single entry point for a multi-user lab machine.

Shows every registered potentiostat with its live claim status (which app,
which experiment, since when) and launches the individual MOLES apps as
independent processes, so one user's electrolysis run and another user's CV
session can share the machine without stepping on each other's boards.
"""

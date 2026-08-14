"""Electrolysis control loops, one per electrochemical method.

Each method is a plain blocking function that takes a potentiostat object,
a parameters dict, and an output queue/stop event. They are GUI-agnostic —
the multichannel UI runs them inside a worker thread but they would work
just as well from a script or notebook.
"""

from .constant_current import run_constant_current
from .constant_potential import run_constant_potential
from .alternating_current import run_alternating_current
from .alternating_polarity import run_alternating_polarity

__all__ = [
    "run_constant_current",
    "run_constant_potential",
    "run_alternating_current",
    "run_alternating_polarity",
]

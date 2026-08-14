"""Hardware driver layer for the MOLES potentiostat.

Contains the low-level interface to the STM32G473-based open-source
potentiostat (``ps4_ref``), shared signal-processing helpers (``proc_echem``),
and a software-only mock potentiostat (``mock``) used for offline UI testing.
"""

from .ps4_ref import Potentiostat, ADC, DAC, Resistors
from .mock import MockPotentiostat

__all__ = ["Potentiostat", "ADC", "DAC", "Resistors", "MockPotentiostat"]

"""MOLES — Modular Open-source Laboratory for Electrochemistry Screening.

Top-level package. Re-exports the most commonly used hardware types so that
user code can write ``from moles import Potentiostat`` without needing to
remember the full submodule path.
"""

from importlib.metadata import PackageNotFoundError, version as _dist_version

from .driver.ps4_ref import Potentiostat, ADC, DAC, Resistors

try:
    # The single source of the version is pyproject.toml; releases are tagged
    # to match it (v0.2.0 <-> version = "0.2.0").
    __version__ = _dist_version("moles")
except PackageNotFoundError:  # running from a source tree that was never installed
    __version__ = "0.0.0"

__all__ = ["Potentiostat", "ADC", "DAC", "Resistors", "__version__"]

"""Package data files (calibration tables, etc.).

This subpackage exists so that data files can be located at runtime via
``importlib.resources.files("moles.resources")``, regardless of whether the
package is installed normally, in editable mode, or zipped.
"""

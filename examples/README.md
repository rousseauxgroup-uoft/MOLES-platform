# Examples

Standalone scripts for analyzing MOLES output files. These do **not** require
a connected potentiostat — they read CSVs that MOLES has already saved.

## Scripts

### `plot_cyclic_voltammetry.py`

Interactive plotter for cyclic voltammograms.

```bash
python examples/plot_cyclic_voltammetry.py
```

Opens a file picker, asks whether you want to reference the plot to an
internal standard (e.g. Fc/Fc⁺), then renders the voltammogram with a
gradient colorbar indicating scan progress. Double-click a point to label it
with its potential and current.

Accepts both the modern MOLES CV format (with header row) and the older
two-column format (current, potential — no header). See
[`sample_data/Ag2.csv`](sample_data/Ag2.csv) for an example of the old format.

### `plot_electrolysis.py`

Plotter for electrolysis runs (constant-current, constant-potential,
alternating-current, alternating-polarity).

```bash
python examples/plot_electrolysis.py
```

Opens a file picker, then shows current, voltage, and cumulative charge over
time. Checkboxes let you toggle each series on or off.

Expects the MOLES electrolysis CSV format:
`Time (s), Current (mA), Voltage (V), Charge Passed (C)`.

## Sample data

[`sample_data/`](sample_data/) contains a small CV scan you can use to test
`plot_cyclic_voltammetry.py` without owning hardware.

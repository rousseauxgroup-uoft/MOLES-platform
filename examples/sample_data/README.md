# Sample data

## `Ag2.csv` + `Ag2_params.csv`

A cyclic voltammogram scan, stored in the legacy two-column format
(current, potential — no header row). `Ag2_params.csv` contains the scan
parameters used (`start_V`, `min_V`, `max_V`, etc).

Use this with the example plotter to confirm your install is working:

```bash
python examples/plot_cyclic_voltammetry.py
# then select sample_data/Ag2.csv in the file picker
```

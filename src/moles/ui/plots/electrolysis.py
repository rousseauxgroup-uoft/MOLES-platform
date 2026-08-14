import tkinter as tk
from tkinter import filedialog
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.widgets import CheckButtons
import os
import sys

# Methods whose CSV data is an oscillating waveform. These render as a
# max/min envelope (two lines + shaded fill) instead of a raw line, because
# at typical experiment durations the oscillations otherwise overlap into
# a solid block that hides the amplitude trend.
ALTERNATING_METHODS = {"alternating_current", "alternating_polarity"}


def truncate_sig(x, sig=4):
    x = np.asarray(x)
    out = np.zeros_like(x)
    for i, val in enumerate(x):
        if val == 0:
            out[i] = 0
        else:
            decimals = sig - int(np.floor(np.log10(abs(val)))) - 1
            out[i] = np.round(val, decimals)
    return out


def _read_csv_method(path):
    """Return the ``method`` string from a '#'-prefixed metadata line at the
    top of the CSV, or ``None`` if no metadata is present (older files).
    """
    try:
        with open(path, 'r') as f:
            for line in f:
                stripped = line.strip()
                if not stripped.startswith('#'):
                    return None
                lower = stripped.lower()
                if lower.startswith('# method:'):
                    return stripped.split(':', 1)[1].strip()
    except OSError:
        return None
    return None


def _envelope_bins(x, y, n_bins):
    """Bin ``x`` into ``n_bins`` equal-width time bins and return the per-bin
    min and max of ``y``. Empty bins come back as NaN and are dropped by
    the caller before plotting.
    """
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    x_edges = np.linspace(np.min(x), np.max(x), n_bins + 1)
    idx = np.clip(np.searchsorted(x_edges, x, side='right') - 1, 0, n_bins - 1)
    x_mid = 0.5 * (x_edges[:-1] + x_edges[1:])
    y_min = np.full(n_bins, np.nan)
    y_max = np.full(n_bins, np.nan)
    for b in range(n_bins):
        mask = idx == b
        if mask.any():
            yb = y[mask]
            yb = yb[~np.isnan(yb)]
            if yb.size:
                y_min[b] = yb.min()
                y_max[b] = yb.max()
    return x_mid, y_min, y_max

class ElectrolysisPlotter:
    def __init__(self):
        self.datasets = [] # List of dicts: {'path': str, 'name': str, 'df': pd.DataFrame, 'lines': dict}
        self.fig = None
        self.ax = None
        self.check = None
        self.rax = None
        self.metric_map = {
            "Current (mA)": {"style": "-", "label": "Current (mA)"},
            "Voltage (V)": {"style": "--", "label": "Voltage (V)"},
            "OCP (V)": {"style": ":", "label": "OCP (V)"}
        }
        self.metrics_visible = {k: True for k in self.metric_map.keys()}
        self.experiments_visible = [] # Will be list of booleans corresponding to self.datasets
        self.x_axis_mode = "Time (min)" # "Time (min)" or "Charge (F/mol)"
        self.plot_derivative = False
        self.faraday = 96485

    def load_files(self, file_paths=None):
        if not file_paths:
            root = tk.Tk()
            root.withdraw()
            file_paths = filedialog.askopenfilenames(
                title="Select CSV Files",
                filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
            )
            root.destroy()
        
        if not file_paths:
            print("No files selected.")
            return

        for path in file_paths:
            if os.path.exists(path):
                try:
                    # comment='#' skips the metadata lines that the live-run
                    # writer prepends (see moles.data_io._write_metadata_header).
                    df = pd.read_csv(path, comment='#')
                    if "Time (s)" not in df.columns:
                        print(f"Skipping {path}: Missing 'Time (s)' column")
                        continue

                    # Clean data
                    available_cols = []
                    for col in self.metric_map.keys():
                        if col in df.columns:
                            df[col] = truncate_sig(df[col].to_numpy(), sig=4)
                            available_cols.append(col)

                    if not available_cols:
                        print(f"Skipping {path}: No valid data columns")
                        continue

                    # Calculate Charge passed in F/mol if possible
                    # We assume 0.2 mmol by default per user request
                    # Formula: F/mol = (Q in C) / (96485 * mmol/1000)
                    mmol = 0.2
                    if "Charge Passed (C)" in df.columns:
                        df["Charge (F/mol)"] = df["Charge Passed (C)"] / (self.faraday * (mmol / 1000))
                    elif "Current (mA)" in df.columns:
                        # Integrate current if Charge is missing
                        dt = df["Time (s)"].diff().fillna(0)
                        charge_c = (df["Current (mA)"] / 1000 * dt).cumsum()
                        df["Charge (F/mol)"] = charge_c / (self.faraday * (mmol / 1000))

                    method = _read_csv_method(path)
                    name = os.path.basename(path).replace(".csv", "")
                    self.datasets.append({
                        'path': path,
                        'name': name,
                        'df': df,
                        'method': method,
                        'is_alternating': method in ALTERNATING_METHODS,
                        'lines': {}, # metric name -> {'artists': [...], 'y_data': ndarray}
                        'color': None, # Will be assigned during plotting
                        'mmol': mmol
                    })
                    self.experiments_visible.append(True)
                except Exception as e:
                    print(f"Error loading {path}: {e}")
        
        # Sort datasets alphabetically by name
        self.datasets.sort(key=lambda x: x['name'])

        # Assign generated display names
        seen = set()
        for d in self.datasets:
            name_parts = d['name'].split("_")
            if len(name_parts) >= 2:
                base_name = "_".join(name_parts[:2])
            else:
                base_name = d['name']
            
            final_name = base_name
            counter = 1
            while final_name in seen:
                final_name = f"{base_name}_{counter}"
                counter += 1
            seen.add(final_name)
            d['display_name'] = final_name

    def plot(self):
        if not self.datasets:
            print("No datasets to plot.")
            return

        self.fig, self.ax = plt.subplots()
        plt.subplots_adjust(left=0.25) # Make room for checkboxes on the left

        # Assign colors
        cmap = plt.get_cmap('tab10')
        
        for i, dataset in enumerate(self.datasets):
            dataset['color'] = cmap(i % 10)
            
        self.refresh_plot()
        self.setup_controls()

    def setup_controls(self):
        # Create checkbox labels
        # 1. Experiments
        exp_labels = [d['display_name'] for d in self.datasets]
        exp_actives = [True] * len(self.datasets)
        
        # 2. X-Axis Modes
        x_modes = ["Time (min)", "Charge (F/mol)"]
        x_actives = [self.x_axis_mode == m for m in x_modes]
        
        # 3. Metrics
        metric_labels = list(self.metric_map.keys())
        metric_actives = [True] * len(metric_labels)
        
        # 4. Processing
        proc_labels = ["1st Derivative"]
        proc_actives = [self.plot_derivative]
        
        all_labels = exp_labels + ["--- X Axis ---"] + x_modes + ["--- Metrics ---"] + metric_labels + ["--- Processing ---"] + proc_labels
        all_actives = exp_actives + [True] + x_actives + [True] + metric_actives + [True] + proc_actives

        # Calculate position and size for checkboxes
        num_items = len(all_labels)
        height_per_item = 0.04
        panel_height = min(0.9, num_items * height_per_item)
        panel_bottom = 0.5 - panel_height / 2
        
        longest_label = max(len(l) for l in all_labels) if all_labels else 10
        panel_width = max(0.15, min(0.35, longest_label * 0.012))
        
        plt.subplots_adjust(left=panel_width + 0.1) 

        self.rax = plt.axes([0.02, panel_bottom, panel_width, panel_height], frameon=False)
        self.check = CheckButtons(self.rax, all_labels, all_actives)

        # Style the labels
        for i, label_text in enumerate(self.check.labels):
            text_val = label_text.get_text()
            
            if i < len(self.datasets):
                dataset = self.datasets[i]
                label_text.set_color(dataset['color'])
                label_text.set_fontweight('bold')
            elif text_val in ["--- X Axis ---", "--- Metrics ---", "--- Processing ---"]:
                label_text.set_color('gray')
                label_text.set_fontstyle('italic')
            elif text_val in x_modes:
                label_text.set_fontweight('bold')
        
        def on_click(label):
            if label in ["--- X Axis ---", "--- Metrics ---", "--- Processing ---"]:
                return
            
            # Update state
            if label in exp_labels:
                idx = exp_labels.index(label)
                self.experiments_visible[idx] = not self.experiments_visible[idx]
            elif label in x_modes:
                self.x_axis_mode = label
                self.refresh_plot()
                return 
            elif label in ["1st Derivative"]:
                self.plot_derivative = not self.plot_derivative
                self.refresh_plot()
                return
            elif label in metric_labels:
                self.metrics_visible[label] = not self.metrics_visible[label]
            
            self.update_visibility()
            self.auto_scale_y()
            plt.draw()

        self.check.on_clicked(on_click)

    def refresh_plot(self):
        self.ax.clear()
        
        for i, dataset in enumerate(self.datasets):
            color = dataset['color']
            df = dataset['df']
            
            display_name = dataset['display_name']

            dataset['lines'] = {} # Reset line objects
            
            if self.x_axis_mode == "Time (min)":
                x_data = df.get("Time (s)")
                if x_data is not None:
                    x_data = x_data / 60
            else:
                x_data = df.get("Charge (F/mol)")

            if x_data is not None:
                x_vals = x_data.to_numpy()
                # Envelope only makes sense for the raw oscillating signal, so
                # it defers to the line path when the user has the derivative
                # view on.
                use_envelope = dataset.get('is_alternating', False) and not self.plot_derivative
                for metric, props in self.metric_map.items():
                    if metric in df.columns:
                        y_vals = df[metric].to_numpy()
                        if self.plot_derivative:
                            dx = np.gradient(x_vals)
                            dy = np.gradient(y_vals)
                            with np.errstate(divide='ignore', invalid='ignore'):
                                y_data = np.where(dx == 0, np.nan, dy / dx)
                        else:
                            y_data = y_vals

                        if use_envelope:
                            # Bin count scales with data length so short runs
                            # still get resolved and long runs stay smooth.
                            n_bins = min(500, max(30, len(x_vals) // 200))
                            x_mid, y_min, y_max = _envelope_bins(x_vals, y_data, n_bins)
                            valid = ~(np.isnan(y_min) | np.isnan(y_max))
                            x_plot = x_mid[valid]
                            y_lo = y_min[valid]
                            y_hi = y_max[valid]
                            line_hi, = self.ax.plot(
                                x_plot, y_hi,
                                label=f"{display_name} - {props['label']}",
                                linestyle=props['style'],
                                color=color,
                                linewidth=1.0,
                            )
                            line_lo, = self.ax.plot(
                                x_plot, y_lo,
                                linestyle=props['style'],
                                color=color,
                                linewidth=1.0,
                            )
                            fill = self.ax.fill_between(
                                x_plot, y_lo, y_hi,
                                color=color, alpha=0.15, linewidth=0,
                            )
                            # y_data for auto-scaling combines both bounds.
                            dataset['lines'][metric] = {
                                'artists': [line_hi, line_lo, fill],
                                'y_data': np.concatenate([y_lo, y_hi]),
                            }
                        else:
                            line, = self.ax.plot(
                                x_vals,
                                y_data,
                                label=f"{display_name} - {props['label']}",
                                linestyle=props['style'],
                                color=color,
                                linewidth=1.0,
                                marker="o",
                                markersize=1.0
                            )
                            dataset['lines'][metric] = {
                                'artists': [line],
                                'y_data': np.asarray(y_data),
                            }
        
        xlabel = self.x_axis_mode
        if self.x_axis_mode == "Charge (F/mol)":
            xlabel = "Charge (F/mol at 0.2 mmol)"
            
        self.ax.set_xlabel(xlabel)
        ylabel = "Value" if not self.plot_derivative else f"d(Value) / d({self.x_axis_mode.split(' ')[0]})"
        self.ax.set_ylabel(ylabel)
        self.ax.grid(True, linestyle='--', linewidth=0.5)
        self.update_title()
        self.update_visibility()
        self.auto_scale_y()
        plt.draw()

    def update_visibility(self):
        for i, dataset in enumerate(self.datasets):
            exp_visible = self.experiments_visible[i]
            for metric, spec in dataset['lines'].items():
                metric_visible = self.metrics_visible.get(metric, True)
                visible = exp_visible and metric_visible
                for artist in spec['artists']:
                    artist.set_visible(visible)

    def auto_scale_y(self):
        # Re-calculate y-limits based on visible data
        all_y_data = []
        for i, dataset in enumerate(self.datasets):
            if self.experiments_visible[i]:
                for metric, spec in dataset['lines'].items():
                    if self.metrics_visible.get(metric, True) and metric in dataset['df']:
                        y_data = np.asarray(spec['y_data'])
                        valid_y = y_data[~np.isnan(y_data)]
                        if len(valid_y) > 0:
                            all_y_data.append(valid_y)
        
        if all_y_data:
            flat_data = np.concatenate(all_y_data)
            y_min, y_max = np.min(flat_data), np.max(flat_data)
            margin = (y_max - y_min) * 0.05 if y_max != y_min else 0.1
            if margin == 0: margin = 1.0
            self.ax.set_ylim(y_min - margin, y_max + margin)

            # Adjust grid interval
            y_range = y_max - y_min
            interval = 1.0
            if y_range <= 0.01: interval = 0.001
            elif y_range <= 0.05: interval = 0.005
            elif y_range <= 0.1: interval = 0.01
            elif y_range <= 0.5: interval = 0.05
            elif y_range <= 2: interval = 0.1
            elif y_range <= 5: interval = 0.2
            elif y_range <= 10: interval = 0.5
            
            from matplotlib.ticker import MultipleLocator
            self.ax.yaxis.set_major_locator(MultipleLocator(interval))

    def update_title(self):
        if len(self.datasets) == 1:
            title = f"Electrolysis Plot: {self.datasets[0]['name']}"
        else:
            title = f"Electrolysis Plot: {len(self.datasets)} Experiments"
        self.ax.set_title(title)

    def save(self):
        if not self.datasets:
            return
        
        # If single file, use its name. If multiple, use 'combined_plot' or first file name
        if len(self.datasets) == 1:
            save_path = os.path.splitext(self.datasets[0]['path'])[0] + ".png"
        else:
            first_dir = os.path.dirname(self.datasets[0]['path'])
            save_path = os.path.join(first_dir, "combined_electrolysis_plot.png")
            
        self.fig.savefig(save_path)
        print(f"Plot saved to: {save_path}")

    def show(self):
        if self.fig:
            plt.show()

def main():
    plotter = ElectrolysisPlotter()
    
    # Check CLI args
    if len(sys.argv) > 1:
        plotter.load_files(sys.argv[1:])
    else:
        plotter.load_files()
        
    if plotter.datasets:
        plotter.plot()
        plotter.save()
        plotter.show()

if __name__ == "__main__":
    main()
# Plots a cyclic voltammogram from a CSV file.
# The user is prompted to select the CSV file, and asked if they want to offset the plot relative to the Fc/Fc+ redox couple 
# The file is then plotted with a colorbar indicating the scan progress (Time)
# The user can then double-click on the plot to offset the data
# Data points can be labeled by double-clicking on them

# EXAMPLE USAGE:
# First use this script to plot a CV containing Fc internal standard.
# Find the offset value by double-clicking on the Fc/Fc+ redox couple.
# Use the CV_referencer.py script to offset CVs run in the same batch but without Fc
# Plot the offsetted CVs and determine the referenced redox potentials of whatever is in the cell! and save the new file with the reference potential.

import pandas as pd
import matplotlib.pyplot as plt
from matplotlib.collections import LineCollection
from matplotlib.colors import LinearSegmentedColormap
from matplotlib.widgets import RangeSlider
import matplotlib.cm as cm
import numpy as np
import tkinter as tk
from tkinter import filedialog, simpledialog
import os

# Function to create a color-line collection
def get_gradient_line_segments(x, y):
    points = np.array([x, y]).T.reshape(-1, 1, 2)
    segments = np.concatenate([points[:-1], points[1:]], axis=1)
    return segments

def plot_cv_file(file_path, ref_offset=0.0):
    print(f"Processing: {file_path}")
    
    # Detect file format and load data
    try:
        # Check if header exists by reading the first line
        with open(file_path, 'r') as f:
            first_line = f.readline()
        
        if "Time" in first_line:
            # New format with header
            data = pd.read_csv(file_path, header=0)
            # Expected columns: Time (s), Potential (V), Current (uA), Cycle, Index, Target V
            time = data['Time (s)'].values
            voltage = data['Potential (V)'].values
            current = data['Current (uA)'].values
            current_label = 'Current (uA)'
        else:
            # Old format without header
            data = pd.read_csv(file_path, header=None)
            voltage = data[1].values
            current = data[0].values
            # Generate dummy time data for gradient if missing (just linear 0 to 1)
            time = np.linspace(0, 1, len(data))
            current_label = 'Current (A)' # Assuming old format was mostly raw Amps or unspecified

    except Exception as e:
        print(f"Error loading file {file_path}: {e}")
        return

    # Apply reference offset
    voltage = voltage - ref_offset

    # Apply rolling average to smooth the current data
    window_size = 5
    smoothed_current = pd.Series(current).rolling(window=window_size).mean().fillna(pd.Series(current)).values

    # Setup the plot
    fig, ax = plt.subplots(figsize=(10, 7))
    plt.subplots_adjust(bottom=0.25) # Make room for slider
    
    file_name = os.path.basename(file_path)
    title_text = file_name
    if ref_offset != 0:
        title_text += f" (Ref: {ref_offset} V)"
    ax.set_title(title_text)
    ax.grid(True)
    ax.set_xlabel('Potential (V)')
    ax.set_ylabel(current_label)

    # Colormap
    original_viridis = cm.get_cmap('viridis')
    colors = original_viridis(np.linspace(0.2, 0.8, 256))
    cmap_custom = LinearSegmentedColormap.from_list('viridis', colors)

    # Initial Plot
    segments = get_gradient_line_segments(voltage, smoothed_current)
    norm = plt.Normalize(time.min(), time.max())
    lc = LineCollection(segments, cmap=cmap_custom, norm=norm)
    lc.set_array(time)
    lc.set_linewidth(1.5)
    line = ax.add_collection(lc)
    
    ax.set_xlim(voltage.min(), voltage.max())
    ax.set_ylim(smoothed_current.min(), smoothed_current.max())
    
    cbar = fig.colorbar(lc, ax=ax)
    cbar.set_label('Time (s)' if "Time" in first_line else 'Scan Progress')

    # Slider
    ax_slider = plt.axes([0.2, 0.1, 0.60, 0.03])
    slider = RangeSlider(ax_slider, 'Data Range', time.min(), time.max(), valinit=(time.min(), time.max()))

    def update(val):
        t_min, t_max = val
        mask = (time >= t_min) & (time <= t_max)
        
        if not np.any(mask):
            return

        # Subset data
        v_sub = voltage[mask]
        c_sub = smoothed_current[mask]
        t_sub = time[mask]
        
        # We need to remove the old collection and add a new one because segments change size
        # Alternatively, we could just assume the user wants to see the subset, 
        # but LineCollection is efficient enough to just update segments if we are careful,
        # but simply replacing it is easier for variable length.
        
        # Easiest way to "update" is to clear and redraw, but that loses grid/labels.
        # Better: remove the collection and add new one.
        for coll in ax.collections:
             coll.remove()
             
        segments_new = get_gradient_line_segments(v_sub, c_sub)
        lc_new = LineCollection(segments_new, cmap=cmap_custom, norm=norm)
        lc_new.set_array(t_sub) # Color mapping follows original norm but uses subset values
        lc_new.set_linewidth(1.5)
        ax.add_collection(lc_new)
        
        # Update limits? User might want to zoom, or keep original. 
        # Usually keeping original global limits context is better, but maybe autoscaling Y is good.
        # Let's autoscale Y, keep X compatible or autoscale both.
        ax.set_xlim(v_sub.min(), v_sub.max())
        ax.set_ylim(c_sub.min(), c_sub.max())
        
        fig.canvas.draw_idle()

    slider.on_changed(update)

    # Annotation Logic
    def on_click(event):
        if event.dblclick and event.inaxes == ax:
            x, y = event.xdata, event.ydata
            print(f"Annotation: {x:.3f} V, {y:.3f} A")
            ax.annotate(f'({x:.3f}, {y:.3f})', xy=(x, y), xytext=(10, 10), textcoords='offset points',
                        arrowprops=dict(arrowstyle='->', lw=1.5))
            fig.canvas.draw_idle()

    fig.canvas.mpl_connect('button_press_event', on_click)
    
    print(f"Displaying plot for {file_name}. Close window to continue to next file.")
    plt.show()


def main():
    # Use a file dialog to select the CSV file(s)
    root = tk.Tk()
    root.withdraw()  # Hide the main window
    
    # Select multiple files
    file_paths = filedialog.askopenfilenames(title='Select CSV file(s)', filetypes=[('CSV files', '*.csv')])

    if not file_paths:
        print("No files selected. Exiting.")
        return

    # Ask for reference offset
    # We can use simpledialog to ask for a float
    ref_offset = simpledialog.askfloat("Input", "Enter reference potential offset (V) to subtract (e.g. 0.0):", initialvalue=0.0)
    
    if ref_offset is None:
        ref_offset = 0.0

    print(f"Using reference offset: {ref_offset} V")

    for file_path in file_paths:
        plot_cv_file(file_path, ref_offset)

if __name__ == "__main__":
    main()
 
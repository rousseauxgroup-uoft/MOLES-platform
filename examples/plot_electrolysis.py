import tkinter as tk
from tkinter import filedialog
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.widgets import CheckButtons
import os

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

def load_csv_file():
    root = tk.Tk()
    root.withdraw()
    file_path = filedialog.askopenfilename(
        title="Select CSV File",
        filetypes=[("CSV files", "*.csv"), ("All files", "*.*")]
    )
    root.destroy()
    return file_path

def main():
    # Select file
    file_path = load_csv_file()
    if not file_path:
        print("No file selected.")
        return

    # Read CSV
    df = pd.read_csv(file_path)

    # Check for required Time column
    if "Time (s)" not in df.columns:
        print("CSV must contain 'Time (s)' column")
        return

    # Check for available data columns
    available_cols = []
    col_labels = []
    possible_cols = {
        "OCP (V)": "OCP (V)",
        "Current (mA)": "Current (mA)",
        "Voltage (V)": "Voltage (V)"
    }
    
    for col_name, label in possible_cols.items():
        if col_name in df.columns:
            available_cols.append(col_name)
            col_labels.append(label)

    if not available_cols:
        print("CSV must contain at least one of: OCP (V), Current (mA), or Voltage (V)")
        return

    # Truncate values to 4 significant digits (except Time, which usually won't need it)
    for col in available_cols:
        df[col] = truncate_sig(df[col].to_numpy(), sig=4)

    # Set up plot
    fig, ax = plt.subplots()
    plt.subplots_adjust(left=0.3)  # leave space for checkboxes

    # Convert Time (s) to minutes for x-axis
    time_minutes = df["Time (s)"] / 60

    # Plot available columns, save the Line2D objects for toggling
    lines = []
    for col in available_cols:
        line = ax.plot(time_minutes, df[col], label=possible_cols[col], linewidth = 0.1, marker = "o", markersize = 0.5)[0]
        lines.append(line)
    
    ax.set_xlabel("Time (min)")
    ax.set_ylabel("Value")
    ax.legend()

    # Dynamically set grid interval for y-axis
    y_min, y_max = ax.get_ylim()
    y_range = y_max - y_min
    
    # Handle very small ranges (common for OCP data)
    if y_range <= 0.01:
        interval = 0.001
    elif y_range <= 0.05:
        interval = 0.005
    elif y_range <= 0.1:
        interval = 0.01
    elif y_range <= 0.5:
        interval = 0.05
    elif y_range <= 2:
        interval = 0.1
    elif y_range <= 5:
        interval = 0.2
    elif y_range <= 10:
        interval = 0.5
    else:
        interval = 1.0
    
    ax.yaxis.set_major_locator(plt.MultipleLocator(interval))
    ax.grid(axis='y', which='major', linestyle='--', linewidth=0.75)
    
    # Ensure y-axis tick labels are visible and formatted
    from matplotlib.ticker import ScalarFormatter, FuncFormatter
    
    # Determine appropriate decimal places based on interval
    if interval < 0.01:
        decimals = 3
    elif interval < 0.1:
        decimals = 2
    elif interval < 1:
        decimals = 1
    else:
        decimals = 0
    
    # Create formatter with appropriate decimal places
    def format_func(x, p):
        return f'{x:.{decimals}f}'
    
    formatter = FuncFormatter(format_func)
    ax.yaxis.set_major_formatter(formatter)
    
    # Explicitly configure y-axis to show labels
    ax.tick_params(axis='y', which='major', labelsize=10, labelleft=True, left=True)
    ax.yaxis.set_ticks_position('left')
    
    # Force all y-axis labels to be visible - this is critical for OCP plots
    plt.setp(ax.get_yticklabels(), visible=True)
    ax.yaxis.set_tick_params(labelleft=True)
    
    # Ensure the y-axis is not hidden
    ax.spines['left'].set_visible(True)

    # Make interactive checkbox panel for toggling lines
    # Adjust checkbox height based on number of available columns
    num_cols = len(col_labels)
    checkbox_height = max(0.05 * num_cols, 0.10)
    checkbox_bottom = 0.5 - (checkbox_height - 0.10) / 2
    
    rax = plt.axes([0.05, checkbox_bottom, 0.20, checkbox_height])
    check = CheckButtons(rax, col_labels, [True] * num_cols)

    def func(label):
        index = col_labels.index(label)
        lines[index].set_visible(not lines[index].get_visible())
        plt.draw()

    check.on_clicked(func)

    ax.set_title(f"Electrochem Plot: {os.path.basename(file_path)}")
    plt.show()

if __name__ == "__main__":
    main()
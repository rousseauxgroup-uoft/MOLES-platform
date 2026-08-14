"""Data input/output and basic charge calculations for electrolysis runs.

Handles:
  - Faraday's law math for converting moles of substrate into a target charge.
  - Opening a CSV file at the start of an experiment so each data point is
    written as it streams in (preserving partial data on early stops).
  - A one-shot CSV writer for cases where all data is held in memory first.

The output directory is read from user settings (see ``moles.settings``) at
the moment each file is opened, so changes made in ``moles-settings`` take
effect on the next experiment without needing a restart.
"""

import csv
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple

from .settings import get_data_folder

logger = logging.getLogger(__name__)

# Faraday's constant in coulombs per mole of electrons.
FARADAY_CONSTANT = 96485  # C/mol


def _write_metadata_header(csv_file, params: Dict) -> None:
    """Prepend '#'-prefixed metadata lines so the post-hoc plotter can tell
    an alternating-method run apart from a constant-method one without
    guessing from the numbers. pandas/numpy readers skip '#' lines via
    ``comment='#'``.
    """
    method = params.get('method', '')
    csv_file.write(f"# method: {method}\n")
    # Record the substrate loading so the plotter can scale each run's charge
    # axis (F/mol) from its own value rather than assuming a fixed amount.
    mmol = params.get('mmol')
    if mmol is not None:
        csv_file.write(f"# mmol: {mmol}\n")
    if method == 'alternating_current':
        csv_file.write(f"# frequency_hz: {params.get('ac_frequency_hz', '')}\n")
    elif method == 'alternating_polarity':
        csv_file.write(f"# frequency_hz: {params.get('ap_frequency_hz', '')}\n")


def calculate_total_charge(mmol: float, electrons_per_mol: float) -> float:
    """Return the charge (in coulombs) needed to fully convert ``mmol``
    millimoles of substrate, given the number of electrons consumed per mole.

    Implements Faraday's law: Q = n * F * N, where N is moles.
    """
    return (mmol / 1000) * electrons_per_mol * FARADAY_CONSTANT


def open_data_csv(params: Dict, pot_id) -> Tuple:
    """Open a CSV file for live streaming of one experiment's data.

    Returns the open file handle and its absolute path. Writing each row as it
    arrives means data is preserved even if the experiment stops early.
    """
    data_folder = get_data_folder()
    data_folder.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    exp_name = params.get('experiment_name', 'Experiment')
    filename = data_folder / f"{exp_name}_PS-{pot_id}_{timestamp}.csv"
    csv_file = open(filename, 'w', newline='')
    _write_metadata_header(csv_file, params)
    writer = csv.writer(csv_file)
    writer.writerow(['Time (s)', 'Current (mA)', 'Voltage (V)', 'Charge Passed (C)'])
    logger.info("[PS %s] Writing data to: %s", pot_id, filename)
    return csv_file, str(filename)


def save_data_to_csv(data_points: List, params: Dict, pot_id) -> Path:
    """Write a complete in-memory list of data points to a single CSV file.

    Used as an alternative to ``open_data_csv`` when data is buffered in
    memory and saved at the end of the experiment in one go.
    """
    data_folder = get_data_folder()
    data_folder.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    exp_name = params.get('experiment_name', 'Experiment')
    filename = data_folder / f"{exp_name}_PS-{pot_id}_{timestamp}.csv"

    with open(filename, 'w', newline='') as csvfile:
        _write_metadata_header(csvfile, params)
        writer = csv.writer(csvfile)
        writer.writerow(['Time (s)', 'Current (mA)', 'Voltage (V)', 'Charge Passed (C)'])
        writer.writerows(data_points)
    logger.info("[PS %s] Data saved to: %s", pot_id, filename)
    return filename

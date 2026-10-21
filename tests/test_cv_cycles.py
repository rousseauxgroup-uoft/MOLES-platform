"""Tests that a CV runs, and labels, exactly the number of cycles requested.

Regression cover for the bug where Start = End = the second vertex (e.g. a
0 -> -2 V window started at 0 V with a Negative first sweep) ran one more
cycle than requested: the lead-in and lead-out together formed an uncounted
round trip. Everything runs on the waveform builders and the mock — no
hardware involved.
"""

import numpy as np
import pytest

from moles.driver.mock import MockLivePotentiostat
from moles.driver.ps4_ref import Potentiostat, cv_cycle_numbers

STEP_V = 0.01


def _cv(min_V, max_V, cycles, start_V, last_V, direction):
    return np.asarray(Potentiostat._build_cv_waveform(
        None, min_V, max_V, cycles, STEP_V, start_V, last_V, direction))


def _turning_points(waveform):
    """The potentials where the sweep starts, reverses, and ends."""
    w = np.asarray(waveform, dtype=float)
    points, last_dir = [w[0]], 0
    for a, b in zip(w[:-1], w[1:]):
        d = np.sign(b - a)
        if d == 0:
            continue
        if last_dir and d != last_dir:
            points.append(a)
        last_dir = d
    points.append(w[-1])
    return np.round(points, 6).tolist()


# ----------------------------------------------------------------------
# Sweep shape
# ----------------------------------------------------------------------

@pytest.mark.parametrize("direction", ['Negative', 'Positive'])
def test_reported_case_runs_one_cycle_in_either_direction(direction):
    # Start = End = Max = 0 V, Min = -2 V, 1 cycle: a single 0 -> -2 -> 0.
    w = _cv(-2.0, 0.0, 1, 0.0, 0.0, direction)
    assert _turning_points(w) == [0.0, -2.0, 0.0]


@pytest.mark.parametrize("cycles", [1, 2, 3])
def test_n_cycles_are_n_round_trips(cycles):
    w = _cv(-2.0, 0.0, cycles, 0.0, 0.0, 'Negative')
    assert _turning_points(w) == [0.0] + [-2.0, 0.0] * cycles


def test_mid_window_start_returns_to_start_each_cycle():
    assert _turning_points(_cv(-2.0, 0.0, 1, -1.0, -1.0, 'Negative')) == [
        -1.0, -2.0, 0.0, -1.0]
    assert _turning_points(_cv(-2.0, 0.0, 2, -1.0, -1.0, 'Negative')) == [
        -1.0, -2.0, 0.0, -2.0, 0.0, -1.0]


def test_start_on_first_vertex_is_a_clean_cycle():
    # No lead-in or lead-out: the waveform is just the vertex-to-vertex sweeps.
    assert _turning_points(_cv(-2.0, 0.0, 1, -2.0, -2.0, 'Negative')) == [
        -2.0, 0.0, -2.0]
    assert _turning_points(_cv(-2.0, 0.0, 1, 0.0, 0.0, 'Positive')) == [
        0.0, -2.0, 0.0]


def test_start_on_first_vertex_emits_no_lead_in():
    w = _cv(-0.5, 0.5, 1, -0.5, -0.5, 'Negative')
    assert w[0] == -0.5
    assert w[1] > w[0]  # already sweeping toward Max, no zero-length lead-in


def test_lead_out_leaves_from_second_vertex():
    # Ending at the first vertex adds only the final sweep back down.
    assert _turning_points(_cv(-2.0, 0.0, 1, -1.0, -2.0, 'Negative')) == [
        -1.0, -2.0, 0.0, -2.0]


# ----------------------------------------------------------------------
# Cycle labels
# ----------------------------------------------------------------------

def test_cycle_numbers_split_reported_case_at_the_return_to_start():
    w = _cv(-2.0, 0.0, 2, 0.0, 0.0, 'Negative')
    labels = cv_cycle_numbers(w, -2.0, 0.0, 2)
    assert set(labels) == {0, 1}
    assert np.all(np.diff(labels) >= 0)
    first, second = w[labels == 0], w[labels == 1]
    # Cycle 0 is the whole 0 -> -2 -> 0 round trip, closing point included
    assert np.isclose(first.min(), -2.0)
    assert first[-1] == 0.0
    # Cycle 1 begins heading back down
    assert second[0] < 0.0
    assert abs(len(first) - len(second)) <= 2


def test_cycle_numbers_mid_window_start():
    w = _cv(-2.0, 0.0, 2, -1.0, -1.0, 'Negative')
    labels = cv_cycle_numbers(w, -2.0, 0.0, 2)
    first = w[labels == 0]
    assert _turning_points(first) == [-1.0, -2.0, 0.0, pytest.approx(-1.0, abs=STEP_V)]
    assert abs(np.sum(labels == 0) - np.sum(labels == 1)) <= 2


def test_cycle_numbers_keep_lead_out_with_last_cycle():
    # 0 -> -2 -> 0 -> -2 -> 0 -> -2: the final sweep to End exceeds two round
    # trips but must not open a third cycle.
    w = _cv(-2.0, 0.0, 2, 0.0, -2.0, 'Negative')
    labels = cv_cycle_numbers(w, -2.0, 0.0, 2)
    assert labels.max() == 1
    assert labels[-1] == 1


def test_cycle_numbers_of_a_stopped_run_match_the_full_run():
    # A stopped scan labels its truncated waveform; the labels must agree
    # with the full run's for the points that were measured.
    w = _cv(-2.0, 0.0, 3, 0.0, 0.0, 'Negative')
    full = cv_cycle_numbers(w, -2.0, 0.0, 3)
    k = len(w) // 2
    assert np.array_equal(cv_cycle_numbers(w[:k], -2.0, 0.0, 3), full[:k])


def test_cycle_numbers_zero_width_window():
    assert np.array_equal(cv_cycle_numbers([0.1, 0.1, 0.1], 0.0, 0.0, 2),
                          np.zeros(3))
    assert cv_cycle_numbers([], -1.0, 0.0, 1).size == 0


# ----------------------------------------------------------------------
# Test Mode (mock) matches the driver
# ----------------------------------------------------------------------

def test_mock_cv_runs_and_labels_requested_cycles():
    ps = MockLivePotentiostat(chunk_delay_s=0)
    data = ps.perform_CV(min_V=-2.0, max_V=0.0, cycles=2, mV_s=1000.0,
                         step_hz=100.0, start_V=0.0, last_V=0.0,
                         scan_direction='Negative')
    assert _turning_points(data[:, 5]) == [0.0, -2.0, 0.0, -2.0, 0.0]
    assert set(data[:, 3]) == {0, 1}

"""Box peak and shoulder detection (moles.analysis.peaks).

Synthetic voltammograms use the point density of real MOLES files: 0.4 mV per
point for a CV at 100 mV/s, 5 mV per point for DPV, with 0.7 µA of noise.
"""

import numpy as np
import pytest

from moles.analysis.datasets import sweep_branches
from moles.analysis.model import (
    EXTREMUM_MAX, EXTREMUM_MIN, LABEL_PEAK, LABEL_SHOULDER, PeakSettings,
)
from moles.analysis.peaks import detect_in_box, window_points

CV_STEP_V = 0.0004
NOISE_UA = 0.7
EVERYTHING = (-10.0, 10.0, -1e4, 1e4)


def gauss(x, centre, width, height):
    return height * np.exp(-(x - centre) ** 2 / (2 * width ** 2))


def sweep(start, stop, step=CV_STEP_V):
    step = step if stop > start else -step
    return np.arange(start, stop, step)


def detect(potential, current, box=EVERYTHING, **kwargs):
    branch, direction = sweep_branches(potential, 0.005)
    return detect_in_box(potential, current, branch, direction, box, **kwargs)


def found(detection, potential):
    return [(f.kind, f.extremum, round(float(potential[f.index]), 2))
            for f in detection.features]


@pytest.fixture
def noise():
    rng = np.random.default_rng(7)
    return lambda n: rng.normal(0, NOISE_UA, n)


def test_a_single_oxidation_peak_and_nothing_at_its_feet(noise):
    e = sweep(-0.5, 1.0)
    i = gauss(e, 0.3, 0.05, 50) + noise(len(e))

    d = detect(e, i)

    assert found(d, e) == [(LABEL_PEAK, EXTREMUM_MAX, 0.3)]


def test_a_single_reduction_peak_on_a_negative_sweep(noise):
    e = sweep(-0.5, -2.0)
    i = -gauss(e, -1.3, 0.05, 40) + noise(len(e))

    d = detect(e, i)

    assert found(d, e) == [(LABEL_PEAK, EXTREMUM_MIN, -1.3)]


def test_the_reported_point_is_a_raw_point_at_the_smoothed_extremum(noise):
    e = sweep(-0.5, 1.0)
    i = gauss(e, 0.3, 0.05, 50) + noise(len(e))

    peak = detect(e, i).features[0]

    assert abs(e[peak.index] - 0.3) < 0.003
    assert peak.prominence_uA == pytest.approx(50, abs=2)


@pytest.mark.parametrize("hidden_at, expected", [
    (0.2, 0.2),    # on the rising flank of the main peak
    (0.4, 0.4),    # in the main peak's decaying tail
])
def test_oxidation_shoulders(noise, hidden_at, expected):
    e = sweep(-0.5, 1.0)
    i = gauss(e, 0.3, 0.05, 50) + gauss(e, hidden_at, 0.03, 20) + noise(len(e))

    features = found(detect(e, i), e)

    assert (LABEL_PEAK, EXTREMUM_MAX, 0.3) in features
    shoulders = [f for f in features if f[0] == LABEL_SHOULDER]
    assert len(shoulders) == 1
    assert shoulders[0][1] == EXTREMUM_MAX
    assert shoulders[0][2] == pytest.approx(expected, abs=0.02)


def test_reduction_shoulder_like_nibr2_bpy(noise):
    e = sweep(-0.5, -2.0)
    i = -gauss(e, -1.72, 0.06, 70) - gauss(e, -1.58, 0.04, 30) + noise(len(e))

    features = found(detect(e, i), e)

    assert (LABEL_PEAK, EXTREMUM_MIN, -1.72) in features
    shoulders = [f for f in features if f[0] == LABEL_SHOULDER]
    assert len(shoulders) == 1
    assert shoulders[0][1] == EXTREMUM_MIN
    assert shoulders[0][2] == pytest.approx(-1.58, abs=0.02)


def test_a_plateau_knee_is_not_a_shoulder(noise):
    e = sweep(-0.5, 1.0)
    i = 40 / (1 + np.exp(-(e - 0.3) / 0.03)) + noise(len(e))

    assert detect(e, i).features == []


def test_noise_alone_finds_nothing(noise):
    e = sweep(-0.5, 1.0)

    assert detect(e, noise(len(e))).features == []


def test_a_switching_potential_is_not_a_peak(noise):
    # Current keeps falling into the vertex and recovers after it: in point
    # order that is a minimum, but it is the sweep turning, not a peak.
    e = np.concatenate([sweep(-0.5, -2.0), sweep(-2.0, -0.5)])
    i = 60 * (e + 0.5) + noise(len(e))

    d = detect(e, i, box=(-2.1, -1.7, -1e4, 1e4))

    assert d.features == []
    assert len(d.runs) == 2   # one run per sweep direction


def test_a_box_over_both_sweeps_reports_each(noise):
    forward, back = sweep(-0.5, 1.0), sweep(1.0, -0.5)
    e = np.concatenate([forward, back])
    i = np.concatenate([gauss(forward, 0.3, 0.05, 50), -gauss(back, 0.24, 0.05, 40)])
    i = i + noise(len(e))

    features = found(detect(e, i, box=(0.1, 0.5, -60, 60)), e)

    assert features == [(LABEL_PEAK, EXTREMUM_MAX, 0.3), (LABEL_PEAK, EXTREMUM_MIN, 0.24)]


def test_the_box_is_in_displayed_coordinates(noise):
    e = sweep(-0.5, 1.0)
    i = gauss(e, 0.3, 0.05, 50) + noise(len(e))
    offsets = (-0.4, 100.0)   # the peak is drawn at (-0.1 V, 150 µA)

    shifted = detect(e, i, box=(-0.2, 0.0, 120, 160), offsets=offsets)
    unshifted = detect(e, i, box=(0.2, 0.4, 20, 60), offsets=offsets)

    assert found(shifted, e) == [(LABEL_PEAK, EXTREMUM_MAX, 0.3)]
    assert unshifted.features == []


def test_the_data_range_limits_detection(noise):
    e = sweep(-0.5, 1.0)
    i = gauss(e, 0.0, 0.05, 30) + gauss(e, 0.6, 0.05, 30) + noise(len(e))
    second_half = (int(np.argmin(abs(e - 0.3))), len(e) - 1)

    features = found(detect(e, i, index_range=second_half), e)

    assert features == [(LABEL_PEAK, EXTREMUM_MAX, 0.6)]


def test_valleys_are_left_out_unless_asked_for(noise):
    e = sweep(-0.5, 1.0)
    i = gauss(e, 0.3, 0.05, 50) + gauss(e, 0.15, 0.04, 15) + noise(len(e))

    with_valleys = found(detect(e, i, settings=PeakSettings(report_valleys=True)), e)
    without = found(detect(e, i), e)

    assert [f[1] for f in with_valleys] == [EXTREMUM_MAX, EXTREMUM_MIN, EXTREMUM_MAX]
    assert [f[1] for f in without] == [EXTREMUM_MAX, EXTREMUM_MAX]


def test_a_box_edge_through_noise_does_not_split_the_feature(noise):
    # The box's top edge runs along the noisy peak top, so points flicker in
    # and out of it; they still form one run and one peak.
    e = sweep(-0.5, 1.0)
    i = gauss(e, 0.3, 0.1, 50) + noise(len(e))

    d = detect(e, i, box=(0.1, 0.5, 0, 50))

    assert len(d.runs) == 1
    assert found(d, e) == [(LABEL_PEAK, EXTREMUM_MAX, 0.3)]


def test_sparse_dpv_points_still_smooth_and_detect():
    rng = np.random.default_rng(3)
    e = sweep(-0.5, -2.0, step=0.005)
    i = -gauss(e, -1.18, 0.05, 55) + rng.normal(0, 0.5, len(e))

    d = detect(e, i)

    assert d.runs[0].window_points == 7
    assert found(d, e) == [(LABEL_PEAK, EXTREMUM_MIN, -1.18)]


def test_a_fixed_prominence_above_the_peak_finds_nothing(noise):
    e = sweep(-0.5, 1.0)
    i = gauss(e, 0.3, 0.05, 50) + noise(len(e))

    assert detect(e, i, settings=PeakSettings(prominence_uA=60.0)).features == []


def test_auto_prominence_has_a_floor_and_follows_the_noise():
    e = sweep(-0.5, 1.0)
    rng = np.random.default_rng(1)
    quiet = detect(e, gauss(e, 0.3, 0.05, 50)).runs[0]
    noisy = detect(e, gauss(e, 0.3, 0.05, 50) + rng.normal(0, 20, len(e))).runs[0]

    assert quiet.threshold_uA == pytest.approx(1.0)
    assert noisy.threshold_uA == pytest.approx(5 * noisy.noise_uA)
    assert noisy.threshold_uA > 1.0


@pytest.mark.parametrize("volts_per_point, available, expected", [
    (0.0004, 5000, 75),   # CV at 100 mV/s: 30 mV is 75 points
    (0.0048, 300, 7),     # DPV with 5 mV steps: the 7-point minimum
    (0.00004, 5000, 751),  # CV at 10 mV/s
    (0.0004, 40, 39),     # a short branch caps the window
    (0.0004, 5, 0),       # too short to smooth at all
])
def test_window_points(volts_per_point, available, expected):
    assert window_points(PeakSettings(), volts_per_point, available) == expected

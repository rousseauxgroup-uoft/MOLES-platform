"""The bench script that measures each current range, run on its simulated board.

dev/measure_current_ranges.py is run by hand at the bench to find out what
each gain index really switches in. These tests drive it through the real
driver code against the script's simulated board, so a surprising bench
result can be told apart from a bug in the script.
"""
import importlib.util
import re
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "dev" / "measure_current_ranges.py"
FIRMWARE = Path(__file__).resolve().parents[1] / "firmware" / "driver-master" / "potentiostat" / "Core"


@pytest.fixture(scope="module")
def mcr():
    spec = importlib.util.spec_from_file_location("measure_current_ranges", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def measure(mcr, range_ohms, resistor_ohms, ranges=(0, 1, 2, 3, 4), board=None):
    board = board or mcr.SimulatedBoard(resistor_ohms, range_ohms, seed=7)
    ps = mcr.Potentiostat(serial_port="SIMULATED")
    ps.serial = board
    results, _, _ = mcr.run_measurement(ps, list(ranges), resistor_ohms, mcr.Limits(), 12,
                                        mcr.Timing(0.0, 0.0), mcr.SampleLog())
    return board, results


def test_predictions_follow_the_calibration_evidence(mcr):
    # Every onboarded board calibrates its range 0 at 93.1-93.6 ohm; only the
    # switch-on-when-HIGH explanation puts range 0 there
    assert 92.5 < mcr.MODELS["as-built"][0] < 95.0
    assert mcr.MODELS["software"][0] == 100.0
    assert mcr.MODELS["on-when-low"][0] > 50e3
    # The candidate fix keeps range 0 exactly as it is
    assert mcr.MODELS["proposed-fix"][0] == mcr.MODELS["as-built"][0]


def test_as_built_board_is_recognised_and_left_safe(mcr):
    truth = mcr.MODELS["as-built"]
    board, results = measure(mcr, truth, 1000.0)
    for r in results:
        assert r["status"] == "ok"
        assert r["measured_ohms"] == pytest.approx(truth[r["range"]], rel=0.003)
    assert mcr.consistent_models(results) == ["as-built"]
    assert board.switch == 0
    assert board.gain == 0
    assert board.auto_gain == 0


def test_ranges_are_measured_as_they_are(mcr):
    truth = (120.0, 470.0, 2200.0, 800.0, 150.0)
    _, results = measure(mcr, truth, 1000.0)
    for r in results:
        assert r["measured_ohms"] == pytest.approx(truth[r["range"]], rel=0.005)
    assert mcr.consistent_models(results) == []


def test_range_too_sensitive_for_the_resistor_gets_a_suggestion(mcr):
    truth = mcr.MODELS["proposed-fix"]
    board, results = measure(mcr, truth, 1000.0)
    by_range = {r["range"]: r for r in results}
    assert by_range[4]["status"] == "too-sensitive"
    # Suggests a resistor that brings the 1 Mohm range to a measurable output
    value, unit = re.search(r"(\d+(?:\.\d+)?) (k|M)ohm", by_range[4]["note"]).groups()
    assert 470e3 <= float(value) * {"k": 1e3, "M": 1e6}[unit] <= 1.5e6
    for index in (0, 1, 2, 3):
        assert by_range[index]["measured_ohms"] == pytest.approx(truth[index], rel=0.005)
    assert mcr.consistent_models(results) == ["proposed-fix"]
    assert board.switch == 0


def test_large_resistor_measures_the_sensitive_ranges(mcr):
    truth = mcr.MODELS["proposed-fix"]
    _, results = measure(mcr, truth, 100e3, ranges=(2, 3, 4))
    for r in results:
        assert r["status"] == "ok"
        assert r["measured_ohms"] == pytest.approx(truth[r["range"]], rel=0.005)


def test_zero_reading_is_a_fixed_voltage_on_every_range(mcr):
    truth = mcr.MODELS["proposed-fix"]
    _, results = measure(mcr, truth, 1000.0, ranges=(0, 1, 2))
    for r in results:
        # The simulated board's zero-current reference sits 50 mV above ground
        assert r["open_offset_V"] == pytest.approx(-0.050, abs=0.002)
        assert r["zero_reading_A"] == pytest.approx(0.050 / truth[r["range"]], rel=0.05)


def test_cell_switch_opens_when_the_board_stops_answering(mcr):
    board = mcr.SimulatedBoard(1000.0, mcr.MODELS["as-built"], seed=7)
    board.fail_after_reads = 150
    with pytest.raises(mcr.SerialReadTimeout):
        measure(mcr, None, 1000.0, board=board)
    assert board.switch == 0


def test_disconnected_working_electrode_is_reported(mcr):
    # A lead off the resistor: the potential still follows, but no current flows
    board = mcr.SimulatedBoard(1e12, mcr.MODELS["as-built"], seed=7)
    with pytest.raises(mcr.WiringError, match="WE lead"):
        measure(mcr, None, 1000.0, board=board)
    assert board.switch == 0


def test_disconnected_reference_is_reported(mcr):
    board = mcr.SimulatedBoard(1000.0, mcr.MODELS["as-built"], seed=7)
    board.re_connected = False
    with pytest.raises(mcr.WiringError, match="RE and CE"):
        measure(mcr, None, 1000.0, board=board)
    assert board.switch == 0


def test_simulated_run_saves_files_and_touches_no_hardware(mcr, tmp_path, monkeypatch):
    import serial

    def no_port(*args, **kwargs):
        raise AssertionError("the simulated run opened a serial port")

    monkeypatch.setattr(serial, "Serial", no_port)
    monkeypatch.setattr(mcr.Potentiostat, "connect", no_port)
    code = mcr.main(["--resistor", "1000", "--simulate", "as-built", "--readings", "10",
                     "--label", "bench test", "--outdir", str(tmp_path)])
    assert code == 0
    summary = list(tmp_path.glob("ranges_SIM-as-built_bench-test_*_summary.csv"))
    samples = list(tmp_path.glob("ranges_SIM-as-built_bench-test_*_samples.csv"))
    assert len(summary) == 1 and len(samples) == 1
    lines = summary[0].read_text().splitlines()
    assert len(lines) == 6
    assert all(",ok," in line for line in lines[1:])


def test_suggested_resistors_are_standard_values(mcr):
    assert mcr.standard_resistor(812e3) == pytest.approx(820e3)
    assert mcr.standard_resistor(200.0) == pytest.approx(220.0)
    assert mcr.standard_resistor(96.0) == pytest.approx(100.0)


def test_bench_path_runs_end_to_end_on_the_simulator(mcr, tmp_path, monkeypatch, capsys):
    # Everything the bench run does, with the serial port swapped for the
    # simulated board: registry lookup, prompt, connect, report, cleanup
    boards = []

    def attach_simulator(self):
        self.serial = mcr.SimulatedBoard(998.7, mcr.MODELS["as-built"], seed=3)
        boards.append(self.serial)

    registry_row = {"slope": 1.2081, "intercept": 0.0494, "read_slope": 1.0691,
                    "read_intercept": -0.592}
    real_timing = mcr.Timing
    monkeypatch.setattr(mcr.Potentiostat, "connect", attach_simulator)
    monkeypatch.setattr(mcr, "resolve_board", lambda args: ("/dev/fake", "TEST", registry_row))
    monkeypatch.setattr(mcr, "Timing", lambda *args: real_timing(0.0, 0.0))
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    code = mcr.main(["--resistor", "998.7", "--board", "TEST", "--readings", "10",
                     "--outdir", str(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "Every measured range matches firmware with the old switch table" in out
    assert "Range 0 check" in out
    assert boards[0].switch == 0
    assert not boards[0].is_open
    assert "TEST" not in mcr.session.ACTIVE_POTENTIOSTATS


def test_resistor_entered_ten_times_too_large_is_flagged(mcr, tmp_path, monkeypatch, capsys):
    # The 27 Sep bench run: a ~99 kohm resistor entered as 999 kohm made every
    # range read ~10x high; the range 0 check against the calibration catches it
    def attach_simulator(self):
        self.serial = mcr.SimulatedBoard(99.2e3, mcr.MODELS["as-built"], seed=3)

    real_timing = mcr.Timing
    monkeypatch.setattr(mcr.Potentiostat, "connect", attach_simulator)
    monkeypatch.setattr(mcr, "resolve_board",
                        lambda args: ("/dev/fake", "TEST", {"slope": 1.2081, "intercept": 0.0494,
                                                            "read_slope": 1.0691,
                                                            "read_intercept": -0.592}))
    monkeypatch.setattr(mcr, "Timing", lambda *args: real_timing(0.0, 0.0))
    monkeypatch.setattr("builtins.input", lambda prompt="": "")

    mcr.main(["--resistor", "999000", "--board", "TEST", "--readings", "10",
              "--outdir", str(tmp_path)])
    assert "Check the --resistor value" in capsys.readouterr().out


def test_range_zero_check_wording(mcr):
    assert mcr.check_note(0.5) == ""
    assert "multimeter" in mcr.check_note(-5.0)
    assert "--resistor" in mcr.check_note(851.0)


def test_firmware_switch_table_is_the_one_the_bench_confirmed(mcr):
    # The table in the firmware source, in gain-index order, must be the
    # corrected one the script's "proposed-fix" prediction is built from
    header = (FIRMWARE / "Inc" / "analog.h").read_text()
    codes = dict(re.findall(r"#define\s+(SW_GAIN_\w+)\s+\((0x[0-9a-fA-F]+)\s*<<\s*TIA_SW_SHIFT\)",
                            header))
    source = (FIRMWARE / "Src" / "analog.c").read_text()
    matrix = source[source.index("tia_switch_matrix[] = {"):]
    order = re.findall(r"\{(SW_GAIN_\w+),", matrix)[:5]
    assert tuple(int(codes[name], 16) for name in order) == mcr.PROPOSED_TABLE

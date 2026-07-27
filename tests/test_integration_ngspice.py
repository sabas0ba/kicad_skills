"""End-to-end simulation tests. These need the real ngspice from the container."""

import json

import pytest

from eda_toolkit.spice import measure, rawfile, runner

pytestmark = pytest.mark.ngspice


def test_version_string():
    assert "ngspice" in runner.version().lower()


def test_rc_lowpass_matches_theory(rc_netlist, tmp_path):
    summary = runner.run_netlist(rc_netlist, tmp_path / "out")
    assert summary["ok"], summary["errors"]
    assert summary["lint"] == []

    plots = {p["analysis"]: p for p in summary["plots"]}
    assert set(plots) == {"ac", "tran"}

    ac_out = plots["ac"]["measurements"]["signals"]["v(out)"]
    # R = 1k, C = 159.155 nF  ->  fc = 1 kHz
    assert ac_out["f_minus_3db_hz"] == pytest.approx(1000.0, rel=0.01)
    assert ac_out["gain_db_max"] == pytest.approx(0.0, abs=0.01)
    assert ac_out["gain_db_at_f_stop"] == pytest.approx(-40.0, abs=0.5)  # -20 dB/decade

    tran_out = plots["tran"]["measurements"]["signals"]["v(out)"]
    assert tran_out["waveform"] == "periodic"
    # 1 kHz sine through a 1 kHz low pass: amplitude drops by 3 dB (0.707)
    assert tran_out["amplitude"] == pytest.approx(0.707, rel=0.05)
    assert tran_out["estimated_frequency_hz"] == pytest.approx(1000.0, rel=0.02)


def test_run_writes_artifacts(rc_netlist, tmp_path):
    out = tmp_path / "out"
    summary = runner.run_netlist(rc_netlist, out)
    assert (out / "summary.json").exists()
    assert (out / "ngspice.log").exists()
    assert json.loads((out / "summary.json").read_text())["ok"]
    for plot in summary["plots"]:
        assert plot["csv"].endswith(".csv")
        assert plot["plot"].endswith(".png")


def test_operating_point_of_a_divider(tmp_path):
    deck = tmp_path / "divider.cir"
    deck.write_text(
        "* resistive divider\n"
        "V1 in 0 DC 10\n"
        "R1 in out 1k\n"
        "R2 out 0 3k\n"
        ".op\n"
        ".end\n"
    )
    summary = runner.run_netlist(deck, tmp_path / "out", make_plots=False)
    assert summary["ok"]
    values = summary["plots"][0]["measurements"]["values"]
    assert values["v(out)"] == pytest.approx(7.5, rel=1e-6)


def test_transient_step_response(tmp_path):
    deck = tmp_path / "step.cir"
    deck.write_text(
        "* RC step response, tau = 100 us\n"
        "V1 in 0 PWL(0 0 1n 1)\n"
        "R1 in out 1k\n"
        "C1 out 0 100n\n"
        ".tran 1u 1m\n"
        ".end\n"
    )
    summary = runner.run_netlist(deck, tmp_path / "out", make_plots=False)
    out = summary["plots"][0]["measurements"]["signals"]["v(out)"]
    assert out["waveform"] == "transient"
    assert out["final"] == pytest.approx(1.0, abs=0.01)
    assert out["rise_time_10_90_s"] == pytest.approx(2.2 * 100e-6, rel=0.1)


def test_broken_deck_reports_the_log(tmp_path):
    deck = tmp_path / "broken.cir"
    deck.write_text("* broken\nX1 a b nonexistent_subckt\n.op\n.end\n")
    from eda_toolkit.util import EdaError

    with pytest.raises(EdaError):
        runner.run_netlist(deck, tmp_path / "out")
    assert (tmp_path / "out" / "ngspice.log").exists()


def test_thd_of_a_diode_clipper(tmp_path):
    deck = tmp_path / "clip.cir"
    deck.write_text(
        "* soft clipper: strong even/odd harmonics\n"
        "V1 in 0 SIN(0 2 1k)\n"
        "R1 in out 1k\n"
        "D1 out 0 DMOD\n"
        "D2 0 out DMOD\n"
        ".model DMOD D(IS=1e-14)\n"
        ".tran 5u 10m\n"
        ".end\n"
    )
    summary = runner.run_netlist(deck, tmp_path / "out", make_plots=False)
    plot = rawfile.parse(summary["raw"])[0]
    result = measure.thd(plot, "v(out)", 1000.0, harmonics=9, skip_seconds=1e-3)
    assert result["thd_percent"] > 5.0  # the clipper really does distort

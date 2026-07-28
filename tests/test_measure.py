"""Measurements are checked against analytically known circuits."""

import numpy as np
import pytest

from eda_toolkit.spice import measure
from eda_toolkit.spice.rawfile import Plot


def make_plot(plotname, sweep_name, sweep, signals, flags=("real",)):
    plot = Plot(plotname=plotname, flags=list(flags))
    plot.variables = [{"index": 0, "name": sweep_name, "type": "frequency", "extra": []}]
    plot.data[sweep_name] = np.asarray(sweep)
    for i, (name, values) in enumerate(signals.items(), start=1):
        plot.variables.append({"index": i, "name": name, "type": "voltage", "extra": []})
        plot.data[name] = np.asarray(values)
    return plot


def rc_lowpass_ac(fc=1000.0, points=2001):
    freq = np.logspace(0, 6, points)
    response = 1.0 / (1.0 + 1j * freq / fc)
    return make_plot("AC Analysis", "frequency", freq, {"v(out)": response}, flags=("complex",))


def test_ac_bandwidth_matches_theory():
    result = measure.measure(rc_lowpass_ac())
    out = result["signals"]["v(out)"]
    assert result["analysis"] == "ac"
    assert out["gain_db_max"] == pytest.approx(0.0, abs=0.01)
    assert out["f_minus_3db_hz"] == pytest.approx(1000.0, rel=0.02)
    assert out["phase_deg_at_f_stop"] == pytest.approx(-90.0, abs=1.0)


def test_ac_unity_gain_and_phase_margin():
    # integrator: gain crosses 0 dB at 1 kHz with a constant -90 degrees
    freq = np.logspace(1, 5, 2001)
    response = 1000.0 / (1j * freq)
    result = measure.measure(
        make_plot("AC Analysis", "frequency", freq, {"v(out)": response}, flags=("complex",))
    )
    out = result["signals"]["v(out)"]
    assert out["unity_gain_hz"] == pytest.approx(1000.0, rel=0.02)
    assert out["phase_margin_deg"] == pytest.approx(90.0, abs=2.0)


def test_tran_step_response_metrics():
    t = np.linspace(0, 1e-3, 5000)
    tau = 1e-4
    step = 1.0 - np.exp(-t / tau)
    result = measure.measure(make_plot("Transient Analysis", "time", t, {"v(out)": step}))
    out = result["signals"]["v(out)"]
    assert out["waveform"] == "transient"
    assert out["final"] == pytest.approx(1.0, abs=1e-3)
    # 10-90 % rise time of a single pole is 2.2 tau
    assert out["rise_time_10_90_s"] == pytest.approx(2.2 * tau, rel=0.05)
    assert "overshoot_pct" not in out


def test_tran_overshoot_is_measured():
    t = np.linspace(0, 1e-3, 4000)
    wn, zeta = 2 * np.pi * 5e3, 0.2
    wd = wn * np.sqrt(1 - zeta**2)
    step = 1 - np.exp(-zeta * wn * t) * (
        np.cos(wd * t) + zeta / np.sqrt(1 - zeta**2) * np.sin(wd * t)
    )
    result = measure.measure(make_plot("Transient Analysis", "time", t, {"v(out)": step}))
    out = result["signals"]["v(out)"]
    expected = 100 * np.exp(-np.pi * zeta / np.sqrt(1 - zeta**2))
    assert out["overshoot_pct"] == pytest.approx(expected, rel=0.1)


def test_tran_periodic_signal_is_classified():
    t = np.linspace(0, 5e-3, 5000)
    sine = np.sin(2 * np.pi * 1000 * t)
    out = measure.measure(make_plot("Transient Analysis", "time", t, {"v(out)": sine}))
    signal = out["signals"]["v(out)"]
    assert signal["waveform"] == "periodic"
    assert signal["estimated_frequency_hz"] == pytest.approx(1000.0, rel=0.05)
    assert signal["rms"] == pytest.approx(0.707, rel=0.01)
    assert "overshoot_pct" not in signal


def test_dc_measurements():
    sweep = np.linspace(0, 5, 501)
    out = measure.measure(
        make_plot("DC transfer characteristic", "v-sweep", sweep, {"v(out)": 2 * sweep})
    )
    signal = out["signals"]["v(out)"]
    assert signal["max"] == pytest.approx(10.0)
    assert signal["max_slope"] == pytest.approx(2.0, rel=1e-6)
    assert signal["monotonic"]


def test_op_measurements():
    plot = make_plot("Operating Point", "v(1)", [3.3], {"v(out)": [1.65]})
    out = measure.measure(plot)
    assert out["analysis"] == "op"
    assert out["values"]["v(out)"] == pytest.approx(1.65)


def test_thd_of_a_clipped_sine():
    fundamental = 1000.0
    t = np.linspace(0, 10e-3, 20001)
    # a pure sine has (almost) no distortion
    clean = make_plot(
        "Transient Analysis", "time", t, {"v(out)": np.sin(2 * np.pi * fundamental * t)}
    )
    result = measure.thd(clean, "v(out)", fundamental)
    assert result["thd_percent"] < 0.5

    # a square wave has a well known THD of ~48 %
    square = make_plot(
        "Transient Analysis", "time", t, {"v(out)": np.sign(np.sin(2 * np.pi * fundamental * t))}
    )
    result = measure.thd(square, "v(out)", fundamental, harmonics=15)
    assert result["thd_percent"] == pytest.approx(48.3, rel=0.15)


def test_thd_needs_two_periods():
    t = np.linspace(0, 0.5e-3, 100)
    plot = make_plot("Transient Analysis", "time", t, {"v(out)": np.sin(2 * np.pi * 1000 * t)})
    with pytest.raises(ValueError):
        measure.thd(plot, "v(out)", 1000.0)

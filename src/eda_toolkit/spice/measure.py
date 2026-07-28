"""Measurements derived from simulation results."""

from __future__ import annotations

from typing import Any

import numpy as np

from .rawfile import Plot


def _db(values: np.ndarray) -> np.ndarray:
    magnitude = np.abs(values)
    floor = np.maximum(magnitude, 1e-30)
    return 20.0 * np.log10(floor)


def measure_ac(plot: Plot) -> dict[str, Any]:
    """Bode style figures for every signal in an AC sweep."""
    freq = np.abs(plot.data[plot.sweep])
    out: dict[str, Any] = {"analysis": "ac", "points": len(freq),
                           "f_start": float(freq[0]), "f_stop": float(freq[-1]),
                           "signals": {}}
    for name in plot.signals():
        values = plot.data[name]
        mag_db = _db(values)
        phase = np.angle(values, deg=True)
        peak = int(np.argmax(mag_db))
        entry: dict[str, Any] = {
            "gain_db_max": float(mag_db[peak]),
            "f_at_gain_max_hz": float(freq[peak]),
            "gain_db_at_f_start": float(mag_db[0]),
            "gain_db_at_f_stop": float(mag_db[-1]),
            "phase_deg_at_f_start": float(phase[0]),
            "phase_deg_at_f_stop": float(phase[-1]),
        }
        entry["f_minus_3db_hz"] = _crossing(freq, mag_db, mag_db[peak] - 3.0, start=peak)
        entry["f_minus_3db_low_hz"] = _crossing(freq, mag_db, mag_db[peak] - 3.0,
                                                start=peak, backwards=True)
        if entry["f_minus_3db_hz"] and entry["f_minus_3db_low_hz"]:
            entry["bandwidth_hz"] = entry["f_minus_3db_hz"] - entry["f_minus_3db_low_hz"]
        unity = _crossing(freq, mag_db, 0.0, start=peak)
        if unity is not None:
            entry["unity_gain_hz"] = unity
            entry["phase_margin_deg"] = float(180.0 + np.interp(unity, freq, np.unwrap(
                np.radians(phase)) * 180.0 / np.pi))
        out["signals"][name] = entry
    return out


def _crossing(x: np.ndarray, y: np.ndarray, level: float, *, start: int = 0,
              backwards: bool = False) -> float | None:
    """First crossing of ``level`` walking away from ``start``; log-interpolated in x."""
    indices = range(start, 0, -1) if backwards else range(start, len(y) - 1)
    for i in indices:
        j = i - 1 if backwards else i + 1
        if (y[i] - level) * (y[j] - level) <= 0 and y[i] != y[j]:
            ratio = (level - y[i]) / (y[j] - y[i])
            if x[i] > 0 and x[j] > 0:
                return float(np.exp(np.log(x[i]) + ratio * (np.log(x[j]) - np.log(x[i]))))
            return float(x[i] + ratio * (x[j] - x[i]))
    return None


def measure_tran(plot: Plot) -> dict[str, Any]:
    """Time domain statistics: levels, edges, overshoot and settling."""
    time = np.real(plot.data[plot.sweep])
    out: dict[str, Any] = {"analysis": "tran", "points": len(time),
                           "t_start": float(time[0]), "t_stop": float(time[-1]),
                           "signals": {}}
    for name in plot.signals():
        values = np.real(plot.data[name])
        low, high = float(np.min(values)), float(np.max(values))
        entry: dict[str, Any] = {
            "min": low,
            "max": high,
            "peak_to_peak": high - low,
            "mean": float(np.mean(values)),
            "rms": float(np.sqrt(np.mean(values**2))),
            "final": float(values[-1]),
        }
        if high - low > 1e-12:
            mid = 0.5 * (high + low)
            crossings = np.nonzero(np.diff(np.signbit(values - mid)))[0]
            if len(crossings) > 4:
                # oscillating: step response figures would be meaningless here
                entry["waveform"] = "periodic"
                entry["amplitude"] = 0.5 * (high - low)
                first = _interpolate_crossing(time, values, crossings[0], mid)
                last = _interpolate_crossing(time, values, crossings[-1], mid)
                if last > first:
                    half_periods = len(crossings) - 1
                    entry["estimated_frequency_hz"] = float(half_periods / (2.0 * (last - first)))
            else:
                entry["waveform"] = "transient"
                entry.update(_edge_metrics(time, values, low, high))
        out["signals"][name] = entry
    return out


def _interpolate_crossing(time: np.ndarray, values: np.ndarray, index: int,
                          level: float) -> float:
    """Sub-sample time at which the signal crosses ``level`` between index and index+1."""
    y0, y1 = values[index], values[index + 1]
    if y1 == y0:
        return float(time[index])
    ratio = (level - y0) / (y1 - y0)
    return float(time[index] + ratio * (time[index + 1] - time[index]))


def _edge_metrics(time: np.ndarray, values: np.ndarray, low: float, high: float) -> dict[str, Any]:
    span = high - low
    lo_level, hi_level = low + 0.1 * span, low + 0.9 * span
    metrics: dict[str, Any] = {}
    t10 = _time_at(time, values, lo_level)
    t90 = _time_at(time, values, hi_level)
    if t10 is not None and t90 is not None and t90 > t10:
        metrics["rise_time_10_90_s"] = float(t90 - t10)
    final = float(values[-1])
    if abs(final - low) > 1e-15:
        overshoot = (high - final) / abs(final - low) * 100.0
        if overshoot > 0.1:
            metrics["overshoot_pct"] = float(overshoot)
    tolerance = 0.02 * max(abs(final), span)
    outside = np.where(np.abs(values - final) > tolerance)[0]
    if len(outside):
        metrics["settling_time_2pct_s"] = float(time[min(outside[-1] + 1, len(time) - 1)] - time[0])
    return metrics


def _time_at(time: np.ndarray, values: np.ndarray, level: float) -> float | None:
    for i in range(len(values) - 1):
        if (values[i] - level) * (values[i + 1] - level) <= 0 and values[i] != values[i + 1]:
            ratio = (level - values[i]) / (values[i + 1] - values[i])
            return float(time[i] + ratio * (time[i + 1] - time[i]))
    return None


def measure_dc(plot: Plot) -> dict[str, Any]:
    sweep = np.real(plot.data[plot.sweep])
    out: dict[str, Any] = {"analysis": "dc", "points": len(sweep),
                           "sweep": plot.sweep, "signals": {}}
    for name in plot.signals():
        values = np.real(plot.data[name])
        gradient = np.gradient(values, sweep) if len(sweep) > 1 else np.array([0.0])
        out["signals"][name] = {
            "min": float(np.min(values)),
            "max": float(np.max(values)),
            "at_sweep_start": float(values[0]),
            "at_sweep_end": float(values[-1]),
            "max_slope": float(np.max(np.abs(gradient))),
            "monotonic": bool(np.all(np.diff(values) >= -1e-12) or np.all(np.diff(values) <= 1e-12)),
        }
    return out


def measure_op(plot: Plot) -> dict[str, Any]:
    return {
        "analysis": "op",
        "values": {name: float(np.real(plot.data[name][0])) for name in plot.data},
    }


def thd(plot: Plot, signal: str, fundamental_hz: float, *, harmonics: int = 7,
        skip_seconds: float | None = None) -> dict[str, Any]:
    """Total harmonic distortion of a transient signal (uniform resampling + FFT)."""
    time = np.real(plot.data[plot.sweep])
    values = np.real(plot.data[signal])
    if skip_seconds:
        mask = time >= time[0] + skip_seconds
        time, values = time[mask], values[mask]
    period = 1.0 / fundamental_hz
    cycles = int((time[-1] - time[0]) / period)
    if cycles < 2:
        raise ValueError("need at least two full periods of the fundamental")
    window = cycles * period
    n = max(1024, 1 << int(np.ceil(np.log2(len(time)))))
    uniform_t = np.linspace(time[0], time[0] + window, n, endpoint=False)
    uniform_v = np.interp(uniform_t, time, values)
    spectrum = np.abs(np.fft.rfft(uniform_v)) * 2.0 / n
    bin_width = 1.0 / window
    fundamental_bin = round(fundamental_hz / bin_width)
    if fundamental_bin >= len(spectrum):
        raise ValueError("fundamental is above the sampled bandwidth")
    fundamental_amp = float(spectrum[fundamental_bin])
    harmonic_amps = []
    for h in range(2, harmonics + 1):
        index = fundamental_bin * h
        harmonic_amps.append(float(spectrum[index]) if index < len(spectrum) else 0.0)
    distortion = float(np.sqrt(sum(a**2 for a in harmonic_amps)))
    ratio = distortion / fundamental_amp if fundamental_amp else float("nan")
    return {
        "signal": signal,
        "fundamental_hz": fundamental_hz,
        "fundamental_amplitude": fundamental_amp,
        "harmonic_amplitudes": harmonic_amps,
        "thd_ratio": ratio,
        "thd_percent": ratio * 100.0,
        "thd_db": float(20 * np.log10(ratio)) if ratio > 0 else float("-inf"),
    }


def measure(plot: Plot) -> dict[str, Any]:
    kind = plot.analysis
    if kind == "ac":
        return measure_ac(plot)
    if kind == "tran":
        return measure_tran(plot)
    if kind == "dc":
        return measure_dc(plot)
    if kind == "op":
        return measure_op(plot)
    return {"analysis": kind, "points": plot.points,
            "signals": {name: {"min": float(np.min(np.abs(plot.data[name]))),
                               "max": float(np.max(np.abs(plot.data[name])))}
                        for name in plot.signals()}}

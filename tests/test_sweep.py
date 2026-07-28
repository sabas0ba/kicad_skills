"""Monte Carlo / temperature sweep helpers. The pure parts need no ngspice."""

import random

import pytest

from eda_toolkit.spice import sweep
from eda_toolkit.util import EdaError

DECK = """* RC low pass
V1 in 0 DC 0 AC 1
R1 in out 1k
C1 out 0 159.155n
.param GAIN=2.5
.ac dec 100 10 100k
.end
"""


@pytest.mark.parametrize(
    "text,expected",
    [("1k", 1000.0), ("4.7k", 4700.0), ("100n", 1e-7), ("1meg", 1e6), ("2.2", 2.2),
     ("1e3", 1000.0), ("10m", 0.01), ("5p", 5e-12), ("3u", 3e-6)],
)
def test_parse_value(text, expected):
    assert sweep.parse_value(text) == pytest.approx(expected)


def test_parse_value_rejects_nonsense():
    with pytest.raises(EdaError):
        sweep.parse_value("4k7")


def test_format_value_round_trips():
    for value in (1000.0, 4700.0, 1e-7, 1e6, 2.2, 1e-12):
        assert sweep.parse_value(sweep.format_value(value)) == pytest.approx(value)


def test_deck_values_finds_passives_and_params():
    values = sweep.deck_values(DECK)
    assert values["R1"] == pytest.approx(1000.0)
    assert values["C1"] == pytest.approx(159.155e-9)
    assert values["GAIN"] == pytest.approx(2.5)
    assert "V1" not in values  # sources are not passives


def test_apply_values_rewrites_in_place():
    text = sweep.apply_values(DECK, {"R1": 1100.0, "GAIN": 3.0})
    assert "R1 in out 1.1k" in text
    assert ".param GAIN=3" in text
    assert "C1 out 0 159.155n" in text  # untouched
    assert sweep.deck_values(text)["R1"] == pytest.approx(1100.0)


def test_apply_values_rejects_unknown_names():
    with pytest.raises(EdaError, match="nothing to vary"):
        sweep.apply_values(DECK, {"R99": 1.0})


def test_set_temperature_replaces_and_inserts():
    text = sweep.set_temperature(DECK, 85)
    assert ".temp 85" in text
    assert text.strip().endswith(".end")
    again = sweep.set_temperature(text, -40)
    assert again.count(".temp") == 1
    assert ".temp -40" in again


@pytest.mark.parametrize("spec,expected", [("R1=1%", 0.01), ("C1=0.05", 0.05), ("L1=10%", 0.1)])
def test_parse_tolerance(spec, expected):
    assert sweep.parse_tolerance(spec)[1] == pytest.approx(expected)


@pytest.mark.parametrize("spec", ["R1", "R1=0%", "R1=200%", "=5%"])
def test_parse_tolerance_rejects_bad_input(spec):
    with pytest.raises((EdaError, ValueError)):
        sweep.parse_tolerance(spec)


def test_sample_stays_inside_the_tolerance_band():
    rng = random.Random(7)
    for distribution in ("normal", "uniform", "worst"):
        for _ in range(500):
            value = sweep.sample(1000.0, 0.05, rng, distribution)
            assert 949.9 <= value <= 1050.1, distribution


def test_sample_is_deterministic_for_a_seed():
    a = [sweep.sample(100.0, 0.1, random.Random(3)) for _ in range(3)]
    b = [sweep.sample(100.0, 0.1, random.Random(3)) for _ in range(3)]
    assert a == b


def test_worst_case_sampling_hits_both_corners():
    rng = random.Random(1)
    values = {round(sweep.sample(100.0, 0.1, rng, "worst"), 6) for _ in range(50)}
    assert values == {90.0, 110.0}


SUMMARY = {
    "plots": [
        {"analysis": "ac", "measurements": {"signals": {"v(out)": {"f_minus_3db_hz": 1000.0}}}},
        {"analysis": "op", "measurements": {"values": {"v(out)": 7.5}}},
    ]
}


def test_read_metric():
    assert sweep.read_metric(SUMMARY, "ac.v(out).f_minus_3db_hz") == pytest.approx(1000.0)
    assert sweep.read_metric(SUMMARY, "op.v(out)") == pytest.approx(7.5)
    assert sweep.read_metric(SUMMARY, "ac.v(out).nope") is None
    assert sweep.read_metric(SUMMARY, "tran.v(out).max") is None


def test_statistics():
    stats = sweep._statistics([1.0, 2.0, 3.0, 4.0, 5.0])
    assert stats["samples"] == 5
    assert stats["mean"] == pytest.approx(3.0)
    assert stats["min"] == 1.0 and stats["max"] == 5.0
    assert stats["median"] == pytest.approx(3.0)
    assert stats["spread_pct"] == pytest.approx(133.333, rel=1e-3)


def test_statistics_of_nothing():
    assert sweep._statistics([])["samples"] == 0

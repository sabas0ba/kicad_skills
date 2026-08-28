"""Crosstalk held to its own physics, not to another simulator.

The anchors are the ones the matrices carry for free: stripline's forward
crosstalk cancels because its medium is one, a slower edge sees less of the
line, and a distant net is no aggressor at all.
"""

from pathlib import Path

import pytest

from eda_toolkit.kicad import crosstalk, pcb


def _board(tracks, stackup=()):
    board = pcb.Board(path=Path("memory.kicad_pcb"), version=0, generator="test")
    board.tracks = list(tracks)
    board.stackup = list(stackup)
    return board


def _pair(layer="F.Cu", *, length=40.0, centre=0.6, width=0.3, nets=("SIG1", "SIG2")):
    return [
        pcb.Track((5.0, 10.0), (5.0 + length, 10.0), width, layer, 1, nets[0]),
        pcb.Track((5.0, 10.0 + centre), (5.0 + length, 10.0 + centre), width, layer, 2, nets[1]),
    ]


# a four-layer stackup that states everything, In1.Cu between two planes
_STACKUP = [
    {"name": "F.Cu", "type": "copper", "thickness": 0.035},
    {"name": "p1", "type": "prepreg", "thickness": 0.2, "epsilon_r": 4.5},
    {"name": "In1.Cu", "type": "copper", "thickness": 0.035},
    {"name": "core", "type": "core", "thickness": 0.3, "epsilon_r": 4.5},
    {"name": "In2.Cu", "type": "copper", "thickness": 0.035},
    {"name": "p2", "type": "prepreg", "thickness": 0.2, "epsilon_r": 4.5},
    {"name": "B.Cu", "type": "copper", "thickness": 0.035},
]


def test_two_long_neighbours_are_found_with_their_distance():
    tracks = [*_pair(), pcb.Track((5.0, 30.0), (45.0, 30.0), 0.3, "F.Cu", 3, "FAR")]
    pairs = crosstalk.coupled_pairs(_board(tracks))
    assert len(pairs) == 1
    (pair,) = pairs
    assert sorted(pair["nets"]) == ["SIG1", "SIG2"]
    assert pair["coupled_mm"] == pytest.approx(40.0, abs=0.5)
    assert pair["centre_mm"] == pytest.approx(0.6, abs=0.01)


def test_a_differential_pair_is_not_its_own_aggressor():
    pairs = crosstalk.coupled_pairs(_board(_pair(nets=("USB_P", "USB_N"))))
    assert pairs == []


def test_stripline_couples_quietly_forward():
    """The homogeneity the matrices proved, arriving at the far end as silence."""
    result = crosstalk.analyse(_board(_pair("In1.Cu"), stackup=_STACKUP))
    (pair,) = result["pairs"]
    assert pair["cross_section"] == {
        "layer": "In1.Cu",
        "kind": "stripline",
        "height_mm": 0.5,
        "height_below_mm": 0.3,
        "epsilon_r": 4.5,
        "assumed": False,
    }
    assert pair["next"]["mv"] > 10
    assert abs(pair["fext"]["mv"]) < pair["next"]["mv"] / 20
    assert "homogeneous" in pair["fext"]["note"]


def test_an_outer_layers_far_end_pulse_is_negative():
    result = crosstalk.analyse(_board(_pair("F.Cu"), stackup=_STACKUP))
    (pair,) = result["pairs"]
    assert pair["cross_section"]["kind"] == "microstrip"
    assert pair["fext"]["mv"] < 0


def test_a_board_with_no_stackup_still_answers_and_says_it_guessed():
    result = crosstalk.analyse(_board(_pair()))
    (pair,) = result["pairs"]
    assert pair["cross_section"]["assumed"] is True
    assert pair["next"]["mv"] > 0


def test_a_slower_edge_hears_less_at_the_near_end():
    board = _board(_pair("In1.Cu"), stackup=_STACKUP)
    fast = crosstalk.analyse(board, rise_ns=0.05)["pairs"][0]
    slow = crosstalk.analyse(board, rise_ns=10.0)["pairs"][0]
    assert fast["next"]["saturated"] is True
    assert slow["next"]["saturated"] is False
    assert slow["next"]["mv"] < fast["next"]["mv"] / 5
    # unsaturated, the fraction is exactly the share of the edge in flight
    expected = fast["next"]["mv"] * 2 * slow["next"]["coupled_delay_ps"] / (10.0 * 1e3)
    assert slow["next"]["mv"] == pytest.approx(expected, rel=0.02)


def test_nonsense_is_refused():
    board = _board(_pair())
    with pytest.raises(ValueError):
        crosstalk.analyse(board, rise_ns=0.0)
    with pytest.raises(ValueError):
        crosstalk.analyse(board, swing_v=-1.0)


def test_the_solve_budget_reports_geometry_instead_of_stalling():
    tracks = []
    for i in range(3):
        offset = i * 10.0
        width = 0.2 + 0.1 * i  # three distinct cross-sections
        tracks += [
            pcb.Track((5.0, 10.0 + offset), (45.0, 10.0 + offset), width, "In1.Cu", 1, f"A{i}"),
            pcb.Track((5.0, 10.5 + offset), (45.0, 10.5 + offset), width, "In1.Cu", 2, f"B{i}"),
        ]
    result = crosstalk.analyse(_board(tracks, stackup=_STACKUP), limit=1)
    solved = [p for p in result["pairs"] if "next" in p]
    skipped = [p for p in result["pairs"] if "not_solved" in p]
    assert len(solved) >= 1 and len(skipped) >= 1
    assert "budget" in skipped[0]["not_solved"]

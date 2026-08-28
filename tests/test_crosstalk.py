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


def test_a_mixed_gap_is_refused_rather_than_granted_the_cancellation():
    """kl = kc is a property of one medium; an averaged solve would invent it.

    The same refusal `--solve` makes, and sharper here: a homogeneous solve of
    a two-material gap does not blur the FEXT, it manufactures its cancellation.
    """
    mixed = [dict(entry) for entry in _STACKUP]
    mixed[1]["epsilon_r"] = 3.2  # prepreg well apart from the 4.5 core
    result = crosstalk.analyse(_board(_pair("In1.Cu"), stackup=mixed))
    (pair,) = result["pairs"]
    assert "fext" not in pair
    assert "dielectrics differ" in pair["not_solved"]


def test_copper_weight_tells_two_otherwise_identical_layers_apart():
    """Same dielectric, same trace, heavier copper: not the same cross-section.

    With p1 = core = p2 the two inner layers see identical spans and offsets,
    so only the copper thickness separates their solve keys - dropping it from
    the key hands one layer the other's matrices.
    """
    symmetric = [
        {"name": "F.Cu", "type": "copper", "thickness": 0.035},
        {"name": "p1", "type": "prepreg", "thickness": 0.2, "epsilon_r": 4.5},
        {"name": "In1.Cu", "type": "copper", "thickness": 0.035},
        {"name": "core", "type": "core", "thickness": 0.2, "epsilon_r": 4.5},
        {"name": "In2.Cu", "type": "copper", "thickness": 0.105},  # 3 oz
        {"name": "p2", "type": "prepreg", "thickness": 0.2, "epsilon_r": 4.5},
        {"name": "B.Cu", "type": "copper", "thickness": 0.035},
    ]
    tracks = [*_pair("In1.Cu"), *_pair("In2.Cu", nets=("SIG3", "SIG4"))]
    result = crosstalk.analyse(_board(tracks, stackup=symmetric))
    by_layer = {pair["layer"]: pair for pair in result["pairs"]}
    sections = [
        {k: v for k, v in by_layer[layer]["cross_section"].items() if k != "layer"}
        for layer in ("In1.Cu", "In2.Cu")
    ]
    assert sections[0] == sections[1]
    assert (
        by_layer["In1.Cu"]["coupling"]["z_odd_ohm"] != by_layer["In2.Cu"]["coupling"]["z_odd_ohm"]
    )


def test_a_pair_says_where_on_the_board_it_couples():
    """ "These two nets couple" is a sentence; "here" is a place to look."""
    pairs = crosstalk.coupled_pairs(_board(_pair()))
    (pair,) = pairs
    x0, y0, x1, y1 = pair["where_mm"]
    assert (x0, x1) == (pytest.approx(5.0, abs=0.5), pytest.approx(45.0, abs=0.5))
    assert y0 == pytest.approx(10.0, abs=0.7) and y1 == pytest.approx(10.6, abs=0.7)
    run = pair["longest_run"]
    assert run["length_mm"] == pytest.approx(40.0, abs=0.5)
    assert run["from"][1] == pytest.approx(10.0, abs=0.1)


def test_the_json_carries_the_place_but_not_the_raw_spans(tmp_path):
    result = crosstalk.analyse(_board(_pair("In1.Cu"), stackup=_STACKUP))
    (pair,) = result["pairs"]
    assert "where_mm" in pair and "longest_run" in pair and pair["index"] == 1
    assert "spans" not in pair
    import json

    json.dumps(result)  # nothing numpy-shaped leaks into the record


def test_the_map_draws_the_reported_pairs(tmp_path):
    board = _board(
        [*_pair("In1.Cu"), pcb.Track((5.0, 30.0), (45.0, 30.0), 0.3, "F.Cu", 3, "FAR")],
        stackup=_STACKUP,
    )
    board.edges = [{"type": "gr_rect", "points": [(0.0, 0.0), (50.0, 40.0)]}]
    result = crosstalk.analyse(board)
    out = tmp_path / "crosstalk.png"
    crosstalk.render(board, result, out)
    assert out.stat().st_size > 1000

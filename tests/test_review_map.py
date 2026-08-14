from pathlib import Path

from eda_toolkit.kicad import pcb, review_map


def _board(tmp_path):
    board = pcb.Board(path=Path("m.kicad_pcb"), version=0, generator="t")
    board.layers = [
        {"id": "0", "name": "F.Cu", "type": "signal", "user_name": ""},
        {"id": "31", "name": "B.Cu", "type": "signal", "user_name": ""},
    ]
    board.edges = [{"type": "gr_rect", "points": [(0, 0), (40, 30)]}]
    board.tracks = [
        pcb.Track(start=(5, 5), end=(20, 5), width=0.3, layer="F.Cu", net_code=1, net="S")
    ]
    return board


def test_a_finding_with_a_position_is_marked_and_legended(tmp_path):
    board = _board(tmp_path)
    findings = [
        {
            "rule": "route.odd_angle",
            "severity": "info",
            "message": "one bend off the grid",
            "details": {"positions": [(20.0, 5.0)]},
        },
        {"rule": "board.size", "severity": "info", "message": "no position", "details": {}},
    ]
    out = tmp_path / "map.png"
    result = review_map.render_review_map(board, findings, out)
    assert out.exists()
    # only the located finding earns a number
    assert [entry["rule"] for entry in result["legend"]] == ["route.odd_angle"]
    assert result["legend"][0]["marks"] == 1


def test_positions_are_read_only_from_details(tmp_path):
    assert review_map.positions_of({"details": {"positions": [[1, 2]]}}) == [(1.0, 2.0)]
    assert review_map.positions_of({"message": "at (1, 2)"}) == []
    assert review_map.positions_of({"details": {"positions": ["nonsense"]}}) == []

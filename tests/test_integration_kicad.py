"""End-to-end KiCad tests. These need kicad-cli from the container."""

import pytest

from eda_toolkit.kicad import kicad_cli, netlist as netlist_mod, pcb_review, render, sch_review

pytestmark = pytest.mark.kicad


def test_version():
    assert kicad_cli.version().split(".")[0].isdigit()


def test_erc_report_shape(project_copy):
    report = kicad_cli.erc(project_copy / "example.kicad_sch")
    assert "sheets" in report
    violations = [v for sheet in report["sheets"] for v in sheet.get("violations", [])]
    assert violations == [], f"the example schematic should be ERC clean: {violations}"


def test_netlist_from_kicad_cli_matches_the_fallback(project_copy):
    from eda_toolkit.kicad import schematic

    official = netlist_mod.get(project_copy, prefer_cli=True)
    fallback = schematic.build_netlist(schematic.parse_project(project_copy))
    assert official["source"] == "kicad-cli"

    def topology(data):
        return {
            frozenset(f"{n['ref']}.{n['pin']}" for n in net["nodes"])
            for net in data["nets"] if net["nodes"]
        }

    assert topology(official) == topology(fallback)


def test_schematic_review_is_clean(project_copy):
    report = sch_review.review(project_copy)
    assert report["statistics"]["erc_available"] is True
    assert report["statistics"]["netlist_source"] == "kicad-cli"
    assert report["summary"]["error"] == 0
    assert report["summary"]["warning"] == 0


def test_board_review_has_no_errors(project_copy):
    report = pcb_review.review(project_copy)
    assert report["drc_available"] is True
    assert report["summary"]["error"] == 0
    rules = {f["rule"] for f in report["findings"]}
    # unconnected copper and net conflicts must not appear on the example board
    assert not any(r.startswith("drc.unconnected") for r in rules)
    assert "drc.parity.net_conflict" not in rules


def test_drc_detects_a_real_short(project_copy):
    """Move a track so it shorts two nets and check DRC catches it."""
    board_file = project_copy / "example.kicad_pcb"
    text = board_file.read_text()
    shorted = text.replace(
        '(segment (start 105 92) (end 111.0875 92) (width 0.25) (layer "F.Cu") (net 3)',
        '(segment (start 105 92) (end 117.9125 92) (width 0.25) (layer "F.Cu") (net 3)',
    )
    assert shorted != text
    board_file.write_text(shorted)

    report = pcb_review.review(project_copy)
    assert report["summary"]["error"] > 0
    rules = {f["rule"] for f in report["findings"]}
    assert any("clearance" in r or "short" in r or "parity" in r for r in rules)


def test_pcb_render_produces_images(project_copy, tmp_path):
    result = render.render_board(project_copy, tmp_path / "img",
                                 views=["front", "copper-front"], dpi=100, three_d=False)
    assert result["errors"] == []
    paths = [i["path"] for i in result["images"]]
    assert len(paths) == 2
    for path in paths:
        assert path.endswith(".png")
        from pathlib import Path

        assert Path(path).stat().st_size > 1000


def test_pcb_render_3d(project_copy, tmp_path):
    result = render.render_board(project_copy, tmp_path / "img3d", views=[], three_d=True)
    kinds = {i["view"] for i in result["images"]}
    assert {"3d-top", "3d-bottom", "3d-iso"} <= kinds, result["errors"]


def test_schematic_render(project_copy, tmp_path):
    result = render.render_schematic(project_copy, tmp_path / "sch", dpi=100)
    assert result["images"]
    from pathlib import Path

    assert Path(result["images"][0]).stat().st_size > 1000


def test_unknown_view_is_reported(project_copy, tmp_path):
    result = render.render_board(project_copy, tmp_path / "img", views=["nope"], three_d=False)
    assert result["images"] == []
    assert result["errors"][0]["error"] == "unknown view"


def test_board_stats(project_copy):
    text = kicad_cli.board_stats(project_copy / "example.kicad_pcb")
    assert "Board" in text or "Component" in text or text == ""

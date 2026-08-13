"""End-to-end KiCad tests. These need kicad-cli from the container."""

import pytest

from eda_toolkit.kicad import kicad_cli, pcb_review, render, sch_review
from eda_toolkit.kicad import netlist as netlist_mod

pytestmark = pytest.mark.kicad


def test_version():
    assert kicad_cli.version().split(".")[0].isdigit()


# The fixture embeds one snapshot of KiCad's symbol library. A different KiCad
# release ships slightly different symbols, so `lib_symbol_mismatch` says the
# fixture is older than the installed library - not that the design is wrong.
# It is the schematic-side twin of the `lib_footprint_mismatch` the simplified
# footprints produce in DRC.
LIBRARY_SNAPSHOT_TYPES = {"lib_symbol_mismatch", "lib_footprint_mismatch"}


def design_violations(violations):
    return [v for v in violations if v.get("type") not in LIBRARY_SNAPSHOT_TYPES]


def test_erc_report_shape(project_copy):
    report = kicad_cli.erc(project_copy / "example.kicad_sch")
    assert "sheets" in report
    violations = [v for sheet in report["sheets"] for v in sheet.get("violations", [])]
    real = design_violations(violations)
    assert real == [], f"the example schematic should be ERC clean: {real}"


def test_netlist_from_kicad_cli_matches_the_fallback(project_copy):
    from eda_toolkit.kicad import schematic

    official = netlist_mod.get(project_copy, prefer_cli=True)
    fallback = schematic.build_netlist(schematic.parse_project(project_copy))
    assert official["source"] == "kicad-cli"

    def topology(data):
        return {
            frozenset(f"{n['ref']}.{n['pin']}" for n in net["nodes"])
            for net in data["nets"]
            if net["nodes"]
        }

    assert topology(official) == topology(fallback)


def test_schematic_review_is_clean(project_copy):
    report = sch_review.review(project_copy)
    assert report["statistics"]["erc_available"] is True
    assert report["statistics"]["netlist_source"] == "kicad-cli"
    assert report["summary"]["error"] == 0
    noise = {f"erc.{t}" for t in LIBRARY_SNAPSHOT_TYPES}
    warnings = [f for f in report["findings"] if f["severity"] == "warning"]
    assert [f for f in warnings if f["rule"] not in noise] == []


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
        "(segment\n\t\t(start 105 92)\n\t\t(end 111.0875 92)",
        "(segment\n\t\t(start 105 92)\n\t\t(end 117.9125 92)",
    )
    assert shorted != text
    board_file.write_text(shorted)

    report = pcb_review.review(project_copy)
    assert report["summary"]["error"] > 0
    rules = {f["rule"] for f in report["findings"]}
    assert any("clearance" in r or "short" in r or "parity" in r for r in rules)


def test_pcb_render_produces_images(project_copy, tmp_path):
    result = render.render_board(
        project_copy, tmp_path / "img", views=["front", "copper-front"], dpi=100, three_d=False
    )
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


def test_fab_package(project_copy, tmp_path):
    """The fabrication package must contain what a board house asks for."""
    import zipfile
    from pathlib import Path

    from eda_toolkit.kicad import fab

    manifest = fab.export_package(project_copy, tmp_path / "fab", pos_format="csv")
    assert manifest["ok"], manifest["errors"]
    steps = {s["step"] for s in manifest["steps"]}
    assert {"gerbers", "drill", "position", "bom"} <= steps

    gerbers = list((tmp_path / "fab" / "gerbers").glob("*"))
    names = " ".join(p.name for p in gerbers)
    assert ".gbr" in names or ".gtl" in names, names
    assert any(p.suffix == ".drl" for p in gerbers), names  # excellon
    assert (tmp_path / "fab" / "example-pos.csv").exists()
    assert (tmp_path / "fab" / "example-bom.csv").exists()

    with zipfile.ZipFile(manifest["zip"]) as zf:
        assert len(zf.namelist()) >= len(gerbers)
    assert Path(manifest["zip"]).stat().st_size > 1000


def test_fab_preview_plots_every_exported_layer(project_copy, tmp_path):
    from pathlib import Path

    from eda_toolkit.kicad import fab, pcb

    out = tmp_path / "fab"
    manifest = fab.export_package(
        project_copy, out, preview=True, preview_dpi=100, background="black", make_zip=False
    )
    preview = next(s["output"] for s in manifest["steps"] if s["step"] == "preview")
    assert preview["errors"] == []
    board = pcb.parse(pcb.find_board(project_copy))
    assert len(preview["images"]) == len(fab.gerber_layers(board))
    assert Path(preview["contact_sheet"]).stat().st_size > 1000

    from PIL import Image

    with Image.open(preview["images"][0]) as im:
        assert im.getpixel((2, 2)) == (0, 0, 0)


def test_transparent_render_keeps_the_board_and_drops_the_backdrop(project_copy, tmp_path):
    result = render.render_board(
        project_copy,
        tmp_path / "clear",
        views=["front"],
        dpi=100,
        three_d=False,
        background="transparent",
    )
    assert result["errors"] == []

    from PIL import Image

    with Image.open(result["images"][0]["path"]) as im:
        assert im.mode == "RGBA"
        assert im.getpixel((2, 2))[3] == 0  # the corner is backdrop, never artwork
        assert im.getchannel("A").getextrema() == (0, 255)  # and the board itself is opaque


def test_fab_layer_selection_follows_the_board(example_pcb):
    from eda_toolkit.kicad import fab, pcb

    layers = fab.gerber_layers(pcb.parse(example_pcb))
    assert "F.Cu" in layers and "B.Cu" in layers and "Edge.Cuts" in layers
    assert "In1.Cu" not in layers  # the example board is two layer


def test_bom_is_grouped(project_copy, tmp_path):
    from eda_toolkit.kicad import fab

    result = fab.bom(project_copy, tmp_path / "bom.csv")
    assert result["line_items"] >= 3  # R, C (x2 grouped), U, J
    assert result["total_parts"] == 5  # J1 R1 C1 C2 U1
    values = " ".join(str(row) for row in result["rows"])
    assert "LM321" in values

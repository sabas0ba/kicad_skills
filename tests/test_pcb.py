import math

import pytest

from eda_toolkit.kicad import pcb, pcb_review
from eda_toolkit.util import EdaError


def test_parse_board(example_pcb):
    board = pcb.parse(example_pcb)
    # the fixture is written by KiCad 9 pcbnew: readable by 9 and 10 alike
    assert board.version == 20241229
    assert board.copper_layers == ["F.Cu", "B.Cu"]
    assert board.size_mm() == (40.0, 30.0)
    assert {fp.ref for fp in board.footprints} == {"J1", "R1", "C1", "U1", "C2"}
    assert board.nets[1] == "GND"


def test_pad_positions_are_absolute(example_pcb):
    board = pcb.parse(example_pcb)
    r1 = board.footprint_by_ref("R1")
    assert r1.x == 112 and r1.y == 92
    pads = {p.number: (round(p.x, 4), round(p.y, 4)) for p in r1.pads}
    assert pads == {"1": (111.0875, 92.0), "2": (112.9125, 92.0)}
    assert r1.is_smd


@pytest.mark.parametrize(
    "angle,expected",
    [(0, (1.0, 0.0)), (90, (0.0, -1.0)), (180, (-1.0, 0.0)), (270, (0.0, 1.0))],
)
def test_rotation_matches_kicad_convention(angle, expected):
    x, y = pcb._rotate(1.0, 0.0, angle)
    assert math.isclose(x, expected[0], abs_tol=1e-9)
    assert math.isclose(y, expected[1], abs_tol=1e-9)


def test_through_hole_pads_carry_drills(example_pcb):
    board = pcb.parse(example_pcb)
    j1 = board.footprint_by_ref("J1")
    assert all(p.type == "thru_hole" for p in j1.pads)
    assert {p.drill for p in j1.pads} == {1.0}
    assert math.isclose(j1.pads[0].annular_ring, 0.35, abs_tol=1e-9)


def test_vias_and_tracks(example_pcb):
    board = pcb.parse(example_pcb)
    assert len(board.vias) == 5
    assert all(v.annular_ring == pytest.approx(0.2) for v in board.vias)
    gnd_tracks = [t for t in board.tracks if t.net == "GND"]
    assert gnd_tracks and all(t.width >= 0.4 for t in gnd_tracks)
    assert any(t.layer == "B.Cu" for t in board.tracks)


def test_zone_is_recognised(example_pcb):
    board = pcb.parse(example_pcb)
    zone = board.zones[0]
    assert zone.net == "GND"
    assert zone.layers == ["B.Cu"]
    assert zone.fill_enabled
    assert zone.filled  # the fixture ships the zone filled, as a real project would


def test_silkscreen_texts_are_collected(example_pcb):
    board = pcb.parse(example_pcb)
    refs = {t["footprint"] for t in board.silk_texts if t["footprint"]}
    assert refs == {"J1", "R1", "C1", "U1", "C2"}


def test_summary(example_pcb):
    data = pcb.summary(pcb.parse(example_pcb))
    assert data["layer_count"] == 2
    assert data["pads"] == 14
    assert data["through_hole_footprints"] == 1
    assert data["drill_sizes_mm"] == [0.4, 1.0]
    assert data["track_length_mm"] > 0


def test_find_board(example_project, example_pcb, tmp_path):
    assert pcb.find_board(example_project) == example_pcb
    assert pcb.find_board(example_project / "example.kicad_pro") == example_pcb
    with pytest.raises(EdaError):
        pcb.find_board(tmp_path)


def test_info_reports_nets(example_project):
    data = pcb_review.info(example_project)
    nets = {n["name"]: n for n in data["nets_detail"]}
    assert nets["GND"]["class"] == "ground"
    assert nets["+5V"]["class"] == "power"
    assert nets["/MID"]["track_length_mm"] > 0


def test_preview_images_stay_out_of_the_fab_zip(example_project, tmp_path):
    """The board house gets the manufacturing files; the pictures are for us.

    The zip is assembled from whatever landed in the output directory, so
    seeding the preview directory tests the exclusion without kicad-cli - every
    export step fails on the host.
    """
    import zipfile

    from eda_toolkit.kicad import fab
    from eda_toolkit.util import ensure_dir

    out = tmp_path / "fab"
    ensure_dir(out / fab.PREVIEW_DIR)
    ensure_dir(out / "gerbers")
    (out / fab.PREVIEW_DIR / "layer-F_Cu.png").write_bytes(b"not really a png")
    (out / "gerbers" / "example-F_Cu.gbr").write_text("G04 not really a gerber*\n")

    manifest = fab.export_package(example_project, out)
    with zipfile.ZipFile(manifest["zip"]) as zf:
        names = zf.namelist()
    assert "gerbers/example-F_Cu.gbr" in names
    assert not any(name.startswith(f"{fab.PREVIEW_DIR}/") for name in names)


def test_a_bad_background_is_refused_before_anything_is_written(example_project, tmp_path):
    from eda_toolkit.kicad import fab

    with pytest.raises(EdaError):
        fab.export_package(example_project, tmp_path / "fab", background="puce")
    assert not (tmp_path / "fab").exists()

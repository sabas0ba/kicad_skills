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


def test_silkscreen_text_carries_its_size_and_visibility(example_pcb):
    board = pcb.parse(example_pcb)
    r1 = next(t for t in board.silk_texts if t["footprint"] == "R1")
    assert r1["height"] > 0 and r1["width"] > 0
    assert r1["hidden"] is False


def test_a_rotated_footprint_turns_its_silkscreen_with_it(tmp_path):
    body = (
        '(kicad_pcb (version 20221018) (generator "t")'
        '  (footprint "l:f" (layer "F.Cu") (at 10 20 90)'
        '    (property "Reference" "R1" (at 0 2 0) (layer "F.SilkS")'
        "      (effects (font (size 1 1) (thickness 0.15))))))"
    )
    path = tmp_path / "b.kicad_pcb"
    path.write_text(body, encoding="utf-8")
    text = pcb.parse(path).silk_texts[0]
    # 2 mm below the origin, turned a quarter turn: 2 mm to one side of it
    assert (round(text["x"], 3), round(text["y"], 3)) == (12.0, 20.0)


def test_pad_bbox_follows_the_footprint_rotation():
    pad = pcb.Pad("1", "smd", "rect", 0.0, 0.0, 0.0, (2.0, 1.0), None, ["F.Cu"], "N")
    assert pad.bbox() == (-1.0, -0.5, 1.0, 0.5)
    turned = pad.bbox(angle_offset=90.0)
    assert (round(turned[0], 6), round(turned[1], 6)) == (-0.5, -1.0)
    assert pad.bbox(margin=0.5) == (-1.5, -1.0, 1.5, 1.0)


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


def test_copper_graphics_are_kept_only_when_filled(tmp_path):
    """A filled polygon on copper is copper; a hollow rectangle is its stroke."""
    body = (
        '(kicad_pcb (version 20221018) (generator "t")'
        '  (gr_poly (pts (xy 0 0) (xy 5 0) (xy 5 5)) (layer "F.Cu") (fill yes))'
        '  (gr_rect (start 10 10) (end 20 20) (layer "F.Cu") (fill none))'
        '  (gr_circle (center 30 30) (end 32 30) (layer "B.Cu") (fill yes))'
        '  (gr_poly (pts (xy 0 0) (xy 5 0) (xy 5 5)) (layer "F.SilkS") (fill yes)))'
    )
    path = tmp_path / "g.kicad_pcb"
    path.write_text(body, encoding="utf-8")
    shapes = pcb.parse(path).copper_shapes
    assert sorted(layer for layer, _ in shapes) == ["B.Cu", "F.Cu"]
    circle = next(points for layer, points in shapes if layer == "B.Cu")
    assert len(circle) > 8  # the circle arrives as a polygon, not a point pair


def test_a_slot_drill_keeps_its_shape_and_offset(tmp_path):
    body = (
        '(kicad_pcb (version 20221018) (generator "t")'
        '  (footprint "l:conn" (layer "F.Cu") (at 10 10 0)'
        '    (pad "1" thru_hole oval (at 0 0) (size 3 5)'
        '      (drill oval 1 2 (offset 0.5 0)) (layers "*.Cu"))))'
    )
    path = tmp_path / "s.kicad_pcb"
    path.write_text(body, encoding="utf-8")
    pad = pcb.parse(path).footprints[0].pads[0]
    assert pad.drill == 1.0
    assert pad.drill_size == (1.0, 2.0)
    assert pad.drill_offset == (0.5, 0.0)


def test_a_rectangular_courtyard_survives_rotation_with_all_four_corners(tmp_path):
    """The file states two diagonal corners; the polygon needs all four."""
    body = (
        '(kicad_pcb (version 20221018) (generator "t")'
        '  (footprint "l:sq" (layer "F.Cu") (at 20 20 45)'
        '    (fp_rect (start -2 -2) (end 2 2) (layer "F.CrtYd"))))'
    )
    path = tmp_path / "c.kicad_pcb"
    path.write_text(body, encoding="utf-8")
    fp = pcb.parse(path).footprints[0]
    assert len(fp.courtyard) == 4
    box = fp.courtyard_box()
    # a 4x4 square turned 45 degrees spans its diagonal both ways
    assert box[2] - box[0] == pytest.approx(4 * math.sqrt(2), abs=1e-6)
    assert box[3] - box[1] == pytest.approx(4 * math.sqrt(2), abs=1e-6)


def test_footprint_rects_and_circles_on_copper_are_copper(tmp_path):
    body = (
        '(kicad_pcb (version 20221018) (generator "t")'
        '  (footprint "l:ant" (layer "F.Cu") (at 10 10 0)'
        '    (fp_rect (start 0 0) (end 4 2) (layer "F.Cu") (fill yes))'
        '    (fp_circle (center 8 0) (end 9 0) (layer "F.Cu") (fill yes))'
        '    (fp_rect (start -5 -5) (end -1 -1) (layer "F.Cu") (fill none))))'
    )
    path = tmp_path / "a.kicad_pcb"
    path.write_text(body, encoding="utf-8")
    shapes = pcb.parse(path).copper_shapes
    assert len(shapes) == 2  # the hollow rect is its stroke, not its area
    assert all(layer == "F.Cu" for layer, _ in shapes)


def test_a_circular_courtyard_is_kept_as_its_rim(tmp_path):
    body = (
        '(kicad_pcb (version 20221018) (generator "t")'
        '  (footprint "l:led" (layer "F.Cu") (at 20 20 0)'
        '    (fp_circle (center 0 0) (end 3 0) (layer "F.CrtYd"))))'
    )
    path = tmp_path / "r.kicad_pcb"
    path.write_text(body, encoding="utf-8")
    fp = pcb.parse(path).footprints[0]
    assert len(fp.courtyard) >= 8
    box = fp.courtyard_box()
    assert box[2] - box[0] == pytest.approx(6.0, abs=0.2)
    assert box[3] - box[1] == pytest.approx(6.0, abs=0.2)

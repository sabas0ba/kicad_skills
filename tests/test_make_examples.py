import ast
import importlib.util
import json
import sys
from dataclasses import replace
from itertools import pairwise
from pathlib import Path

import pytest


def _generator():
    path = Path(__file__).parents[1] / "tools" / "make_examples.py"
    spec = importlib.util.spec_from_file_location("_make_examples_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _design(examples, **changes):
    return examples.Design(
        **{
            "name": "cache-test",
            "title": "",
            "rev": "",
            "company": "",
            "notes": [],
            "parts": [],
            "nets": {"SIG": []},
            "power_flags": [],
            "board_size": (20.0, 20.0),
            "tracks": [],
        }
        | changes
    )


def test_route_cache_preserves_fixed_layers_through_cleanup(tmp_path, monkeypatch):
    examples = _generator()
    monkeypatch.setattr(examples, "ROUTE_CACHE", tmp_path)
    tracks = [examples.Track("SIG", "B.Cu", 0.3, [(5.0, 5.0), (8.0, 5.0)], keep_layer=True)]
    vias = [examples.Via("SIG", x=x, y=5.0) for x in (5.0, 8.0)]
    design = _design(examples, tracks=tracks, vias=vias, pour=(1.0, 1.0, 19.0, 19.0))
    examples._cache_write(design, "test", design, [])
    cached = examples._cache_read(design.name, "test")
    assert cached == (tracks, vias)
    restored = examples._surfaced(replace(design, tracks=cached[0], vias=cached[1]))
    assert restored.tracks == tracks
    assert restored.vias == vias


def test_route_cache_invalidates_legacy_format(tmp_path, monkeypatch):
    examples = _generator()
    monkeypatch.setattr(examples, "ROUTE_CACHE", tmp_path)
    (tmp_path / "cache-test.old.json").write_text(json.dumps({"tracks": [], "vias": []}))
    assert examples._cache_read("cache-test", "old") is None


@pytest.mark.parametrize(
    "net,wired,expected",
    [
        ("VM", (), "/VM"),
        ("GND", (), "GND"),
        ("+3V3", (), "+3V3"),
        ("+3V3", ("+3V3",), "/+3V3"),
    ],
)
def test_zone_uses_same_net_name_as_pads_and_schematic_labels(net, wired, expected):
    examples = _generator()
    design = _design(examples, wired_power=wired, pour=(1.0, 1.0, 19.0, 19.0))
    zone = examples.sexp.loads(examples._zone(design, 1, "In2.Cu", net_name=net))
    assert zone.child("net_name").atom(0) == expected
    assert examples.board_net_name(design, net) == expected


def test_connector_legend_can_be_placed_explicitly_without_moving_copper(monkeypatch):
    examples = _generator()
    part = examples.Part(
        "J1",
        "test:connector",
        "OUT",
        "test:fp",
        (0.0, 0.0),
        (10.0, 10.0, 0.0),
        pin_legend_at={"1": (15.0, 12.0, "left")},
    )
    node = examples.sexp.loads("""(footprint "fp"
      (pad "1" thru_hole circle (at 0 0) (size 2 2) (drill 1) (layers "*.Cu" "*.Mask")))""")
    monkeypatch.setattr(examples, "footprint_definition", lambda _name: node)
    design = _design(
        examples, parts=[part], nets={"SIG": ["J1.1"]}, rev="A", board_size=(50.0, 40.0)
    )
    root = examples.sexp.loads("(root " + "\n".join(examples._board_silk(design)) + ")")
    legend = next(t for t in root.children("gr_text") if t.atom(0) == "SIG")
    assert list(legend.child("at").atoms())[:2] == [
        design.origin[0] + 15.0,
        design.origin[1] + 12.0,
    ]
    assert part.board == (10.0, 10.0, 0.0)


def test_a_header_legend_stays_on_the_pin_it_names(monkeypatch):
    """Four long names on a 2.54 mm header cannot sit side by side on one
    side of the row. The placer once cleared them by sliding every other
    legend one pin along, onto its neighbour; it now keeps each on its own
    pin and uses the other side of the row instead."""
    examples = _generator()
    part = examples.Part("J1", "test:hdr", "IO", "test:fp", (0.0, 0.0), (20.0, 20.0, 0.0))
    node = examples.sexp.loads(
        '(footprint "fp"'
        ' (pad "1" thru_hole circle (at 0 0) (size 1.7 1.7) (drill 1) (layers "*.Cu" "*.Mask"))'
        ' (pad "2" thru_hole circle (at 2.54 0) (size 1.7 1.7) (drill 1) (layers "*.Cu" "*.Mask"))'
        ' (pad "3" thru_hole circle (at 5.08 0) (size 1.7 1.7) (drill 1) (layers "*.Cu" "*.Mask"))'
        ' (pad "4" thru_hole circle (at 7.62 0) (size 1.7 1.7) (drill 1) (layers "*.Cu" "*.Mask")))'
    )
    monkeypatch.setattr(examples, "footprint_definition", lambda _name: node)
    nets = {f"SIGNAL{n}": [f"J1.{n}"] for n in (1, 2, 3, 4)}
    design = _design(examples, parts=[part], nets=nets, rev="A", board_size=(50.0, 40.0))

    root = examples.sexp.loads("(root " + "\n".join(examples._board_silk(design)) + ")")

    for n in (1, 2, 3, 4):
        legend = next(t for t in root.children("gr_text") if t.atom(0) == f"SIGNAL{n}")
        x = next(iter(legend.child("at").atoms())) - design.origin[0]
        assert x == pytest.approx(20.0 + (n - 1) * 2.54, abs=1e-6)


_TERMINAL = (
    '(footprint "term"'
    ' (fp_rect (start -5 -2.5) (end 5 7.5) (layer "F.CrtYd"))'
    ' (fp_rect (start -4.7 -2.2) (end 4.7 7.2) (layer "F.Fab"))'
    ' (fp_rect (start -4.7 -2.2) (end 4.7 7.2) (layer "F.SilkS"))'
    ' (pad "1" thru_hole circle (at 0 0) (size 2.2 2.2) (drill 1.2) (layers "*.Cu"))'
    ' (pad "2" thru_hole circle (at 0 5) (size 2.2 2.2) (drill 1.2) (layers "*.Cu")))'
)
_FUSE = (
    '(footprint "fuse"'
    ' (fp_rect (start -1.6 -0.9) (end 1.6 0.9) (layer "F.CrtYd"))'
    ' (fp_rect (start -1.3 -0.6) (end 1.3 0.6) (layer "F.Fab"))'
    ' (pad "1" smd rect (at -1.1 0) (size 1 1.2) (layers "F.Cu"))'
    ' (pad "2" smd rect (at 1.1 0) (size 1 1.2) (layers "F.Cu")))'
)


def test_a_legend_with_no_room_beside_its_pin_is_framed_and_pointed_at_it(monkeypatch):
    """A supply terminal against the edge of a board has the fuse every supply
    input carries standing in the only strip it could be labelled from. The
    name used to be pushed out past the fuse, where it printed a millimetre
    from the fuse's pad and named that instead. It goes somewhere legible now,
    in a frame, with a leader back to the pin it means."""
    examples = _generator()
    shapes = {"test:term": _TERMINAL, "test:fuse": _FUSE}
    monkeypatch.setattr(
        examples, "footprint_definition", lambda name: examples.sexp.loads(shapes[name])
    )
    parts = [
        examples.Part("J1", "test:term", "IN", "test:term", (0.0, 0.0), (6.0, 10.0, 0.0)),
        examples.Part("F1", "test:fuse", "1A", "test:fuse", (0.0, 0.0), (14.0, 10.0, 0.0)),
    ]
    design = _design(
        examples,
        parts=parts,
        nets={"VIN": ["J1.1"], "GND": ["J1.2"], "SIG": ["F1.1", "F1.2"]},
        rev="A",
        board_size=(40.0, 30.0),
    )

    root = examples.sexp.loads("(root " + "\n".join(examples._board_silk(design)) + ")")
    legend = next(t for t in root.children("gr_text") if t.atom(0) == "VIN")
    lines = [
        (tuple(line.child("start").atoms()), tuple(line.child("end").atoms()))
        for line in root.children("gr_line")
    ]
    # a frame is four lines; every line of it and of the leader is horizontal,
    # vertical or at 45 degrees
    assert len(lines) >= 5
    for (x1, y1), (x2, y2) in lines:
        dx, dy = abs(x2 - x1), abs(y2 - y1)
        assert dx < 1e-6 or dy < 1e-6 or abs(dx - dy) < 1e-6

    # and one end of the drawing reaches the pad the legend names
    pin = (design.origin[0] + 6.0, design.origin[1] + 10.0)
    reach = min(examples.math.dist(point, pin) for segment in lines for point in segment)
    assert reach < 3.0
    # the name itself is clear of the fuse it used to sit against
    label = next(iter(legend.child("at").atoms()))
    assert abs(label - (design.origin[0] + 14.0)) > 3.0


def test_a_designator_is_not_printed_under_its_own_part(monkeypatch):
    """A library puts the name of a part that spans its own pads in the clear
    gap between them, which is under the part: an electrolytic capacitor, an
    inductor, a module. The courtyard is not the test - a name in the margin
    beside a chip resistor is read on the finished board - the part's own
    fabrication outline is."""
    examples = _generator()
    node = examples.sexp.loads(
        '(footprint "can"'
        ' (property "Reference" "C1" (at 0 0 0))'
        ' (fp_rect (start -4.5 -6.2) (end 4.5 6.2) (layer "F.CrtYd"))'
        ' (fp_rect (start -4.2 -5.9) (end 4.2 5.9) (layer "F.Fab"))'
        ' (pad "1" smd rect (at 0 -3.7) (size 3 2) (layers "F.Cu"))'
        ' (pad "2" smd rect (at 0 3.7) (size 3 2) (layers "F.Cu")))'
    )
    monkeypatch.setattr(examples, "footprint_definition", lambda _name: node)
    part = examples.Part("C1", "test:c", "100u", "test:fp", (0.0, 0.0), (20.0, 20.0, 0.0))
    design = _design(examples, parts=[part], board_size=(40.0, 40.0))

    examples._move_reference_off_pads(design, part, node)

    prop = next(p for p in node.children("property") if p.atom(0) == "Reference")
    cx, cy = (float(a) for a in list(prop.child("at").atoms())[:2])
    half_x, half_y = examples._text_extent("C1", 1.0)
    box = (20 + cx - half_x, 20 + cy - half_y, 20 + cx + half_x, 20 + cy + half_y)
    assert examples._silk_intrusion(design, box, [], [examples._body_box(design, part)]) == 0


@pytest.mark.parametrize(
    "start,end",
    [((0.0, 0.0), (6.0, 3.0)), ((0.0, 0.0), (3.0, 6.0)), ((0.0, 0.0), (-4.0, 4.0))],
)
def test_a_leader_bends_only_at_45_degrees(start, end):
    examples = _generator()
    paths = examples._leg_points(start, end)
    assert paths
    for path in paths:
        assert path[0] == start
        assert path[-1] == end
        for (x1, y1), (x2, y2) in pairwise(path):
            dx, dy = abs(x2 - x1), abs(y2 - y1)
            assert dx < 1e-9 or dy < 1e-9 or abs(dx - dy) < 1e-9


def test_no_module_defines_a_name_twice():
    """A second `def` of a name replaces the first silently, and the first
    one's callers then get the second one's signature. A leader-geometry helper
    called `_crosses` took over the schematic wire planner's `_crosses`, and
    every example stopped generating - on CI, where the wire plan is not
    cached, which is the only place it showed."""
    root = Path(__file__).parents[1]
    for path in sorted([*(root / "tools").glob("*.py"), *(root / "src").rglob("*.py")]):
        tree = ast.parse(path.read_text())
        defined = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]
        twice = sorted({name for name in defined if defined.count(name) > 1})
        assert not twice, f"{path.relative_to(root)} defines {twice} more than once"


def test_a_legend_is_read_as_naming_the_net_on_the_pad_beside_it():
    examples = _generator()
    pads = [((0.0, 0.0, 1.0, 1.0), "VIN"), ((8.0, 0.0, 9.0, 1.0), "OUT")]
    assert examples._names_its_pin((2.0, 0.0, 4.0, 1.0), "VIN", pads)
    assert not examples._names_its_pin((6.0, 0.0, 7.5, 1.0), "VIN", pads)
    # the fuse's near pad carries the same net as the pin, so the name between
    # them names both of them and both are that net
    assert examples._names_its_pin((6.0, 0.0, 7.5, 1.0), "OUT", pads)


@pytest.mark.parametrize("reverse", [False, True])
def test_fixed_layer_survives_loop_cleanup_and_merging(reverse):
    examples = _generator()
    tracks = [
        examples.Track("SIG", "B.Cu", 0.3, [(5.0, 5.0), (8.0, 5.0)]),
        examples.Track("SIG", "B.Cu", 0.3, [(8.0, 5.0), (11.0, 5.0)], keep_layer=True),
    ]
    if reverse:
        tracks.reverse()
    design = _design(examples, tracks=tracks, pour=(1.0, 1.0, 19.0, 19.0))
    result = examples._surfaced(examples._join_runs(examples._unlooped(design)))
    assert len(result.tracks) == 1
    assert result.tracks[0].keep_layer
    assert result.tracks[0].layer == "B.Cu"


def test_a_back_run_under_a_front_pad_is_not_a_loop(monkeypatch):
    """The cutter once read a stub up to a via and the back run coming down
    from it as a cycle, because it split the back run at a front-side pad it
    passed under and then shared the node with the stub's end. The stub and
    the via went, and the run was left on the wrong layer with no way up."""
    examples = _generator()
    part = examples.Part("R1", "test:r", "1k", "test:fp", (0.0, 0.0), (10.0, 10.0, 0.0))
    node = examples.sexp.loads(
        '(footprint "fp" (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Mask")))'
    )
    monkeypatch.setattr(examples, "footprint_definition", lambda _name: node)
    tracks = [
        examples.Track("N", "F.Cu", 0.3, [(10.0, 10.0), (10.0, 8.0)]),
        examples.Track("N", "B.Cu", 0.3, [(10.0, 8.0), (10.0, 14.0)]),
        examples.Track("N", "F.Cu", 0.3, [(10.0, 14.0), (14.0, 14.0)]),
    ]
    vias = [examples.Via("N", x=10.0, y=8.0), examples.Via("N", x=10.0, y=14.0)]
    design = _design(examples, parts=[part], nets={"N": ["R1.1"]}, tracks=tracks, vias=vias)

    cut = examples._unlooped(design)

    assert sorted((v.x, v.y) for v in cut.vias) == [(10.0, 8.0), (10.0, 14.0)]
    assert any(t.layer == "F.Cu" and (10.0, 8.0) in t.points for t in cut.tracks)
    assert any(t.layer == "B.Cu" and t.points == [(10.0, 8.0), (10.0, 14.0)] for t in cut.tracks)


def test_route_digest_includes_fixed_layer_intent():
    examples = _generator()
    track = examples.Track("SIG", "B.Cu", 0.3, [(5.0, 5.0), (8.0, 5.0)])
    design = _design(examples, tracks=[track])
    assert examples._routing_digest(design) != examples._routing_digest(
        replace(design, tracks=[replace(track, keep_layer=True)])
    )


def test_route_digest_includes_unconnected_library_pad_geometry(monkeypatch):
    examples = _generator()
    part = examples.Part("U1", "test:part", "test", "test:fp", (0.0, 0.0), (10.0, 10.0, 0.0))
    design = _design(examples, parts=[part])
    node = examples.sexp.loads('(footprint "fp" (pad "NC" smd rect (at 1 2) (size 1 1)))')
    monkeypatch.setattr(examples, "footprint_definition", lambda _name: node)
    first = examples._routing_digest(design)
    node.child("pad").child("size").args = [2, 1]
    assert examples._routing_digest(design) != first


def test_the_printed_cache_key_is_the_name_a_cold_run_files_under(tmp_path, monkeypatch):
    """CI keys a GitHub cache on `--route-digest` and then asks the generator
    for that same answer by filename. `resolve_routes` takes its digest after
    straightening the stated copper, so a key taken before that step names a
    different question the moment straightening touches a track - and an
    Actions cache key cannot be rewritten once populated, so the two would
    never agree again."""
    examples = _generator()
    monkeypatch.setattr(examples, "ROUTE_CACHE", tmp_path)
    monkeypatch.setattr(examples, "_pipeline", lambda d: d)
    # A stated route that goes up, along and back down to join two points in a
    # straight line: straightening takes it out, and the digest reads every
    # waypoint, so this is a design whose key moves across that step. An auto
    # link gives the router something to answer and the cache something to hold.
    stated = examples.Track("SIG", "F.Cu", 0.3, [(2.0, 2.0), (2.0, 6.0), (8.0, 6.0), (8.0, 2.0)])
    auto = examples.Track("SIG", "F.Cu", 0.3, [(5.0, 15.0), (8.0, 15.0)], auto=True)
    design = _design(examples, tracks=[stated, auto])
    monkeypatch.setattr(
        examples, "_route_all", lambda _d, _o: ([(1, replace(auto, auto=False))], [], [])
    )

    key = examples.route_cache_key(design)
    examples.resolve_routes(design, use_cache=False)

    written = [
        p.name.split(".")[1]
        for p in tmp_path.glob("cache-test.*.json")
        if p.stem != "cache-test.order"
    ]
    assert written == [key]


def test_cold_run_populates_cache_and_required_hit_never_routes(tmp_path, monkeypatch):
    examples = _generator()
    monkeypatch.setattr(examples, "ROUTE_CACHE", tmp_path)
    monkeypatch.setattr(examples, "_pipeline", lambda d: d)
    track = examples.Track("SIG", "B.Cu", 0.3, [(5.0, 5.0), (8.0, 5.0)], auto=True, keep_layer=True)
    design = _design(examples, tracks=[track])
    routed = replace(track, auto=False)
    monkeypatch.setattr(examples, "_route_all", lambda _d, _o: ([(0, routed)], [], []))
    cold = examples.resolve_routes(design, use_cache=False)

    def must_not_route(*_args):
        pytest.fail("a required cache hit called the router")

    monkeypatch.setattr(examples, "_route_all", must_not_route)
    # Routing source participates in the key, so keep the cold key here while
    # replacing the router with a sentinel. The digest contract is tested above.
    digest = next(
        p for p in tmp_path.glob("cache-test.*.json") if p.stem != "cache-test.order"
    ).name.split(".")[1]
    monkeypatch.setattr(examples, "_routing_digest", lambda _d: digest)
    warm = examples.resolve_routes(design, require_cache=True)
    assert warm == cold
    monkeypatch.setattr(examples, "_routing_digest", lambda _d: "absent")
    with pytest.raises(SystemExit, match="required route cache is missing"):
        examples.resolve_routes(design, require_cache=True)


def test_chamfer_does_not_cut_a_track_away_from_its_via():
    examples = _generator()
    corner = (10.0, 0.0)
    design = examples.Design(
        name="via-corner",
        title="",
        rev="",
        company="",
        notes=[],
        parts=[],
        nets={"SIG": []},
        power_flags=[],
        board_size=(20.0, 20.0),
        tracks=[examples.Track("SIG", "F.Cu", 0.3, [(0.0, 0.0), corner, (10.0, 10.0)])],
        vias=[examples.Via("SIG", x=corner[0], y=corner[1])],
    )

    chamfered = examples._chamfer_tracks(design)

    assert corner in chamfered.tracks[0].points


def _two_hops(examples, first, second):
    """Two same-net layer changes, each with its own via, at the given points."""
    return _design(
        examples,
        tracks=[
            examples.Track("SIG", layer, 0.3, [point, (point[0] + 3.0, point[1])])
            for point in (first, second)
            for layer in ("F.Cu", "B.Cu")
        ],
        vias=[examples.Via("SIG", x=point[0], y=point[1]) for point in (first, second)],
    )


def test_vias_drilled_too_close_together_become_one():
    examples = _generator()
    design = _two_hops(examples, (10.0, 10.0), (10.0, 10.5))

    merged = examples._uncrowded(design)

    assert [(via.x, via.y) for via in merged.vias] == [(10.0, 10.25)]


def test_a_via_that_would_stop_reaching_its_copper_is_left_where_it_is():
    examples = _generator()
    # The first via already spans two ends 0.35 mm either side of it. Pulling it
    # 0.2 mm towards the second takes the far one past the 0.4 mm copper radius,
    # so the pair stays as it is even though the holes are 0.6 mm apart.
    design = _design(
        examples,
        tracks=[
            examples.Track("SIG", "F.Cu", 0.3, [(9.65, 10.0), (5.0, 10.0)]),
            examples.Track("SIG", "B.Cu", 0.3, [(10.35, 10.0), (15.0, 10.0)]),
            examples.Track("SIG", "F.Cu", 0.3, [(10.0, 10.6), (10.0, 15.0)]),
            examples.Track("SIG", "B.Cu", 0.3, [(10.0, 10.6), (13.0, 10.6)]),
        ],
        vias=[examples.Via("SIG", x=10.0, y=10.0), examples.Via("SIG", x=10.0, y=10.6)],
    )

    assert examples._uncrowded(design).vias == design.vias


def test_vias_of_different_nets_are_never_merged():
    examples = _generator()
    design = _two_hops(examples, (10.0, 10.0), (10.0, 10.5))
    design = replace(
        design,
        nets={"SIG": [], "OTHER": []},
        vias=[design.vias[0], replace(design.vias[1], net="OTHER")],
    )

    assert examples._uncrowded(design).vias == design.vias


def _links(examples, *specs):
    """Auto links as (net, width) pairs, all between the same two points."""
    return [
        examples.Track(net, "F.Cu", width, [(0.0, 0.0), (5.0, 0.0)], auto=True)
        for net, width in specs
    ]


def test_a_wider_link_outranks_the_thinnest():
    examples = _generator()
    design = _design(examples)
    power, signal = _links(examples, ("VM", 0.4), ("AIN1", 0.3))

    assert examples._route_rank(design, power, 0.3) == 0
    assert examples._route_rank(design, signal, 0.3) == 1


def test_a_named_priority_net_outranks_its_width():
    examples = _generator()
    design = _design(examples, priority_nets=("CLK",))
    (clock,) = _links(examples, ("CLK", 0.2))

    assert examples._route_rank(design, clock, 0.2) == 0


def test_a_plain_link_is_promoted_only_to_the_front_of_its_class():
    examples = _generator()
    design = _design(examples)
    vm, aout, ain1, ain2 = _links(
        examples, ("VM", 0.4), ("AOUT1", 0.4), ("AIN1", 0.3), ("AIN2", 0.3)
    )
    order = [vm, aout, ain2, ain1]

    def rank(track):
        return examples._route_rank(design, track, 0.3)

    promoted = examples._promoted(order, ain1, rank)

    assert promoted == [vm, aout, ain1, ain2]


def test_a_priority_link_is_promoted_to_the_very_front():
    examples = _generator()
    design = _design(examples)
    vm, aout, ain1 = _links(examples, ("VM", 0.4), ("AOUT1", 0.4), ("AIN1", 0.3))
    order = [vm, ain1, aout]

    def rank(track):
        return examples._route_rank(design, track, 0.3)

    assert examples._promoted(order, aout, rank) == [aout, vm, ain1]


def test_board_uuid_canonicalization_ignores_random_input_ids(tmp_path):
    examples = _generator()
    first = tmp_path / "same.kicad_pcb"
    second_dir = tmp_path / "other"
    second_dir.mkdir()
    second = second_dir / first.name
    template = """(kicad_pcb
  (footprint "R" (uuid "{one}"))
  (group "g" (uuid "{two}") (members "{one}"))
)
"""
    first.write_text(template.format(one="1" * 36, two="2" * 36))
    second.write_text(template.format(one="3" * 36, two="4" * 36))

    examples._canonicalize_board_uuids(first)
    examples._canonicalize_board_uuids(second)

    assert first.read_text() == second.read_text()
    root = examples.sexp.load(first)
    uuids = {str(atom) for node in root.walk() if node.name == "uuid" for atom in node.atoms()}
    members = root.child("group").child("members")
    assert set(map(str, members.atoms())) <= uuids

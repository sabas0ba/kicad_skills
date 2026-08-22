from pathlib import Path

from eda_toolkit.kicad import pcb, pcb_review


def board_from(footprints=(), tracks=(), vias=(), zones=(), edges=None, silk=()):
    board = pcb.Board(path=Path("memory.kicad_pcb"), version=0, generator="test")
    board.layers = [
        {"id": "0", "name": "F.Cu", "type": "signal", "user_name": ""},
        {"id": "31", "name": "B.Cu", "type": "signal", "user_name": ""},
    ]
    board.footprints = list(footprints)
    board.tracks = list(tracks)
    board.vias = list(vias)
    board.zones = list(zones)
    board.silk_texts = list(silk)
    board.edges = (
        edges if edges is not None else [{"type": "gr_rect", "points": [(0, 0), (50, 40)]}]
    )
    for fp in board.footprints:
        for pad in fp.pads:
            if pad.net:
                board.nets.setdefault(len(board.nets) + 1, pad.net)
    return board


def pad(number, x, y, net, size=(1.0, 1.0), type_="smd", drill=None):
    return pcb.Pad(
        number=number,
        type=type_,
        shape="rect",
        x=x,
        y=y,
        angle=0,
        size=size,
        drill=drill,
        layers=["F.Cu"],
        net=net,
    )


def footprint(ref, x, y, pads, layer="F.Cu", attrs=("smd",)):
    return pcb.Footprint(
        ref=ref,
        value="",
        lib_id="lib:fp",
        x=x,
        y=y,
        angle=0,
        layer=layer,
        pads=list(pads),
        attributes=list(attrs),
    )


def track(x1, y1, x2, y2, width=0.25, net="", layer="F.Cu"):
    return pcb.Track(start=(x1, y1), end=(x2, y2), width=width, layer=layer, net_code=1, net=net)


def ctx_for(board, **kwargs):
    return pcb_review.PcbContext.from_board(board, **kwargs)


def rules_of(findings):
    return {f.rule for f in findings}


def test_missing_outline_is_an_error():
    ctx = ctx_for(board_from(edges=[]))
    findings = pcb_review.rule_outline(ctx)
    assert [f.rule for f in findings] == ["board.no_outline"]
    assert findings[0].severity == "error"


def test_thin_tracks_are_errors():
    ctx = ctx_for(board_from(tracks=[track(1, 1, 5, 1, width=0.1, net="SIG")]))
    findings = pcb_review.rule_track_width(ctx)
    assert "track.below_minimum" in rules_of(findings)


def test_track_threshold_is_configurable():
    board = board_from(tracks=[track(1, 1, 5, 1, width=0.18, net="SIG")])
    assert "track.below_minimum" not in rules_of(pcb_review.rule_track_width(ctx_for(board)))
    tight = ctx_for(board, thresholds={"min_track_mm": 0.2})
    assert "track.below_minimum" in rules_of(pcb_review.rule_track_width(tight))


def test_thin_power_tracks_warn():
    ctx = ctx_for(board_from(tracks=[track(1, 1, 20, 1, width=0.25, net="+5V")]))
    assert "track.thin_power" in rules_of(pcb_review.rule_track_width(ctx))


def test_a_short_power_neck_is_allowed():
    """A pad entry or an escape may neck down; only a long thin run is the track."""
    ctx = ctx_for(
        board_from(
            tracks=[
                track(1, 1, 4, 1, width=0.25, net="+5V"),  # 3 mm neck
                track(4, 1, 30, 1, width=0.8, net="+5V"),
            ]
        )
    )
    assert "track.thin_power" not in rules_of(pcb_review.rule_track_width(ctx))


def test_detour_flags_the_scenic_route():
    parts = [
        footprint("R1", 5, 5, [pad("1", 5, 5, "SIG")]),
        footprint("R2", 15, 5, [pad("1", 15, 5, "SIG")]),
    ]
    scenic = ctx_for(
        board_from(
            footprints=parts,
            tracks=[
                track(5, 5, 5, 35, net="SIG"),
                track(5, 35, 15, 35, net="SIG"),
                track(15, 35, 15, 5, net="SIG"),
            ],
        )
    )
    findings = pcb_review.rule_detour(scenic)
    assert [f.rule for f in findings] == ["route.detour"]
    assert "SIG" in findings[0].details["examples"][0]

    direct = ctx_for(board_from(footprints=parts, tracks=[track(5, 5, 15, 5, net="SIG")]))
    assert pcb_review.rule_detour(direct) == []


def test_detour_leaves_poured_nets_alone():
    """A net with a pour is stitched, not routed; length says nothing there."""
    parts = [
        footprint("R1", 5, 5, [pad("1", 5, 5, "+5V")]),
        footprint("R2", 15, 5, [pad("1", 15, 5, "+5V")]),
    ]
    ctx = ctx_for(
        board_from(
            footprints=parts,
            tracks=[
                track(5, 5, 5, 35, net="+5V"),
                track(5, 35, 15, 35, net="+5V"),
                track(15, 35, 15, 5, net="+5V"),
            ],
            zones=[pcb.Zone(net="+5V", layers=["F.Cu"], filled=True)],
        )
    )
    assert pcb_review.rule_detour(ctx) == []


def test_wander_flags_one_run_that_goes_out_and_comes_back():
    """The net is short, the run between its two pads is not."""
    parts = [
        footprint("R1", 5, 5, [pad("1", 5, 5, "SIG")]),
        footprint("R2", 15, 5, [pad("1", 15, 5, "SIG")]),
    ]
    scenic = ctx_for(
        board_from(
            footprints=parts,
            tracks=[
                track(5, 5, 5, 25, net="SIG"),
                track(5, 25, 15, 25, net="SIG"),
                track(15, 25, 15, 5, net="SIG"),
            ],
        )
    )
    findings = pcb_review.rule_wander(scenic)
    assert [f.rule for f in findings] == ["route.wander"]
    assert "SIG" in findings[0].details["examples"][0]

    direct = ctx_for(board_from(footprints=parts, tracks=[track(5, 5, 15, 5, net="SIG")]))
    assert pcb_review.rule_wander(direct) == []


def test_wander_measures_runs_not_whole_nets():
    """A net long enough to pass `route.detour` can still hold one bad run."""
    parts = [
        footprint("J1", 5, 5, [pad("1", 5, 5, "SIG")]),
        footprint("J2", 15, 5, [pad("1", 15, 5, "SIG")]),
        footprint("J3", 90, 5, [pad("1", 90, 5, "SIG")]),
    ]
    board = board_from(
        footprints=parts,
        tracks=[
            # J1 to J2 the long way round
            track(5, 5, 5, 25, net="SIG"),
            track(5, 25, 15, 25, net="SIG"),
            track(15, 25, 15, 5, net="SIG"),
            # J2 to J3 straight, and long enough to dilute the ratio
            track(15, 5, 90, 5, net="SIG"),
        ],
    )
    ctx = ctx_for(board)
    assert pcb_review.rule_detour(ctx) == []
    assert "route.wander" in rules_of(pcb_review.rule_wander(ctx))


def test_wander_leaves_a_knee_alone():
    """Going round something costs a few millimetres, and that is not a tour."""
    parts = [
        footprint("R1", 5, 5, [pad("1", 5, 5, "SIG")]),
        footprint("R2", 15, 5, [pad("1", 15, 5, "SIG")]),
    ]
    ctx = ctx_for(
        board_from(
            footprints=parts,
            tracks=[
                track(5, 5, 5, 7, net="SIG"),
                track(5, 7, 15, 7, net="SIG"),
                track(15, 7, 15, 5, net="SIG"),
            ],
        )
    )
    assert pcb_review.rule_wander(ctx) == []


def test_wander_leaves_poured_nets_alone():
    parts = [
        footprint("R1", 5, 5, [pad("1", 5, 5, "GND")]),
        footprint("R2", 15, 5, [pad("1", 15, 5, "GND")]),
    ]
    ctx = ctx_for(
        board_from(
            footprints=parts,
            tracks=[
                track(5, 5, 5, 25, net="GND"),
                track(5, 25, 15, 25, net="GND"),
                track(15, 25, 15, 5, net="GND"),
            ],
            zones=[pcb.Zone(net="GND", layers=["B.Cu"], filled=True)],
        )
    )
    assert pcb_review.rule_wander(ctx) == []


def _plane_zone(cut=False):
    """A B.Cu ground pour over (0,0)-(50,40); optionally with a slot cut out."""
    outline = [(0.0, 0.0), (50.0, 0.0), (50.0, 40.0), (0.0, 40.0)]
    if cut:
        # fill in two halves, leaving x=20..30 empty across the board
        fills = [
            ("B.Cu", [(0.0, 0.0), (20.0, 0.0), (20.0, 40.0), (0.0, 40.0)]),
            ("B.Cu", [(30.0, 0.0), (50.0, 0.0), (50.0, 40.0), (30.0, 40.0)]),
        ]
    else:
        fills = [("B.Cu", outline)]
    return pcb.Zone(net="GND", layers=["B.Cu"], filled=True, outline=outline, fills=fills)


def test_return_path_sees_the_cut():
    crossing = [track(1, 20, 49, 20, net="SIG")]  # 10 mm of it over the slot
    over_cut = ctx_for(
        board_from(tracks=crossing, zones=[_plane_zone(cut=True)]),
        thresholds={"return_path_mm": 5.0},
    )
    findings = pcb_review.rule_return_path(over_cut)
    assert [f.rule for f in findings] == ["route.return_path"]
    assert "SIG" in findings[0].details["examples"][0]

    solid = ctx_for(
        board_from(tracks=crossing, zones=[_plane_zone(cut=False)]),
        thresholds={"return_path_mm": 5.0},
    )
    assert pcb_review.rule_return_path(solid) == []


def test_small_vias_and_annular_rings():
    vias = [
        pcb.Via(x=5, y=5, size=0.45, drill=0.25, layers=["F.Cu", "B.Cu"], net_code=1, net="GND")
    ]
    findings = pcb_review.rule_vias(ctx_for(board_from(vias=vias)))
    assert rules_of(findings) == {"via.small_drill", "via.annular_ring"}


def test_unrouted_net_detected_without_drc():
    board = board_from(
        footprints=[
            footprint("R1", 5, 5, [pad("1", 5, 5, "SIG")]),
            footprint("R2", 20, 5, [pad("1", 20, 5, "SIG")]),
        ]
    )
    findings = pcb_review.rule_unrouted(ctx_for(board))
    assert [f.location for f in findings] == ["SIG"]


def test_unrouted_rule_defers_to_drc():
    board = board_from(
        footprints=[
            footprint("R1", 5, 5, [pad("1", 5, 5, "SIG")]),
            footprint("R2", 20, 5, [pad("1", 20, 5, "SIG")]),
        ]
    )
    ctx = ctx_for(board, drc={"violations": [], "unconnected_items": []})
    assert pcb_review.rule_unrouted(ctx) == []


def test_decoupling_distance():
    ic = footprint(
        "U1",
        10,
        10,
        [pad(str(i), 10 + i, 10, "SIG") for i in range(1, 7)] + [pad("7", 10, 10, "+3V3")],
    )
    far_cap = footprint("C1", 40, 30, [pad("1", 40, 30, "+3V3"), pad("2", 41, 30, "GND")])
    findings = pcb_review.rule_decoupling_placement(ctx_for(board_from([ic, far_cap])))
    assert [f.rule for f in findings] == ["layout.decoupling_distance"]
    assert findings[0].details["cap"] == "C1"

    near_cap = footprint("C1", 12, 10, [pad("1", 12, 10, "+3V3"), pad("2", 13, 10, "GND")])
    assert pcb_review.rule_decoupling_placement(ctx_for(board_from([ic, near_cap]))) == []


def test_missing_decoupling_on_the_board():
    ic = footprint(
        "U1",
        10,
        10,
        [pad(str(i), 10 + i, 10, "SIG") for i in range(1, 7)] + [pad("7", 10, 10, "+3V3")],
    )
    findings = pcb_review.rule_decoupling_placement(ctx_for(board_from([ic])))
    assert [f.rule for f in findings] == ["layout.no_decoupling"]


def test_ground_plane_states():
    none = pcb_review.rule_ground_plane(ctx_for(board_from()))
    assert [f.rule for f in none] == ["layout.no_ground_plane"]

    unfilled = pcb.Zone(net="GND", layers=["B.Cu"], filled=False, fill_enabled=True)
    findings = pcb_review.rule_ground_plane(ctx_for(board_from(zones=[unfilled])))
    assert [f.rule for f in findings] == ["layout.unfilled_zone"]

    filled = pcb.Zone(net="GND", layers=["B.Cu"], filled=True, fill_enabled=True)
    findings = pcb_review.rule_ground_plane(ctx_for(board_from(zones=[filled])))
    assert [f.rule for f in findings] == ["layout.ground_plane"]
    assert findings[0].severity == "info"


def test_footprints_outside_the_outline():
    inside = footprint("R1", 10, 10, [pad("1", 10, 10, "A")])
    outside = footprint("R2", 500, 10, [pad("1", 500, 10, "A")])
    findings = pcb_review.rule_placement(ctx_for(board_from([inside, outside])))
    assert "layout.outside_outline" in rules_of(findings)
    assert findings[0].details["refs"] == ["R2"]


def test_bottom_side_assembly_is_reported():
    bottom = footprint("R2", 10, 10, [pad("1", 10, 10, "A")], layer="B.Cu")
    findings = pcb_review.rule_placement(ctx_for(board_from([bottom])))
    assert "layout.double_sided_assembly" in rules_of(findings)


def test_edge_clearance():
    ctx = ctx_for(board_from(tracks=[track(0.05, 5, 20, 5, net="SIG")]))
    assert "board.edge_clearance" in rules_of(pcb_review.rule_edge_clearance(ctx))


OUTLINE_WITH_CUTOUT = [
    {"type": "gr_rect", "start": (0, 0), "end": (50, 40)},
    {"type": "gr_circle", "centre": (25, 20), "radius": 5},
]


def test_edge_clearance_sees_a_cutout_the_bounding_box_hides():
    """Copper hugging a mounting hole is 15 mm from every bounding-box side."""
    ctx = ctx_for(
        board_from(tracks=[track(25, 25.1, 40, 25.1, net="SIG")], edges=OUTLINE_WITH_CUTOUT)
    )
    findings = pcb_review.rule_edge_clearance(ctx)
    assert "board.edge_clearance" in rules_of(findings)


def test_copper_inside_a_cutout_is_reported_as_outside_the_board():
    ctx = ctx_for(board_from(tracks=[track(25, 20, 26, 20, net="SIG")], edges=OUTLINE_WITH_CUTOUT))
    findings = pcb_review.rule_edge_clearance(ctx)
    assert "board.copper_outside_outline" in rules_of(findings)
    assert [f.severity for f in findings if f.rule == "board.copper_outside_outline"] == ["error"]


def test_copper_well_clear_of_a_curved_edge_is_quiet():
    round_board = [{"type": "gr_circle", "centre": (25, 25), "radius": 25}]
    ctx = ctx_for(board_from(tracks=[track(20, 25, 30, 25, net="SIG")], edges=round_board))
    assert pcb_review.rule_edge_clearance(ctx) == []


def test_a_corner_of_the_bounding_box_is_not_the_board_on_a_round_outline():
    """(4,4) is 4 mm from the bbox corner but outside a circle of radius 25."""
    round_board = [{"type": "gr_circle", "centre": (25, 25), "radius": 25}]
    ctx = ctx_for(board_from(tracks=[track(4, 4, 6, 6, net="SIG")], edges=round_board))
    assert "board.copper_outside_outline" in rules_of(pcb_review.rule_edge_clearance(ctx))


def test_open_outline_only_checks_distance():
    open_edges = [
        {"type": "gr_line", "start": (0, 0), "end": (50, 0)},
        {"type": "gr_line", "start": (50, 0), "end": (50, 40)},
    ]
    ctx = ctx_for(board_from(tracks=[track(10, 0.05, 20, 0.05, net="SIG")], edges=open_edges))
    findings = pcb_review.rule_edge_clearance(ctx)
    assert rules_of(findings) == {"board.edge_clearance"}
    assert findings[0].details["outline_closed"] is False


def test_vias_are_checked_against_the_edge_too():
    via = pcb.Via(x=0.2, y=20, size=0.8, drill=0.4, layers=[], net_code=1, net="A")
    ctx = ctx_for(board_from(vias=[via]))
    assert "board.edge_clearance" in rules_of(pcb_review.rule_edge_clearance(ctx))


def test_drill_variety():
    vias = [
        pcb.Via(x=0, y=0, size=0.8, drill=0.3 + i * 0.05, layers=[], net_code=1, net="A")
        for i in range(8)
    ]
    assert "fab.many_drill_sizes" in rules_of(
        pcb_review.rule_drill_variety(ctx_for(board_from(vias=vias)))
    )


def test_drc_violations_become_findings():
    drc = {
        "violations": [
            {
                "type": "clearance",
                "severity": "error",
                "description": "Clearance violation",
                "items": [{"description": "Track [GND] on F.Cu"}],
            }
        ],
        "unconnected_items": [
            {
                "type": "unconnected_items",
                "severity": "warning",
                "description": "Missing connection",
                "items": [],
            }
        ],
        "schematic_parity": [],
    }
    findings = pcb_review.rule_drc(ctx_for(board_from(), drc=drc))
    assert {f.rule for f in findings} == {"drc.clearance", "drc.unconnected.unconnected_items"}
    # unconnected copper is always an error, whatever DRC calls it
    assert all(f.severity == "error" for f in findings)


def test_review_of_the_example_board(example_project):
    report = pcb_review.review(example_project, use_cli=False)
    assert report["summary"]["error"] == 0
    assert report["statistics"]["layer_count"] == 2
    rules = {f["rule"] for f in report["findings"]}
    assert "drc.unavailable" in rules
    assert "layout.no_decoupling" not in rules
    assert "route.unrouted_net" not in rules


# -- artwork readability and buildability ----------------------------------


def silk(text, x, y, height=1.0, layer="F.SilkS", angle=0, hidden=False, footprint=""):
    return {
        "text": text,
        "layer": layer,
        "x": x,
        "y": y,
        "angle": angle,
        "height": height,
        "width": height,
        "thickness": 0.15,
        "hidden": hidden,
        "footprint": footprint,
    }


def test_silkscreen_below_the_print_limit_is_reported():
    board = board_from(silk=[silk("R1", 5, 5, height=0.4), silk("R2", 9, 5, height=1.0)])
    findings = pcb_review.rule_silk_text_size(ctx_for(board))
    assert findings[0].details["count"] == 1
    assert (
        pcb_review.rule_silk_text_size(ctx_for(board, thresholds={"min_silk_text_height_mm": 0.2}))
        == []
    )


def test_hidden_silkscreen_is_not_judged():
    board = board_from(silk=[silk("R1", 5, 5, height=0.4, hidden=True)])
    assert pcb_review.rule_silk_text_size(ctx_for(board)) == []


def test_silkscreen_printed_across_a_pad():
    board = board_from(
        footprints=[footprint("R1", 10, 10, [pad("1", 10, 10, "SIG", size=(2.0, 2.0))])],
        silk=[silk("R1", 10, 10, height=1.0, footprint="R1")],
    )
    findings = pcb_review.rule_silk_over_pad(ctx_for(board))
    assert findings[0].details["examples"] == ["'R1' over R1.1"]


def test_silkscreen_clear_of_the_pad_is_quiet():
    board = board_from(
        footprints=[footprint("R1", 10, 10, [pad("1", 10, 10, "SIG", size=(1.0, 1.0))])],
        silk=[silk("R1", 10, 14, height=1.0, footprint="R1")],
    )
    assert pcb_review.rule_silk_over_pad(ctx_for(board)) == []


def test_back_silkscreen_is_not_matched_against_front_pads():
    board = board_from(
        footprints=[footprint("R1", 10, 10, [pad("1", 10, 10, "SIG", size=(2.0, 2.0))])],
        silk=[silk("R1", 10, 10, layer="B.SilkS")],
    )
    assert pcb_review.rule_silk_over_pad(ctx_for(board)) == []


def test_placement_off_the_grid_and_at_an_odd_angle():
    board = board_from(
        footprints=[
            footprint("R1", 10.0, 10.0, []),
            footprint("R2", 10.3, 10.0, []),
            footprint("R3", 10.5, 10.0, []),
        ]
    )
    board.footprints[2].angle = 37.0
    board.footprints[0].angle = 270.0
    findings = {f.rule: f for f in pcb_review.rule_placement_grid(ctx_for(board))}
    assert findings["layout.off_grid_placement"].details["count"] == 1
    assert findings["layout.odd_rotation"].details["examples"] == ["R3 at 37.0 deg"]


def test_a_finer_placement_grid_can_be_asked_for():
    board = board_from(footprints=[footprint("R1", 10.3, 10.0, [])])
    ctx = ctx_for(board, thresholds={"placement_grid_mm": 0.1})
    assert "layout.off_grid_placement" not in rules_of(pcb_review.rule_placement_grid(ctx))


def test_pads_of_two_footprints_on_the_same_copper():
    board = board_from(
        footprints=[
            footprint("U1", 10, 10, [pad("1", 10, 10, "A", size=(2.0, 2.0))]),
            footprint("C1", 11, 10, [pad("1", 11, 10, "B", size=(2.0, 2.0))]),
        ]
    )
    findings = pcb_review.rule_pad_collision(ctx_for(board))
    assert findings[0].details["examples"] == ["U1.1 / C1.1"]


def test_pads_on_opposite_sides_do_not_collide():
    top = pad("1", 10, 10, "A", size=(2.0, 2.0))
    bottom = pcb.Pad("1", "smd", "rect", 10, 10, 0, (2.0, 2.0), None, ["B.Cu"], "B")
    board = board_from(
        footprints=[footprint("U1", 10, 10, [top]), footprint("C1", 10, 10, [bottom])]
    )
    assert pcb_review.rule_pad_collision(ctx_for(board)) == []


def test_an_acute_corner_is_reported_and_a_right_angle_is_context():
    acute = board_from(tracks=[track(0, 0, 5, 0, net="S"), track(5, 0, 1, 1, net="S")])
    assert pcb_review.rule_track_angles(ctx_for(acute))[0].details["count"] == 1
    right = board_from(tracks=[track(0, 0, 5, 0, net="S"), track(5, 0, 5, 5, net="S")])
    assert [f.rule for f in pcb_review.rule_track_angles(ctx_for(right))] == ["route.right_angle"]


def test_a_corner_beside_a_pad_is_still_a_corner():
    """The exemption is the pad's connection point, not a disc around it.

    Measured by radius it covered a 0805's whole 0.47 mm, and every chamfered
    pad entry leaves an ordinary corner inside that.
    """
    board = board_from(
        footprints=[footprint("R1", 0, 0, [pad("1", 0, 0, "S", size=(1.0, 1.0))])],
        tracks=[track(0.3, 0.3, 5, 0.3, net="S"), track(0.3, 0.3, 1, 1.3, net="S")],
    )
    assert "route.acute_angle" in rules_of(pcb_review.rule_track_angles(ctx_for(board)))


def test_two_branches_leaving_one_pad_are_not_an_acid_trap():
    """The pad's own copper fills the wedge between them."""
    board = board_from(
        footprints=[footprint("R1", 0, 0, [pad("1", 0, 0, "S", size=(1.0, 1.0))])],
        tracks=[track(0, 0, 5, 0, net="S"), track(0, 0, 4, 4, net="S")],
    )
    assert pcb_review.rule_track_angles(ctx_for(board)) == []


def test_copper_laid_back_along_itself_is_reported_even_on_a_pad():
    """Nought degrees is one run drawn twice, and no pad excuses that."""
    board = board_from(
        footprints=[footprint("R1", 0, 0, [pad("1", 0, 0, "S", size=(1.0, 1.0))])],
        tracks=[track(0, 0, 5, 0, net="S"), track(0, 0, 3, 0, net="S")],
    )
    findings = pcb_review.rule_track_angles(ctx_for(board))
    assert [f.rule for f in findings] == ["route.acute_angle"]
    assert "0 deg" in findings[0].details["examples"][0]


def test_a_bend_off_the_45_grid_is_context():
    # a 20-degree-ish bend: neither straight, nor 45, nor 90
    odd = board_from(tracks=[track(0, 0, 5, 0, net="S"), track(5, 0, 10, 1.8, net="S")])
    assert [f.rule for f in pcb_review.rule_track_angles(ctx_for(odd))] == ["route.odd_angle"]
    # a clean 45 stays silent
    fine = board_from(tracks=[track(0, 0, 5, 0, net="S"), track(5, 0, 8, 3, net="S")])
    assert pcb_review.rule_track_angles(ctx_for(fine)) == []


def test_a_track_end_that_reaches_nothing_is_a_stub():
    board = board_from(
        footprints=[footprint("R1", 0, 0, [pad("1", 0, 0, "S", size=(1.0, 1.0))])],
        tracks=[track(0, 0, 5, 0, net="S")],
    )
    findings = pcb_review.rule_track_stubs(ctx_for(board))
    assert findings[0].details["count"] == 1
    assert findings[0].details["examples"] == ["S on F.Cu at (5, 0)"]


def test_a_track_between_two_pads_is_not_a_stub():
    board = board_from(
        footprints=[
            footprint("R1", 0, 0, [pad("1", 0, 0, "S", size=(1.0, 1.0))]),
            footprint("R2", 5, 0, [pad("1", 5, 0, "S", size=(1.0, 1.0))]),
        ],
        tracks=[track(0, 0, 5, 0, net="S")],
    )
    assert pcb_review.rule_track_stubs(ctx_for(board)) == []


def test_a_track_ending_in_a_pour_of_its_own_net_is_connected():
    board = board_from(
        tracks=[track(0, 0, 5, 0, net="GND")],
        zones=[pcb.Zone(net="GND", layers=["F.Cu"], filled=True)],
    )
    assert pcb_review.rule_track_stubs(ctx_for(board)) == []


def test_decoupling_without_a_via_to_the_plane():
    caps = [pad("1", 10, 10, "+3V3"), pad("2", 11, 10, "GND")]
    board = board_from(
        footprints=[footprint("C1", 10, 10, caps)],
        zones=[pcb.Zone(net="GND", layers=["B.Cu"], filled=True)],
        vias=[pcb.Via(30, 30, 0.6, 0.3, ["F.Cu", "B.Cu"], 1, "GND")],
    )
    findings = pcb_review.rule_decoupling_via(ctx_for(board))
    assert findings[0].location == "C1"
    board.vias = [pcb.Via(11.5, 10, 0.6, 0.3, ["F.Cu", "B.Cu"], 1, "GND")]
    assert pcb_review.rule_decoupling_via(ctx_for(board)) == []


def test_a_cap_already_sitting_in_the_pour_needs_no_via():
    """Two-layer boards poured on both sides made this the noisiest rule in the
    suite until the pad's own layer was taken into account."""
    caps = [pad("1", 10, 10, "+3V3"), pad("2", 11, 10, "GND")]
    board = board_from(
        footprints=[footprint("C1", 10, 10, caps)],
        zones=[pcb.Zone(net="GND", layers=["F.Cu", "B.Cu"], filled=True)],
        vias=[pcb.Via(30, 30, 0.6, 0.3, ["F.Cu", "B.Cu"], 1, "GND")],
    )
    assert pcb_review.rule_decoupling_via(ctx_for(board)) == []


def test_a_net_routed_at_three_widths():
    board = board_from(
        tracks=[
            track(0, 0, 1, 0, width=0.2, net="D0"),
            track(1, 0, 2, 0, width=0.3, net="D0"),
            track(2, 0, 3, 0, width=0.4, net="D0"),
            track(0, 5, 1, 5, width=0.2, net="D1"),
            track(1, 5, 2, 5, width=0.3, net="D1"),
        ]
    )
    findings = pcb_review.rule_track_width_consistency(ctx_for(board))
    assert findings[0].details["examples"] == ["D0: [0.2, 0.3, 0.4]"]


def test_a_board_with_no_free_silk_has_no_name():
    bare = board_from(
        silk=[{"text": "J1", "layer": "F.SilkS", "x": 5.0, "y": 5.0, "footprint": "J1"}]
    )
    findings = pcb_review.rule_board_markings(ctx_for(bare))
    assert "silk.missing_board_id" in {f.rule for f in findings}

    named = board_from(
        silk=[{"text": "demo rev A", "layer": "F.SilkS", "x": 25.0, "y": 38.0, "footprint": None}]
    )
    assert "silk.missing_board_id" not in {
        f.rule for f in pcb_review.rule_board_markings(ctx_for(named))
    }


def test_a_connector_with_no_nearby_silk_is_reported():
    j1 = pcb.Footprint(
        ref="J1",
        value="CONN",
        lib_id="Connector:Conn",
        x=5.0,
        y=20.0,
        angle=0,
        layer="F.Cu",
        pads=[pad("1", 5.0, 19.0, "IN"), pad("2", 5.0, 21.0, "GND")],
    )
    silent = board_from(
        footprints=[j1],
        silk=[{"text": "demo rev A", "layer": "F.SilkS", "x": 45.0, "y": 38.0, "footprint": None}],
    )
    findings = pcb_review.rule_board_markings(ctx_for(silent))
    assert any(f.rule == "silk.unlabeled_connector" for f in findings)

    labelled = board_from(
        footprints=[j1],
        silk=[
            {"text": "demo rev A", "layer": "F.SilkS", "x": 45.0, "y": 38.0, "footprint": None},
            {"text": "IN", "layer": "F.SilkS", "x": 8.0, "y": 19.0, "footprint": None},
        ],
    )
    assert not any(
        f.rule == "silk.unlabeled_connector"
        for f in pcb_review.rule_board_markings(ctx_for(labelled))
    )


def test_a_pour_cut_in_half_is_reported_and_a_whole_one_is_not():
    cut = board_from(zones=[_plane_zone(cut=True)])
    findings = pcb_review.rule_pour_fragmented(ctx_for(cut))
    assert [f.rule for f in findings] == ["layout.pour_fragmented"]
    assert findings[0].details["islands"] == 2
    # 800 of 1600 mm2 in the larger half
    assert findings[0].details["largest_fraction"] == 0.5
    assert pcb_review.rule_pour_fragmented(ctx_for(board_from(zones=[_plane_zone()]))) == []


def test_two_halves_stitched_to_the_far_side_are_one_piece():
    # The same cut plane, with a via in each half. They are the same copper
    # through the other layer, which is what a stitched front pour is, and it
    # is not the defect this rule names.
    stitched = board_from(
        zones=[_plane_zone(cut=True)],
        vias=[
            pcb.Via(x=10, y=20, size=0.8, drill=0.4, layers=["F.Cu", "B.Cu"], net_code=1, net="GND")
        ],
    )
    # one via alone still leaves the other half on its own
    assert [f.rule for f in pcb_review.rule_pour_fragmented(ctx_for(stitched))] == [
        "layout.pour_fragmented"
    ]
    both = board_from(
        zones=[_plane_zone(cut=True)],
        vias=[
            pcb.Via(x=x, y=20, size=0.8, drill=0.4, layers=["F.Cu", "B.Cu"], net_code=1, net="GND")
            for x in (10, 40)
        ],
    )
    assert pcb_review.rule_pour_fragmented(ctx_for(both)) == []


def test_welded_rectangles_of_one_island_are_not_fragmentation():
    # what the generator emits: overlapping strips that are electrically one
    outline = [(0.0, 0.0), (50.0, 0.0), (50.0, 40.0), (0.0, 40.0)]
    fills = [
        ("B.Cu", [(0.0, 0.0), (25.1, 0.0), (25.1, 40.0), (0.0, 40.0)]),
        ("B.Cu", [(24.9, 0.0), (50.0, 0.0), (50.0, 40.0), (24.9, 40.0)]),
    ]
    zone = pcb.Zone(net="GND", layers=["B.Cu"], filled=True, outline=outline, fills=fills)
    assert pcb_review.rule_pour_fragmented(ctx_for(board_from(zones=[zone]))) == []


def test_a_nibbled_edge_is_not_fragmentation():
    outline = [(0.0, 0.0), (50.0, 0.0), (50.0, 40.0), (0.0, 40.0)]
    fills = [
        ("B.Cu", [(0.0, 0.0), (50.0, 0.0), (50.0, 38.0), (0.0, 38.0)]),
        ("B.Cu", [(0.0, 39.0), (5.0, 39.0), (5.0, 40.0), (0.0, 40.0)]),  # a sliver
    ]
    zone = pcb.Zone(net="GND", layers=["B.Cu"], filled=True, outline=outline, fills=fills)
    assert pcb_review.rule_pour_fragmented(ctx_for(board_from(zones=[zone]))) == []


def test_a_pour_mostly_eaten_by_clearance_is_reported():
    outline = [(0.0, 0.0), (50.0, 0.0), (50.0, 40.0), (0.0, 40.0)]
    # only the left third ever became copper
    thin = pcb.Zone(
        net="GND",
        layers=["B.Cu"],
        filled=True,
        outline=outline,
        fills=[("B.Cu", [(0.0, 0.0), (15.0, 0.0), (15.0, 40.0), (0.0, 40.0)])],
    )
    findings = pcb_review.rule_pour_coverage(ctx_for(board_from(zones=[thin])))
    assert [f.rule for f in findings] == ["layout.pour_coverage"]
    assert 0.28 < findings[0].details["coverage"] < 0.32
    # a pour that filled its outline says nothing
    assert pcb_review.rule_pour_coverage(ctx_for(board_from(zones=[_plane_zone()]))) == []


def test_pour_coverage_counts_overlapping_pieces_once():
    """The generated fill is welded rectangles; their areas cannot be added."""
    outline = [(0.0, 0.0), (50.0, 0.0), (50.0, 40.0), (0.0, 40.0)]
    doubled = pcb.Zone(
        net="GND",
        layers=["B.Cu"],
        filled=True,
        outline=outline,
        fills=[("B.Cu", outline), ("B.Cu", outline)],
    )
    assert pcb_review.rule_pour_coverage(ctx_for(board_from(zones=[doubled]))) == []


def test_a_one_sided_pour_is_context():
    one = board_from(zones=[_plane_zone()])
    findings = pcb_review.rule_pour_sides(ctx_for(one))
    assert [f.rule for f in findings] == ["layout.pour_single_sided"]

    both = board_from(
        zones=[
            _plane_zone(),
            pcb.Zone(
                net="GND",
                layers=["F.Cu"],
                filled=True,
                outline=[(0.0, 0.0), (50.0, 0.0), (50.0, 40.0), (0.0, 40.0)],
                fills=[("F.Cu", [(0.0, 0.0), (50.0, 0.0), (50.0, 40.0), (0.0, 40.0)])],
            ),
        ]
    )
    assert pcb_review.rule_pour_sides(ctx_for(both)) == []


def test_a_right_angle_corner_is_context():
    tracks = [
        track(10.0, 10.0, 20.0, 10.0, net="SIG"),
        track(20.0, 10.0, 20.0, 20.0, net="SIG"),
    ]
    findings = pcb_review.rule_track_angles(ctx_for(board_from(tracks=tracks)))
    assert "route.right_angle" in {f.rule for f in findings}

    mitred = [
        track(10.0, 10.0, 18.0, 10.0, net="SIG"),
        track(18.0, 10.0, 20.0, 12.0, net="SIG"),
        track(20.0, 12.0, 20.0, 20.0, net="SIG"),
    ]
    assert "route.right_angle" not in {
        f.rule for f in pcb_review.rule_track_angles(ctx_for(board_from(tracks=mitred)))
    }


def test_an_indicator_without_silk_is_reported():
    led = footprint("D1", 10, 10, [pad("1", 10, 10, "GND")], layer="F.Cu")
    led.lib_id = "LED_SMD:LED_0805_2012Metric"
    bare = board_from(footprints=[led])
    assert [f.rule for f in pcb_review.rule_indicator_markings(ctx_for(bare))] == [
        "silk.unlabeled_indicator"
    ]
    told = board_from(
        footprints=[led],
        silk=[
            {
                "text": "5V OK",
                "x": 12.0,
                "y": 10.0,
                "size": 1.0,
                "footprint": "",
                "layer": "F.SilkS",
            }
        ],
    )
    assert pcb_review.rule_indicator_markings(ctx_for(told)) == []


def test_a_connector_marooned_in_the_middle_is_reported():
    middle = footprint("J1", 25, 20, [pad("1", 25, 20, "A"), pad("2", 26, 20, "B")])
    assert [f.rule for f in pcb_review.rule_connector_at_edge(ctx_for(board_from([middle])))] == [
        "layout.connector_not_at_edge"
    ]
    edge = footprint("J1", 2, 20, [pad("1", 2, 20, "A"), pad("2", 3, 20, "B")])
    assert pcb_review.rule_connector_at_edge(ctx_for(board_from([edge]))) == []


def test_a_width_step_away_from_a_pad_is_reported():
    parts = [footprint("R1", 5, 5, [pad("1", 5, 5, "P")])]
    stepped = board_from(
        footprints=parts,
        tracks=[
            track(5, 5, 20, 5, width=0.3, net="P"),
            track(20, 5, 35, 5, width=0.8, net="P"),
        ],
    )
    findings = pcb_review.rule_track_width_steps(ctx_for(stepped))
    assert [f.rule for f in findings] == ["route.width_step"]
    # the same step, but at the pad the neck was there for
    at_pad = board_from(
        footprints=parts,
        tracks=[
            track(5, 5, 6, 5, width=0.3, net="P"),
            track(6, 5, 30, 5, width=0.8, net="P"),
        ],
    )
    assert pcb_review.rule_track_width_steps(ctx_for(at_pad)) == []
    # a fine-pitch escape is a neck, and a neck has to end somewhere: eight
    # millimetres of it walking out of a pin row is still the pad's own neck
    escaped = board_from(
        footprints=parts,
        tracks=[
            track(5, 5, 13, 5, width=0.3, net="P"),
            track(13, 5, 30, 5, width=0.8, net="P"),
        ],
    )
    assert pcb_review.rule_track_width_steps(ctx_for(escaped)) == []


def test_a_foreign_track_under_a_package_is_reported():
    ic = footprint("U1", 20, 20, [pad(str(i), 16 + i, 16, "OWN") for i in range(1, 9)])
    ic.pads += [pad(str(i + 8), 16 + i, 24, "OWN") for i in range(1, 9)]
    crossing = board_from(footprints=[ic], tracks=[track(10, 20, 30, 20, net="OTHER")])
    assert [f.rule for f in pcb_review.rule_route_under_package(ctx_for(crossing))] == [
        "route.under_package"
    ]
    around = board_from(footprints=[ic], tracks=[track(10, 32, 30, 32, net="OTHER")])
    assert pcb_review.rule_route_under_package(ctx_for(around)) == []


def test_a_keepout_left_at_the_origin_is_reported():
    """A footprint zone is stored in board coordinates, so a placer that moves
    the pads and forgets the zone leaves the keep-out where the library drew
    it - and nothing else on the board complains."""
    stray = pcb.Zone(
        net="",
        layers=["F.Cu"],
        filled=False,
        keepout=True,
        outline=[(0.05, -5.95), (1.95, -5.95), (1.95, -4.05), (0.05, -4.05)],
    )
    findings = pcb_review.rule_zone_outside_outline(ctx_for(board_from(zones=[stray])))
    assert [f.rule for f in findings] == ["layout.zone_outside_outline"]
    assert findings[0].severity == "error"
    assert "keep-out" in findings[0].details["zones"][0]

    # the same keep-out under the part it belongs to
    stray.outline = [(20.0, 20.0), (22.0, 20.0), (22.0, 22.0), (20.0, 22.0)]
    assert pcb_review.rule_zone_outside_outline(ctx_for(board_from(zones=[stray]))) == []


def test_two_silk_strings_through_each_other_are_reported():
    board = board_from(silk=[silk("J1", 10.0, 10.0), silk("IN GND OUT", 11.0, 10.2)])
    findings = pcb_review.rule_silk_over_silk(ctx_for(board))
    assert [f.rule for f in findings] == ["silk.text_over_text"]
    assert findings[0].details["examples"] == ["'J1' over 'IN GND OUT'"]

    # two rows apart, and the same pair reads
    board = board_from(silk=[silk("J1", 10.0, 10.0), silk("IN GND OUT", 11.0, 13.0)])
    assert pcb_review.rule_silk_over_silk(ctx_for(board)) == []

    # front ink cannot collide with back ink
    board = board_from(
        silk=[silk("J1", 10.0, 10.0), silk("IN GND OUT", 11.0, 10.2, layer="B.SilkS")]
    )
    assert pcb_review.rule_silk_over_silk(ctx_for(board)) == []


def test_a_net_crossing_its_own_copper_is_reported():
    """Same potential, so DRC is silent - but the X on the plot is real."""
    parts = [
        footprint("R1", 5, 5, [pad("1", 5, 5, "SIG")]),
        footprint("R2", 15, 15, [pad("1", 15, 15, "SIG")]),
    ]
    board = board_from(
        footprints=parts,
        tracks=[
            track(5, 5, 15, 15, net="SIG"),  # the diagonal
            track(5, 15, 15, 5, net="SIG"),  # its own branch, straight through it
        ],
    )
    findings = pcb_review.rule_self_crossing(ctx_for(board))
    assert [f.rule for f in findings] == ["route.self_crossing"]
    assert findings[0].details["count"] == 1

    # two branches *meeting* at a shared endpoint are a junction, not a cross
    board = board_from(
        footprints=parts,
        tracks=[track(5, 5, 10, 10, net="SIG"), track(10, 10, 15, 5, net="SIG")],
    )
    assert pcb_review.rule_self_crossing(ctx_for(board)) == []

    # two different nets crossing are a short: DRC's finding, not this rule's
    board = board_from(
        footprints=parts,
        tracks=[track(5, 5, 15, 15, net="SIG"), track(5, 15, 15, 5, net="OTHER")],
    )
    assert pcb_review.rule_self_crossing(ctx_for(board)) == []


def test_a_folded_reversal_is_a_hairpin():
    """90 + 45 within a tenth of a millimetre is one fold, not two corners."""
    parts = [footprint("R1", 2, 10, [pad("1", 2, 10, "SIG")])]
    board = board_from(
        footprints=parts,
        tracks=[
            track(10, 10, 5, 10, net="SIG"),  # west
            track(5, 10, 5, 9.9, net="SIG"),  # north, 0.1 mm
            track(5, 9.9, 8, 6.9, net="SIG"),  # back north-east
        ],
    )
    findings = pcb_review.rule_hairpin(ctx_for(board))
    assert [f.rule for f in findings] == ["route.hairpin"]
    assert findings[0].details["count"] == 1

    # the same two corners a stride apart read as a deliberate wrap
    board = board_from(
        footprints=parts,
        tracks=[
            track(10, 10, 5, 10, net="SIG"),
            track(5, 10, 5, 8.5, net="SIG"),
            track(5, 8.5, 8, 5.5, net="SIG"),
        ],
    )
    assert pcb_review.rule_hairpin(ctx_for(board)) == []

    # a staircase alternates direction: the signed turns cancel
    board = board_from(
        footprints=parts,
        tracks=[
            track(0, 10, 2, 8, net="SIG"),
            track(2, 8, 3, 8, net="SIG"),
            track(3, 8, 5, 6, net="SIG"),
        ],
    )
    assert pcb_review.rule_hairpin(ctx_for(board)) == []

    # a fold whose middle sits inside its own pad is the escape fan's hook
    hooked = [footprint("U1", 5, 10, [pad("1", 5, 9.95, "SIG", size=(1.0, 1.0))])]
    board = board_from(
        footprints=hooked,
        tracks=[
            track(10, 10, 5, 10, net="SIG"),
            track(5, 10, 5, 9.9, net="SIG"),
            track(5, 9.9, 8, 6.9, net="SIG"),
        ],
    )
    assert pcb_review.rule_hairpin(ctx_for(board)) == []


def test_a_poured_net_may_cross_itself():
    """A plane is a mesh on purpose; its stitching is not a redundant loop."""
    zone = pcb.Zone(
        net="GND", layers=["B.Cu"], filled=True, outline=[(0, 0), (50, 0), (50, 40), (0, 40)]
    )
    board = board_from(
        footprints=[footprint("R1", 5, 5, [pad("1", 5, 5, "GND")])],
        tracks=[track(5, 5, 15, 15, net="GND"), track(5, 15, 15, 5, net="GND")],
        zones=[zone],
    )
    assert pcb_review.rule_self_crossing(ctx_for(board)) == []


def test_a_via_in_a_land_is_reported_whoever_owns_it():
    """A hole in a land starves the joint above it, ground pad or not."""
    parts = [
        footprint("C1", 10, 10, [pad("1", 9.5, 10, "+5V"), pad("2", 10.5, 10, "GND")]),
    ]
    on_the_land = pcb.Via(
        x=10.5, y=10, size=0.8, drill=0.4, layers=["F.Cu", "B.Cu"], net_code=1, net="GND"
    )
    findings = pcb_review.rule_via_in_pad(ctx_for(board_from(footprints=parts, vias=[on_the_land])))
    assert [f.rule for f in findings] == ["via.in_pad"]
    assert findings[0].details["count"] == 1
    assert "C1.2" in findings[0].details["examples"][0]

    # touching the land's edge is still drilling into it: the copper of a
    # 0.8 mm via reaches 0.4 mm out of its centre, and the land ends at 11.0
    touching = pcb.Via(
        x=11.35, y=10, size=0.8, drill=0.4, layers=["F.Cu", "B.Cu"], net_code=1, net="GND"
    )
    assert pcb_review.rule_via_in_pad(ctx_for(board_from(footprints=parts, vias=[touching])))

    # a stub away, it is the layout every hand board draws
    beside = pcb.Via(
        x=11.8, y=10, size=0.8, drill=0.4, layers=["F.Cu", "B.Cu"], net_code=1, net="GND"
    )
    assert pcb_review.rule_via_in_pad(ctx_for(board_from(footprints=parts, vias=[beside]))) == []

    # the exposed pad under a package is the one land a via array belongs in
    package = [
        footprint(
            "U1", 30, 20, [pad("49", 30, 20, "GND", size=(3.5, 3.5)), pad("1", 27, 20, "SIG")]
        )
    ]
    thermal = pcb.Via(
        x=30, y=20, size=0.8, drill=0.4, layers=["F.Cu", "B.Cu"], net_code=1, net="GND"
    )
    assert pcb_review.rule_via_in_pad(ctx_for(board_from(footprints=package, vias=[thermal]))) == []

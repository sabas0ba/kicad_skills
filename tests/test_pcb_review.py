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


def test_connection_span_flags_theoretical_distance_not_route_style():
    parts = [
        footprint("U1", 5, 5, [pad("1", 5, 5, "SIG")]),
        footprint("R1", 15, 5, [pad("1", 15, 5, "SIG")]),
        footprint("J1", 45, 5, [pad("1", 45, 5, "SIG")]),
    ]
    findings = pcb_review.rule_connection_span(ctx_for(board_from(parts)))
    assert [f.rule for f in findings] == ["layout.connection_span"]
    assert findings[0].location == "SIG"
    assert findings[0].details["items"] == [{"from": "J1.1", "to": "R1.1", "span_mm": 30.0}]


def test_connection_span_collapses_multiple_pads_inside_one_footprint():
    parts = [
        footprint(
            "U1",
            5,
            5,
            [pad("1", 5, 5, "+3V3"), pad("2", 45, 5, "+3V3")],
        ),
        footprint("C1", 46, 5, [pad("1", 46, 5, "+3V3")]),
    ]
    assert pcb_review.rule_connection_span(ctx_for(board_from(parts))) == []


def test_connection_span_breaks_equal_distance_pad_ties_by_number():
    parts = [
        footprint(
            "U1",
            5,
            5,
            [pad("2", 5, 4, "SIG"), pad("1", 5, 6, "SIG")],
        ),
        footprint("J1", 45, 5, [pad("1", 45, 5, "SIG")]),
    ]
    finding = pcb_review.rule_connection_span(ctx_for(board_from(parts)))[0]
    assert finding.details["items"] == [{"from": "J1.1", "to": "U1.1", "span_mm": 40.012}]


def test_connection_span_is_configurable_and_ignores_ground():
    signal = [
        footprint("U1", 5, 5, [pad("1", 5, 5, "SIG")]),
        footprint("J1", 24, 5, [pad("1", 24, 5, "SIG")]),
    ]
    assert pcb_review.rule_connection_span(ctx_for(board_from(signal))) == []
    tight = ctx_for(board_from(signal), thresholds={"max_connection_span_mm": 15.0})
    assert [f.rule for f in pcb_review.rule_connection_span(tight)] == ["layout.connection_span"]

    ground = [
        footprint("U1", 5, 5, [pad("1", 5, 5, "GND")]),
        footprint("J1", 45, 5, [pad("1", 45, 5, "GND")]),
    ]
    assert pcb_review.rule_connection_span(ctx_for(board_from(ground))) == []


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


def test_a_through_via_reaches_inner_copper_layers():
    board = board_from(
        tracks=[track(0, 0, 5, 0, net="S", layer="In2.Cu")],
        vias=[
            pcb.Via(0, 0, 0.6, 0.3, ["F.Cu", "B.Cu"], 1, "S"),
            pcb.Via(5, 0, 0.6, 0.3, ["F.Cu", "B.Cu"], 1, "S"),
        ],
    )
    board.layers = [
        {"id": "0", "name": "F.Cu", "type": "signal", "user_name": ""},
        {"id": "1", "name": "In1.Cu", "type": "power", "user_name": ""},
        {"id": "2", "name": "In2.Cu", "type": "power", "user_name": ""},
        {"id": "31", "name": "B.Cu", "type": "signal", "user_name": ""},
    ]
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
    # Two vias are still not a connection when there is nothing on the other
    # side to connect to: this zone pours B.Cu only, so both vias rise into
    # bare laminate and the halves stay two planes.
    both = board_from(
        zones=[_plane_zone(cut=True)],
        vias=[
            pcb.Via(x=x, y=20, size=0.8, drill=0.4, layers=["F.Cu", "B.Cu"], net_code=1, net="GND")
            for x in (10, 40)
        ],
    )
    assert [f.rule for f in pcb_review.rule_pour_fragmented(ctx_for(both))] == [
        "layout.pour_fragmented"
    ]

    # Give the far side ground copper under both vias and they do join the
    # halves - the same two vias, now landing on the same plane.
    landed = _plane_zone(cut=True)
    landed.fills = [*landed.fills, ("F.Cu", [(0.0, 0.0), (50.0, 0.0), (50.0, 40.0), (0.0, 40.0)])]
    joined = board_from(
        zones=[landed],
        vias=[
            pcb.Via(x=x, y=20, size=0.8, drill=0.4, layers=["F.Cu", "B.Cu"], net_code=1, net="GND")
            for x in (10, 40)
        ],
    )
    assert pcb_review.rule_pour_fragmented(ctx_for(joined)) == []


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


def test_a_plane_that_floods_its_own_drilled_pads_is_reported():
    """A joint that is part of a heat sink cannot be soldered by hand."""
    zone = pcb.Zone(
        net="GND",
        layers=["B.Cu"],
        filled=True,
        pad_connection="solid",
        outline=[(0, 0), (50, 0), (50, 40), (0, 40)],
        fills=[("B.Cu", [(0, 0), (50, 0), (50, 40), (0, 40)])],
    )
    header = footprint(
        "J1",
        5,
        5,
        [pad("1", 5, 5, "GND", type_="thru_hole", drill=1.0), pad("2", 7.54, 5, "SIG")],
        attrs=("through_hole",),
    )
    board = board_from(footprints=[header], zones=[zone])
    findings = pcb_review.rule_solid_pad_connection(ctx_for(board))
    assert [f.rule for f in findings] == ["layout.solid_pad_connection"]
    assert findings[0].details["examples"] == ["J1.1"]

    # thermal relief - KiCad's own default - is the answer, and says nothing
    relieved = pcb.Zone(
        net="GND",
        layers=["B.Cu"],
        filled=True,
        pad_connection="thermal",
        outline=zone.outline,
        fills=zone.fills,
    )
    assert (
        pcb_review.rule_solid_pad_connection(ctx_for(board_from([header], zones=[relieved]))) == []
    )

    # a plane with no drilled pad of its own has no iron to worry about
    smd_only = footprint("C1", 20, 20, [pad("1", 20, 20, "GND"), pad("2", 21, 20, "SIG")])
    assert pcb_review.rule_solid_pad_connection(ctx_for(board_from([smd_only], zones=[zone]))) == []


def test_a_fine_pitch_board_with_no_fiducial_is_reported():
    """A machine placing a 0.5 mm part aligns to copper dots, not to the outline."""
    fine = footprint(
        "U1",
        10,
        10,
        [pad(str(n), 10 + n * 0.5, 10, "SIG", size=(0.3, 1.0)) for n in range(1, 6)],
    )
    findings = pcb_review.rule_fiducials(ctx_for(board_from(footprints=[fine])))
    assert [f.rule for f in findings] == ["fab.no_fiducials"]
    assert "U1" in findings[0].details["examples"][0]

    # with the targets on the board, nothing to say
    marks = footprint("FID1", 2, 2, [pad("1", 2, 2, "")], attrs=("smd",))
    assert pcb_review.rule_fiducials(ctx_for(board_from(footprints=[fine, marks]))) == []

    # and a board of 1.27 mm parts is one tweezers can build
    coarse = footprint(
        "J1",
        30,
        10,
        [pad(str(n), 30 + n * 2.54, 10, "SIG") for n in range(1, 6)],
    )
    assert pcb_review.rule_fiducials(ctx_for(board_from(footprints=[coarse]))) == []


def hole(ref, x, y, net=""):
    """A mounting hole: one plated or unplated pad, and a courtyard the size
    of the drill, which is what the library draws and what misled the placer."""
    fp = footprint(
        ref,
        x,
        y,
        [pad("1", x, y, net, size=(3.2, 3.2), type_="np_thru_hole", drill=3.2)],
        attrs=("through_hole",),
    )
    fp.lib_id = "MountingHole:MountingHole_3.2mm_M3"
    fp.courtyard = [(x - 1.8, y - 1.8), (x + 1.8, y - 1.8), (x + 1.8, y + 1.8), (x - 1.8, y + 1.8)]
    return fp


def test_a_screw_head_that_lands_on_a_part_is_reported():
    """Seven millimetres of washer, not 3.2 mm of drill, is what has to fit."""
    parts = [hole("H1", 20, 20)]
    near = footprint("C1", 24, 20, [pad("1", 24, 20, "+5V")])
    near.courtyard = [(23, 19), (25, 19), (25, 21), (23, 21)]
    findings = pcb_review.rule_fastener_clearance(ctx_for(board_from(footprints=[*parts, near])))
    assert [f.rule for f in findings] == ["mechanical.fastener_clearance"]
    assert findings[0].details["items"][0]["part"] == "C1"

    far = footprint("C1", 30, 20, [pad("1", 30, 20, "+5V")])
    far.courtyard = [(29, 19), (31, 19), (31, 21), (29, 21)]
    assert pcb_review.rule_fastener_clearance(ctx_for(board_from(footprints=[*parts, far]))) == []


def test_a_hole_the_cable_covers_is_reported_against_the_connector():
    """A connector is judged by what plugs into it, not by its outline."""
    connector = footprint("J1", 26, 20, [pad("1", 26, 20, "+5V")])
    connector.courtyard = [(25, 18), (27, 18), (27, 22), (25, 22)]
    findings = pcb_review.rule_fastener_clearance(
        ctx_for(board_from(footprints=[hole("H1", 20, 20), connector]))
    )
    # the body itself is 1 mm clear of the head; the mating space is not
    assert [f.rule for f in findings] == ["mechanical.connector_access"]
    assert findings[0].details["items"][0]["hole"] == "H1"


def test_copper_under_a_screw_head_is_reported():
    """An uninsulated washer resting on a track is a short waiting to happen."""
    under = track(16, 23, 24, 23, net="SIG")
    board = board_from(footprints=[hole("H1", 20, 20)], tracks=[under])
    assert "mechanical.fastener_copper" in rules_of(
        pcb_review.rule_fastener_clearance(ctx_for(board))
    )

    # the hole's own net is the bond, not an accident
    bonded = board_from(
        footprints=[hole("H1", 20, 20, net="GND")], tracks=[track(16, 23, 24, 23, net="GND")]
    )
    assert "mechanical.fastener_copper" not in rules_of(
        pcb_review.rule_fastener_clearance(ctx_for(bonded))
    )


def test_a_designator_printed_off_the_board_is_reported():
    """Ink past the outline is never printed - the offcut takes it away."""
    off = {
        "text": "H1",
        "x": 20,
        "y": -3,
        "height": 1.0,
        "width": 1.0,
        "thickness": 0.15,
        "layer": "F.SilkS",
        "footprint": "H1",
    }
    findings = pcb_review.rule_silk_off_board(ctx_for(board_from(silk=[off])))
    assert [f.rule for f in findings] == ["silk.off_board"]

    on = dict(off, y=3)
    assert pcb_review.rule_silk_off_board(ctx_for(board_from(silk=[on]))) == []


def _gnd_via(x, y):
    return pcb.Via(x=x, y=y, size=0.6, drill=0.3, layers=["F.Cu", "B.Cu"], net_code=2, net="GND")


def test_two_nets_that_share_a_channel_are_reported_and_a_pair_is_not():
    """The 3W rule: length times proximity, with the deliberate pair exempt."""
    tight = [
        track(0, 10, 30, 10, width=0.25, net="CLK"),
        track(0, 10.5, 30, 10.5, width=0.25, net="DATA"),  # 2W away for 30 mm
    ]
    findings = pcb_review.rule_parallel_runs(ctx_for(board_from(tracks=tight)))
    assert [f.rule for f in findings] == ["emc.parallel_run"]
    assert abs(findings[0].details["pairs"][0]["coupled_mm"] - 30) <= 1

    # the same geometry as a named differential pair is the intended layout
    pair = [
        track(0, 10, 30, 10, width=0.25, net="/USB_P"),
        track(0, 10.5, 30, 10.5, width=0.25, net="/USB_N"),
    ]
    assert pcb_review.rule_parallel_runs(ctx_for(board_from(tracks=pair))) == []

    # three widths apart, or crossing at an angle, is not a shared channel
    spaced = [
        track(0, 10, 30, 10, width=0.25, net="CLK"),
        track(0, 11.5, 30, 11.5, width=0.25, net="DATA"),
    ]
    assert pcb_review.rule_parallel_runs(ctx_for(board_from(tracks=spaced))) == []
    crossing = [
        track(0, 10, 30, 10, width=0.25, net="CLK"),
        track(15, 0, 15.2, 20, width=0.25, net="DATA"),
    ]
    assert pcb_review.rule_parallel_runs(ctx_for(board_from(tracks=crossing))) == []


def test_the_pair_exemption_does_not_cross_sheets():
    """Two nets from different sheets sharing a leaf name are not a pair."""
    impostors = [
        track(0, 10, 30, 10, width=0.25, net="/channel_a/USB_P"),
        track(0, 10.5, 30, 10.5, width=0.25, net="/channel_b/USB_N"),
    ]
    findings = pcb_review.rule_parallel_runs(ctx_for(board_from(tracks=impostors)))
    assert [f.rule for f in findings] == ["emc.parallel_run"]


def test_only_the_close_part_of_a_converging_run_is_counted():
    """Heading within 15 degrees but mostly far apart: count the close metres."""
    converging = [
        track(0, 12, 40, 12, width=0.25, net="CLK"),
        track(0, 16, 40, 8, width=0.25, net="DATA"),  # crosses at x=20, 11 deg
    ]
    assert pcb_review.rule_parallel_runs(ctx_for(board_from(tracks=converging))) == []


def test_arcs_are_measured_as_curves_not_chords():
    """Two arcs sharing endpoints but bowing apart never actually run together."""
    bowed_apart = [
        pcb.Track((0, 10), (30, 10), 0.25, "F.Cu", 1, "CLK", kind="arc", mid=(15, 3)),
        pcb.Track((0, 10), (30, 10), 0.25, "F.Cu", 2, "DATA", kind="arc", mid=(15, 17)),
    ]
    assert pcb_review.rule_parallel_runs(ctx_for(board_from(tracks=bowed_apart))) == []


def test_a_cell_boundary_does_not_hide_a_shared_channel():
    """The 4 mm bucket edge at y=12 must not separate y=11.8 from y=12.2."""
    straddling = [
        track(0, 11.8, 30, 11.8, width=0.25, net="CLK"),
        track(0, 12.2, 30, 12.2, width=0.25, net="DATA"),
    ]
    findings = pcb_review.rule_parallel_runs(ctx_for(board_from(tracks=straddling)))
    assert [f.rule for f in findings] == ["emc.parallel_run"]
    assert abs(findings[0].details["pairs"][0]["coupled_mm"] - 30) <= 1


def test_a_double_sided_pour_wants_its_rim_stitched():
    """Two facing pours are a capacitor until the vias make them a conductor."""
    full = [(0, 0), (50, 0), (50, 40), (0, 40)]
    pours = [
        pcb.Zone(net="GND", layers=["F.Cu"], filled=True, fills=[("F.Cu", full)]),
        pcb.Zone(net="GND", layers=["B.Cu"], filled=True, fills=[("B.Cu", full)]),
    ]
    # a ring of rim vias 10 mm apart on a 50x40 board: nothing to report
    ring = [_gnd_via(x, y) for x in (5, 15, 25, 35, 45) for y in (2, 38)] + [
        _gnd_via(x, y) for x in (2, 48) for y in (12, 22, 30)
    ]
    quiet = pcb_review.rule_stitching_pitch(ctx_for(board_from(zones=pours, vias=ring)))
    assert quiet == []

    # only two vias, both in one corner: one enormous gap round the rim
    sparse = [_gnd_via(2, 2), _gnd_via(6, 2)]
    findings = pcb_review.rule_stitching_pitch(ctx_for(board_from(zones=pours, vias=sparse)))
    assert [f.rule for f in findings] == ["emc.stitching_pitch"]
    assert findings[0].details["widest_gap_mm"] > 18

    # a via parked outside the outline joins no pour and mends no fence
    parked = [*sparse, _gnd_via(-5, 20), _gnd_via(25, -5)]
    findings = pcb_review.rule_stitching_pitch(ctx_for(board_from(zones=pours, vias=parked)))
    assert [f.rule for f in findings] == ["emc.stitching_pitch"]
    assert findings[0].details["rim_vias"] == 2

    # nor does one standing in a clearance cut of the front fill: the fill
    # polygons, not the net name, say where a via actually stitches
    notched = [(0, 0), (50, 0), (50, 40), (30, 40), (30, 30), (20, 30), (20, 40), (0, 40)]
    cut_pours = [
        pcb.Zone(net="GND", layers=["F.Cu"], filled=True, fills=[("F.Cu", notched)]),
        pours[1],
    ]
    in_cut = [*sparse, _gnd_via(25, 38)]  # inside the board, inside the notch
    findings = pcb_review.rule_stitching_pitch(ctx_for(board_from(zones=cut_pours, vias=in_cut)))
    assert [f.rule for f in findings] == ["emc.stitching_pitch"]
    assert findings[0].details["rim_vias"] == 2

    # two overlapping same-net fills on one face are a union, not an XOR: a
    # via standing in the overlap stitches, and must not vanish from the rim
    left = [(0, 0), (30, 0), (30, 40), (0, 40)]
    right = [(20, 0), (50, 0), (50, 40), (20, 40)]
    lapped = [
        pcb.Zone(net="GND", layers=["F.Cu"], filled=True, fills=[("F.Cu", left)]),
        pcb.Zone(net="GND", layers=["F.Cu"], filled=True, fills=[("F.Cu", right)]),
        pours[1],
    ]
    overlap_ring = [*ring, _gnd_via(25, 2)]  # in the overlap band, on the rim
    quiet = pcb_review.rule_stitching_pitch(ctx_for(board_from(zones=lapped, vias=overlap_ring)))
    assert quiet == []

    # two local patches facing each other in the middle of the board are not
    # an edge plane: there is no rim sandwich, so the rule has nothing to say
    inland = [
        pcb.Zone(
            net="GND",
            layers=["F.Cu"],
            filled=True,
            fills=[("F.Cu", [(20, 15), (30, 15), (30, 25), (20, 25)])],
        ),
        pcb.Zone(
            net="GND",
            layers=["B.Cu"],
            filled=True,
            fills=[("B.Cu", [(21, 16), (29, 16), (29, 24), (21, 24)])],
        ),
    ]
    assert pcb_review.rule_stitching_pitch(ctx_for(board_from(zones=inland, vias=[]))) == []

    # nor is a pair that only clips one corner an edge plane: the rim has to
    # carry ground on both faces along a good part of its length
    corner = [
        pcb.Zone(
            net="GND",
            layers=[layer],
            filled=True,
            fills=[(layer, [(0, 0), (8, 0), (8, 8), (0, 8)])],
        )
        for layer in ("F.Cu", "B.Cu")
    ]
    assert pcb_review.rule_stitching_pitch(ctx_for(board_from(zones=corner, vias=[]))) == []

    # a single-sided pour has no sandwich to stitch
    single = [pours[0]]
    assert pcb_review.rule_stitching_pitch(ctx_for(board_from(zones=single, vias=sparse))) == []


def _gnd_via_at(x, y, layers=("F.Cu", "B.Cu")):
    return pcb.Via(x=x, y=y, size=0.6, drill=0.3, layers=list(layers), net_code=1, net="GND")


def test_a_via_joins_two_islands_only_where_it_reaches_the_same_far_copper():
    """A same-net via proves nothing on its own: it has to land somewhere.

    Two halves of a front pour are one plane when the back is unbroken under
    them, and two planes when the back is cut in the same place - the vias are
    identical either way, so the fill on the far side is what decides.
    """
    left = [(0, 0), (20, 0), (20, 40), (0, 40)]
    right = [(30, 0), (50, 0), (50, 40), (30, 40)]
    vias = [_gnd_via_at(10, 20), _gnd_via_at(40, 20)]

    whole = [
        pcb.Zone(
            net="GND",
            layers=["F.Cu", "B.Cu"],
            filled=True,
            fills=[
                ("F.Cu", left),
                ("F.Cu", right),
                ("B.Cu", [(0, 0), (50, 0), (50, 40), (0, 40)]),
            ],
        )
    ]
    quiet = pcb_review.rule_pour_fragmented(ctx_for(board_from(zones=whole, vias=vias)))
    assert [f for f in quiet if f.rule == "layout.pour_fragmented"] == []

    cut = [
        pcb.Zone(
            net="GND",
            layers=["F.Cu", "B.Cu"],
            filled=True,
            fills=[("F.Cu", left), ("F.Cu", right), ("B.Cu", left), ("B.Cu", right)],
        )
    ]
    findings = pcb_review.rule_pour_fragmented(ctx_for(board_from(zones=cut, vias=vias)))
    # both faces are cut in the same place, so both are in pieces
    assert [f.rule for f in findings if f.rule == "layout.pour_fragmented"] == [
        "layout.pour_fragmented",
        "layout.pour_fragmented",
    ]


def test_a_pour_that_only_filled_one_face_is_one_sided():
    """The zone asked for both; what landed is what the board has."""
    full = [(0, 0), (50, 0), (50, 40), (0, 40)]
    partial = [pcb.Zone(net="GND", layers=["F.Cu", "B.Cu"], filled=True, fills=[("F.Cu", full)])]
    findings = pcb_review.rule_pour_sides(ctx_for(board_from(zones=partial)))
    assert [f.rule for f in findings] == ["layout.pour_single_sided"]

    both = [
        pcb.Zone(
            net="GND",
            layers=["F.Cu", "B.Cu"],
            filled=True,
            fills=[("F.Cu", full), ("B.Cu", full)],
        )
    ]
    assert pcb_review.rule_pour_sides(ctx_for(board_from(zones=both))) == []

    # a zone with no computed fill at all is `layout.unfilled_zone`'s business
    unfilled = [pcb.Zone(net="GND", layers=["F.Cu", "B.Cu"], filled=False, fills=[])]
    assert pcb_review.rule_pour_sides(ctx_for(board_from(zones=unfilled))) == []


def test_the_edge_index_answers_what_the_exhaustive_pass_would():
    """The grid is an optimisation; it must not change a single verdict.

    The cases that could go wrong are the ones near a cell boundary, so every
    shape here is placed on one: two rectangles welded along x = 1.0, two more
    a whisker apart across it, and a small square wholly inside a large one
    with no edge in common at all.
    """
    touch = pcb_review._polygons_touch

    def box(x0, y0, x1, y1):
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]

    def exhaustive(a, b):
        for p0, p1 in zip(a, [*a[1:], a[0]], strict=True):
            for q0, q1 in zip(b, [*b[1:], b[0]], strict=True):
                if pcb_review._segments_meet(p0, p1, q0, q1):
                    return True
        return pcb_review._point_in_polygon(a[0], b) or pcb_review._point_in_polygon(b[0], a)

    pairs = [
        (box(0, 0, 1.0, 1), box(1.0, 0, 2, 1), True),  # welded on a cell edge
        (box(0, 0, 1.0, 1), box(1.001, 0, 2, 1), False),  # a whisker apart
        (box(0, 0, 4, 4), box(1.5, 1.5, 2.5, 2.5), True),  # wholly inside
        (box(0, 0, 1, 1), box(3, 3, 4, 4), False),  # nowhere near
        (box(0, 0, 2, 2), box(1, 1, 3, 3), True),  # overlapping corners
    ]
    for a, b, expected in pairs:
        assert touch(a, b) is expected, f"{a} vs {b}"
        assert exhaustive(a, b) is expected, f"the reference disagrees on {a} vs {b}"


def test_the_edge_index_survives_a_polygon_off_the_origin():
    """Negative coordinates floor to negative cells; the grid must still line up."""
    a = [(-10.0, -10.0), (-5.0, -10.0), (-5.0, -5.0), (-10.0, -5.0)]
    b = [(-5.0, -10.0), (0.0, -10.0), (0.0, -5.0), (-5.0, -5.0)]
    assert pcb_review._polygons_touch(a, b) is True
    away = [(0.5, -10.0), (5.0, -10.0), (5.0, -5.0), (0.5, -5.0)]
    assert pcb_review._polygons_touch(a, away) is False


def test_the_row_index_answers_what_the_full_ray_cast_would():
    """Skipping the edges that cannot straddle the point must skip nothing else.

    An L takes the interesting cases with it: points in the notch are outside
    while sitting inside the bounding box, and the ones on a row boundary are
    where an index that files an edge in the wrong band goes wrong.
    """
    el = [(0.0, 0.0), (4.0, 0.0), (4.0, 1.0), (1.0, 1.0), (1.0, 4.0), (0.0, 4.0)]
    index = pcb_review._edge_index(el)
    probes = [
        (0.5, 0.5),
        (3.5, 0.5),
        (3.5, 2.0),  # in the notch: inside the box, outside the copper
        (0.5, 3.5),
        (2.0, 1.0),  # exactly on an edge, and on a row boundary
        (0.5, 2.0),
        (-1.0, 0.5),
        (2.0, 4.5),
    ]
    for probe in probes:
        assert pcb_review._point_in_polygon_indexed(probe, el, index) is (
            pcb_review._point_in_polygon(probe, el)
        ), f"the index and the full cast disagree at {probe}"


def test_a_chain_of_buried_vias_makes_two_front_islands_one_plane():
    """Connectivity is transitive; grouping vias by one region is not.

    A front island reaches an inner region, a buried via carries that region
    on to a second inner region, and a third via comes back up to the second
    front island. That is one piece of ground, and calling the front pour
    fragmented because no single region holds both front vias is a fault the
    board does not have.
    """
    board = pcb.Board(path=Path("memory.kicad_pcb"), version=0, generator="test")
    board.layers = [
        {"id": "0", "name": "F.Cu", "type": "signal", "user_name": ""},
        {"id": "1", "name": "In1.Cu", "type": "signal", "user_name": ""},
        {"id": "2", "name": "In2.Cu", "type": "signal", "user_name": ""},
        {"id": "31", "name": "B.Cu", "type": "signal", "user_name": ""},
    ]
    board.edges = [{"type": "gr_rect", "points": [(0, 0), (50, 40)]}]
    left = [(0, 0), (20, 0), (20, 40), (0, 40)]
    right = [(30, 0), (50, 0), (50, 40), (30, 40)]
    board.zones = [
        pcb.Zone(
            net="GND",
            layers=["F.Cu", "In1.Cu", "In2.Cu"],
            filled=True,
            # the front in two islands, and each inner layer carrying one
            # patch that spans the gap between a front island and the middle
            fills=[
                ("F.Cu", left),
                ("F.Cu", right),
                ("In1.Cu", [(5, 15), (28, 15), (28, 25), (5, 25)]),
                ("In2.Cu", [(22, 15), (45, 15), (45, 25), (22, 25)]),
            ],
        )
    ]
    board.vias = [
        _gnd_via_at(10, 20, layers=("F.Cu", "In1.Cu")),  # left island -> In1
        _gnd_via_at(25, 20, layers=("In1.Cu", "In2.Cu")),  # In1 -> In2, buried
        _gnd_via_at(40, 20, layers=("F.Cu", "In2.Cu")),  # In2 -> right island
    ]
    findings = pcb_review.rule_pour_fragmented(ctx_for(board))
    assert findings == [], f"the chain was not followed: {[f.message for f in findings]}"

    # break the chain and the two front islands are two islands again
    board.zones[0].fills[3] = ("In2.Cu", [(35, 15), (45, 15), (45, 25), (35, 25)])
    assert [f.rule for f in pcb_review.rule_pour_fragmented(ctx_for(board))] == [
        "layout.pour_fragmented"
    ]


def test_one_unfilled_zone_does_not_speak_for_a_board_poured_on_one_face():
    """The fallback to stated layers is for a board nobody has filled yet.

    Asked per zone, an unfilled zone declaring both faces hides a board whose
    only real copper is on the front - which is exactly the case this rule
    exists to name.
    """
    full = [(0, 0), (50, 0), (50, 40), (0, 40)]
    zones = [
        pcb.Zone(net="GND", layers=["F.Cu"], filled=True, fills=[("F.Cu", full)]),
        pcb.Zone(net="GND", layers=["F.Cu", "B.Cu"], filled=False, fills=[]),
    ]
    findings = pcb_review.rule_pour_sides(ctx_for(board_from(zones=zones)))
    assert [f.rule for f in findings] == ["layout.pour_single_sided"]
    assert findings[0].details["layers"] == ["F.Cu"]

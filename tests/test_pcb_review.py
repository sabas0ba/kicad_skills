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

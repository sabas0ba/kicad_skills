import importlib.util
import json
import sys
from dataclasses import replace
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

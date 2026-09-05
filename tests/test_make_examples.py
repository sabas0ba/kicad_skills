import importlib.util
import sys
from pathlib import Path


def _generator():
    path = Path(__file__).parents[1] / "tools" / "make_examples.py"
    spec = importlib.util.spec_from_file_location("_make_examples_test", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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

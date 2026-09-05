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

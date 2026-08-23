"""Measure the visual signature of a board: the numbers behind how it looks.

A reviewer can tell a hand-routed board from an autorouted one across the
room, and none of the ordinary checks say why - DRC, ERC and the review rules
all pass boards that look wrong. The tell is statistical, and this measures
it, so that "looks autorouted" becomes a comparison instead of an opinion:

* **layer balance** - a person routing two layers gives each a direction
  (front vertical, back horizontal, or the reverse) and the copper splits
  between them; an autorouter charged to stay off the plane puts everything
  on one face.
* **median segment length** - a person draws long strokes with one 45-degree
  jog; a grid search keeps the cell size as its rhythm.
* **corners per decimetre** - the same difference, counted the other way.
* **corner angles** - human boards are almost all 45s; staircases and odd
  angles are machine artefacts.
* **vias per decimetre of track** - stitching carpets and layer thrash both
  show up here.

Run it over KiCad's demo projects and a generated board side by side::

    docker run --rm -u $(id -u):$(id -g) -v "$PWD:/work" -w /work \
      -e PYTHONPATH=/work/src --entrypoint python3 eda-toolkit:10.0.4 \
      tools/board_signature.py /usr/share/kicad/demos/* examples/*/reviewed

The corpus baseline (16 parsable demo boards, 2026-08): two-layer boards
carry 10-47% of their copper on the second face, median segments run
1.8-3.5 mm, corners come 9-25 per decimetre and 91-98% of them are 45s.
"""

from __future__ import annotations

import collections
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from eda_toolkit.kicad import pcb


def signature(path: Path) -> dict:
    board = pcb.parse(path)
    size = board.size_mm() or (0.0, 0.0)
    per_layer: collections.Counter = collections.Counter()
    total = 0.0
    for track in board.tracks:
        per_layer[track.layer] += track.length
        total += track.length

    # Corner census: the angle at every point where exactly two segments of
    # one net meet on one layer. Pads and junctions are excluded by the
    # "exactly two" condition, the same way `route.wander` cuts its runs.
    ends = collections.defaultdict(list)
    for index, track in enumerate(board.tracks):
        if track.length < 0.01:
            continue
        for point in (track.start, track.end):
            key = (round(point[0], 3), round(point[1], 3), track.layer, track.net_code)
            ends[key].append(index)
    n45 = n90 = nodd = 0
    for key, indices in ends.items():
        if len(indices) != 2:
            continue
        one, other = board.tracks[indices[0]], board.tracks[indices[1]]
        here = (key[0], key[1])

        def away(track, here=here):
            start = (round(track.start[0], 3), round(track.start[1], 3))
            out = track.end if start == here else track.start
            return (out[0] - here[0], out[1] - here[1])

        v1, v2 = away(one), away(other)
        l1, l2 = math.hypot(*v1), math.hypot(*v2)
        if l1 < 0.01 or l2 < 0.01:
            continue
        dot = (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2)
        turn = 180 - math.degrees(math.acos(max(-1.0, min(1.0, dot))))
        if turn < 5:
            continue
        if abs(turn - 45) < 6:
            n45 += 1
        elif abs(turn - 90) < 6:
            n90 += 1
        else:
            nodd += 1

    lengths = sorted(t.length for t in board.tracks if t.length >= 0.05)
    corners = n45 + n90 + nodd
    dm = total / 100.0
    return {
        "size": f"{size[0]:.0f}x{size[1]:.0f}",
        "track_mm": round(total),
        "dominant_layer_share": round(max(per_layer.values()) / total, 2) if total else 0.0,
        "vias_per_dm": round(len(board.vias) / dm, 1) if dm else 0.0,
        "med_seg_mm": round(lengths[len(lengths) // 2], 2) if lengths else 0.0,
        "corners_per_dm": round(corners / dm, 1) if dm else 0.0,
        "pct45": round(100 * n45 / corners) if corners else 0,
        "pct90": round(100 * n90 / corners) if corners else 0,
        "pctodd": round(100 * nodd / corners) if corners else 0,
    }


def main(argv: list[str]) -> int:
    targets: list[Path] = []
    for arg in argv:
        p = Path(arg)
        if p.is_dir():
            targets.extend(sorted(p.glob("*.kicad_pcb")))
        elif p.suffix == ".kicad_pcb":
            targets.append(p)
    if not targets:
        print("usage: board_signature.py <board.kicad_pcb | project-dir>...", file=sys.stderr)
        return 2
    header = (
        f"{'board':30s} {'size':9s} {'trk_mm':6s} {'dom%':5s} "
        f"{'via/dm':6s} {'seg_med':7s} {'crn/dm':6s} {'45%':4s} {'90%':4s} {'odd%':4s}"
    )
    print(header)
    for path in targets:
        try:
            s = signature(path)
        except Exception as exc:  # a corpus sweep should not die on one board
            print(f"{path.stem:30s} skip: {type(exc).__name__}")
            continue
        print(
            f"{path.stem:30s} {s['size']:9s} {s['track_mm']:6d} "
            f"{s['dominant_layer_share']:5.2f} {s['vias_per_dm']:6.1f} "
            f"{s['med_seg_mm']:7.2f} {s['corners_per_dm']:6.1f} "
            f"{s['pct45']:4d} {s['pct90']:4d} {s['pctodd']:4d}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))

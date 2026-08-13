---
name: kicad-pcb-review
description: Read a KiCad board (.kicad_pcb) and review the artwork - DRC, schematic parity, unrouted nets, track widths, vias and drills, edge clearance, decoupling placement, ground pour, silkscreen over pads and text size, placement grid and rotation, overlapping footprints, track stubs and acute corners, decoupling vias - and render the layers and 3D views as PNGs for visual inspection. Use when asked to review, check or look at a PCB layout, artwork, routing, stackup or fabrication readiness.
---

# KiCad PCB / artwork review

> One of the [kicad_skills](https://github.com/sabas0ba/kicad_skills) usage guides for the
> `eda` CLI — [all of them](README.md). Plain Markdown: read it directly, or hand it to
> whatever assistant you use.

Reads `.kicad_pcb` files, runs KiCad's DRC and renders the artwork so it can be
looked at. Runs in the container (see the `eda-environment` guide).

## The commands

```bash
./bin/eda.sh pcb info   hardware/                      # stackup, footprints, nets, routing stats
./bin/eda.sh pcb review hardware/ --text               # DRC + layout heuristics
./bin/eda.sh gate       hardware/ --policy ai-generated --text  # one pass/fail verdict
./bin/eda.sh pcb render hardware/ -o /tmp/art --dpi 300
./bin/eda.sh pcb glb    hardware/ -o /tmp/board.glb    # 3D model for a browser
./bin/eda.sh pcb electrical hardware/                  # current, resistance, impedance
./bin/eda.sh report     hardware/ -o /tmp/report       # all of the above, one page
```

`pcb render` writes PNGs (plus the intermediate PDFs) and an `images.json`
manifest. Default views: `front`, `back`, `copper-front`, `copper-back`,
`silk-front`, plus 3D `top`/`bottom`/`iso` renders. Add `--per-layer` for one
image per copper layer, `--views outline assembly-front ...` to choose,
`--glb` for a 3D model, and `--no-3d` to skip the (slow) ray-traced renders.

Whenever there is more than one image it also writes **`contact-sheet.png`**,
every view tiled and labelled. Read that first: one image answers "is anything
on the wrong layer" without opening a dozen files. `--no-sheet` turns it off.

`pcb electrical` does the arithmetic the width checks only gesture at, using the
board's own stackup:

* **per net, sorted by the tightest first** — the narrowest segment, the current
  it carries at a 10 K rise (`--temperature-rise` to change that), the total
  track length, and the resistance of all of it in series. That last one is an
  upper bound on the resistance between any two points on the net, because
  parallel paths only lower it.
* **per layer** — whether it is microstrip or stripline on this stackup, and the
  trace width that gives 50 Ω, 75 Ω, and 90/100 Ω differential. The differential
  numbers take the gap equal to the width, because one target cannot fix two
  unknowns; move from there once the router has an opinion.

Copper thickness comes from the stackup when the board has one and falls back to
1 oz otherwise — the output says which, so a number resting on an assumption is
visible as one. The formulas are the IPC-2221 and IPC-2141 closed forms: good for
sizing and for catching mistakes, worth about ±10 % on impedance, and not a
substitute for your fab's own stackup calculator.

`eda diff OLD NEW -o DIR` compares two revisions: which footprints moved and how
far, what the board statistics did, and a rendered diff of the plots - red for
what the old revision had and the new one does not, green the other way round, so
a moved part is red where it was and green where it is now. Use it when reviewing
somebody else's change, or against `git worktree add /tmp/base <ref>` for your own.

`eda report TARGET -o DIR` runs the schematic review, the board review, every
render, the BOM and (with `--simulation deck.cir`) a SPICE run, then writes
`report.md`, a self-contained `report.html` and a machine-readable
`report.json`. Use it when you want one artefact to hand back, or at the end of
a work session so the state of the design is visible rather than described.

## How to actually review artwork

1. **`pcb review --text`** — DRC first. Errors are hard stops: shorts,
   clearance violations, unconnected copper, parity mismatches with the
   schematic. The tool refills zones before checking, so pours are evaluated
   the way the fab will see them.
2. **`pcb render` then Read the PNGs** — this is the part no rule catches.
   Start with `contact-sheet.png` for the overview, then go into
   `copper-front`/`copper-back` for routing quality, `front`/`back` for the
   assembled picture, `silk-front` for legibility, and the 3D views for
   mechanical sanity. Say what you see: a rendered image the user never sees
   is worth nothing, so describe it and attach it.
3. **`pcb info`** — cross-check the numbers: board size, layer count, track
   widths in use, drill sizes, net-by-net track length and via count.
4. Judge against the *purpose* of the board: current paths, return paths,
   sensitive analog nets, connector placement, mounting.

## What `pcb review` checks

**From KiCad's own DRC** (`drc.*` — authoritative, the same engine as the GUI):
clearance and creepage, track/via/hole size rules, courtyard overlaps,
silk-over-pad, zone fill problems, **unconnected items** (reported as errors),
and **schematic parity** (net conflicts, missing/extra footprints, field
mismatches).

**Layout heuristics on top of the parsed board:**

| Rule | Default threshold | Meaning |
| --- | --- | --- |
| `track.below_minimum` | 0.15 mm | tracks the fab cannot make |
| `track.thin_power` | 10 mm neck | a contiguous run of power/ground track under 0.4 mm longer than the neck allowance — pad entries and fine-pitch escapes pass, thin trunks fail |
| `via.small_drill` / `via.annular_ring` | 0.3 mm / 0.13 mm | via geometry vs fab capability |
| `board.edge_clearance` | 0.3 mm | copper too close to the outline (measured against the real Edge.Cuts geometry - arcs, circles and cutouts included, not a bounding box) |
| `board.copper_outside_outline` | — | copper past the outline entirely: it would be milled away |
| `layout.decoupling_distance` | 5 mm | nearest decoupling cap to each IC supply pad |
| `layout.no_decoupling` | — | IC supply pad with no capacitor on that net |
| `layout.no_ground_plane` / `layout.unfilled_zone` | — | return path quality |
| `layout.outside_outline` | — | footprints off the board |
| `layout.double_sided_assembly` | — | bottom side parts (assembly cost) |
| `fab.many_drill_sizes` | 6 | drill count drives fab cost |
| `silk.missing_reference` | — | parts without a visible designator |
| `mechanical.no_mounting_holes`, `test.no_testpoints` | — | informational |
| `silk.over_pad` | — | silkscreen printed across a pad: ink on a pad keeps solder off it |
| `silk.text_too_small` | 0.8 mm | below the screen printer's limit it comes back a smudge |
| `layout.pad_collision` | — | pads of two footprints sharing copper - parts placed on top of each other |
| `layout.off_grid_placement` | 0.5 mm | footprint origins off the placement grid |
| `layout.odd_rotation` | 90 deg | parts turned to something other than a multiple of it |
| `layout.decoupling_via` | 1.5 mm | decoupling ground pad to the nearest via: the return loop runs through whatever separates them |
| `route.stub` | — | a track end reaching no pad, via or other track |
| `route.acute_angle` | 90 deg | corners that trap etchant and step the impedance |
| `route.right_angle` | 90 deg | corners that turn a full 90 deg — two 45s cost nothing |
| `silk.missing_board_id` | — | no free silkscreen text: the bare board states neither name nor revision |
| `silk.unlabeled_connector` | — | a connector with no silk text near it saying which pin carries what |
| `layout.pour_single_sided` | — | a two-layer board pouring ground on only one face |
| `route.mixed_track_widths` | 3 widths | a net nobody decided the width of |
| `route.detour` | 2.5x | routed copper against the minimum spanning tree of the net's pads — the scenic tour an autorouter leaves |
| `route.return_path` | 10 mm | on a two-layer board, signal track running over cuts in the other layer's ground fill: the return current detours and the loop grows |

Override any threshold: `--threshold min_track_mm=0.2 --threshold max_decoupling_distance_mm=3`.
The full set: `min_track_mm`, `min_via_drill_mm`, `min_annular_ring_mm`,
`min_edge_clearance_mm`, `max_decoupling_distance_mm`, `max_drill_sizes`,
`min_silk_text_height_mm`, `placement_grid_mm`, `rotation_step_deg`,
`max_decoupling_via_mm`, `min_track_angle_deg`.
Use the fab's real capability, not the defaults, when the fab is known.

Exit code is `2` when there is at least one error.

`eda gate` turns the board review and the schematic review into a single
verdict against a policy, which is what to use when the layout is being
generated rather than drawn: see the `kicad-design-gate` guide.

A rule that fires more than six times is folded into a single finding carrying
the count and the first examples (`details.collapsed`), so one noisy rule cannot
bury the rest of the report. `--collapse N` changes the limit, `--collapse 0`
prints everything.

## Things to check visually (no rule can)

* Return-current path under fast signals; splits and slots in the ground pour.
* Analog/digital partitioning, star grounding, keeping switching nodes small.
* Loop area of the input/output capacitors on a switching regulator.
* Copper pour thermal relief on high-current pads, thermal vias under a pad.
* Silkscreen readable, not under parts, polarity/pin-1 markers present.
* Connector orientation and keep-outs, mounting hole clearance to copper.
* Panelisation/edge rail requirements and the fab's minimum feature sizes.

## Notes

* `--no-cli` parses the board without KiCad (no DRC, no zone refill); the
  fallback `route.unrouted_net` rule then reports nets with pads on several
  footprints and no copper at all.
* `./bin/eda.sh pcb drc <target>` gives KiCad's raw DRC JSON.
* `./bin/eda.sh pcb stats <target>` adds KiCad's own board statistics report.
* Reviewing the board is not a substitute for reviewing the schematic — run the
  `kicad-schematic-review` guide as well; parity only proves they match, not
  that either is right.
* Once the board is clean, the `kicad-fabrication-output` guide covers producing the
  manufacturing package.

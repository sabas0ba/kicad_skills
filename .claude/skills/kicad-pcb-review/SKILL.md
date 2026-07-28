---
name: kicad-pcb-review
description: Read a KiCad board (.kicad_pcb) and review the artwork - DRC, schematic parity, unrouted nets, track widths, vias and drills, edge clearance, decoupling placement, ground pour, silkscreen, placement - and render the layers and 3D views as PNGs for visual inspection. Use when asked to review, check or look at a PCB layout, artwork, routing, stackup or fabrication readiness.
---

# KiCad PCB / artwork review

Reads `.kicad_pcb` files, runs KiCad's DRC and renders the artwork so it can be
looked at. Runs in the container (`eda-environment` skill).

## The commands

```bash
./bin/eda.sh pcb info   hardware/                      # stackup, footprints, nets, routing stats
./bin/eda.sh pcb review hardware/ --text               # DRC + layout heuristics
./bin/eda.sh pcb render hardware/ -o /tmp/art --dpi 300
./bin/eda.sh pcb glb    hardware/ -o /tmp/board.glb    # 3D model for a browser
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
| `track.thin_power` | 0.4 mm | power/ground tracks that may not carry the current |
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

Override any threshold: `--threshold min_track_mm=0.2 --threshold max_decoupling_distance_mm=3`.
Use the fab's real capability, not the defaults, when the fab is known.

Exit code is `2` when there is at least one error.

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
  `kicad-schematic-review` skill as well; parity only proves they match, not
  that either is right.
* Once the board is clean, the `kicad-fabrication-output` skill produces the
  manufacturing package.

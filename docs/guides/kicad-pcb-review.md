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
./bin/eda.sh pcb review hardware/ --map findings.png   # the same findings, drawn on the board
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

### Background colour

`--background white|black|transparent` (default `white`) sets what the plots and
the contact sheet are drawn on — black for reading on a dark screen, transparent
for dropping a layer into a document or stacking two of them:

```bash
./bin/eda.sh pcb render hardware/ -o /tmp/art --background black
./bin/eda.sh pcb render hardware/ -o /tmp/art --background transparent --no-3d
```

KiCad plots onto an unpainted PDF page, so the colour is chosen while the page
is rasterised rather than keyed out afterwards — anti-aliased edges stay clean
instead of fringing white, and `transparent` writes RGBA PNGs with the board
fully opaque and only the backdrop see-through. The intermediate PDFs are
KiCad's own output and are unaffected.

Two things to know before switching:

* **The 3D views have no white to replace** — they are drawn on the 3D viewer's
  own themed background. At `white` they keep it (that is the default output);
  `black` and `transparent` re-render them with an empty background instead.
* **KiCad blends layer transparency against white paper when it plots**, so a
  layer the theme draws semi-transparent comes out pale on a dark background.
  The geometry is right; only the shade is off.

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
visible as one. Current capacity is IPC-2221. Microstrip impedance is
Hammerstad–Jensen with its thickness correction, good to a couple of percent;
stripline is the IPC-2141 fit, worth about ±10 % inside its band. Neither model
knows your laminate's real permittivity, so the last word stays with the fab.

**`--solve` re-measures those widths with a 2D field solver.** The closed form
proposes a width; the solver takes the cross-section as a grid — trace, laminate,
air, planes — solves the electrostatic field on it at two resolutions, and
extrapolates to zero cell size. It answers with no fitted validity band, which
is what makes it worth the few seconds per layer it costs:

```console
$ ./bin/eda.sh pcb electrical hardware/ --solve
...
  "impedance": [{
    "layer": "F.Cu", "kind": "microstrip",
    "width_50r_mm": 2.797,               the width Hammerstad-Jensen proposes
    "width_50r_solved_ohm": 50.67,       what that width solves to as a field
    "width_100r_diff_mm": 2.2602,        the differential pair, gap = width
    "width_100r_diff_solved_ohm": 100.72,  solved as two coupled traces
    ...
```

When the two columns agree, the geometry is comfortably inside the models and
either number can be trusted. When they drift apart, believe the solve — it is
the same physics your fab's calculator runs — and treat the disagreement itself
as the finding: the geometry has left the band the fit was made in. The
differential figure is the one that earns the flag most often, because the
closed form treats the gap as an exponential correction factor while the solver
treats it as copper.

The solver is importable on its own for geometries the table does not pose —
`eda_toolkit.kicad.field2d` has `microstrip`, `differential_microstrip` and
`stripline`, each returning the impedance plus a `meta` block that shows the
two raw grid answers and the snap correction, so an answer can always be argued
with. It is quasi-static: no dispersion, loss or surface roughness, so above a
few GHz on thick laminates the fab's full-wave numbers pull ahead.

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
   schematic. A board whose zones are already filled is checked **as it
   stands** — that fill is what goes to the fab and what the plots draw, and
   refilling first would report on a board nobody has. Only an unfilled zone
   is refilled before checking, because otherwise every pad on that net reads
   as unconnected; `layout.unfilled_zone` is the finding that says so.
2. **`pcb review --map findings.png` then Read it.** The same findings, drawn
   where they are: a numbered mark per located finding over the copper, keyed
   to a legend in the JSON. A count in a list is a statistic and gets waived;
   the same marks clustered on one fan, or scattered over the whole board, is
   a cause. This is how you check your own waiver — "those corners are the
   escape fan" is a claim the picture either supports or refutes.
3. **`pcb render` then Read the PNGs** — this is the part no rule catches.
   Start with `contact-sheet.png` for the overview, then go into
   `copper-front`/`copper-back` for routing quality, `front`/`back` for the
   assembled picture, `silk-front` for legibility, and the 3D views for
   mechanical sanity. Say what you see: a rendered image the user never sees
   is worth nothing, so describe it and attach it.
4. **`pcb info`** — cross-check the numbers: board size, layer count, track
   widths in use, drill sizes, net-by-net track length and via count.
5. Judge against the *purpose* of the board: current paths, return paths,
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
| `via.in_pad` | 0 mm gap | a via whose copper reaches a surface-mount land, its own net's included: solder wicks down an open barrel and the joint above it starves, and nothing on the assembled board tells that apart from a cold joint. Via-in-pad is a filled-and-capped *process*, not a drawing. The exposed thermal pad under a package is exempt — the via array in one is what the datasheet asks for, and nothing a signal reaches is 4 mm² |
| `board.edge_clearance` | 0.3 mm | copper too close to the outline (measured against the real Edge.Cuts geometry - arcs, circles and cutouts included, not a bounding box) |
| `board.copper_outside_outline` | — | copper past the outline entirely: it would be milled away |
| `layout.decoupling_distance` | 5 mm | nearest decoupling cap to each IC supply pad |
| `layout.no_decoupling` | — | IC supply pad with no capacitor on that net |
| `layout.no_ground_plane` / `layout.unfilled_zone` | — | return path quality |
| `layout.outside_outline` | — | footprints off the board |
| `layout.zone_outside_outline` | — | a zone — a pour or a keep-out — drawn wholly off the board. A footprint may carry zones of its own and KiCad stores *those* in board coordinates while everything else in a footprint is stored relative to it, so a placer that moves the pads and forgets the zone leaves the keep-out at the origin. Nothing else complains: the keep-out keeps nothing out, DRC is silent because an empty region violates no rule, and the only visible sign is that every plot comes out at half scale in one corner |
| `layout.double_sided_assembly` | — | bottom side parts (assembly cost) |
| `fab.no_fiducials` | 0.8 mm pitch | a board carrying parts at or below that pitch with no fiducial for the assembly machine to align to. It aligns to *copper*, not to the drawing: two or three dots in bare mask windows, and everything else measured from them. Without them it has the routed outline, cut to a tolerance ten times looser than the placement being asked for. Context, not a fault — plenty of boards are built one at a time with tweezers |
| `fab.many_drill_sizes` | 6 | drill count drives fab cost |
| `silk.missing_reference` | — | parts without a visible designator |
| `mechanical.no_mounting_holes`, `test.no_testpoints` | — | informational |
| `mechanical.fastener_clearance` | 0.5 mm | what goes through an M3 hole is a pan head on a washer — seven millimetres of steel lying flat on the board, turned by a driver that wants more. The footprint's courtyard is the drill plus a whisker and says none of that, so a placer that only avoids courtyard overlap puts the screw head on a capacitor and the board does not bolt down until somebody files something. Measured against every part body and against the board edge, where a washer that overhangs does not sit flat |
| `mechanical.connector_access` | 2 mm | a hole inside a connector's mating space. The shell, the wires leaving a screw terminal and the fingers that fit both live above the courtyard, so a screw tucked against a connector can only be driven before the cable goes on — which on a board that gets serviced is never |
| `mechanical.fastener_copper` | 7 mm head | bare copper of another net under the screw head, where an uninsulated washer would sit on it. A grounded hole's own net is exempt: that is the bond, not an accident |
| `silk.off_board` | — | a silkscreen string whose middle falls outside the outline. KiCad's own test measures ink against the *edge*, so it reports a string that straddles Edge.Cuts and says nothing at all about one that clears it entirely — which is the worse of the two: ink past the outline is not trimmed, it is never printed, because the panel is routed at the line and the designator leaves with the offcut |
| `silk.over_pad` | — | silkscreen printed across a pad: ink on a pad keeps solder off it |
| `silk.text_over_text` | — | two silkscreen strings on the same side printed through each other. The schematic has `readability.text_over_text` for this and the board had nothing, though the board is the harder case: a sheet can be zoomed and a bare board cannot, and the legend beside a connector is the only thing telling an assembler which pin is which. Fires on 6 of KiCad's 16 parsable demo boards, 173 times — real ink on ink, measured from the font size the file states rather than from a character count, which is why it is a warning and not an info |
| `silk.text_too_small` | 0.8 mm | below the screen printer's limit it comes back a smudge |
| `layout.pad_collision` | — | pads of two footprints sharing copper - parts placed on top of each other |
| `layout.off_grid_placement` | 0.5 mm | footprint origins off the placement grid |
| `layout.odd_rotation` | 90 deg | parts turned to something other than a multiple of it |
| `layout.decoupling_via` | 1.5 mm | decoupling ground pad to the nearest via: the return loop runs through whatever separates them |
| `layout.solid_pad_connection` | — | a filled zone that floods its own pads with solid copper instead of relieving them thermally. Every drilled pad counts, because an iron cannot heat a plane: it pours its heat into a hundred square millimetres of copper and the joint never wets. A *surface* pad counts from 4 mm² and 2 mm across — a chip land below that reflows with the board and is better off solid, while a regulator's tab tied straight into the pour reaches solder temperature after the part's own leads do, and the part lifts on the leads that got there first. A pad with a via array in it is exempt: there the copper is the heat path and somebody chose it, which is what a QFN's exposed pad is for |
| `route.stub` | — | a track end reaching no pad, via or other track |
| `route.acute_angle` | 90 deg | corners that trap etchant and step the impedance. Two branches leaving one *pad* are exempt — the pad's own copper fills the wedge — except at nought degrees, which is one run drawn twice and no pad excuses. The exemption is the pad's connection point, not a disc around it: measured by radius it covered a 0805's whole 0.47 mm and hid every ordinary corner a chamfered pad entry leaves inside that. Re-cutting it took the demo corpus from 8 boards / 127 corners to 9 / 205 |
| `route.hairpin` | 100 deg / 1.2 mm | a run that turns back on itself over two adjacent corners: a 90 and a 45 with a tenth of a millimetre between them passes the angle rule corner by corner and still reads as one folded bend. Signed turns, so a staircase's alternating 45s cancel; arms shorter than 0.8 mm are a clearance artefact skirting a via, not a legible fold; a fold whose middle sits inside its own pad is the escape fan's deliberate micro-hook and stays |
| `route.right_angle` | 90 deg | corners that turn a full 90 deg — two 45s cost nothing |
| `route.odd_angle` | — | corners off the 45-degree grid: a 20 or 70 degree bend reads as a slip of the mouse |
| `route.width_step` | 3 mm | a track changing width away from any pad, where the narrow side is not that pad's own neck either — the narrow part already set the current. A fine-pitch escape gets the same `power_neck_mm` budget `track.thin_power` gives it |
| `route.under_package` | — | another net's track threaded under a package body, unprobeable and with no plane under it |
| `layout.connector_not_at_edge` | 6 mm | a connector the cable has to cross the board to reach |
| `silk.unlabeled_indicator` | — | an LED or switch with no silk saying what it means |
| `silk.missing_board_id` | — | no free silkscreen text: the bare board states neither name nor revision |
| `silk.unlabeled_connector` | — | a connector with no silk text near it saying which pin carries what |
| `layout.pour_single_sided` | — | a two-layer board pouring ground on only one face |
| `layout.pour_coverage` | 80 % | how much of its own outline a ground pour actually filled — context, since it is a density and a smaller board scores lower |
| `layout.pour_fragmented` | 70 % | a ground pour whose largest island holds less than this share of its copper: the plane is pieces |
| `route.mixed_track_widths` | 3 widths | a net nobody decided the width of |
| `route.detour` | 2.5x | routed copper against the minimum spanning tree of the net's pads — the scenic tour an autorouter leaves |
| `route.self_crossing` | — | a net whose own copper crosses itself on one layer. The same potential, so DRC has nothing to say — but two branches of one net crossing means the copper carries a redundant loop, and a person never draws one: the plot reads as tracks driven through each other. KiCad's demo boards carry at most one to three, at dense escapes |
| `route.wander` | 2.0x | one run of copper — pad or junction at each end — against the shortest way between those two ends that clears the packages in between. `route.detour` weighs a whole net and a net hides things; this is the track that leaves its pad, goes three sides of a rectangle and arrives 4 mm away |
| `route.return_path` | 10 mm | on a two-layer board, signal track running over cuts in the other layer's ground fill: the return current detours and the loop grows |

Override any threshold: `--threshold min_track_mm=0.2 --threshold max_decoupling_distance_mm=3`.
The full set: `min_track_mm`, `min_via_drill_mm`, `min_annular_ring_mm`,
`min_edge_clearance_mm`, `max_decoupling_distance_mm`, `max_drill_sizes`,
`min_silk_text_height_mm`, `placement_grid_mm`, `rotation_step_deg`,
`max_decoupling_via_mm`, `min_track_angle_deg`, `min_pour_coverage`,
`min_pour_island_fraction`, `max_connector_edge_mm`, `width_step_free_mm`,
`wander_ratio`.
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

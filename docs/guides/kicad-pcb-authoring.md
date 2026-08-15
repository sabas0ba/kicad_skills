---
name: kicad-pcb-authoring
description: Lay out a KiCad board with its electrical characteristics intact - required checks and the layout method for an agent generating or editing .kicad_pcb files. Anchoring decoupling ground vias against their pads, return-path and router cost trade-offs on two-layer boards, static copper as routing obstacles, escape geometry budgeting, and the DRC/review/gate loop with quantified waivers. Use when generating, routing or placing a board, not merely reviewing one.
---

# KiCad PCB authoring

> One of the [kicad_skills](https://github.com/sabas0ba/kicad_skills) usage guides for the
> `eda` CLI — [all of them](README.md). Plain Markdown: read it directly, or hand it to
> whatever assistant you use.

How to *lay out* a board, for an agent that writes `.kicad_pcb` files. The
[board review guide](kicad-pcb-review.md) covers judging one; this guide is
the authoring direction, distilled from laying out five generated boards and
fixing what their physics got wrong. Unlike the schematic side, almost every
check here already existed as a rule — the improvements were in *how to
satisfy them*, which is what this records.

## The required checks

```bash
./bin/eda.sh pcb drc     hardware/                # KiCad's own DRC, zone-refilled
./bin/eda.sh pcb review  hardware/ --text         # the toolkit's rules
./bin/eda.sh gate        hardware/ --policy ai-generated --text
```

Run DRC through the toolkit, not raw `kicad-cli`: the wrapper refills zones
first and uses consistent severities, and a raw run on the other KiCad
version will report zone-fill artefacts that are not real. Where both KiCad
images exist, run the wrapper under each — zone fill algorithms differ
between releases.

Then render both sides and look at them:

```bash
./bin/eda.sh pcb render hardware/ -o /tmp/pcb --dpi 300 --views front back --no-3d
```

The rules see loop areas and track widths; only the plot shows a board that
*reads* as machine work.

## Decoupling: the loop is the deliverable

`layout.decoupling_distance` and `layout.decoupling_via` measure the two
halves of one physical quantity — the loop inductance between an IC's supply
pin, its capacitor, and the plane.

* **Anchor the ground via against the capacitor's own pad**, on the far side
  from the supply pad, with a short stub (about a millimetre) as the whole
  top-side path. Declare it as copper you place, not a target you hope the
  router reaches: a via at the end of a routed track puts that track's
  inductance inside the loop, which is exactly what the rule measures.
  This retired seven findings on the densest example board.
* **The distance half is package geometry.** A fine-pitch part spends the
  budget escaping the package before any capacitor can be placed; on two
  layers with parts on one side, that is a fact, not a mistake. The fix that
  exists (capacitors on the back, under the pins) needs the layer count the
  board chose not to have — waive it *with that reason*, per project.

## Two layers and the return path

On a two-layer board the back is the ground plane, and every track routed on
it cuts a channel through the plane. Any top-side signal crossing the channel
has its return current detoured around it: the loop grows by the detour
(`route.return_path`).

* **Prefer the top layer**; drop to the plane layer only to cross, and get
  back up. The router's `back_cost` prices this, and the examples price a
  signal's crossing at roughly forty times the front-side detour that avoids
  it — a search will then only cross where the board has left it no front
  side at all. Ground is not charged: its own copper is the plane.
* **Choose which net crosses, and say so.** When a supply pin sits in the
  middle of a row the signals leave from either side of, something has to
  cross, and the choice is between one rail and every signal. Take the rail:
  it is low impedance, the plane it crosses is its own return, and the
  signals cross the cut it leaves at right angles — a track width of return
  path each rather than a detour. Put it in the file as a stated link with
  its two vias, not as something the search stumbled into. The motor driver
  example does exactly this, and it is the difference between a clean board
  and 190 mm of copper for a 40 mm net.
* **A detour that big is a floorplan problem, not a router problem.** When
  `route.detour` reports 4x, look at what is walling the corridor off before
  touching the router: on the motor board it was the bulk-cap-to-charge-pump
  run standing between the package and the header.
* **What remains is a costed decision.** I2S and SPI at single-digit
  megahertz over millimetre gaps is acceptable and waivable, with the
  frequency and the gap in the waiver text; the same crossing under a clock
  ten times faster is a re-layout.

## Static copper is a wall

Every via, track and pad you *declare* is an obstacle the router cannot move.
The failures this caused, each costing a rip-up spiral or an unroutable net:

* A declared via placed one grid cell from a pad plugged the only corridor a
  neighbouring ground stub could use. Before anchoring copper next to a dense
  region, check what has to route *through* that region.
* Obstacle expansion is conservative at corners: a pocket that looks walkable
  can admit no via anywhere in it. If a short hop will not route, the fix is
  almost always moving a part half a grid step, not fighting the router.
* Escape fans are stated, not searched. The escape's lead length, column and
  pitch decide how much of the decoupling budget survives - budget them
  before placing anything else around a fine-pitch part.

## Corners, clearance, and sensitive paths

* **Bend at 45 degrees, not 90** (`route.right_angle`): two 45s cost nothing,
  and the square corner is a small impedance discontinuity and an etch/nick
  risk. Anything under 90 is worse (`route.acute_angle`) — and anything off
  the 45 grid entirely (`route.odd_angle`) reads as a slip of the mouse. A
  fine-pitch fan does not need shallow angles either: stagger the 45 bends
  so no two neighbours turn abreast and the escape holds the grid.
* **Branch as a Y, not a T.** Where one track splits, bring the branches in
  at 45 so the join is a fork, not a crossroads: a square tee is the same
  etch nick as a square corner, twice.
* **One width per run.** Widening a track after it has already run narrow
  for centimetres buys nothing — the narrow length sets the current. Leave
  a pin field as wide as the row allows and widen at the field's edge, in
  one place, where the constraint visibly ends.
* **Do not crowd clearances you do not have to.** Minimum clearance is for
  where the board leaves no choice; open board routed at minimum is asking
  the fab to be perfect for no reason. The router's crowding cost exists for
  exactly this — leave it on.
* **A regulator's feedback path is a measurement.** Route it as short as the
  geometry allows and away from the switch node; every millimetre parallel
  to SW couples switching noise straight into the error amplifier. Sense at
  the output capacitor where regulation is wanted, but get there directly.

## The board explains itself in silk

* **Name, revision and author on the board** (`silk.missing_board_id`) — ten
  bare boards on a bench are identical without it, and the author line says
  whose design the bench is looking at.
* **Connector pins say what they carry** (`silk.unlabeled_connector`):
  net names beside the pins, outside the footprint's courtyard. That is the
  reverse-connection insurance, and it costs silkscreen — real silkscreen,
  in the floorplan, before the parts go down. On the Pico carrier the legend
  for twenty pins is what decides where the decoupling capacitors can sit.
* **Measure the area a legend takes from its neighbours, not the collisions.**
  "Half a legend across a module's pads" and "a tenth of a millimetre into a
  chip capacitor's courtyard" are both one collision; only one is a defect.
  Pick the side of the pad row with the smaller intrusion and outboard wins
  on its own wherever there is an edge to face.
* **Silk over a pad is a pad that will not wet** (`silk.over_pad`): the mask
  opens there and the ink comes off in fabrication. That applies to the board
  id, to the pin legend, and to a designator left where the library drew it —
  on a module with pads down both sides and along the bottom, the library's
  spot is the middle of a pad. Measure a footprint that draws no courtyard by
  its pads: treating a missing courtyard as "takes up no board" is how a
  legend ends up printed across one.
* **Indicators say what they indicate**: "5V OK" beside the power LED, the
  function beside every switch. A lit LED nobody can interpret is decoration.

## Connectors, pours and returns

* **Power and interface connectors live on the board edge, facing out** —
  the cable leaves the board, not crosses it, and a screw terminal's wire
  entry points off the edge, not along it. Pull debug and GPIO headers to
  the edge too when the routing allows; on a board whose edge corridors are
  the escape fan's, an interior debug header is the honest trade, stated.
* **A thermal tab gets its via ring beside the pad, not on it.** Vias in a
  hand-soldered tab drink the solder at reflow; a ring just off the pad ties
  the tab into both planes and doubles as the return path. (A QFN's exposed
  pad is the exception — via-in-pad there is the datasheet's own ask.)
* **Keep through-routes out from under digital packages.** The strip between
  a package's pad rows has no plane over it and the die right above it;
  close it to everything but the package's own pad entries and route around
  or on the far face.
* **A pour is only a plane while it is mostly copper** (`layout.pour_fragmented`
  faults it, `layout.pour_coverage` reports it — coverage is a density, so a
  board made smaller scores lower on it while getting better, which is why it
  informs rather than blocks). Every track crossing it takes a clearance channel
  with it, and two tracks running a couple of millimetres apart take the strip
  between them as well — it comes out thinner than the filler's sliver limit
  and disappears, so a loose bundle costs the plane far more than its own
  width. Bundle parallel runs tight, keep them short, and send them along the
  edge: a track through the middle bisects the plane, the same track along the
  edge only trims it.
* **Stitch the middle, not just the rim.** In the dense part of a board the
  channels shred the pour into pieces, and a piece that touches no ground pad
  of its own is not poured copper at all — the filler drops it as an orphan,
  which is where the blank areas on a plot come from. A ground via every few
  millimetres gives each piece something to hold onto, and is the return path
  the pour was there to provide.
* **Pour ground on both faces and stitch them** (`layout.pour_single_sided`):
  the spare face's copper is free ground impedance, but only if a ring of
  stitching vias ties it to the plane — an unstitched island or edge strip
  is an antenna, not a ground (KiCad's `isolated_copper` catches the worst
  of it).
* **A high-current return is drawn, not assumed**: give the loop an explicit
  ground path at the same width as its forward path, alongside it, and let
  the pour be reinforcement rather than the only way home.

## Width, angles, and what to waive

* **Power tracks get power widths** (`track.thin_power`): the rule measures
  the longest contiguous narrow run, so pad-entry necks pass. Where a whole
  distribution must stay narrow because nothing wider fits, the waiver
  argues in numbers — current, width, temperature rise — not in adjectives.
* **A rail leaves its package at the width it keeps.** Escaping at signal
  width and widening two millimetres later is a step nobody chose
  (`route.width_step`), and widening the far half instead only moves the
  complaint to the thin one (`track.thin_power`). Give the supply and ground
  pins of a fine-pitch escape their own width in the fan: a 0.65 mm row holds
  0.4 mm, a 0.95 mm row holds 0.5 mm, and the row's pitch — not the run's
  current — is what sets it.
* **`route.detour` and `route.acute_angle`** flag machine-looking routing.
  Corners that come from a stated escape fan meeting the 45° grid are the
  fan's geometry and waivable as such; a track three times its spanning-tree
  length across open board is a routing failure, not a style choice.
* **Every waiver names its reason** in the project's `gate.toml`, stated so a
  reviewer can disagree with it. A finding is fixed, checked, or answered —
  never silently absent. That is the shape of the whole mechanism.
* **A waiver is not a place to put a review comment.** Everything a reviewer
  raised on the worked examples is fixed in the geometry, not argued away:
  the four waivers that remain are about what a two-layer board with parts on
  one side physically cannot do, and each one names the four-layer answer it
  is standing in for.

## Where the rules live

`eda gate --list-rules` prints all of them. The ones this guide exists to
satisfy: `layout.decoupling_distance`, `layout.decoupling_via`,
`route.return_path`, `route.detour`, `route.acute_angle`,
`track.thin_power`, plus KiCad's own DRC. What cannot be a rule — where to
spend the escape budget, how hard to price the plane layer, when a crossing
is cheap enough to keep — is this guide, and the rendered board.

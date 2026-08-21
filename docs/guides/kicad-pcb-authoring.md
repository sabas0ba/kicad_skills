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
* **Order is the other half of it.** Routing one net at a time means an early
  net takes the lane a later one needed, and the later one then goes round —
  the op-amp's feedback wrap had thirteen millimetres to cover and took
  fifty-six of them, because everything nearer was already spoken for. Two
  things fix most of it. Route **shortest first**: a thirteen millimetre
  connection has few ways to be made and a forty millimetre one has many, so
  the short ones should choose while there is still room. And when a track
  does come out long, **rip it up and route it first** — the same loop that
  handles a net with no room at all handles a net with no *sensible* room,
  and a track that still tours from first pick has nowhere better to be.
* **Price a wrap against going round, not through.** A run from one side of a
  package to the other cannot take the straight line, because the straight
  line is through the package: a SOT-23-5's feedback wrap is three
  millimetres of separation and eighteen of copper, and that is correct.
  Measure it against the shortest path that clears the packages — which is
  what `route.wander` does — or a re-ordering loop spends its afternoon
  chasing wraps that were right all along.
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
* **A strip of board that is routable and never right is a keepout.** A
  connector at an edge leaves a few millimetres behind it; a search that runs
  out of front-side room will go round the back of the connector and come at
  its pads from the side nothing arrives from, crossing the plane to do it.
  Saying the strip is not for routing is how a layout states which side a
  connector is approached from. It is a floorplan statement, not a fix: if
  every route needs that strip, the floorplan is what has to change.

## Corners, clearance, and sensitive paths

* **Bend at 45 degrees, not 90** (`route.right_angle`): two 45s cost nothing,
  and the square corner is a small impedance discontinuity and an etch/nick
  risk. Anything under 90 is worse (`route.acute_angle`) — and anything off
  the 45 grid entirely (`route.odd_angle`) reads as a slip of the mouse. A
  fine-pitch fan does not need shallow angles either: stagger the 45 bends
  so no two neighbours turn abreast and the escape holds the grid.
* **A reversal is two corners a stride apart, not one fold.** A net that has
  to double back — an escape that leaves one way, a destination the other —
  turns 135 degrees somewhere, and folding the whole turn into half a
  millimetre reads as a hairpin however legal each corner is alone
  (`route.hairpin`). Put a track-width-or-three of straight between the two
  corners and the same turn reads as a deliberate wrap.
* **Branch as a Y, not a T.** Where one track splits, bring the branches in
  at 45 so the join is a fork, not a crossroads: a square tee is the same
  etch nick as a square corner, twice.
* **Junctions belong on the trunk, not on a pad.** A pad used as a
  three-way junction is legal and common, but a net whose *every* junction
  sits on a pad is a net drawn pad-to-pad: three diagonals converging on one
  0603 is the tell. Tap the nearest point of copper the net already has —
  the branch gets shorter and the pad stops being a crossroads. (The example
  generator does this itself: a link whose far end is already reachable
  through laid copper is allowed to finish on that copper instead of
  funnelling into the named pad.)
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
* **Stitch the middle, not just the rim — and stitch every piece.** In the
  dense part of a board the channels shred the pour into pieces, and a piece
  that touches no ground pad of its own is not poured copper at all — the
  filler drops it as an orphan, which is where the blank areas on a plot come
  from. A ground via every few millimetres gives each piece something to hold
  onto, and each surviving piece should hold at least one of its own: a strip
  whose only tie is somewhere far away reads as fenced-off copper even when
  it is not.
* **The board's outermost feature should be ground.** Pour to within about a
  millimetre of the outline, so a trace that has to run near the edge keeps
  shell copper outside it — a signal as the outermost copper has no return
  beside it and no shield either. And keep the pour out of sharp corners: the
  wedge it fills where two tracks converge at 45 degrees tapers to a point,
  which is an acid trap on the board and a spike to the eye. Retract dead-end
  strips to where the plane is wide; a narrow channel that connects two wide
  regions is worth keeping, a narrow tongue that dead-ends is not.
* **Pour ground on both faces and stitch them** (`layout.pour_single_sided`):
  the spare face's copper is free ground impedance, but only if a ring of
  stitching vias ties it to the plane — an unstitched island or edge strip
  is an antenna, not a ground (KiCad's `isolated_copper` catches the worst
  of it).
* **A high-current return is drawn, not assumed**: give the loop an explicit
  ground path at the same width as its forward path, alongside it, and let
  the pour be reinforcement rather than the only way home.

## The numbers behind the look

A reviewer can tell a hand-routed board from an autorouted one across the
room, and DRC, ERC and every list-shaped check pass both. The tell is
statistical, and
[`tools/board_signature.py`](https://github.com/sabas0ba/kicad_skills/blob/main/tools/board_signature.py)
measures it, so "looks autorouted" becomes a comparison instead of an
opinion. Run it over KiCad's own demo projects and your board side by side;
the corpus baseline (16 parsable demo boards) for hand-routed two-layer work:

| measure | human range | what a miss looks like |
| --- | --- | --- |
| second-layer share of copper | 10–47% | everything on one face: the plane was priced as untouchable, so the front grew wandering channels |
| median segment length | 1.8–3.5 mm | 0.75 mm: the router's grid cell became the drawing's rhythm |
| corners per dm of track | 9–25 | 38: the same stutter counted the other way |
| corner angles | 91–98% at 45° | staircases and odd angles are machine artefacts |
| vias per dm of track | 0.3–16 | a uniform stitching carpet reads as a printed pattern, not a decision |

Three habits of the hand-routed boards are worth copying outright — all
three are visible in one glance at the `interf_u` demo:

* **A layer has a direction.** Front vertical, back horizontal (or the
  reverse): nearly every track on `interf_u` obeys it, through-hole pads act
  as free layer changes, and the two faces stay legible separately. A search
  that prices the back layer as merely *expensive* never learns this — it
  uses the back only in despair, one desperate hop at a time.
* **A bus travels as a bundle.** The four lines of a port run in one
  corridor, one pitch apart, turning together. Route them one at a time with
  no knowledge of each other and the same four nets scatter across four
  corridors. The generator's router now discounts cells beside a
  sibling's path (nets sharing a name prefix — `I2S_*`, `SPI_*` — are
  siblings), so the bundle look wins every tie without ever buying a detour.
* **A stroke is long, with one 45° jog.** A person covers an offset with two
  segments: the straight along the dominant direction and one diagonal.
  The generator redraws every wiggly stretch that way when the dogleg is
  clear (`_doglegged`), which is what moved the op-amp board's median
  segment from 0.75 mm to 1.5 mm and its corner rate from 38 to 23 per dm —
  into the human range — without moving a single endpoint.

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
* **Price the plane side, do not forbid it.** A signal on the ground layer
  cuts the plane under its own return current, so it costs more than front
  copper — but if it costs *forty times* more per millimetre, one millimetre
  of crossing buys a forty millimetre tour, and the router will take it every
  time. The boards grew supply runs at four times the straight line, all of
  them on the front, all of them legal — `route.wander` is the rule that
  caught it. Below thirty the trade reverses: the short back-layer hops the
  router takes instead cut the plane under the same net's own front copper,
  and `route.return_path` picks that up. Thirty is where neither fires, and
  finding it took a sweep, not an argument.
* **A footprint's own zones do not move with it.** Everything else inside a
  footprint — pads, graphics, text — is stored relative to the part and KiCad
  places it for you. A `zone` is not: KiCad stores a footprint zone in *board*
  coordinates, so a library entry drawn at the origin stays at the origin
  however the part is placed. The Raspberry Pi Pico module carries two pad
  keep-outs and for four rounds they sat at (0, −6), off the board, keeping
  nothing out. DRC is silent — an empty region violates nothing — and the only
  visible sign was the plot: "fit to page" fits the bounding *box*, so every
  view of that board came out at half scale in one corner.
  `layout.zone_outside_outline` reports it.
* **On the silkscreen, the anchored string wins and the free one moves.** A
  connector legend names one pin of one connector and has to sit against it; a
  designator can go anywhere legible. So place the legends first and let the
  designators get out of their way — the same order the schematic side uses
  for a label and a field. And weigh a pad far above a courtyard when choosing
  where a string goes: a legend a little close to a part is still readable,
  and ink on a pad is a pad that will not wet.
* **Never draw one run on top of another.** Two runs of a net that meet at a
  point and leave it along the same line are one run drawn twice: the shorter
  carries nothing the longer does not, and on the plot it reads as a track
  that stops in mid air. `route.acute_angle` calls it a corner of nought
  degrees, which is what it is — on the FPGA board one was nine millimetres of
  track laid back along itself. Trim the duplicate *and* pull the other run
  back to where it ended, or the second one is left hanging over the gap.
* **Route a feedback wrap pad to pad, not column to column.** An opamp's
  output and inverting pins sit on opposite sides of the package; asked for
  between the two escape columns, the wrap leaves the output heading away
  from its partner, reaches the column, and comes back past its own package —
  twenty-three millimetres for a pin pair three millimetres apart. Asked for
  between the pads, it goes round the package, which is what a person draws.
  A pin whose only connection is that wrap does not belong in the fan at all.
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
`route.return_path`, `route.detour`, `route.wander`, `route.acute_angle`,
`track.thin_power`, plus KiCad's own DRC. What cannot be a rule — where to
spend the escape budget, how hard to price the plane layer, when a crossing
is cheap enough to keep — is this guide, and the rendered board.

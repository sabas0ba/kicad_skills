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
  back up. The router's `back_cost` prices this.
* **Do not push the price too high.** Raising it from 0.4 to 0.6 on the
  densest example jammed every escape corridor onto the top layer until a
  ground stub two grid cells long became unroutable. The trade is real:
  return-path hygiene against routability, and on a full board the middle
  of the range wins.
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

## Width, angles, and what to waive

* **Power tracks get power widths** (`track.thin_power`): the rule measures
  the longest contiguous narrow run, so pad-entry necks pass. Where a whole
  distribution must stay narrow because nothing wider fits, the waiver
  argues in numbers — current, width, temperature rise — not in adjectives.
* **`route.detour` and `route.acute_angle`** flag machine-looking routing.
  Corners that come from a stated escape fan meeting the 45° grid are the
  fan's geometry and waivable as such; a track three times its spanning-tree
  length across open board is a routing failure, not a style choice.
* **Every waiver names its reason** in the project's `gate.toml`, stated so a
  reviewer can disagree with it. A finding is fixed, checked, or answered —
  never silently absent. That is the shape of the whole mechanism.

## Where the rules live

`eda gate --list-rules` prints all of them. The ones this guide exists to
satisfy: `layout.decoupling_distance`, `layout.decoupling_via`,
`route.return_path`, `route.detour`, `route.acute_angle`,
`track.thin_power`, plus KiCad's own DRC. What cannot be a rule — where to
spend the escape budget, how hard to price the plane layer, when a crossing
is cheap enough to keep — is this guide, and the rendered board.

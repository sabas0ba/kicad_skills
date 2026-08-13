---
name: kicad-schematic-authoring
description: Draw a KiCad schematic a human can read - required checks and the drawing method for an agent generating or editing .kicad_sch files. Wire routing as trees with junction dots, splitting wires at tees, orienting connectors toward their signals, placing notes and power flags clear of the circuit and the sheet furniture, and the ERC/review/gate loop to run on every sheet. Use when generating, writing or laying out a schematic, not merely reviewing one.
---

# KiCad schematic authoring

> One of the [kicad_skills](https://github.com/sabas0ba/kicad_skills) usage guides for the
> `eda` CLI — [all of them](README.md). Plain Markdown: read it directly, or hand it to
> whatever assistant you use.

How to *draw* a schematic, for an agent that writes `.kicad_sch` files. The
[schematic review guide](kicad-schematic-review.md) covers reading and judging
one; this guide is the other direction, distilled from making five generated
designs readable and watching exactly where they failed. The checks named here
are enforced by rules, so the loop is closed: draw, review, fix what fires.

## The required checks

Run all three on every sheet you produce, and treat the first two as gates:

```bash
./bin/eda.sh sch erc     hardware/                 # KiCad's own ERC: zero errors
./bin/eda.sh sch review  hardware/ --text          # the toolkit's rules
./bin/eda.sh gate        hardware/ --policy ai-generated --text
```

**ERC must pass on every KiCad version you claim to support**, not just the
one you develop against. KiCad 9 and KiCad 10 build connectivity differently:
a wire that runs *through* a junction instead of being broken at it connects
on both sides in KiCad 10 and on one side in KiCad 9 — the netlist silently
differs between versions, and only ERC on the older version shows it. If the
container images for both versions exist, run ERC in each.

Then render the sheet and look at it:

```bash
./bin/eda.sh sch render hardware/ -o /tmp/sch --dpi 150
```

Half of what makes a sheet unreadable changes nothing in the netlist —
notes printed over the regulator, a connector parked on the title block —
and is only visible in the plot. Several of those are now rules
(`readability.margin_intrusion`, `readability.text_over_symbol`), but the
render is still the only check that sees everything.

## Wires, not labels

The most recognisable mark of a generated sheet is every connection drawn as
a stub and a net label — a valid netlist and an unreadable drawing.
`readability.label_only` measures it. The method that fixed it:

* **Draw each signal net as a tree of wire runs** — straight, L- and Z-shaped,
  on the 1.27 mm grid, each leg leaving a pin along its own stub direction.
  A run that cannot be drawn cleanly keeps its label: a wire that dodges
  three parts to avoid a fourth reads worse than a name.
* **One label per wire fragment survives** — the label is what names the net.
  Delete the rest: a label on every pin of a drawn net is clutter, but a
  fully wired net with *no* label is anonymous, and a fragment that never
  got wired needs its own.
* **Stagger where wires escape and jog.** Two parallel runs cannot share a
  column; a bus is exactly many parallel runs. Give each net its own escape
  distance and jog lane, 2.54 mm apart.
* **Leave every pin an approach corridor.** A wire that rides along the line
  a pin faces walls that pin off — nothing can ever reach it. Crossing the
  corridor is fine; running along it is not.
* **Labels are for distance.** A programming header in the far corner, a
  reset line crossing the whole sheet — a human labels those too. Wire what
  is local, name what is remote.
* **A power rail that is the circuit draws as a wire.** A converter's output
  rail reads as one horizontal line, source on the left, load on the right,
  capacitors tapping down in board order — and the feedback wire visibly
  returns from that line to the pin. Six power symbols on the same net leave
  the reader to reassemble the loop by name; save the symbols for rails that
  merely supply things.
* **The surviving label sits on the generic part.** A net that reaches a
  connector is named at the connector pin — the header a harness plugs into —
  not in the middle of the circuit.
* **Miscellaneous logic may keep names at both ends.** Eight control lines
  drawn as wires lattice the power section; as a pair of labels per net they
  read as the pin map they are. A 1:1 breakout board is the extreme case:
  all names, and better for it. Say so in the gate policy — the
  `readability.label_only` waiver is where that convention is argued.

## Junctions and tees

* **Break a wire wherever a branch tees in, and put the dot there.** KiCad's
  editor always saves files this way; generated files must match. Unbroken:
  KiCad 9 connects one side only (`readability.wire_through_junction`).
  Undotted: the tee reads as a crossing (`readability.missing_junction`).
* **Never draw two wires along the same stretch of line**
  (`readability.overlapping_wires`). Overlaps appear naturally when two
  branches leave one pin along the same axis — union them into one wire and
  tee the branch off it.
* **Round every coordinate.** A wire endpoint 2e-14 off its pin is a wire
  KiCad has to be lucky to connect. Emit coordinates rounded (0.1 µm is
  plenty); never write raw floating-point arithmetic into the file.

## Power symbols and flags

* **Rails point up, grounds hang down, negative rails hang below ground** —
  the one orientation every reader assumes without looking
  (`readability.power_symbol_orientation`). Never turn the symbol to follow
  the wire; bend the wire. A sideways pin gets a short jog; a connector's
  mid-row ground runs out past the pins' approach corridors to a shared
  vertical rail with one upright symbol beyond the body — one rail, four
  taps, one symbol, exactly as a human draws a header with four grounds.
* **PWR_FLAG goes where the power comes onto the board**, wired in beside
  the source pin's symbol — the input terminal, the regulator output, the
  diode cathode. A flag parked in a labelled row at the sheet edge answers
  ERC and tells the reader nothing about where the rail is made.

## Notes sit beside their subject

One block of prose in a corner reads as none: nobody carries sentence four
across the sheet to capacitor three. Split the design notes and anchor each
block beside the circuit it explains — the input note by the input, the
filter math by the filter, the indicator note by the LED. On an analog sheet,
add test points where the simulation is meant to meet the board, and say so
in a note beside them (`test.no_testpoints` notices when there are none).

## Parts and their annotations

* **Draw a capacitor next to the IC pin it serves**, in the order the board
  places them: bypass tightest, bulk further out. A capacitor that lives in
  a row at the top of the sheet says nothing about which pin it decouples.
* **Ratings go on the page, not only in fields.** "100n 50V 10%" printed at
  the part is what an engineer reads; a hidden field is what a script reads.
  Both should exist (`spec.missing_rating` checks the fields; the page is
  yours). Keep the printed block beside an upright part and under a lying
  one, where the ground symbol is not.
* **No text on other text.** References, values, ratings and net labels each
  need their own clear ground; measure the neighbours before placing, the
  way the reference and value are already measured from the pin extent.
* **Around an IC, wires beat labels** unless the destination is a separate
  functional block. When the datasheet has a reference schematic or an
  application note, match its layout — readers know it already, and the
  review that follows will be diffing against it in their head.

## Orientation and placement

* **Point a connector's pins at its signals.** A header drawn with pins
  facing away from everything it connects to forces every wire to lap the
  body — mirror the symbol instead (`(mirror y)`); rotating 180° reverses
  the pin order top-to-bottom, which is usually wrong for a bus.
  `readability.facing_away` measures this.
* **Orient two-pin parts with the current flow.** An LED drawn with its
  ground symbol pointing *up* into the resistor feeding it is upside down;
  no rule reads polarity semantics, so this is on the author: supply above,
  ground below, arrows pointing the way the current goes.
* **Power symbols carry rails; wires carry signals.** A rail drawn as a wire
  across the whole sheet is noise — that is what `power:+3V3` and its
  siblings are for. Turn the symbol to point the way the pin leaves so its
  name lands in clear space.
* **Notes, power flags and the title block get their own ground.** Notes go
  below or beside the circuit, never on it (`readability.text_over_symbol`);
  nothing goes on the outer frame strip or the title block corner
  (`readability.margin_intrusion`); the flag row needs a clear strip of
  sheet. When the circuit grows, these placements must move — they were
  chosen for the old extent.

## The failure the netlist never shows

Two nets touching on the sheet — a stub ending on another net's wire — makes
one net where the design says two, and everything downstream is self-
consistently wrong; the only symptom is KiCad's schematic parity check
disagreeing about a net name. Check for it *at generation time* by comparing
every wire against every other for shared endpoints and tees across nets, and
refuse to write the file. Finding it later costs an evening; finding it at
build time costs a message.

## Where the rules live

Every check named above is in `eda sch review` / `eda gate --list-rules`:
`readability.label_only`, `readability.missing_junction`,
`readability.wire_through_junction`, `readability.overlapping_wires`,
`readability.facing_away`, `readability.margin_intrusion`,
`readability.text_over_symbol`, plus the off-grid, dangling-wire and
overlapping-symbol rules the review guide describes. What cannot be a rule —
polarity semantics, which nets deserve wires versus names, whether a drawing
*reads* — is this guide, and the render.

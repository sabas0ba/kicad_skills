# An engineer's pass over the five examples

The five projects under [`examples/`](README.md) were generated to be reviewed.
This is the review: the five designs read the way an electrical engineer would
read them — circuit theory first, then the physics of the layout, then whether
the drawings can be read at all. Every finding ends in one of four places, and
the point of writing them down is to say which:

* **rule** — the toolkit can check it, so now it does
  (`eda gate --list-rules` is the authoritative list);
* **fixed** — the `reviewed/` variant was wrong and has been corrected;
* **waived** — the finding is real, the design is right anyway, and the
  project's `gate.toml` says why;
* **open** — real, understood, and not yet either checked or fixed.

## 1. Electrical

### Found wrong and fixed

* **The PCM5102A charge pump was miswired** (fpga-audio). The flying capacitor
  belongs between CAPP and CAPM and the reservoir from VNEG to ground; the
  generated board had a capacitor from CAPP to ground and one from CAPM to
  VNEG. The inverter cannot run that way — no negative rail, no output stage.
  Notably, `analog.missing_decoupling` *was* firing on VNEG, and the earlier
  write-up dismissed it as a false positive. It was half right: the rule cannot
  name the topology, but the pin it pointed at really was missing its
  capacitor. **Fixed** — and the finding it silences went with it.
* **An AC-coupled output with no DC path** (opamp-filter). `OUT_AC` connected a
  capacitor to a connector and nothing else, so the node floats at whatever it
  last charged to and pops when a load is plugged in. **Fixed** with a 100k
  bleed to ground, and generalised as the **rule** `analog.no_dc_path`: a net
  whose every pin belongs to a capacitor or a connector has, provably from the
  netlist alone, nothing setting its DC level.
* **VCCPLL tied straight to the core rail** (fpga-audio). Lattice asks for an
  RC from the 1.2 V rail so the PLL does not eat the core's switching noise;
  the board tied them together and KiCad's ERC said so (two power outputs
  connected). **Fixed** — 100 Ω series with 10 µF + 100 nF at the pin — which
  also retires the ERC finding.
* **The boot flash had no chip-select pull-up** (fpga-audio). Between power-up
  and configuration the FPGA's pins float; nothing held the W25Q32 deselected
  while the bus it boots from was undriven. **Fixed** with 10 k to 3.3 V.
* **The LDO reservoir was undersized** (fpga-audio): 1 µF on LDOO where the
  datasheet's application shows 2.2 µF. **Fixed**.

### Judged right, and answered in the gate file

* **The DRV8833 charge pump** (motor-driver): VCP's 10 nF goes to VM, not to
  ground, because the datasheet says exactly that. The decoupling rule cannot
  know a charge pump from a supply pin — **waived**, with the reason in
  [`motor-driver/gate.toml`](https://github.com/sabas0ba/kicad_skills/blob/main/examples/motor-driver/gate.toml).
* **Rails a board exposes but does not make** (pico-carrier): VBUS and VSYS
  belong to the module; ADC_VREF is the module's own filtered reference,
  deliberately handed to the user undecoupled. The schematic-side rule now
  reads pin electrical types instead of net names (see §5), which retires most
  of these; what remains is **waived**.
* **A reference made by an op-amp** (opamp-filter): VREF is U2's output.
  Decoupling an op-amp output is a stability problem, not hygiene — the
  capacitor lands inside the control loop. The schematic rule now knows this
  from the netlist (`output` pin on the net vetoes the ask); the board-side
  rule cannot see pin types and still fires, and is **waived**.

### Open

* No input protection anywhere: no reverse-polarity device or fuse behind any
  of the screw terminals (all five), no ESD or current-limit resistor on the
  op-amp filter's input jack. Deliberate scope on a demonstration set, but a
  production review would ask.
* The buck's LM2596 wants its output capacitor's ESR inside a stated window —
  an all-ceramic substitution would ring, and no rule reads ESR.
* Motor outputs leave the board unfiltered (motor-driver); fine on the bench,
  an EMC gamble on a metre of cable.
* No series termination on CLK12 (fpga-audio) — 12 MHz over ~30 mm forgives
  it, and a rule would need to know which nets are clocks.
* PCM5102A XSMT is strapped high, so the DAC un-mutes with the rail rather
  than under control: the power-up pop is accepted, not managed.

## 2. Electromagnetics and layout physics

* **Return paths** — the strongest physical criticism of a two-layer layout.
  On a two-layer board with the ground plane on the back, every bottom-layer track
  cuts a channel through the plane, and any top-layer signal crossing that
  channel has its return current detoured around the gap: the loop area, and
  with it emission and coupling, grows by the detour. Now the **rule**
  `route.return_path`: the parser keeps the pour's outline *and* its computed
  fill, and the difference between them is exactly where the plane is not.
* **Decoupling geometry** (already ruled: `layout.decoupling_distance`,
  `layout.decoupling_via`). The three fine-pitch boards all fail the distance
  rule for the same reason — the escape from the package spends the distance
  budget before a capacitor can be placed. With parts on one side this is a
  fact of the package rather than a loose placement; the closest mechanical
  answer is caps on the back under the pins. The four-layer FPGA and motor
  boards instead give the local supply and return paths a continuous nearby
  inner plane.
  **Waived** per project, with that reason. The *via* half is now
  **fixed** where it was failing: on the FPGA board every 0603's ground via
  is anchored against its own pad, on the far side from the supply pad, with
  the 1.2 mm stub as the whole loop — placed as a declared via next to the
  pad rather than found by the router at the end of a track.
* **Power track width** (`track.thin_power`). The rule used to damn a rail for
  its narrowest millimetre, which on a fine-pitch board is the escape neck it
  cannot avoid. Now it measures the longest *contiguous* narrow run against a
  `power_neck_mm` allowance — necks pass, thin trunks still fail. Where a
  whole distribution stays narrow, the finding stands. The FPGA now uses an
  In2 +3V3 plane and a short +1V2 spine, but its QFN escape necks remain 0.2 mm;
  the waiver argues in numbers that an iCE40 draws tens of milliamps while
  0.2 mm carries 0.74 A at a 10 °C rise.
* **Thermals, open**: nothing yet judges copper area under a TO-263 tab or a
  QFN's exposed pad against the watts the part dissipates (the buck and the
  motor driver both care); stitching-via count under the iCE40's pad is
  eyeballed, not checked. `eda pcb thermal` can now *answer* the question —
  state the watts and it solves where they go — but answering is not judging:
  the review still has no rule that fails a board for it.
* **Crosstalk channels** — now the **rule** `emc.parallel_run`: the 3W rule
  measured as accumulated same-layer run closer than three trace widths,
  differential pairs exempt by name. Quiet on all five boards — the router's
  habit of spreading nets across free space turns out to be an EMC feature.
* **Rim stitching pitch** — now the **rule** `emc.stitching_pitch`, and
  **fixed** on all five boards. It was open because the stitcher aimed a 10 mm
  rim ring and *dropped* a candidate that collided with a pad, a track or a
  hole, so the congested stretches — which are the stretches the rule is about
  — kept gaps of 20–40 mm against the rule's 18, measured along the perimeter
  the way edge noise travels. Twenty-four gaps across the five boards, the
  widest 40 mm; now none, for thirty-three vias across the whole set.

  Three things it took, and only the first was the one anticipated:

  * **Slide, do not drop.** A collision is a reason to give up on that
    millimetre, not on the station. Each candidate carries the direction its
    edge runs in and the one that goes deeper into the band, and walks half a
    step either way before trying a row further in. Half a step is the limit:
    past it a via stands at its neighbour's station.
  * **Anchor both corners.** Stepping from one corner and stopping when the
    next is overshot ends the last stretch short of it, and the rule measures
    round the corner, so the run to the first station on the next side is two
    spans. That is arithmetic, not congestion — the buck board's residue was
    exactly 2 × step — and each edge is now divided into equal spans no longer
    than a step.
  * **Fill what is left, and let a fill station walk.** The remainder was
    stretches of 19 mm against a limit of 18, so the placed ring is walked and
    any gap over target is halved. Two measurements settled the shape of this:
    the target is 14 and not 18, because the rule walks the board *outline*
    and this walks the ring inset from it (at 18 the pass placed nothing at
    all); and a fill station's reach is half its gap, not half a step, because
    it has no neighbour to stand at. That last one is what gets round the
    motor board's two remaining gaps, which turned out to be its two M3
    mounting holes: the corner belongs to the screw, the nearest copper a via
    may sit on is 8 mm along the edge from it, and one via on each side makes
    16.8 mm where one via anywhere leaves 20.8.

  A fourth thing was a defect the round found rather than fixed: sliding put a
  via 0.4944 mm from a fiducial against a 0.6 mm rule. The predicate had always
  taken a flat 0.3 mm off every pad's box; the nominal grid had simply never
  landed there. A fiducial states `(clearance 0.6)` on its own pad, and the
  stitcher reads it now.

## 3. Schematic readability and semantics

* **Connections by name instead of by wire** — the single most recognisable
  mark of a generated schematic, and until now nothing measured it. Every pin
  got a stub and a label; the reader greps. Now the **rule**
  `readability.label_only` (fraction of label-stub connections over the wire
  graph, power symbols exempt), and the **fix**: a sheet router. Each signal
  net becomes a tree of straight, L-, Z- and detour-shaped runs on the
  schematic grid — legs leave a pin along its own stub, escape distances
  stagger so a bus does not fight over one column, every pin reserves a
  runway another net may cross but not ride along, and three ends meeting on
  a point get their junction dot. One label per wire fragment survives,
  because the label is what names the net. The result: buck, motor and op-amp
  sheets fully wired; the Pico carrier wired 31 of 33 nets (the two module
  control pins whose route would lap the module symbol keep their labels);
  the FPGA board 16 of 19 (the programming header and CRESET stay named,
  which is what a human does with a far corner connector). `as-generated/`
  stays a pure name table, which is what the rule is for.
* **What trying to draw the wires found** — four placement defects no rule
  had caught, each invisible until a wire had to be drawn through it: the
  buck's LED was drawn upside down, its ground symbol pointing up into the
  resistor above it; both breakout headers (Pico J4, FPGA J3) faced *away*
  from everything they connect to — fixed with mirrored symbols, which the
  generator could not draw at all before; the FPGA sheet's notes printed
  straight over the 1.2 V regulator and its capacitors; and its programming
  header sat inside the title block. The router now treats the sheet frame,
  the title block and the notes as obstacles, so a wire cannot be drawn onto
  them either.
* **A bypass capacitor's sheet position says nothing about which pin it
  serves** — C6 near U1 on the board is C6 in a column of capacitors on the
  sheet. Splitting the iCE40 into its four library units (banks + supplies)
  was the first step; placing each capacitor against its unit is open.
* **Ratings live in fields, not on the page.** Tolerance, voltage and MPN are
  machine-checked (`spec.*`) but hidden in the plot; an engineer's sheet
  prints "100n 50V C0G" where it matters. Open.
* **Multi-unit symbols, no-connects, junctions**: drawing the iCE40 as one
  unit, leaving unused pins bare — both found by KiCad or the rules, both
  fixed in earlier rounds; kept here because each was invisible until a rule
  or a build check said otherwise.

## 4. Artwork readability

* **The scenic route** — the second unmistakable autorouter signature: a legal
  track three times longer than it needs to be, crossing open board at 45°.
  Now the **rule** `route.detour`: routed length against the minimum spanning
  tree of the net's pads, which no honest route beats. The examples' own
  router still takes tours the rule flags; those are **waived** as known
  machine routing, and tightening the router is open.
* **Corners** (`route.acute_angle`, existing): the stated escape fans meet the
  45° routing grid at angles under 90°. Acid-trap folklore aside, they read as
  machine work. Waived where they come from the fan geometry.
* **Silkscreen**: references print over pads on the dense boards
  (`silk.over_pad`, KiCad's own checks) — partly library footprints, partly
  crowding; waived where the footprint itself is the cause. No board states
  its name, revision or fab notes in copper or silk. Open.
* **One via per power transition, no mounting holes, no test points** — all
  reported today as context rules; a production board would want all three
  judged, not narrated.

## 5. What changed in the toolkit because of this pass

| finding | disposition |
| --- | --- |
| connections by name, not wire | rule `readability.label_only` + fixed (sheet router: wire trees, junction dots, mirrored connectors) |
| inverted LED, headers facing away, notes over parts, connector in the title block | fixed — found by the router, not by a rule; the checkable parts became rules afterwards (below) |
| ground vias at the end of a track | fixed on fpga-audio (vias anchored beside each capacitor's ground pad) |
| a wire run through a junction — KiCad 9 connects one side only | rule `readability.wire_through_junction` |
| two wires drawn along one line | rule `readability.overlapping_wires` |
| a connector row facing away from its signals | rule `readability.facing_away` |
| pins or notes on the frame strip or title block | rule `readability.margin_intrusion` |
| a note printed over a symbol | rule `readability.text_over_symbol` |
| what stays judgment — polarity semantics, wire-versus-label taste, router cost trade-offs | the two authoring guides, `kicad-schematic-authoring` and `kicad-pcb-authoring` |
| floating AC-coupled node | rule `analog.no_dc_path` + fixed (R6 on opamp-filter) |
| scenic-route routing | rule `route.detour` |
| signals over plane cuts | rule `route.return_path` (parser now keeps zone outline + fill) |
| escape necks damned as thin power | `track.thin_power` re-judged on contiguous run vs `power_neck_mm` |
| decoupling asked of names, not pins | `analog.missing_decoupling` now reads pin types; output-driven nets exempt |
| unused pins as defects | `net.single_pin` honours no-connect flags |
| charge pump miswire, VCCPLL tie, missing CS pull-up, LDO reservoir | fixed in `fpga-audio/reviewed` |
| package-geometry decoupling distance | waived per project, reason in each `gate.toml` |

## 6. Calibration

Every new or changed rule was run over the 18 human-drawn demo projects that
ship with KiCad, before and after, because a rule that fires on human work is
measuring something other than machine work:

| rule | on the demo corpus |
| --- | --- |
| `readability.label_only` | 0 findings — human sheets draw their wires |
| `analog.no_dc_path` | 0 findings |
| `route.detour` | 8 boards at a 2.5x threshold, **0 at 4x** — 4x is where human routing stops and machine tours begin, and is the shipped default |
| `route.return_path` | 4 boards — real two-layer boards genuinely have this disease, which is why it is a warning and not an error |
| `track.thin_power` (re-judged) | 9 boards before, 0 after — every one of the nine was a pad-entry or escape neck, which is exactly what the rule was wrong about |
| `readability.wire_through_junction` | 0 findings — the editor splits wires at tees, so human files never contain one |
| `readability.overlapping_wires` | 1 board, 7 spots — and they are real drawn-twice wires, not noise |
| `readability.facing_away` | 6 boards — humans park edge connectors facing outward on purpose, so it is graded info; the ai-generated policy still blocks on it |
| `readability.margin_intrusion` | 6 boards — mounting holes and logos live in the margins of human sheets, so info, same reasoning |
| `readability.text_over_symbol` | 2 boards — info from the start: the text extent is estimated, not measured |
| `readability.text_over_wire` | 10 boards, 942 findings — **graded info** for the same reason. A generated design still has to have none: the `ai-generated` policy promotes it to an error |
| `readability.text_over_text` | **14 of the 18 boards, 4531 findings** — also info. Human sheets are full of text a character-count estimate reads as touching, and a rule that fires that often on human work is measuring the estimate rather than the drawing. Under `ai-generated` it is still an error, and every `reviewed/` sheet has zero |
| `route.acute_angle` (pad exemption re-cut) | **8 boards / 127 corners → 9 boards / 205** on the same demo boards. A 61 % rise on a rule already graded info, in exchange for seeing 11 of the 13 real hairpins on our own five boards that the old disc hid. It was never quiet on human work, which is why it is info and only the `ai-generated` policy promotes it |
| `route.wander` | **1 board of the 18** at the shipped 2.0x — the same order as `route.detour` at 4x, and the one it finds is a real out-and-back. Measured against a baseline that walks round whatever package the straight line crosses, so a feedback wrap is not counted as a tour |

Every `reviewed/` project now carries a `gate.toml` and passes
`eda gate --policy examples/<name>/gate.toml`; every waiver in those files is
one of the judgments above, stated as a reason a reviewer can disagree with.
That is the intended shape of the mechanism: findings are either fixed,
checked, or answered — never silently absent.

## 7. The reviewer's pass, round two

The project owner reviewed the regenerated set and returned a list of
conventions the drawings and the artwork still broke. Each item went the
same three ways as before — into the generator, into a rule where a rule can
check it, into the authoring guides where only judgment can:

| item | disposition |
| --- | --- |
| power symbols drawn sideways | fixed (upright symbols, jog wires, per-connector ground/rail bus columns) + rule `readability.power_symbol_orientation` — graded info because twelve of eighteen human demo sheets rotate them, and the ai-generated policy promotes it anyway |
| PWR_FLAG parked in a labelled row | fixed — flags are wired in beside the pin the rail actually comes from; placement is judgment, so the rest is the guide |
| ratings hidden in fields | fixed — R/C/L print voltage, tolerance and power beside the part |
| capacitors drawn far from their IC, notes and fields colliding | guide (`kicad-schematic-authoring`), with `readability.text_over_symbol` and `margin_intrusion` catching the checkable part; per-unit capacitor adjacency stays open |
| right-angle corners | rule `route.right_angle` (info — thirteen of eighteen human demo boards corner at 90, and this generator's own router still does; chamfering it is open) |
| connector pins unlabeled on silk | fixed (net names beside every connector pad, outside the courtyard) + rule `silk.unlabeled_connector` |
| indicators unlabeled | fixed — `silk_label` per part: "5V OK" beside the power LED |
| no board name or revision | fixed (name + rev, bottom centre) + rule `silk.missing_board_id` |
| ground pour on one face only | fixed — both faces poured, stitched by an automatic ring of edge vias + rule `layout.pour_single_sided` (context: a one-sided pour is a design choice real boards ship with) |
| high-current return left to the pour | fixed on buck-5v — an explicit 1.0 mm ground return along the bottom edge, input terminal to output terminal |
| feedback path stretched | verified rather than changed: the buck's FB trace is Manhattan-minimal to the output capacitor it senses; the guide records the rule of thumb (short, and never parallel to SW) |
| clearance crowded without need, connectors not at the board edge | guides — the router's crowding cost and the connector-placement convention are stated there; neither is measurable enough to be a rule yet |

## 8. The reviewer's pass, round three

The owner read the round-two set and held it to a stricter standard: the
schematic is judged from its plot, as the original an artwork is checked
against — so structures that only make sense as data (an S-expression's
label table, a rail assembled from six power symbols) are out wherever the
drawing can carry the meaning instead. The artwork adds manufacturing and
performance: no wasted copper, no accidental angles.

| item | disposition |
| --- | --- |
| IC surroundings drawn as labels | fixed — the buck's converter loop is all wire now: `wired_power` draws the +12 V and +5 V rails as horizontal lines with taps, and the FB wire visibly returns from the output rail to the pin |
| capacitors connected by symbol, not wire | fixed — rail caps tap the drawn rail in board order, bulk to bypass |
| indicator blocks mixed into the power run | fixed — LED blocks sit apart with their own note on every design |
| notes piled in one corner | fixed — `note_blocks` anchors each note beside the circuit it explains, on all five sheets |
| labels on the circuit side of a net | fixed — the surviving label prefers the connector pin (`J*`), and label text reads away from its pin so it cannot merge with the pin number |
| miscellaneous logic latticing the sheet | fixed — `label_nets` keeps the motor's control lines and the Pico's forty-pin map as names at both ends; the `readability.label_only` waiver in each `gate.toml` is where that convention is argued |
| VM rail wandering | fixed — the motor's VM runs J1 → bulk → bypass on one line, rises once, and crosses to the pin with the charge-pump cap tapping the riser |
| test points for sim-vs-measurement | fixed on opamp-filter — TP1-TP3 on filter input, output and VREF, wired on sheet and board; `test.no_testpoints` already notices their absence |
| screw terminals facing along the board | fixed — J1/J2 entries point off the board edge on buck-5v |
| pour cut out around connectors for no reason | fixed — the buck pours edge to edge; the pico's stripes under the module are forty foreign through-holes at 2.54 mm, and the sheet now says so |
| TO-263 tab treated as just a pad | fixed — a seven-via ring beside the tab (never on it: via-in-pad drinks solder) ties it into both planes as heatsink and return |
| board silk missing author | fixed — name, rev and author line on every board |
| 90-degree corners in tracks | fixed — every square corner chamfers to two 45s unless a via needs the square one (`_chamfer_tracks`) |
| shallow fan angles (20-30 deg) | fixed — escape fans bend at staggered 45s (`fan`), and the new rule `route.odd_angle` reports corners off the 45 grid (info: eleven of eighteen human demo boards bend off-grid somewhere) |
| widening a track mid-run | fixed — motor outputs leave the pin field at the row's full width and widen once, at the field's edge; the guide states the principle |
| J2/J3 colliding when mated | fixed — the opamp's power header moved to the top-left edge, clear of the output header |
| return path through the front face centre | improved — four mid-board ground vias give the sliced front pour short ways to the plane on opamp-filter |
| routing under digital ICs | rule of thumb in the guide + a `route_keepout` mechanism in the generator; on fpga-audio even the codec's underside turned out to be load-bearing corridor - with it closed the ground drops beside it lose their last lane - which is the unavoidable case the guide names |
| GPIO/programming header interior | judged — two routing attempts at the bottom edge found no lane: that edge is the south escape fan's corridor, so the debug header keeps the one interior window that routes, and the guide states the trade |
| capacitors with no visible owner | fixed — fpga bank caps sit beside the bank unit they feed, codec caps beside the codec, each group with its note |
| I2S weave between FPGA and codec | fixed — the bus is names at both ends now, and the sheet reads as the pin map |
| feed diode backwards | **found by this pass**: the pico's D1 had cathode on the external 5 V - the supply could never reach VSYS; polarity fixed in the netlist |

## 9. The reviewer's pass, round four

The buck passed. The other four came back with artwork faults, and one
sentence recurred across three of them: *do not let the ground pour be cut
up*. Three rounds had asked for it in different words and nothing in the
toolkit could say whether it had happened, so this round starts by building
the measurement.

**Two rules, and what measuring taught us.** `layout.pour_fragmented` reads
the share of a pour's copper in its largest connected island — the case where
the plane is genuinely in pieces. `layout.pour_coverage` reads how much of
its own outline a pour actually filled, which is the number the eye takes off
the plot. Both raster the fill rather than adding polygon areas, because a
generated fill is hundreds of overlapping rectangles and their areas cannot
be added.

Measuring immediately explained the plots. In the dense half of a layout the
clearance channels shred the pour into fragments, and a fragment touching no
ground pad of its own is dropped by the filler as an orphan — that is where
the blank fields come from, not from the tracks themselves. **Stitching the
interior on a six millimetre mesh, not just the rim**, gives every fragment
an anchor; the motor driver went from 63 % of its outline to 76 % on that
change alone.

Measuring also corrected the rule. Compacting the filter — 80 × 45 mm down to
58 × 42, every run shorter, which is exactly what the review asked for — made
its coverage *fall*, because the same copper in less area is a smaller share
of it. **A number that drops when the artwork improves cannot be a verdict**,
so coverage reports and `pour_fragmented` faults.

| item | disposition |
| --- | --- |
| motor: ground pour fragmented | fixed — interior stitching, and the output bundle drawn at 1.4 mm instead of 2.0: two tracks that far apart leave 0.3 mm between them, under the filler's sliver limit, so the strip vanishes and the pair reads as one wide hole |
| motor: track width changed mid-run | fixed — the escape is short enough that the narrow section is a pad neck (inside `track.thin_power`'s allowance) and the widening happens once, where the package stops constraining it |
| motor: 45s bent too early, right angles, doubled-back runs | fixed — every escape gets a three millimetre straight before its first bend, and the board lost the twelve millimetres of open field the router was detouring across |
| pico: LED circuit overlapping | fixed — the indicator has its own column and its note moved with it |
| opamp: analog and power traces routed carelessly | fixed — the board is redrawn around its nets: the half-rail buffer sits with the two parts that use its output instead of across the board from them, so VREF no longer runs corner to corner, and the supply leaves the header as two short branches |
| opamp: placement and routing that fragment the front pour | improved — total routed length 412 mm on a board 40 % smaller in area; the remaining crossing of the back plane's cut is waived in numbers (ten nanohenries at a kilohertz) |
| fpga: parts too far apart, wiring cluttered | fixed — the sheet gathers into blocks (power, clock, config, codec), each with its note beside it, in place of parts spread over the whole of an A3 |
| fpga: bends off 45, pour fragmented, routing under the codec, analog return under digital, bypass caps away from the regulator | partly open — the QFN's four-sided 0.5 mm escape is the constraint behind most of them, and two attempts at closing the codec's underside left the ground drops beside it with no lane. What is fixed is fixed; what is not is stated with the reason rather than quietly waived |
| calibration | on the thirteen KiCad demo boards whose zones parse, `layout.pour_fragmented` fires once and `layout.pour_coverage` on eight — which is the split the two are meant to have: the fault is rare on boards a human drew, and the reading is common because most real boards route on a poured face |
| courtyard overlaps found by this round | fixed — a courtyard pre-flight now runs before routing. Editing coordinates by pattern had let one part's replacement land on another's, which is how a divider ended up under an input resistor; every part on that board now carries its position explicitly |

## 10. The reviewer's pass, round five: no waivers

The instruction this round was two sentences long and changed the shape of
the work: **every item raised is critical and cannot be waived**, and the
evaluation is to be made from the output images. The second sentence explains
why the first was needed. Three rounds of findings had been answered with
argument rather than evidence, and the arguments were not being checked
against the plots they were about.

### The mechanism that was missing

`pcb review --map findings.png` draws the board from its own geometry and puts
a numbered mark at every finding that carries a position, keyed to a legend.
Rules opt in by putting coordinates in their details; nothing is parsed back
out of a message.

It found its first thing immediately, and it was a lie of mine. The waiver on
this repository's FPGA board said its off-grid corners were the QFN's escape
fan and therefore unavoidable. The map showed the marks: **not one of the
hundred and forty-eight was near the QFN.** They were on the power block, on
the flash bus, on runs crossing open board. A count in a list can carry an
excuse; the same count drawn where it happens cannot.

That is the case for the mechanism, and it is why it went in before anything
else this round.

### What went into the tool

| built in | kind | what it catches |
| --- | --- | --- |
| `pcb review --map` | mechanism | every located finding, drawn on the copper with a legend |
| `route.width_step` | rule | a run widening away from the pad the neck was for - the narrow part already set the current |
| `route.under_package` | rule | another net threaded under a package body: no plane under it, no way to probe it |
| `layout.connector_not_at_edge` | rule | a connector the cable has to cross the board to reach |
| `silk.unlabeled_indicator` | rule | an LED or switch whose silk names the schematic line, not the function |
| courtyard parsing | parser | what a part *occupies*, so a terminal block is measured by its body and not its pads |
| `route.acute_angle`, `route.odd_angle`, `route.right_angle` | grading | moved from info to blocking: grading them info was calibration against what human boards do, and this repository's subject is what a generated board must do |
| angle rule: pad junctions | fix | two branches leaving one pad have an angle between them and it is not a bend in either - six false findings on the filter alone |

### What the tool then made us fix

| fixed | how | measured |
| --- | --- | --- |
| bends at angles nobody chose | the router works on a grid and the pads do not, so the segment joining a path to a pad landed at any angle. Each is now a straight leg plus a 45 into the pad, skipped where the knee would not clear | off-grid corners on the four two-layer boards: **91 to 0** |
| square corners at run joins | a route arrives as several `Track` objects and the chamfer only looked inside one, so the corner between two of them was never cut. Runs are joined first | right angles: 12 to 3 |
| copper drawn twice | identical segments deduplicated - a doubled end reads to the angle rule as a run folding back on itself | the zero-degree findings |
| width changed mid-run | the bridge outputs leave at 0.4 mm and stay there; the escape carries the current whatever the rest is widened to, and the note says what 0.4 mm is worth | width steps: motor 4, filter 1, both to 0 |
| headers a cable could not reach | the carrier's two breakout headers moved to the top edge; the board lost twelve millimetres of empty field under them | connectors off the edge: 2 to 0 |
| a junction that was only a coincidence | the buck's feedback ended at a coordinate its output rail happened to pass through. It ends on the capacitor pad it senses at, which is what the note always claimed | one dangling end |

### What is not fixed, stated rather than waived

Every waiver covering an item the reviewer raised has been deleted -
`route.acute_angle`, `route.odd_angle`, `route.return_path`, `track.thin_power`
and `silk.over_pad` across five projects. What those rules report now stands
as failure, because that is what it is. The FPGA board is the honest limit:
a 0.5 mm four-sided QFN escaping on two layers cannot hold the 45 grid, keep
its plane whole, and stay off its own package's underside at once, and the
board's own notes have said so since it was written. The fix is an inner
layer, not an argument, and it is the next thing to build.

## 11. The reviewer's pass, round six: the last of the waivers

Round five stated the policy — nothing the reviewer raised may be waived — and
built the mechanism that made the policy checkable. This round is the work
that policy demanded, and it went deeper than the findings it started from:
three of the five boards were failing for reasons that had nothing to do with
the rule that reported them.

### What went into the tool

| built in | kind | what it catches |
| --- | --- | --- |
| `readability.text_over_wire` | rule | a symbol's designator, value or rating printed across a net — a value with a wire drawn through it is a value nobody can read off the plot |
| property positions | parser | where a symbol field actually prints. The parser had been dropping the coordinate, so nothing downstream could ask |
| `hide` as a bare atom | parser fix | KiCad 7 writes `(effects ... hide)`, not `(hide yes)`. Reading only the second form made every hidden field visible, and forty hidden designators drowned the real findings |
| `Design.keepouts` | mechanism | rectangles of board closed to the router on both faces, so a layout can say which side a connector is approached from |
| per-net `back_cost` | mechanism | a signal's plane crossing priced against the front-side detour that avoids it, per net, so ground is not charged for its own plane |
| `route.width_step`: the neck | rule fix | the rule's own docstring said a width change is honest at a pad *or at the edge of the pin field that forced the neck*, and only the first half was implemented. A 0.5 mm row holds 0.2 mm and nothing wider, so every fine-pitch escape was a finding with no fix but an argument. The narrow side now gets the same `power_neck_mm` budget `track.thin_power` gives it |

### What the tool then made us fix

| fixed | how | measured |
| --- | --- | --- |
| fields printed across nets | every field now states the spots it would accept, in order, and takes the first that prints over nothing — measured with the same rectangle the rule measures. The pin stubs and the `PWR_FLAG`'s own value were missing from the picture the placer looked at; they are in it now | `text_over_wire`: buck 11, motor 6, filter 17, carrier 7 — **all to 0** |
| copper laid down and walked back along | joining two routes at a shared end left the polyline going out past the join and straight back to it. The overshoot carries no current and comes out | zero-degree corners: filter 1, motor 1, both to 0 |
| corners cut off the pads they were reaching | once two routes meeting at a pad are merged, the pad is an interior corner like any other and the chamfer cut it — moving copper off the pad and leaving the net unconnected, invisibly. Both clean-up passes now hold every pad and every track end still | unconnected nets: buck 2, filter 3, carrier 1 — all to 0 |
| plane islands that reached ground but not each other | touching *some* ground copper was enough to keep a piece of pour. Two pieces each holding one bypass cap's ground pad are still two pieces. The far side of the board is a node in the graph now, every via and through-hole pad an edge to it, and a piece survives only if it can be walked from there | zone-island DRC pairs to 0 |
| the motor board's whole right-hand third | four logic signals left the package on the east side, went over the top of the board, round the outside and back into the header from behind — 190 mm of copper for a 40 mm net, and four back-layer runs each cutting the plane under the track that fed it. The header now sits where the fan lands, pin for pin; the small caps sit in the middle band with the supply pins that own them; and **VM crosses the plane on one stated link** so that the signals cross its cut at right angles instead of going round | `return_path` 4→0, `detour` 1→0, `acute_angle` 1→0, `pour_fragmented` 1→0, `thin_power` and `width_step` to 0 |
| silk printed across pads | the board id was written at the bottom centre whether or not the bottom centre was a module; the pin legend went toward the middle of the board, which on a carrier is the module it is labelling; a designator stayed where its library drew it, which on a part with pads on three sides is the middle of a pad. All three ask first, and the pin legend picks its side by the *area* it would take rather than by whether it collides | `silk.over_pad`: carrier 8→0, motor 5 DRC silk warnings→1 |
| the carrier's plane in three pieces | its two headers and the module run the length of the board and the pour could not get past their pin rows at either end. Six millimetres of board below the last pin is what a plane needs to be one plane | `pour_fragmented` 2→0 |
| rails escaping at signal width | a supply pin leaving a fine-pitch row at 0.3 mm and widening two millimetres later is a step nobody chose; widening the far half only moves the complaint to the thin one. The row's pitch sets the width, and the pin leaves at the width it keeps | `width_step` and `thin_power` to 0 on the motor and filter boards |

### A note on which KiCad reads the file

The zone-island errors above are reported by KiCad 9's DRC and not by
KiCad 10's, on the same file. The fill these examples write is a set of
overlapping rectangles rather than one traced polygon, and the two releases
disagree about when overlapping fill polygons are one piece of copper. The
generator writes KiCad 9 format because that is the oldest release in the CI
matrix; the verdicts quoted here are KiCad 10's, which is the default the
toolkit runs. Both are recorded rather than reconciled, because the
disagreement is real and a reader meeting it deserves to know.

## 12. The reviewer's pass, round seven: what the plot showed

Round six closed the findings the tools could see. The reviewer then sent
thirteen screenshots with circles drawn on them, and the circles fell into two
groups: **red**, strings printed through each other on the schematics, and
**blue**, copper that leaves a pad, travels three sides of a rectangle and
arrives a few millimetres away.

Neither group had a rule. `readability.text_over_wire` measured text against
*nets*, and nothing measured text against text; `route.detour` weighed a whole
net, and a net with six good connections and one bad one averages out under
any ratio worth setting. So both got one.

### What went into the tool

| built in | kind | what it catches |
| --- | --- | --- |
| `readability.text_over_text` | rule | two printed strings whose extents overlap, or a string printed across a symbol body. The netlist does not change, KiCad does not complain, and the only way to see it is to look at the plot - which is what this does arithmetically |
| `route.wander` | rule | one continuous run of copper - pad or junction at each end - longer than `wander_ratio` times the shortest way between those two ends that clears the packages in between. Where `route.detour` asks whether a *net* is long, this asks whether a *track* goes out and comes back |
| the wander baseline | mechanism | a run from one end of a package to the other cannot take the straight line, because the straight line is through the package. The baseline walks the perimeter of whatever the line crosses, so a feedback wrap is measured against going round rather than against going through |
| `Label.angle` / `Label.justify` | parser | which way a net label reads. Without them a label's extent was a guess, and half of them were guessed backwards |

### What the tool then made us fix

| fixed | how | measured |
| --- | --- | --- |
| net labels printed through the part next door | a label's anchor cannot move - it joins the net by sitting on the wire - but the direction it reads in can. Each name is now offered the four quarters of its anchor and takes the first that prints over nothing. "Away from the pin" is still the first choice, but a five-character name on a 2.54 mm stub is twice as long as the stub, and away regularly meant straight into a diode | `text_over_text`: buck 1, motor 2, carrier 1, fpga 7 - **all to 0** |
| four grounds printing GNGNGNGND | a power symbol's name had a fixed offset and no collision check at all. Four grounds hanging off one row of pins put four "GND"s on one line a pin pitch apart. The name is now offered the row below and either side of the stem | the seven overlapping pairs on the FPGA sheet, to 0 |
| `PWR_FLAG` printed across the module it declares | the flag's own name checked wires and nothing else, so on the carrier it landed on the forty-pin module. It now checks symbol bodies and the names already placed, and climbs a ladder outwards - on that sheet the nearest clear air is three text rows away, because the strip beside the module is its pin legend | carrier 1 → 0 |
| a plane crossing priced as a prohibition | at forty times the front-side cost per millimetre, one millimetre of crossing buys a forty millimetre tour, and the boards had grown them. Thirty is where neither `route.wander` nor `route.return_path` fires: below it the router takes short back-layer hops that cut the plane under the same net's own front copper | opamp `+5V` 4.4x → gone; buck, motor and carrier to **0 wander findings** |
| copper straightened off its own pads | `write_variant` resolves the routes a second time, so the straightening pass ran again on the *routed* design - where the run into a pad and the run out of it have been merged into one polyline with the pad as an interior corner. Straightening through it took the copper off the pad. Pads are pinned now, as track ends already were | opamp: 2 unconnected nets, to 0 |
| stitching vias measured as circles | the stitcher kept a via clear of a track by its radius; `check_board` measures the same via as a square, and the corner is 0.17 mm nearer. One board would not build | opamp: 1 short, to 0 |

### The corners the exemption was hiding

The reviewer then said the 135 degree bends and the odd-angle runs were still
there. They were, and the rule could not see them: `route.acute_angle` skipped
any corner **within a pad's radius**, and a 0805's radius is 0.47 mm, which
covers the five or six ordinary corners a chamfered pad entry leaves inside
it. Eleven of the thirteen hairpins on these five boards landed in that
shadow, because a pad is exactly where a route that has to double back does
it.

| fixed | how | measured |
| --- | --- | --- |
| the exemption itself | it is the pad's *connection point* now, not a disc around it. Two branches leaving one pad are still exempt - the pad's own copper fills the wedge between them, so it is not an acid trap - but nought degrees never is: that is one run drawn twice and no pad excuses it | 13 hairpins, of which the rule had been reporting 2 |
| one run drawn on top of another | the shorter is inside the longer and carries nothing it does not. Both get trimmed: dropping only the duplicate leaves the other hanging over the gap, which is `route.stub`. It runs before the runs are joined, while the pair is still two tracks sharing an end | 180 degree reversals: fpga 2, filter 3, carrier 1 - **all to 0**, and the motor board's `return_path` finding went with the 11 mm of copper it was measuring |
| straightening onto no grid at all | the round-trip remover from the last round would replace a stated route with its direct line whenever that was clear - and between two pads at whatever coordinates their packages give them, the direct line is usually at no angle anyone drew. It made a nine millimetre run at 169.7 degrees on the FPGA board | off-grid segments: 1 → **0**, and 0 on all five |

Three corners survive, all on the FPGA board, all where a knee lands a
fraction past the point its own two legs cross: legs of 0.07 and 0.25 mm on
two of them, 0.15 and 0.5 mm on the third. Declining to take a knee that short
is worse - the segment then keeps the angle it had, and measuring that put 157
off-grid segments back across the five boards - so the nub stays and the rule
reports it.

### What is still there

The FPGA board is not fixed. It carries `route.detour` (VCCPLL at 5x),
`route.return_path` on seven signals and `route.wander` on four runs, and one
DRC unconnected item. Every one of them is a floorplan question on a 48-pin
QFN with two layers, and each attempt at it costs the better part of an hour
of routing; they are recorded here rather than waived. The opamp board keeps
one wander finding: every placement tried moves it between `+5V` and `OUT`
without removing it, which is what a congested two-layer board looks like when
the measurement is honest.

## 13. The reviewer's pass, round eight: the boxes were the wrong boxes

Round seven took `readability.text_over_text` to zero on all five sheets and
called the red circles closed. Re-rendering the plots said otherwise: an LED
with its ratings printed across its own arrows, a note running through a
capacitor's designator, "50ppm" and "GND" printed on the same line, a
sentence lapping the title block.

The rule reported none of them, and for one reason each time - it was
measuring a rectangle that is not the one on the paper.

### What went into the tool

| built in | kind | what it catches |
| --- | --- | --- |
| `Symbol.outline` / `Symbol.body_bbox` | parser | the shape KiCad actually draws. A schematic embeds the full library definition of every symbol it uses, graphics included, so the outline is knowable from the file alone. It had been guessed from the pins, and an LED's two pins span 2.54 mm while its emission arrows reach 4.6 mm the other way - which is why a value "cleared of the pins" printed through the part |
| `Symbol.property_angle` / `sch_review._field_box` | parser + rule | which way a field reads. KiCad adds the symbol's own rotation to the field's, and where the sum is half a turn it keeps the glyphs upright by swapping the justification instead. Every rotated part's fields were therefore measured on the opposite side from the one they print on - the check and the plot were looking at different rectangles |
| notes in `readability.text_over_text` | rule | a design note printed through a designator, a value or another note. The rule had compared fields with fields and fields with symbols, and left the third kind of string on the sheet out of it |
| `readability.margin_intrusion` measured by extent | rule | a note that *ends* on the title block. It had tested the anchor point alone, and the sentence that ran into the carrier's title block started 12 mm clear of it and was 67 mm long. The block's own geometry was wrong too - 110 mm wide inside a 10 mm frame and 44 mm tall on a sheet that fills its comment rows, not the 115 by 30 the rule assumed. On KiCad's demo corpus this takes it from 1 sheet and 5 findings to 2 and 7 |
| a 1.9 mm text row | rule | two strings one pin pitch apart. The row had been measured at 1.6 mm, which reads 1.27 mm of separation as a third of a millimetre of overlap - inside the tolerance, and on the plot one unreadable word. KiCad stacks a part's own fields on a 2.54 mm pitch, and 1.9 mm is what separates those two cases |

That last change is why the corpus numbers moved: `text_over_text` now fires
on 15 of KiCad's 18 demo sheets rather than 14, and `text_over_wire` on 11
rather than 10 - but `text_over_wire` reports 602 findings where it reported
942, because the boxes that were on the wrong side of a rotated part have
stopped being counted. Both stay **info**, promoted to errors by the
`ai-generated` policy.

### What the tool then made us fix

| fixed | how | measured |
| --- | --- | --- |
| ratings printed through their own part | the generator measured the block against the pin column plus 2 mm. It reads the same library outline the rule reads now, and a part's own body is on the list its designator and value have to miss | buck's D2, the carrier's D1 and D3, the motor board's D2 and C3, the opamp's R1/R2/C3/C6 |
| a lying part's ratings placed blind | only the *upright* branch searched for clear paper; a part on its side stacked its ratings 5.08 mm under its body whatever was there. Under the FPGA board's oscillator sits its own ground symbol, so "50ppm" printed through the word GND. The flat case now asks the same question - which row, how far left or right | fpga: `X1 Tolerance over #PWR20 Value`, to 0 |
| every rotated part's fields on the wrong side | the generator picks a side and then writes the justification KiCad will render, flipping it where the symbol's rotation flips it. Placing text against a rule while writing a file the rule reads differently is not placement at all | all five sheets |
| a note through the circuit, and through the title block | notes are emitted last and are the string with room to move: a field is anchored to its part and a label to its wire, but a sentence only has to be near its subject. Each block now slides to the nearest clear paper within a centimetre, measured against the symbols, every string already placed, the wires, and the title block | motor: the bridge note off D2; fpga: the bank note off C12; carrier: the plane note out of the title block |
| a note ending on the title block | the carrier's plane note is fifty-eight characters starting in the right-hand column: 67 mm of sentence with 55 mm of paper beside it. No amount of sliding fixes a line wider than the space left for it, so the anchor moved to the left column - which is the case the slide *cannot* handle, and worth saying so | carrier: `margin_intrusion` 2 → 0 |
| a designator with a wire either side | six candidate spots above a two-pin part are none at all when both flanks carry a net. The ladder reaches outwards now, and a `PWR_FLAG`'s name ladder was half duplicates - it read `justify left` for the spot to its left, which puts the same box back over the flag | `text_over_wire` on the reviewed sheets: 8 → 0 |

The fixture moved too. `tests/fixtures/example_project` had `10k` printed
down its own resistor and `100n` on both capacitors' plates - three collisions
that had been there since the fixture was written and that nothing could see.

### Where the five sheets stand

`readability.text_over_text`, `text_over_wire` and `text_over_symbol` all
report **0 on every `reviewed/` sheet**, measured against the drawn outlines
rather than the pin boxes. The one collision left anywhere in the set is on
`opamp-filter/as-generated`, where two net labels print through each other -
which is the variant whose job is to be wrong.

## 14. The reviewer's pass, round nine: the copper the plot showed

Round eight closed the schematic side. The reviewer then said the artwork was
not fixed either, and re-rendering the boards agreed: three defects were
plainly visible and no rule reported any of them.

### What went into the tool

| built in | kind | what it catches |
| --- | --- | --- |
| rip-up and reroute for tidiness | mechanism | `_route_all` now reports what each track cost against `route.wander`'s own baseline - imported rather than copied, because two implementations of one measurement drift - and the worst tour goes to the front of the order and the set is routed again. A track that still tours from first pick has nowhere better to be and is left alone |
| shortest first | mechanism | the default routing order. A thirteen millimetre connection has few ways to be made and a forty millimetre one has many, so the short ones should choose while there is still room. It is also what a fresh clone starts from, having no learned order |
| `layout.zone_outside_outline` | rule | a zone drawn wholly off the board. KiCad stores a *footprint's* zones in board coordinates while everything else in a footprint is relative to it, so a placer that moves the pads and forgets the zone leaves the keep-out where the library drew it |
| `silk.text_over_text` | rule | two silkscreen strings on the same side printed through each other. The schematic has had `readability.text_over_text` for two rounds and the board had nothing, though the board is the harder case: a sheet can be zoomed and a bare board cannot |

### What the tool then made us fix

| fixed | how | measured |
| --- | --- | --- |
| a track that tours the board to cover thirteen millimetres | the op-amp's feedback wrap goes from one side of a SOT-23-5 to the other. Routed last it took fifty-six millimetres, because everything nearer was already spoken for. Routed first it costs nothing | opamp `/OUT` 56.5 mm at 4.3x → **gone**; the board goes from FAIL to **PASS** |
| a keep-out at the origin | the Pico module's two pad keep-outs had been at (0, -6) for four rounds - off the board, keeping nothing out. DRC is silent because an empty region violates no rule, and the only visible sign was the plot: "fit to page" fits the bounding *box*, so every view of that board came out at half scale in one corner with the rest blank | carrier: 2 → **0**, and the board is legible in the documentation for the first time |
| silkscreen printed through silkscreen | the connector legends are placed before any designator and the designators get out of *their* way - a legend names one pin of one connector and has to sit against it, while a designator can go anywhere legible. Same order the schematic side uses for a label and a field. A pad is weighed fifty times a courtyard, so reaching further out along a row never buys ink on copper | `silk.text_over_text` 3 → **0** and `silk.over_pad` 0 across the five; the fixture had "IN GND OUT" printed through "J1" |
| the FPGA board | eleven rip-ups and three re-orderings later: `route.detour` on VCCPLL at 5x **gone**, `route.wander` 4 → **2**, acute corners 3 → **2**, `route.return_path` 7 → **6**, and the last DRC error - one unconnected item - **gone**. Board findings 1/10/8 → **0/9/8** | 5 blocking → **3** |

### What re-ordering cannot do, and what happens when it tries

Promoting a wandering net to the front takes a lane some other net had. On the
FPGA board the sixth promotion left `I2S_SCK` with nowhere to go, and the
first version of the loop called that an impossible floorplan and refused to
write the design at all - a generator that had worked now exited with an
error.

Feasibility is the hard constraint and tidiness is not. The loop keeps the
last order that routed everything; when a promotion makes a net unroutable it
goes back to that order, stops chasing tours, and writes the board with the
tours still in it. Routing is deterministic in the order, so the restored pass
is the one that already succeeded and the loop terminates. The learned order
file records rip-ups only: a rip-up is knowledge - that net has to go early or
it has no room - while a promotion for tidiness is a guess, and writing those
down poisoned the file for the next run.

### What is still there

Six nets on the FPGA board run over cuts in the back-side plane, two runs
still wander and two corners are still acute. The `return_path` six are the
two-layer choice itself: a 48-pin QFN with no inner layer has to bring the SPI
bus out somewhere, and re-ordering cannot make a plane that is not there.
They are recorded here rather than waived.

## 15. The reviewer's pass, round ten: learn from the boards people drew

The reviewer's brief for this round was direct: relying on the autorouter and
the numbers alone is not enough - go and organise what *pictorial*
correctness looks like from ordinary circuits and open designs, and improve
the drawing itself. So this round started not in the generator but in KiCad's
own demo corpus: sixteen parsable human-drawn boards, measured and looked at.

### What the corpus says

[`tools/board_signature.py`](https://github.com/sabas0ba/kicad_skills/blob/main/tools/board_signature.py)
(new) reduces "looks autorouted" to five numbers per board. Hand-routed
two-layer work clusters tightly: 10-47% of copper on the second face, median
segments of 1.8-3.5 mm, 9-25 corners per decimetre, 91-98% of them 45s.
Rendering `interf_u` beside our boards made the same point visually: a layer
has a direction, a bus travels as a bundle in one corridor, and a person
covers an offset with two strokes - the long straight and one diagonal.
Against that baseline the generated boards read as machine work for exactly
three reasons: everything on one face, the router's 0.25 mm cell as the
drawing's rhythm (op-amp median segment 0.75 mm, 38 corners/dm), and a
uniform via carpet.

### What went into the tool

| built in | kind | what it does |
| --- | --- | --- |
| `tools/board_signature.py` | tool | the five numbers, runnable over any mix of demo and generated boards, so the comparison is repeatable rather than an impression |
| `Router.route(follow=...)` | mechanism | cells beside a sibling net's path are discounted, so a bus (nets sharing a name prefix - `I2S_*`, `SPI_*`) travels as a bundle: the parallel-lanes look wins every tie, and the discount is a fraction of a step so it never buys a detour |
| `_doglegged` | pass | redraws every wiggly stretch as the human stroke - the straight along the dominant direction plus one 45 - when the dogleg is clear, splitting the stretch in half recursively where a crossing blocks the whole. Endpoints never move |
| the copper oracle's `pinned()` | fix | reshaping passes may not move copper off any point *inside* a pad of its own net - the centre alone was not enough, because a routed run can touch its pad off-centre and the overlap is the connection. Found by the pass pulling C4's connection off by a hair: one DRC unconnected item, invisible in the shape |
| seam guard | fix | a dogleg meeting the copper it did not redraw can turn back on itself, and the chamfer pass carves that seam into a 45-degree acute corner. Junction turns are limited to a right angle, and a split is taken only when both halves redraw |
| purposeful stitching | mechanism | the 6 mm via carpet is gone. A hand-stitched board puts vias where the plane needs them - a ring at the rim, and beside every place a signal crosses on the back layer, because that is where the plane is cut - and only then a coarse 12 mm mesh so no orphaned pour floats. Vias per decimetre: buck 41 → 18, carrier 28 → 13, all five inside the corpus band, and `layout.pour_fragmented` still reports nothing |
| the oracle updates as it goes | fix | the clearance oracle was a snapshot, and a pass that redraws two tracks against a snapshot lets each move into the corridor the other just left - I2S_DIN and I2S_LRCK both doglegged into one lane and ended 0.03 mm apart. Every accepted redraw now goes straight back into the oracle |
| `route_keepout=("U4", "U2")` | design | no foreign copper under the boot flash or the DAC. Fencing U4 alone just moved the 1.2 V rail under U2, which is the worse place - the DAC is the one analogue part on the board. `route.under_package` is the rule that kept saying so |
| `_net_of` in the pcb parser | fix | KiCad also writes the name-only `(net "VCC")` form - the pic_programmer demo does - and reading the name as a code crashed the whole board. The corpus grew from 15 parsable boards to 16 |

### Measured against the corpus

| board | med. segment | corners/dm | vias/dm | verdict |
| --- | --- | --- | --- | --- |
| motor-driver | 1.77 → 1.95 mm | 18.5 → 17.5 | 13 → 8.5 | in the human band |
| opamp-filter | 0.75 → 0.88 mm | 38.0 → 33.5 | 13 → 10.5 | direction right; the densest board in the set affords the least redrawing once every dogleg must clear DRC with a full cell of margin |
| buck-5v | 5.75 mm | 6.2 | 41 → 18 | already past the human band - six parts in a row |
| pico-carrier | 6.31 mm | 7.3 | 28 → 13 | likewise |
| fpga-audio | 1.43 → 1.54 mm | 24.8 → 17.8 | 13 → 7.8 | in the human band on every style measure; the floorplan debt below is a different axis |

All four still pass their gates with KiCad's own DRC, zero blocking. The
first version of the pass scored better - op-amp at 1.50 mm and 23.5/dm -
and was wrong twice: it pulled a connection off a pad (the `pinned()` fix)
and it parked a redrawn segment at exactly the 0.2 mm clearance limit, which
DRC fails by the width of a rounding error. The honest numbers are the ones
above, and the two failures are why the oracle now demands a full router
cell of margin.

### The FPGA board: two fences and an honest trade

`route.under_package` kept reporting the 1.2 V core rail under first the boot
flash and then the DAC - the two bellies a rail sneaks through when the
escape field has taken everything else. Fencing both (`route_keepout`) is the
right call and the board pays for it: the rail now hauls 131 mm to cover
40 mm, `route.detour` is back on +3V3 at 4.3x, and the gate holds four
blocking findings (2 acute corners at the pour edge, the detour, 4
`return_path` nets, 4 wander runs). What this board needs next is not more
re-ordering - the rip-up loop rolled back twice trying - but a *stated* 1.2 V
spine: the designer's power backbone, written into the file with its own
waypoints the way the motor board states its VM link. That is the next
round's work, and it is named here rather than waived.

### What this round is really about

The seven rounds before this one built rules: each finding became a number
and the generator chased the number. This round built a *reference*: the
drawing habits of people, measured from their boards, stated in the
authoring guide ("The numbers behind the look"), and pushed into the
generator as habits rather than penalties - travel with your bus, draw long
strokes, keep a direction per layer. The difference shows in what happened
to the op-amp board: its number barely moved, and the plot still got
calmer, because the strokes that did redraw are the long ones the eye
follows first.

## 16. The reviewer's pass, round eleven: "are the other checks really passing?"

The reviewer looked at the plots, saw tracks driven straight through each
other, and asked the only fair question: with crossings that blatant, are
the other checks actually passing?

They were, and both halves of that are worth writing down. Measured
directly - every pair of same-layer segments on every reviewed board -
there is not one crossing between *different* nets: KiCad's DRC reports
zero errors, zero shorts, zero unconnected items on all five, and the DRC
is not being fooled. What the plots show is thirteen places (FPGA), one
(op-amp) and one (buck) where a net crosses **itself**. Same potential, so
DRC has nothing to say; no length ratio catches it, because the loop can be
short; and no rule of ours looked. The checks were honest. The check *list*
had a hole exactly the shape of what the eye catches first.

### What went into the tool

| built in | kind | what it does |
| --- | --- | --- |
| `route.self_crossing` | rule | a net whose own copper crosses itself on one layer, warning, promoted by `ai-generated`. On KiCad's demo corpus: 6 of 16 boards carry one to three, at dense escapes - ours carried thirteen on one board |
| `_unlooped` | pass | the fix, as a graph question: split every same-net crossing so the X is a node, and while any cycle remains in the net's copper, remove the longest junction-to-junction chain of the cycle. Connectivity is kept by definition - a cycle has two ways round - and the amputated X is left as an ordinary corner. The pour net keeps its mesh |
| the oracle refuses new X's | guard | `_doglegged` and `_straighten` may touch their own net's copper - that is a junction - but not cross it, or they would redraw the loops the cutter just removed |

### What the cutter got wrong twice before it was right

Both failures were the same lesson: **KiCad's connectivity is geometric,
a track graph is endpoint topology, and the difference is exactly a pad.**
The escape fan draws a deliberate micro-hook *inside* an off-grid pad -
copper overlapping copper is the connection - and the graph saw a dangling
loop feeding nothing, called it redundant, and amputated a pad's only feed:
one DRC unconnected item per pad, invisible in the shape. The cutter now
treats any node inside a pad of the net's own as an anchor, refuses to
touch a cycle that sits wholly inside one pad, and splits segments that
pass over a pad so the feed is a node the cut has to respect. The DRC run
that verifies all this is the point of the reviewer's question.

### Where it landed

Self-crossings on the five reviewed boards: **0, 0, 0, 0, 0** (were 13, 1,
1, 0, 0). Cutting the loops removed real copper, and the FPGA board got
lighter for it: `route.detour` on +3V3 retired outright, `route.wander`
4 → 3, `route.return_path` down to 3 nets, and KiCad's DRC is clean again -
zero errors, zero unconnected. Its gate is down to three blocking findings,
all floorplan, all named in round ten's "what this board needs next".

## 17. The reviewer's pass, round twelve: the circle on C15

The reviewer circled one spot on the FPGA board's front render — a cluster
of 45-degree tracks converging on one 0402 — and asked whether the judging
is buggy, or missing a fundamental viewpoint.

Measured, the spot was electrically blameless: every track in the circle is
the same net (the 1.2 V rail), nothing crosses, DRC is clean, and no rule
had anything to say. The first suspicion — that a pad being used as a
junction is itself the defect — did not survive contact with the corpus:
counting pads that carry three or more track arms across the 16 parsable
KiCad demo boards finds them everywhere people route by hand (10 on the
Jetson carrier, 95 on the VME board). Humans tee on pads freely. That is
not the missing viewpoint.

The missing viewpoint was one level down, in the router's contract: **a
link was only allowed to finish on the pad it names.** A net with three
branches therefore funnelled all three into one capacitor pad — the
junction could not form anywhere else, however awkward the convergence,
because nowhere else was a legal place to stop. People do not route under
that constraint: they tap the trunk at the nearest point, and the junction
lands where the geometry is shortest.

### What went into the tool

| built in | kind | what it does |
| --- | --- | --- |
| `tee` in `Router.route` | router | the search may finish on any cell whose centre lies exactly on the net's own already-laid copper, not only on the named pad; the junction then forms wherever is nearest |
| `_tee_component` | guard | only copper already electrically joined to the link's endpoint counts — through shared points, the net's vias and the net's pads — or the named pad is left unconnected and only DRC would notice |
| pad-named endpoints only | guard | a link aimed at a bare coordinate is aimed at the stated end of a trunk someone drew, and stopping short of it strands the trunk's tail as a stub; a pad is a terminal in its own right, so copper between a tee landing and a pad still ends somewhere real |
| `_absorb_tee` | pass | a landing mid-segment is inserted into the trunk's points as a stated corner, so every reshaping pass pins the join the way it pins any other track end |

Each guard cost one broken board to learn: the first regeneration left a
0.5 mm stub of the op-amp's 5 V trunk hanging in air, because a link had
teed onto the trunk half a millimetre before the stated coordinate the
trunk was drawn to end on.

### The stated 1.2 V spine

Round ten named the FPGA board's next work: state the 1.2 V rail the way
the motor board states its VM link. The tee is what makes a stated spine
worth having — branches can tap it anywhere — and the corridor came from
looking at what the router had been doing instead: its consumers sit on
both sides of the FPGA, every east-west lane south of the package is a
comb of SPI escapes, and the link-by-link answer was a 122 mm tour of the
board's south edge to cover 39 mm. The one corridor nothing else can use
is under the FPGA's own die: the QFN's pads are surface copper, the strip
between its south pad row and its ground-via grid is empty on the back,
and the rail is the package's own supply, so nothing runs under a part it
does not feed. One straight stroke on the back, a via at each end, and the
links tap it.

### Where it landed

The circled defect is gone as a class, not as an instance: on the
regenerated FPGA board C15 is fed by one arm, the junctions sit on the
trunks, and no small pad on any of the five boards carries more than three
arms - which is where the human corpus sits too. The tee also made the
boards lighter: the op-amp board lost 12% of its copper, the FPGA board
15% (1860 mm down to 1583 mm), because a branch that may stop at the
nearest trunk no longer duplicates the trunk's own distance. Crossings
stay at zero on all five, KiCad's DRC stays at zero errors, and the four
boards that passed their gates still pass them with the same waivers.

The FPGA board also finally got the spine, and the round shows why the
order of those two things mattered: regenerated with the tee alone, the
rail still toured (122 mm for 39) and the signals it displaced put seven
nets over plane cuts - worse than the three the board started with. The
spine reclaimed the corridors: `route.return_path` is back to three nets
(all SPI, 11-28 mm), `route.wander` no longer names a rail at all (two
signal detours remain, 2.1x and 2.6x), and `route.acute_angle` holds two
45-degree corners at the pour edge plus one folded corner the clean-up
passes have not learned to unfold. Three blocking findings, all
floorplan-class, all still stated by the gate rather than waived - the
honest price of a QFN-48 on two layers, now without the tours that used
to sit on top of it.

Stating the spine took three broken boards of its own, each caught by
`check_board` before anything was written: a through via parked in the
west escape comb lands on whichever escape line owns that lane; at
y = 42.5 the east via missed the QFN's south pads by a tenth of a
millimetre; and the die centre belongs to the exposed pad, so the east
end may not drill at all - the stroke and its tap meet on the back
instead, with no layer change. The corrected stroke sits at y = 42.25:
a quarter-millimetre off the pads' inner ends, and still on the router's
quarter-millimetre grid - off it, no tee could land on the stroke, which
would have defeated the reason it exists.

## 18. The reviewer's pass, round thirteen: the plane is a drawing too

Six circles this time, five of them about the same thing seen from
different corners: the ground plane is part of the drawing, and the
generator treated it as leftovers. A wedge of pour tapering to a point in
a 45-degree corner on the motor board; the op-amp board's 5 V feed running
outside its own shell, the outermost copper on the board with no return
beside it; the carrier's top edge a blank strip and its back plane sliced
into panels; the FPGA board's bottom band and line-out pocket fenced off
by the SPI bundle; and - the sixth - the FPGA sheet's power-entry block
printing over the frame's ruler strip.

### What went into the tool

| built in | kind | what it does |
| --- | --- | --- |
| `_pruned_tongues` | fill pass | a dead-end strip of pour narrower than `ZONE_TONGUE` (0.9 mm) retracts until the plane is wide again - a narrow *channel* touches neighbours on two sides and stays; a strip feeding this net's own pad or via stays too. The acid-trap wedges in bent corners go away as a class |
| pour to 1.2 mm of the edge | design | the pour rectangles stop 1.2 mm inside the outline instead of 2-3 mm, so an edge-hugging trace keeps shell copper *outside* it and the board's outermost feature is ground again |
| per-piece stitching | `_stitch_vias` | every piece of the front pour over 8 mm² holds a via of its own, placed against the same clearance checks as the mesh - and the pieces are taken *before* the orphan drop, so a strip nothing else reached gets a via instead of staying a blank |
| `ZONE_CLEARANCE` 0.4 → 0.25 | fill | at 0.4 the web between a 2.54 mm header's pads came to 0.34 mm - under `ZONE_SLIVER`, so every column became a full-height slot. At 0.25 the web is 0.44 mm and the plane flows between the pins. DRC clearance on these boards is 0.2, so nothing legal got closer than allowed |
| tee stays 2 mm off the pad | router | the round-twelve tee moved junctions off the pads - and promptly fed the carrier's bulk capacitor through a one-millimetre stub from the rail. Within two millimetres of the goal a tee saves nothing and costs the flow-through, so there the pad wins |
| `readability.margin_intrusion` reads fields | rule | the rule measured pins and notes but not symbol fields, and power symbols not at all - and the FPGA sheet's PWR_FLAG printed its name across the ruler strip. Fields are text like any other now, power symbols included. Seven findings across KiCad's 18 demo projects - people do park a label on the frame here and there, which is why the rule stays `info` and only the `ai-generated` policy promotes it - and three real catches on our own sheets the moment the rule could see: the motor board's power connector, and two more corners of the FPGA sheet |

### What stayed by hand

Two of the circles were placement, not machinery. The carrier's 22 µF
bulk capacitor sat a millimetre south of the VSYS run, so the rail passed
straight by and fed it through a stub - it now sits *on* the run, current
in one pad-side and out the other, no stub at all. And two power-entry
connectors (the FPGA board's and, once the extended rule could see, the
motor board's too) moved eight millimetres in from the sheet edge so
their printed names clear the frame.

One cut stays, stated rather than hidden: the carrier module's own pad
columns still slot the back plane top to bottom. The module footprint
carries a 2.2 mm unplated pad behind every castellation at 2.54 mm pitch,
which leaves negative room for a web at any legal clearance - that is
what soldering a forty-pin module onto two layers costs. The panels it
makes are tied along the full top and bottom bands the wider pour now
reaches, and the middle panel carries the module's eight ground pins
straight into the plane.

### Two more circles while the paint dried

The reviewer's next pass found the hook and the step (round thirteen's
last two circles, on the op-amp board): two runs of the reference rail
down the same lane - the second *rode* the first, because a net's own
copper costs the search nothing and nothing forbade travelling along it -
and a width that changed from 0.5 to 0.3 mid-run, where power-width links
met a 0.3 mm escape. The ride is now priced (crossing own copper stays
cheap, travelling along it never wins), VREF runs 0.3 end to end with a
waiver stating its current, and the divider's 5 V feed keeps the trunk's
width to the junction. Measured after: zero doubled runs and zero mid-run
width steps on all five boards.

### Where it landed

All four fast boards pass their gates. The FPGA board still does not, and
its three blocking findings have a single name now: `route.return_path`
holds four SPI nets at 11-20 mm over plane cuts (the worst used to be 27),
`route.acute_angle` two corners, and `route.wander` three detours all on
the 3.3 V rail - the twenty-link net that wants the same stated spine the
1.2 V rail got. That is the named next work. Everything else measured
clean across all five: zero crossings, zero rides, zero mid-run width
steps, every pour piece over 8 mm² holding its own via, and the planes'
outer shell reaching to 1.2 mm of every edge.

## 19. The reviewer's pass, round fourteen: the fold, correctly this time

The reviewer accepted the round and re-reviewed - with one correction.
Round thirteen read the two circles on the motor board as pour wedges and
pruned the pour; the circles were about the *bends*. Each circled corner
is a legal 45 on its own; the pair of them - a 90 and a 45 with a tenth
of a millimetre between - turns the run 135 degrees from its direction of
travel, folded into half a millimetre. The angle rule is structurally
blind to it: it measures one corner at a time, and every corner passes.

### What went into the tool

| built in | kind | what it does |
| --- | --- | --- |
| `route.hairpin` | rule | two same-direction corners within 1.2 mm of track whose signed turns sum past 100 degrees. Signed, so a staircase's alternating 45s cancel; arms shorter than 0.8 mm are a clearance artefact skirting a via, not a legible fold; a fold whose middle sits inside its own pad is the escape fan's micro-hook and stays. Six findings across KiCad's 18 demo projects - info, promoted only under `ai-generated` |
| `_spread_hairpins` | pass | the generator's answer: the fold's middle leg stretches to 1.2 mm and the exit re-doglegs onto the far end of the outgoing straight. Aimed at the first vertex it folded straight back - the exit carries a redundant collinear point half a millimetre out, and the first attempt found it the hard way |
| stitch vias stay inside the pour | guard | a candidate offset from a track end can land past the pour and onto the outline itself - two did, one dead on the board edge - where it is an edge violation and an orphan at once. `check_board` cannot see either; KiCad's DRC caught both, and the candidate filter now owns the bound |

The motor board's two circled folds are now wraps: the same 135 degrees,
drawn as spread 45s over two millimetres, which is how a person turns
back. All five boards measure zero hairpins, zero crossings, zero doubled
runs, zero mid-run width steps.

### Where the FPGA board stands

Twice this round the FPGA board was re-routed from a cold cache, and the
two draws tell the story of what the rip-up loop's learned order was
worth: the first came back with one bad net (the line-out pair touring at
4.3x, `route.detour` and `route.wander` both naming it) on top of the
standing SPI-over-cuts debt; the second came back worse - four tours, a
stub and an off-grid corner - and was discarded for the first. The
committed board is that better draw: zero DRC errors, zero hairpins, zero
crossings, and four blocking findings - three corners, one line-out tour,
five SPI/CRESET nets at 11-19 mm over plane cuts, and the tour again as
`route.wander`. The 3.3 V spine and a floorplan that gives the line-out
pair a corridor stay the named next work.

The reversal of fortune is worth writing down: the environment reclaimed
the container twice during this round, and each reclaim rolled the
working tree - and the route cache with it - back a day. The code came
back from the remote in minutes each time; the FPGA board's *route* did
not, because a route is an afternoon of rip-up learning keyed to code
that no longer hashes the same. The five commits of history survived
because every one of them was pushed the moment it existed. The lesson is
already this repository's working agreement; the round is what enforcing
it looks like.

## 20. The reviewer's pass, round fifteen: the arc, and the retraction

Two more corrections, both accepted in full.

The reversal is now a continuation of the escape, not a spread fold -
and it took three passes to hear the drawing correctly. The first arc
kept a lead stub out the old heading and took every 45 from there; the
stub meant the trace stepped back to nine o'clock after the escape's
own diagonal had already left that heading, and the reviewer had to
point at the bend twice. The final form has no stub: `_spread_hairpins`
starts the turn right at the head of the incoming straight - where the
escape's 45 already points the line - stands it up square, takes the
remaining 45s, and solves the equal leg length so the turn lands
exactly on the outgoing line; when the exit leg is too short to land
on, it carries the turn through the exit's own 45 and lands on the
straight beyond. On the motor board each of the two nets now draws one
continuous curve from pad to bus - nine o'clock, half-past ten, twelve,
half-past one, three - with the fold, its corners, and the
three-millimetre ride to the fan column all gone.

And the round-thirteen tongue pruning is reverted outright. It was built
on the misreading round fourteen corrected - the pour filling a bent
corner was never the complaint - and the reviewer called the leftover
retractions what they were: an incorrect fix left in place. The fill goes
back to what the sweep makes of the copper, the guide sentence that
recommended the pruning is gone, and the boards regenerate identically
minus the missing wedges.

Where it landed: all four fast boards pass their gates; every board
measures zero hairpins, zero crossings, zero doubled runs. The FPGA
board's fresh route reproduced its round-fourteen debt to the digit -
three corners, the line-out tour at 4.3x, four nets at 11-18 mm over
plane cuts - which after four independent cold draws looks less like
luck and more like the floorplan fact the gate says it is. The 3.3 V
spine and a line-out corridor stay the named next work.

## 21. The reviewer's pass, round sixteen: the hole in the land

> "パッドオンビアが多用されています。これは製造上の問題があるので避けてください"
> — via-in-pad is used a lot here; it is a manufacturing problem, please avoid it.

Six circles on the op-amp board, and the reviewer said plainly that the
board in the picture was only where they had noticed it. They were right
on both counts: eighteen vias across three of the five boards sat inside
a surface-mount land.

A hole in a land is a hole solder wicks down during reflow. The joint
above it starves, and nothing on the assembled board distinguishes that
from a cold joint - it is the failure that ships. Via-in-pad is a real
technique and a *process*: the barrel is filled with resin and plated
flat before the board ever sees paste. A layout that has not specified
that process may not draw it, and here nothing had.

The router was the source, and the mechanism is worth writing down
because it is the same shape as the tee bug two rounds ago. `_blocked`
skips obstacles belonging to the route's own net - it has to, or a track
could never reach its own pad - and the via placement test was built on
top of it. So a layer change was legal on the very land the route
started from, and a ground stub asked to reach the plane spent its via
without leaving the capacitor. Pads are now marked as pads in the
obstacle list, and a pad is a cell where a via may not go whoever owns
it. The escape leaves the land first and turns its via beside it, which
is the layout a hand would have drawn anyway.

The stitching pass had a smaller version of the same blind spot: it kept
a signal land a via-radius *plus clearance* away and a ground land only
a radius, on the reasoning that ground copper touching ground copper
harms nothing. Electrically true, and beside the point - the solder does
not know whose net it is wicking down. Both distances are the same now.

`via.in_pad` measures what is left: the copper gap from every via to
every land, its own net's included. The one exemption is the exposed
thermal pad under a package, where the via array *is* what the datasheet
asks for; nothing a single signal reaches is four square millimetres, so
the two are told apart by size. The rule blocks under the ai-generated
policy, because a generated board has no reason to draw a via in a land.

The test fixture had one too - twenty-five microns of overlap into C1's
ground land, invisible on any plot - which is the useful part of a rule
that measures rather than looks.

### What the ban cost, and what it bought

Forbidding the layer change on a land is a small rule with a large
consequence: the FPGA board stopped routing. Five runs, five different
nets, each one out of lanes in the corridor between the FPGA's east
escape fan and the codec's west one - and every one of those nets routed
on its own when asked, which is congestion looking for an order, not a
floorplan with no lane.

Three things fixed it, in the order a layout would do them. The via
spacing was guesswork and is now the fab's: hole to hole goes from
1.2 mm to 0.9, half a millimetre of laminate between barrels at the
shipped via, which in a 1 mm-pitch escape comb is the difference between
a lane and no lane. Every decoupling capacitor's ground via is now
*placed* against its own pad rather than nine of them asking the search
for one - and where it goes is chosen, not fixed: `anchor_site` takes
the nearest position that clears what is already there, fanning either
side of the direction facing off the part. The first cut used a fixed
1.2 mm offset and walled off the corridor its neighbour's supply needed;
the via was legal, and it was in the way.

Then the floorplan. Seven parts - four decoupling capacitors, the two
1.2 V ones and the LED resistor - were sitting in the eleven millimetres
between a twelve-lane escape and a ten-lane one. They move four
millimetres south into the empty band below, and the corridor carries
the bundle it was always for.

That last move paid a debt three rounds old. The line-out tour - 88 mm
of copper for a 20 mm net, `route.detour` and `route.wander` both naming
it, reproduced across four independent cold draws and written down each
time as floorplan rather than routing - is gone. Both rules report
nothing. The FPGA board's gate goes from four blocking findings to two,
and what is left is the two-layer bill it has always been: three SPI
nets crossing cuts in the plane, and three corners.

The lesson is the one the guides keep circling. A rule that forbids
something the router was quietly relying on does not just cost that
thing; it exposes what was propping the rest up.

## 22. The reviewer's pass, round seventeen: making it, not just routing it

> "ベタのクリアランスは考慮足りてないように思います。R/Cのあいだにまではいってしまっていて、クラアランス無視しているか狭すぎる。こらはDRCでも検出できると思っていましたが、未実施でしょうか"
> — the pour's clearance looks wrong; it reaches in between an 0603's pads.
> I thought DRC would catch that. Was it not run?

It was run. It was green. Both of those were true, and so was the
reviewer, because the check and the drawing were looking at different
polygons.

The fill in the file was ours - a sweep of the pour outline with
everything of another net subtracted - written years of rounds ago
because a comment here said KiCad's own filler needs a display the
container has not got. That was never checked again. `pcbnew`'s
`ZONE_FILLER` runs headless in both images and fills a board in a
second. Meanwhile `pcb drc` was passing `--refill-zones`, so KiCad
replaced our polygons with its own before checking and reported on a
board nobody had. Turn the refill off and the shipped fill has 199
pieces of copper KiCad calls isolated and reaches 0.075 mm from a
foreign pad where the rule says 0.25.

So the zone is declared and left empty, KiCad fills it, and `drc()`
stops refilling a board whose zones are already filled - that fill is
what goes to the fab and what the plots draw. Everything the sweep had
been approximating is now the board's own rules: the clearance, the
minimum web width, and the thermal relief the zone had been asking for
all along and our filler had been ignoring.

That last one was the reviewer's next point, and it arrived for free:
every through-hole ground pad now sits in a gap bridged by four spokes,
so an iron can bring the joint up without heating a hundred square
millimetres of plane. `layout.solid_pad_connection` reports a board that
turns it off.

The rest of the round is the same kind of work - the part of a layout
that has nothing to do with whether the circuit is right:

* **Teardrops.** The step from a 0.2 mm track to a 1.7 mm land is where
  copper cracks when a connector is levered on and off. Each run now
  ends in a three-step taper into the land. It *replaces* the end of the
  run rather than lying on top of it: the first cut drew the same copper
  twice and `route.acute_angle` reported all thirty of the nought-degree
  pairs, correctly. `route.mixed_track_widths` learned what a fillet
  looks like, so a filleted board no longer reads as one nobody decided.
* **Mounting holes.** Four M3 holes near the corners, placed *before*
  routing because they are obstacles - the first attempt placed them
  afterwards and found room for two. A corner with a connector body in
  it takes its hole a few millimetres along the edge; the motor board
  and the op-amp end up with three, which is what those floorplans have.
  Grounded on the motor driver, whose chassis is metal and part of the
  shield; plain elsewhere, because bonding the enclosure to the
  reference is the enclosure's decision and a ground loop is what the
  bond adds when the answer is no.
* **Fiducials.** Three copper dots in bare mask windows, near three
  different corners, so a pick-and-place gets rotation as well as
  offset. `fab.no_fiducials` reports a fine-pitch board without them.

Two bugs surfaced by placing parts at the edge, both older than this
round: a designator was kept off the pads but not inside the board, so
every edge part had its reference clipped; and `_courtyard_box` read
lines and rectangles only, while a mounting hole draws its courtyard as
a single circle - so the holes measured as taking up no room, and the
first three fiducials landed on top of three of them.

## 23. The reviewer's pass, round eighteen: room for the screw, ink on the board

Two things, from one screenshot of the opamp board's top-left corner with two
circles on it. One round the designator `H1`, printed above the board edge. One
round the screw hole itself, sitting against the screw terminal's body.

### The hole was placed against the drawing, not against the screw

A `MountingHole_3.2mm_M3` draws its courtyard as the drill plus a whisker:
1.8 mm of radius. What goes through it is an M3 pan head on a DIN 125 washer,
seven millimetres of steel lying flat on the board, turned by a driver that
wants more again. The placer avoided courtyards, so it put screw heads on
capacitors and, on four of the five boards, inside a connector's mating space:

| board | hole | reaches | clear | wanted |
| --- | --- | --- | --- | --- |
| fpga-audio | H1 | J1 | 0.42 mm | 2.5 mm |
| pico-carrier | H2 | J1 | 0.53 mm | 2.5 mm |
| motor-driver | H2 | J3 | 1.50 mm | 2.5 mm |
| buck-5v | H1 | J1 | 1.58 mm | 2.5 mm |

A connector is judged by what plugs into it. A screw terminal's wires leave
horizontally from its face and a barrel jack swallows a plug the size of the
jack again, so a screw beside either can only be driven before the cable goes
on - which, on a board that gets serviced, is never. A pin header is the other
case: the socket that fits it lands inside the outline the header already
draws, so it asks for nothing beyond the screw head's own room. Treating the
two alike is what left a carrier lined with headers with two holes on one edge.

Three rules say all of this now - `mechanical.fastener_clearance`,
`mechanical.connector_access`, `mechanical.fastener_copper` - and all three
block a generated design.

Making room cost a second round. The first fix kept the clearance and lost the
holes: the search slid along two edges from each corner and gave up, so boards
that had had four came back with one. It walks a ring round the board now, at
the inset the screw needs, taking the nearest free point to each corner - and
only from that corner's own quarter, because a search that asked for the
nearest free point anywhere put three of the four along one edge, each of them
the nearest thing to a different corner. Where a quarter has no room there is
no screw: opamp-filter's left edge is a screw terminal, a pin header and two
resistors standing 6 mm in, and two holes that hold the board flat are the
honest answer there.

### The ink was measured a fifth short

`H1` printed off the board for two reasons, and the second is the one that
mattered elsewhere too.

The first: a designator that found nowhere to go kept the library's position,
and a library puts a mounting hole's reference above the hole - which in a
corner is above the board. Every silk placement is scored now, over a candidate
set wide enough to have an answer, and the score counts ink past the outline as
the worst thing it can do. `silk.off_board` reports it, because KiCad's own
test measures ink against the *edge* and says nothing at all about a string
that clears it: ink past the outline is not trimmed, it is never printed, since
the panel is routed at the line and the designator leaves with the offcut.

The second: the placement measured a string at 0.78 of the text size per
character. KiCad's stroke font is proportional and runs 0.94 to 1.07, measured
on written boards with `GetBoundingBox`. A fifth under is the difference
between a legend beside a capacitor and a legend across its land, which is what
`ADC_VREF` was doing on the carrier - and what nothing caught, because the
generator's own check used the same short ruler as the placement it was
checking. `_text_extent` takes the top of the measured range and adds the gap
the fab wants; the review rule keeps its own short ruler on purpose, so it
reports overlap it is sure of.

### What came out of the pipeline behind them

Moving parts and re-routing three boards turned up three more:

* A 45 degree spike on the motor driver - out of a via, a quarter of a
  millimetre in the wrong direction, then back across it. `_unspiked` redraws
  any corner tighter than a right angle as the elbow between its neighbours,
  one diagonal and one straight, never longer than what it replaced.
* Two drills a millimetre apart on the opamp board: a via beside a plated pad
  of its own net, 0.15 mm of laminate between them at a 0.25 mm rule. The
  plating already joins the faces, so the via bought nothing. `_landed` slides
  the joint onto the pad and drops it.
* Landing a joint moves a track end, and a teardrop built before that points at
  where the run used to stop - a nought-degree corner. The fillets moved to the
  end of the pipeline.

### What was left, and what it is

Two of the FPGA board's findings survived the round and are waived with their
measurements in `gate.toml`: three nets running 10 to 19 mm over cuts in the
back plane, and one run that goes round at twice the straight line. They are
the same fact twice. A 48-pin QFN on two layers has one signal layer for its
pins to escape onto, so every pin that cannot get out on the front crosses on
the back, and every back-layer crossing saws the plane under somebody's return
current. The router already prices a millimetre on the plane side at thirty on
the front, and that number is a frontier rather than a preference: at forty the
crossings stop and the tours start, and `route.wander` fires instead of
`route.return_path`. Thirty is where the pair is smallest. What is left at
thirty is what a second signal layer would buy.

`_surfaced` was written for exactly this and asks the question once more at the
end - a short back-layer hop is offered the front against the *finished* board,
because the router priced it before the rest of the board existed. On these
five it lifts nothing: where the plane is cut, the front above it is full. It
stays in the pipeline because it costs nothing when it finds nothing, and the
next board may not be so tight.

## 24. The reviewer's pass, round nineteen: the tab, and the first edition

Two things, and the first is a manufacturing defect the review round before had
introduced without noticing.

### The plane drank U1's heat

Moving the fill to KiCad's own filler changed what `connect_pads thru_hole_only`
means. The generator had used it to mean "the plane ties on at the through-holes
and the surface pads keep the track they were routed with" - which is what its
*own* filler did. KiCad reads the same token as *Reliefs for PTH*: through-hole
pads get spokes and surface pads get **solid** copper. Nothing said so, because
the fill was correct by every rule the toolkit had.

U1 on the buck board is an LM2596 in TO-263, and its tab is 101.5 mm² on GND.
Tied straight into the pour, it reaches solder temperature after the part's own
five leads do, and the part lifts on the leads that got there first. The
reviewer saw it on the plot: a pad with no relief ring, merging into the plane.

The answer is not the zone. A chip capacitor's land reflows with the whole board
and a solid tie is the better electrical answer; the tab is a different object
that happens to be drawn the same way. So the decision is per pad, and the pad
carries it (`zone_connect`):

| pad | area | vias in it | connection |
| --- | --- | --- | --- |
| a 0805 land | 1.45 mm² | — | solid, as before |
| C1.2, C3.2 (electrolytic grounds) | 11.0 mm² | 0 | relief |
| U1.3 (TO-263 tab) | 101.5 mm² | 0 | relief |
| fpga U1.49 (QFN exposed pad) | 12.2 mm² | 9 | solid, and it already said so |

The last row is the whole reason the rule needs a second clause. A via array in
the pad is the board saying the copper *is* the heat path, and relieving it
would be undoing the thermal design. `layout.solid_pad_connection` now reports
the reflow case as well as the iron, with the same exemption.

Then the reviewer asked for the spokes to carry the current, not just release
the heat - "パスを少し多くするか太くしたい". KiCad draws four spokes and offers
no way to ask for more, so the answer is width. Each relieved pad now sets its
own `thermal_bridge_width`: half the widest track that reaches it, which puts
twice the track's own copper across the four, and for a tab with no track at all
- whose whole return current leaves through the plane - the widest the relief
still survives. Both KiCad versions in the matrix read the override back.

Lifting a back-layer hop to the front then produced two width steps where the
via used to be: a 0.2 mm hop between 0.4 mm runs, and a width change at a layer
change is a change nobody reads while the same change mid-run is
`route.width_step`. `_surfaced` widens a hop to match its neighbours when they
agree, and leaves it on the back when they do not.

### The middle column

The examples compared two variants, and the left one had quietly stopped being
a fair "before". Eighteen rounds of findings went into the *generator*, not into
patches on its output, so `as-generated` improved every round without anyone
reviewing it. The comparison understated what the review had been worth.

So each comparison has three columns now, and the leftmost is each design as it
came out of the generator the day it was written - recovered from this
repository's own history, one `git show` per file, rendered with today's
renderer so the only difference is the design:

| design | first edition | as-generated | reviewed |
| --- | --- | --- | --- |
| buck-5v | 45 blocking | 28 blocking | PASS |
| motor-driver | 43 | 37 | PASS |
| pico-carrier | 50 | 33 | PASS |
| opamp-filter | 33 | 32 | PASS |
| fpga-audio | 34 | 29 | PASS |

The distance between the first two columns is the review turned into code,
which arrives before anyone runs the gate. The distance between the second and
the third is what the gate still had to catch on the day.

## 25. The reviewer's pass, round twenty: floorplan before routing

The previous rounds taught the router to make poor placement look orderly.
That was useful, but it left the decision in the wrong place: once two parts
that must talk are thirty millimetres apart, no choice of 45-degree corners can
make the floorplan compact.

`layout.connection_span` now asks the question before copper exists. For each
non-ground net it collapses all pads on one footprint into one node, computes
the nearest pad-to-pad distance between every pair of footprints, and builds a
deterministic minimum spanning tree. Any edge over
`max_connection_span_mm` (25 mm by default) is a placement finding. The use of
a tree matters: a three-pin net needs two local connections, not every possible
pair, and two far-apart pads inside one package must not accuse the package of
being far from itself. The `ai-generated` policy promotes the rule to an error;
a long backplane can raise the threshold in its own policy.

Running that rule over the old reviewed baselines found 2 long logical hops on
buck-5v, 10 on motor-driver and 13 on fpga-audio. It found none on
opamp-filter or pico-carrier, which is also useful evidence: a rule intended to
judge placement did not merely rediscover dense routing everywhere. Rebuilding
the three floorplans took all 25 findings to zero:

| design | old baseline | rebuilt baseline | consequence |
| --- | --- | --- | --- |
| buck-5v | 126 × 56 mm, 459.30 mm tracks, 124 vias | 92 × 38 mm, 284.04 mm, 87 vias | input switch loop and output filter become one power-flow row |
| motor-driver | 88 × 50 mm, 654.06 mm tracks, 94 vias | 68 × 46 mm, four layers, 472.54 mm, 89 vias | supply capacitors share the package fan; logic header meets its lanes; In1 is continuous GND and In2 is VM |
| pico-carrier | 88 × 62 mm, 90 vias | 80 × 60 mm, 78 vias | edge clearances retained while unused perimeter is removed |
| opamp-filter | 58 × 42 mm | 58 × 42 mm | a smaller trial made the analogue feedback routing worse, so the honest optimum stayed put |
| fpga-audio | 100 × 84 mm, two layers | 76 × 58 mm, four layers | the QFN gets a solid In1 reference and In2 +3V3 plane; return-path and routing-tour waivers disappear |

The FPGA change is not a concession to the gate. It is the engineering result
the gate exposed. A four-sided QFN-48, codec and boot flash can be forced onto
two layers, but the resulting cuts in the only reference plane are not a good
teaching baseline. The rebuilt board spends two inner layers on an uninterrupted
ground reference and power distribution, leaving the outer layers for escape
and signals. One SPI clock lane crosses the In2 pour, still referenced to In1;
the +3V3 plane remains continuous around it rather than being mistaken for the
signal's reference.

That board also exposed one parser assumption: KiCad writes a through via as
`F.Cu B.Cu` on a four-layer board. Those are the barrel's endpoints, not the
only copper layers it reaches. The stub and via-in-pad checks now expand the
inclusive layer range; a regression test puts an In2 track into a through via
so the mistake cannot return.

The cold KiCad passes found final defects that a no-CLI review could not close.
Buck-5v's output electrolytic overlapped J2's assembly courtyard even though
their copper was legal, so C3 moved 1.5 mm toward the inductor. On the motor
board, D2 moved 2 mm into the space between R2 and C1 to clear both courtyards.
Route straightening had also moved AIN2's first via back across its fixed fan,
leaving a 0-degree foldback; that escape now stops at its declared 45-degree
fan exit and stays on B.Cu until the through-hole header, instead of returning
to the front above the cuts made by the other control lanes. None is waived:
manufacturability, a non-self-reversing route and a local return path are
properties of the baseline.

Finally, regeneration itself is a gate. CI now rebuilds all five projects from
an empty route cache inside the pinned KiCad 9 image, diffs every schematic,
project and board file, runs each reviewed policy, and renders both drawings.
The generated projects, verdicts and renders are uploaded together. A checked-in
example therefore has to be reproducible as well as electrically and
geometrically acceptable; a hand-edited golden file can no longer drift away
from the generator that claims to own it.

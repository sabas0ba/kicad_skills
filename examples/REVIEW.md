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

* **Return paths** — the strongest physical criticism of the set. On a
  two-layer board with the ground plane on the back, every bottom-layer track
  cuts a channel through the plane, and any top-layer signal crossing that
  channel has its return current detoured around the gap: the loop area, and
  with it emission and coupling, grows by the detour. Now the **rule**
  `route.return_path`: the parser keeps the pour's outline *and* its computed
  fill, and the difference between them is exactly where the plane is not.
* **Decoupling geometry** (already ruled: `layout.decoupling_distance`,
  `layout.decoupling_via`). The three fine-pitch boards all fail the distance
  rule for the same reason — the escape from the package spends the distance
  budget before a capacitor can be placed. On two layers, with parts on one
  side, this is a fact of the package, not a placement mistake; the real-world
  fix is caps on the back under the pins, which this generator cannot yet
  place. **Waived** per project, with that reason. The *via* half is now
  **fixed** where it was failing: on the FPGA board every 0603's ground via
  is anchored against its own pad, on the far side from the supply pad, with
  the 1.2 mm stub as the whole loop — placed as a declared via next to the
  pad rather than found by the router at the end of a track.
* **Power track width** (`track.thin_power`). The rule used to damn a rail for
  its narrowest millimetre, which on a fine-pitch board is the escape neck it
  cannot avoid. Now it measures the longest *contiguous* narrow run against a
  `power_neck_mm` allowance — necks pass, thin trunks still fail. Where a
  whole distribution stays narrow (the fpga board routes its rails at 0.2 mm
  because nothing wider fits between the fans), the finding stands and the
  waiver argues in numbers: an iCE40 draws tens of milliamps, and 0.2 mm
  carries 0.74 A at a 10 °C rise.
* **Thermals, open**: nothing yet judges copper area under a TO-263 tab or a
  QFN's exposed pad against the watts the part dissipates (the buck and the
  motor driver both care); stitching-via count under the iCE40's pad is
  eyeballed, not checked.

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

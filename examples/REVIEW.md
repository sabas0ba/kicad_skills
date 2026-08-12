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
  `layout.decoupling_via`). The three fine-pitch boards all fail it for the
  same reason — the escape from the package spends the distance budget before
  a capacitor can be placed. On two layers, with parts on one side, this is a
  fact of the package, not a placement mistake; the real-world fix is caps on
  the back under the pins, which this generator cannot yet place. **Waived**
  per project, with that reason.
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
  graph, power symbols exempt), and half the **fix**: the generator now draws
  a real wire wherever two pins of a net face each other on one axis with a
  clear run, and keeps labels elsewhere. The `reviewed/` sheets are partly
  wired; `as-generated/` stays a pure name table, which is what the rule is
  for. Wiring the *unaligned* rest is open — it needs a schematic router with
  taste, and a wire that dodges three parts is worse than a name.
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
| connections by name, not wire | rule `readability.label_only` + partial fix (aligned pairs drawn) |
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

Every `reviewed/` project now carries a `gate.toml` and passes
`eda gate --policy examples/<name>/gate.toml`; every waiver in those files is
one of the judgments above, stated as a reason a reviewer can disagree with.
That is the intended shape of the mechanism: findings are either fixed,
checked, or answered — never silently absent.

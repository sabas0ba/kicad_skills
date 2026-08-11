---
name: kicad-design-gate
description: Hold a generated KiCad design to a stated standard - one pass/fail verdict over ERC, DRC, schematic readability, layout practice and part specification, with waivers that must carry a reason. Use when generating or editing a schematic or board automatically, when a design "looks fine" but was never checked, or when setting up a CI gate for hardware.
---

# The design gate

> One of the [kicad_skills](https://github.com/sabas0ba/kicad_skills) usage guides for the
> `eda` CLI — [all seven](README.md). Plain Markdown: read it directly, or hand it to
> whatever assistant you use.

`sch review` and `pcb review` answer *what is wrong with this design*. That is
the right shape for a person, who reads the list and decides. It is the wrong
shape for anything generating a design, which will emit a board, print a page of
warnings and move on — the review has no opinion about which of its own findings
were allowed to survive.

`eda gate` is that opinion, written down.

```bash
./bin/eda.sh gate hardware/ --policy ai-generated --text     # exit 2 until it passes
```

It reviews the schematic and the board, applies a **policy** that says which
rules block and how many findings of each severity may remain, and exits `2`
until the design meets it. Nothing can be passed over quietly: a finding either
blocks, or it appears under `waived` next to the sentence somebody wrote to
excuse it — in a file in the repository, so excusing a violation is a diff a
reviewer sees rather than a step a generator can skip.

## What it checks, exactly

```bash
./bin/eda.sh gate --list-rules          # JSON: every rule, in full
./bin/eda.sh gate --list-rules --text   # the same, readable
```

That is the specification, not a summary of one. Each entry states the rule's
id, which review produces it, **the exact condition that makes it fire**, the
severity it reports, the `--threshold` key that tunes it with its default value,
and the built-in policies under which it blocks:

```json
"spec.voltage_derating": {
  "origin": "schematic",
  "checks": "a capacitor whose stated voltage rating is below the highest rail its nets name ('error'), or below that rail times the derating factor ('warning'). Rails that state no voltage are not judged",
  "severity": "error / warning",
  "threshold": "capacitor_derating_factor",
  "threshold_default": 1.5,
  "context_only": false,
  "blocks_under": ["default", "ai-generated", "fabrication"]
}
```

It is assembled from the review modules rather than written out beside them, and
`tests/test_rule_spec.py` reads the rule sources themselves: a rule that emits an
id the catalogue does not describe, a catalogue entry no rule produces any more,
a threshold named by no rule, or a policy pattern matching no rule, is a **test
failure**. The catalogue cannot drift from the code.

## What it asserts

Given a target and a policy, `eda gate` does exactly this:

1. Run the **schematic review** and the **board review**. Either one whose file
   is absent is recorded as `skipped`; if *both* are absent that is an error
   (exit `1`), not a pass.
2. For each finding, decide its **effective severity**:
   * a rule listed in `CONTEXT_RULES` keeps the severity it reported — these
     describe the design rather than fault it, and no policy can promote them;
   * otherwise, the longest `severity` pattern in the policy that matches the
     rule id wins; with no match, the finding keeps its own severity.
3. Move every finding matched by a **waiver** out of the verdict and into
   `waived`. A waiver matches on the rule id (glob) and optionally on a substring
   of the location, and must carry a `reason`.
4. **Count** the findings that remain, by effective severity.
5. For each entry in the policy's `limits`, assert `count[severity] <= limit`.
   A severity with no limit is not asserted on. Everything counted at a severity
   whose limit was exceeded is listed under `blocking`.
6. `pass` is true when no limit was exceeded.

Exit codes: **`0`** the design meets the policy · **`2`** it does not · **`1`**
usage or file error (unknown policy, malformed policy, nothing to review).

The JSON carries both severities for every finding — `reported_severity` is what
the rule said, `severity` is what the policy decided — so a report can show one
and gate on the other.

## The loop

Generating a design and reviewing it afterwards produces a design that was
reviewed, not a design that is right. The gate is meant to be the loop condition:

1. **Generate or edit** the schematic.
2. **`eda gate <project> --policy ai-generated --text`** — read `## blocking`.
3. **Fix the top finding**, not all of them: fixing off-grid geometry usually
   removes the dangling wires and the ERC errors underneath it in one go.
4. **Repeat from 2** until it exits `0`.
5. Only then lay out the board, and run the same loop again with the board in
   place.
6. **`eda report <project> -o build/report`** and *look at the pictures*. The
   gate has no eye: it cannot tell you the topology is wrong.

Two things to hold on to while looping:

* **Fix the cause, not the finding.** `readability.off_grid_pin` is not a
  cosmetic complaint. KiCad joins a wire to a pin only where their coordinates
  match exactly, so a symbol placed half a grid step off looks connected at every
  zoom level a human uses and is not. The ERC error it produces will be about a
  floating pin somewhere else entirely.
* **Do not tune the policy to make the gate pass.** Loosening a threshold and
  waiving a rule are both legitimate, and both are decisions someone has to
  agree with. That is why a waiver must state a reason.

## The built-in policies

```bash
./bin/eda.sh gate --list-policies
```

| Policy | What it holds you to |
| --- | --- |
| `default` | What the review commands already enforce: an error blocks, a warning is for a human to judge. |
| `ai-generated` | Everything a machine can check has to be clean first: readability, part specification and layout practice all block, because a generator has no eye to catch them later. |
| `fabrication` | Ready to send out: ERC, DRC and everything that changes what comes back from the fab. Drawing style is not judged. |

`ai-generated` is the strict one, and it is the one to point a generator at.
`fabrication` is what to run before ordering. `default` is what `sch review` and
`pcb review` already do, expressed as a policy so a project can extend it.

Rules that describe the design rather than fault it — the board size, which
layers carry copper, that a ground pour exists — stay informational under every
policy. Promoting them would make a gate no correct design could pass.

## Writing your own policy

A policy is JSON or TOML, and extends a built-in one:

```toml
# hardware/gate.toml
name = "house-rules"
extends = "ai-generated"

[limits]
error = 0
warning = 0

[severity]
"readability.diagonal_wire" = "info"   # we draw buses at 45 degrees on purpose
"route.stub" = "error"

[thresholds.schematic]
grid_mm = 2.54

[thresholds.board]
min_track_mm = 0.2
max_decoupling_distance_mm = 3.0

[[waivers]]
rule = "drc.lib_footprint_mismatch"
reason = "our footprints are project-local copies; the library is the reference, not the source"

[[waivers]]
rule = "route.stub"
location = "B.Cu"
reason = "the test coupon on the back is deliberately unconnected copper"
```

```bash
./bin/eda.sh gate hardware/ --policy hardware/gate.toml --text
```

| Key | Meaning |
| --- | --- |
| `extends` | which built-in policy to start from (default: `default`) |
| `severity` | rule glob → the severity the gate treats it as; the **longest matching pattern wins**, so you can promote a family and exempt one member |
| `limits` | severity → how many may remain; a severity that is absent is not limited |
| `waivers` | `rule` (glob), optional `location` (substring), and a **required** `reason` |
| `thresholds` | `schematic` and `board` sections, passed to the two reviews |

A waiver with no `reason` is a policy error, not a warning. That is the whole
point of the file.

## What the gate adds over the two reviews

The rules below exist because ERC and DRC have no opinion about any of them. A
design can be ERC-clean, DRC-clean and still be one no engineer would sign. The
tables say what each rule is *for*; `--list-rules` says what each one *does*, to
the letter.

**Schematic readability** — none of this changes the netlist, which is exactly
why nothing else catches it:

| Rule | Why it matters |
| --- | --- |
| `readability.off_grid_pin` / `_wire` / `_junction` / `_label` | KiCad connects on exact coordinates; off-grid geometry draws as a connection that is not one |
| `readability.missing_junction` | a wire ending on another wire is only a connection where a junction dot says so |
| `readability.dangling_wire` | a wire end reaching no pin, label, junction or wire |
| `readability.overlapping_symbols` | parts drawn on top of each other |
| `readability.outside_page` | anything past the page border is missing from the plot and the PDF |
| `readability.diagonal_wire` | schematics are read on the assumption that wires run orthogonally |
| `readability.unnamed_nets` | `Net-(U1-Pad7)` tells a reader nothing about what the wire carries |
| `readability.sheet_density` | one sheet holding more than a reader can follow |
| `readability.title_block` | which board this is, and which revision |

**Part specification** — `C3 = 100n` is not a specification. On a 24 V rail the
16 V part fails and the 50 V part does not, and a schematic that names neither
cannot be reviewed, ordered, or built twice the same way:

| Rule | Meaning |
| --- | --- |
| `spec.missing_rating` | R without tolerance/power, C without voltage/tolerance, L without current |
| `spec.voltage_derating` | a capacitor's voltage rating against the rail it actually sits on |
| `spec.missing_part_number` | an active part with no orderable identity (MPN/manufacturer) |
| `spec.no_design_notes` | nothing on any sheet records *why* the design is the way it is |

`spec.voltage_derating` only judges rails whose name states a voltage (`+3V3`,
`-12V`, `VDD_1V8`, `VBUS`). Derating a part against a number nobody wrote down
would be inventing the requirement. Rated below the rail is an `error`; rated
above it but with less than `capacitor_derating_factor` (default 1.5×) headroom
is a `warning`, because a ceramic loses most of its capacitance well before its
rating.

**Artwork readability and buildability:**

| Rule | Why it matters |
| --- | --- |
| `silk.over_pad` | ink on a pad keeps solder off it |
| `silk.text_too_small` | below the screen printer's limit it comes back a smudge |
| `layout.pad_collision` | pads of two footprints sharing copper — parts placed on top of each other |
| `layout.off_grid_placement` / `layout.odd_rotation` | free electrically, and most of why a generated layout looks generated |
| `layout.decoupling_via` | the capacitor closes a loop through the plane; a ground pad millimetres from the nearest via has more inductance in the path than the part removes |
| `route.stub` | copper with one free end is an antenna nobody asked for |
| `route.acute_angle` | an acute corner traps etchant and is a discontinuity for anything fast |
| `route.mixed_track_widths` | three widths on one net is usually nobody having decided |

Thresholds for all of these are adjustable — `--threshold key=value` on the
command line, or the `thresholds` sections of a policy. The defaults are
conservative low-cost-fab and common-practice values, not your fab's rules.

## Reading the verdict

```console
$ ./bin/eda.sh gate hardware/ --policy ai-generated --text
# gate FAIL: hardware/ against policy 'ai-generated'
  Everything a machine can check has to be clean before a human is asked to look.

## schematic: hardware/board.kicad_sch (error=0, warning=4, info=9 as reported)
## board: skipped - no .kicad_pcb found in hardware/

## after the policy: error=11, warning=0, info=2
  over the limit: 11 error(s), 0 allowed

## blocking
  ERROR   schematic/readability.off_grid_pin: 12 pin(s) are off the 1.27 mm grid ... (reported as warning)
  ERROR   schematic/spec.missing_rating [/:C3]: C3 (100n) states no voltage, tolerance (reported as info)

## waived
  drc.lib_footprint_mismatch: our footprints are project-local copies
```

`(reported as warning)` is the policy at work: the rule graded it a warning, this
policy blocks on it. The JSON carries both — `severity` is what the policy
decided, `reported_severity` is what the rule said — so a report can show one and
gate on the other.

A missing schematic or board is **skipped, not failed**. A design is gated all
the way through its life, and for most of that life the artwork does not exist
yet.

## In CI

```yaml
- name: Gate the design
  run: ./bin/eda.sh gate hardware/ --policy hardware/gate.toml --text
- name: Build the report
  if: always()
  run: ./bin/eda.sh report hardware/ -o build/report
```

Exit `0` when the design meets the policy, `2` when it does not, `1` on a usage
error. `-o verdict.json` keeps the structured version for a PR comment.

## Things the gate cannot judge

Everything that matters most:

* Whether the topology implements the intended function.
* Whether the values are right — gain, corner frequency, divider ratios, current
  limits. Compute them, or verify them with the `spice-simulation` guide.
* Whether a part's operating conditions are respected. That needs the datasheet;
  see the `datasheet-analysis` guide.
* Power budget, thermal dissipation, EMC.
* Whether the placement makes sense as a *circuit* — signal flow across the
  board, what sits next to what, where the noisy things are.

A design that passes the gate has cleared the checks a machine can make. That is
the floor, not the ceiling. Render it and look at it: `eda report` exists so that
whoever is driving — a person on a CI artifact, or an assistant mid-task — can
see the design rather than take a JSON summary on faith.

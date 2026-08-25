# Usage guides

One guide per area of the toolkit. They cover the part a `--help` listing cannot:
which command to reach for, what order to do things in, how to read the output,
and what the tool *cannot* check for you.

Plain Markdown, and the source of truth — read them here, in your editor, or hand
them to whatever assistant you use. Nothing about them is specific to one tool.

| Guide | Covers |
| --- | --- |
| [datasheet-analysis](datasheet-analysis.md) | Pulling text, parameter tables, figures and page images out of a datasheet PDF |
| [spice-simulation](spice-simulation.md) | Writing and running SPICE decks, reading the measurements, Monte Carlo and temperature sweeps |
| [kicad-schematic-review](kicad-schematic-review.md) | Reading a schematic, running ERC, and reviewing a circuit properly |
| [kicad-schematic-authoring](kicad-schematic-authoring.md) | Drawing a schematic a human can read: wires as trees, junctions, orientation, and the checks to run on every sheet |
| [kicad-pcb-review](kicad-pcb-review.md) | DRC, layout heuristics, and what to look for in the rendered artwork |
| [kicad-pcb-authoring](kicad-pcb-authoring.md) | Laying out a board with its physics intact: anchored decoupling vias, return-path trade-offs, and quantified waivers |
| [kicad-design-gate](kicad-design-gate.md) | Holding a design to a stated standard: one pass/fail verdict, readability and specification rules, waivers with a reason |
| [kicad-fabrication-output](kicad-fabrication-output.md) | Producing and checking the manufacturing package |
| [eda-environment](eda-environment.md) | The container, pinning the KiCad version, and troubleshooting |

Each file carries a short YAML header (`name`, `description`). That is a plain
Markdown front-matter block — GitHub renders it as a table, editors ignore it —
and it is what lets a tool decide which guide is relevant without reading all of them.

## Using them with an assistant

Nothing needs installing: point your tool at this directory, or let it read
[`AGENTS.md`](../../AGENTS.md), which names the guides.

Codex and other Agent Skills-compatible assistants discover skills at
`.agents/skills/<name>/SKILL.md`; Claude Code discovers them at
`.claude/skills/<name>/SKILL.md`. `bin/install-skills.sh` produces both layouts
from these files as symlinks, so there is never a second copy to keep in step:

```bash
./bin/install-skills.sh                              # this checkout
./tools/kicad_skills/bin/install-skills.sh           # a project using the submodule
./bin/install-skills.sh --dest .cursor/rules --copy  # some other tool's directory
```

The generated `.agents/skills/` and `.claude/skills/` are git-ignored on purpose:
they are adapters, not content. Delete them and re-run the script whenever you
like. Supplying `--dest` creates only the requested custom layout.

And if you use no assistant at all, the guides still stand on their own — they
are how a careful engineer would use these commands.

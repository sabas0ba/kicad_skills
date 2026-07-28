# Usage guides

One guide per area of the toolkit. They cover the part a `--help` listing cannot:
which command to reach for, what order to do things in, how to read the output,
and what the tool *cannot* check for you.

They are plain Markdown — read them in a browser, in your editor, or hand them to
whatever assistant you use. Nothing in them is specific to one tool.

| Guide | Covers |
| --- | --- |
| [datasheet-analysis](../../.claude/skills/datasheet-analysis/SKILL.md) | Pulling text, parameter tables, figures and page images out of a datasheet PDF |
| [spice-simulation](../../.claude/skills/spice-simulation/SKILL.md) | Writing and running SPICE decks, reading the measurements, Monte Carlo and temperature sweeps |
| [kicad-schematic-review](../../.claude/skills/kicad-schematic-review/SKILL.md) | Reading a schematic, running ERC, and reviewing a circuit properly |
| [kicad-pcb-review](../../.claude/skills/kicad-pcb-review/SKILL.md) | DRC, layout heuristics, and what to look for in the rendered artwork |
| [kicad-fabrication-output](../../.claude/skills/kicad-fabrication-output/SKILL.md) | Producing and checking the manufacturing package |
| [eda-environment](../../.claude/skills/eda-environment/SKILL.md) | The container, pinning the KiCad version, and troubleshooting |

## Why they live under `.claude/skills/`

Claude Code discovers *skills* by looking for `.claude/skills/<name>/SKILL.md`,
and loads the matching one when a task calls for it. Rather than keep a second
copy of the same prose, the guides are stored where that discovery works and are
linked from here — the files are ordinary Markdown with a small YAML header, so
nothing about that location makes them Claude-only.

If you drive the toolkit with a different assistant, point it at this directory,
or copy the guides to wherever it looks:

```bash
# Claude Code (default)
./bin/install-skills.sh

# anything else - e.g. a rules directory, a docs folder, a prompt library
./bin/install-skills.sh --dest .cursor/rules --copy
./bin/install-skills.sh --dest docs/circuit-design --copy
```

And if you use no assistant at all, the guides still stand on their own: they are
how a careful engineer would use these commands.

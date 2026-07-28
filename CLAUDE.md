# Working in this repository

The working agreement for this repository lives in **[AGENTS.md](AGENTS.md)** —
ground rules, the test gate, how to add and tune a review rule, the pinning
rules and the fixture constraints. It is tool-neutral on purpose; this file
exists only so Claude Code finds it.

The per-task usage guides live in **[`docs/guides/`](docs/guides/README.md)**:
one each for datasheets, SPICE simulation, schematic review, board review,
fabrication output and the container. Read the relevant one before starting that
kind of work. `make skills` mirrors them into `.claude/skills/` so they load on
demand; that directory is generated and git-ignored, so edit `docs/guides/`.

@AGENTS.md

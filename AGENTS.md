# Working in this repository

`eda` is a command-line toolkit for circuit design — datasheets, SPICE
simulation, KiCad schematic and board review, fabrication output — implemented
as the `eda_toolkit` Python package and executed inside a container.

This file is the working agreement for anyone changing the code: humans, and
coding agents alike. (`CLAUDE.md` points here; there is one copy, not two.)

## Ground rules

* **Never run `kicad-cli`, `ngspice` or the `eda` CLI on the host.** Use
  `./bin/eda.sh <command>` — it builds and runs the container, maps your uid, and
  keeps the host clean. Anything else pollutes the host or fails outright.
* Paths passed to `./bin/eda.sh` are relative to the repository root (mounted at
  `/work` in the container).
* `KICAD_VERSION` selects the KiCad release; the default is in `bin/eda.sh` and
  the `Makefile`. Keep the two in sync when changing it.

## Development

```bash
make build        # build (or rebuild) the image
make test         # full suite in the container - this is the gate
make test-host    # fast pure-python subset (needs .venv with the test extra)
make smoke        # end-to-end run of every top-level command
```

`make test` mounts the working tree and puts `/work/src` on `PYTHONPATH`, so it
tests the code you are editing, not the copy baked into the image. Rebuild the
image only when the Dockerfile or the dependencies change.

CI runs the suite against **every KiCad version in the `ci.yml` matrix**
(currently 10.0.4 and 9.0.9). Run `make test KICAD_VERSION=9.0.9` before
pushing anything that touches `kicad_cli.py`, the fixtures or the review rules.
Not every flag exists in every release: gate on `kicad_cli.supports([...],
"--flag")` rather than on a version number, and keep the fixtures in the oldest
format the matrix covers — KiCad never reads a file newer than itself.

## Adding a review rule

Rules are functions registered with the `@rule` decorator in
`src/eda_toolkit/kicad/sch_review.py` or `pcb_review.py`. They receive a context
(parsed design + netlist/board + ERC/DRC output) and return `Finding` objects.
Add the rule, then add a test in `tests/test_sch_review.py` /
`tests/test_pcb_review.py` using the in-memory `ReviewContext.from_netlist` /
`PcbContext.from_board` constructors — no filesystem needed.

Keep severities honest: `error` = the design is broken, `warning` = a human must
judge it, `info` = context. A rule that reads the *drawing* rather than the
netlist (grid, overlap, page) belongs in the `readability.*` family; one about
what a part has to state to be orderable belongs in `spec.*`. Anything tunable
goes in the module's `THRESHOLDS` dict rather than as a literal in the rule, so
`--threshold key=value` reaches it.

Every rule id also needs an entry in the module's `RULE_SPEC`, saying what
condition produces it, the severity it reports and the threshold that tunes it.
That table is what `eda gate --list-rules` prints, and `tests/test_rule_spec.py`
parses the rule sources to enforce it in both directions — an id with no entry,
an entry no rule emits, a threshold no rule names, or a policy pattern matching
no rule, all fail. It also checks that the guides only name rules that exist.

Then decide what the rule does to `eda gate`. A rule that *describes* the design
rather than faulting it (`board.size`, `layout.ground_plane`) must be added to
`CONTEXT_RULES` in `src/eda_toolkit/gate.py`, or a policy could promote it into
an error no correct design can clear. A rule a generated design has to get right
belongs in `_AI_BLOCKING` in the same file. `tests/test_gate.py` covers both.

## Regenerating the worked examples

`tools/make_examples.py` builds both variants of every design in `examples/`.
Routing is what it spends its time on: the FPGA board is a 48-pin QFN on two
layers and takes the better part of an hour, and a net that finds no room sends
the whole set round again.

So the routed copper is cached under `.cache/routes/` (git-ignored), keyed by
everything the router reads — the outline, the parts and their pads, every
stated track and via, and the source of `tools/autoroute.py` and `_route_all`
themselves. Editing where a designator prints or how a legend picks its side
does not move copper, so those runs reuse the answer and finish in seconds;
editing the router invalidates every answer it ever gave. `--no-route-cache`
routes from scratch.

The rip-up order is kept separately, in `<design>.order.json`, and survives a
change that does invalidate the cache: it is what an afternoon of rip-up
attempts learned, and starting from it is usually the difference between
seventeen attempts and none.

## Tuning a rule

`tools/review_demos.py` runs both reviews over the 18 KiCad demo projects in the
image and aggregates the findings per rule. Use it before and after changing a
rule: a rule that fires thousands of times across that corpus is noise, however
correct each instance is. Findings that repeat more than `COLLAPSE_LIMIT` times
are folded into one entry by `util.collapse_findings`, so prefer grading a rule
(`warning` vs `info`) over deleting it.

## Pinning rules (enforced by tests/test_pinning.py)

* Python dependencies: edit `pyproject.toml`, then `make lock` (`uv lock`).
  `uv.lock` is the only lock file — it carries the hashes and the image installs
  it with `uv sync --frozen`. Do not add a second requirements file.
* Build backend: `[build-system] requires` is resolved *outside* `uv.lock`, so it
  is pinned with `==` in `pyproject.toml` and mirrored by wheel hash in
  `docker/build-backend.txt`. Change both together, and keep the requirement list
  free of transitive dependencies — `--require-hashes` needs every one of them
  pinned too, which is why `wheel` is not listed.
* KiCad releases: add `<version> <sha256 digest>` to `docker/kicad-digests.txt`
  before building with a new `KICAD_VERSION`. Keep the Dockerfile's default
  `KICAD_VERSION`/`KICAD_DIGEST`, the `Makefile` and `bin/eda.sh` in agreement,
  and add the version to the `ci.yml` matrix if it is meant to be supported.
* GitHub Actions: `uses: owner/action@<40-char-sha> # vX.Y.Z`. No tag-only refs,
  no `ubuntu-latest`. When a bot proposes a bump, verify the SHA really is that
  tag (`git ls-remote --tags https://github.com/<owner>/<action>`) before merging.

## Test fixtures

`tests/fixtures/example_project` is a small, DRC-clean KiCad project (RC filter
+ LM321 buffer) that also passes every built-in gate policy. It states what it
expects of a design: ratings and tolerances on the passives, manufacturer part
numbers on the actives, and a sheet note explaining the values. Keep it that way
— a new `spec.*` or `readability.*` finding there means either the rule or the
fixture drifted. Its symbols come from the real KiCad libraries; its footprints
are simplified, which is why DRC reports `lib_footprint_mismatch` for each of
them. Keep the project clean: a new error there means the toolkit changed
behaviour. Its own README documents the two constraints that are easy to break
by accident — no KiCad 10 only tokens, and the ground pour stays filled.

## Documentation

* `README.md` — what the toolkit is and how to use it.
* **`docs/guides/`** — one usage guide per area, and the source of truth for how
  to *use* the toolkit well. Read the one that matches the task before doing it:
  `datasheet-analysis`, `spice-simulation`, `kicad-schematic-review`,
  `kicad-schematic-authoring`, `kicad-pcb-review`, `kicad-pcb-authoring`,
  `kicad-design-gate`, `kicad-fabrication-output`, `eda-environment`.
* `docs/examples/` — committed sample output, regenerable with the commands
  documented there.

When behaviour changes, update the guide that covers it in the same commit. A
guide that describes a flag the CLI no longer has is worse than no guide.

These same files are the website: GitHub Pages serves `main` / `(root)` with the
settings in `_config.yml` — no build workflow, no `gh-pages` branch, nothing
generated. That puts three constraints on anything published (README.md,
AGENTS.md, docs/**), all enforced by `tests/test_docs.py`:

* **Start the file with its `#` heading.** The page title comes from the first
  heading, and only if nothing precedes it.
* **Never write Liquid delimiters** — a doubled curly brace, or a curly brace
  followed by a percent sign. Jekyll expands Liquid inside fenced code blocks
  too, and silently deletes what it cannot parse: a documented command lost its
  `--format` argument that way. There is no per-file opt-out on the Jekyll that
  Pages runs, and the escape hatch would itself show up verbatim on github.com,
  so far every case has had a clean alternative.
* **Link to source files on github.com, not by relative path.** `src/`, `tests/`,
  `docker/` and friends are excluded from the site, so a relative link to them
  resolves on github.com and 404s on the site.

`make site` renders it locally with the same pinned gem set Pages uses, which is
how those three were found in the first place.

The shell around that Markdown is four small files, and nothing in them
generates content:

| File | What it is |
| --- | --- |
| `_layouts/default.html` | header, sidebar, footer and the client-side "on this page" list, shadowing the copy `jekyll-theme-primer` ships |
| `assets/css/style.scss` | imports the theme (so the body still renders like github.com) and adds the shell plus a dark palette |
| `_data/nav.yml` | the sidebar and the small-screen header nav |
| `assets/logo.svg`, `assets/favicon.svg` | the mark, in the header, in the README heading and as the favicon |

**Adding a guide means adding it to `_data/nav.yml`** as well as to the guides
index — `tests/test_docs.py` fails otherwise, the same way it does for the
index. Every entry names both the Markdown file it points at and the URL Jekyll
publishes it under, and the test checks that the two agree and that the target
is a page the site actually serves.

`bin/install-skills.sh` renders `docs/guides/` into the tool-neutral Agent Skills
layout (`.agents/skills/<name>/SKILL.md`) and Claude Code's compatibility layout
(`.claude/skills/<name>/SKILL.md`). Both use git-ignored symlinks. Never edit
those copies: they are generated, and `make skills` regenerates them.

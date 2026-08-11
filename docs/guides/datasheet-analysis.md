---
name: datasheet-analysis
description: Extract text, parameter tables, embedded figures and page images from a datasheet PDF so its contents (absolute maximum ratings, electrical characteristics, pinout, typical application circuits, curves) can be read and quoted. Use when a datasheet PDF has to be read, when a specific parameter or pin function has to be looked up, or when a figure/graph in a PDF needs to be viewed.
---

# Datasheet analysis

> One of the [kicad_skills](https://github.com/sabas0ba/kicad_skills) usage guides for the
> `eda` CLI — [all seven](README.md). Plain Markdown: read it directly, or hand it to
> whatever assistant you use.

Turns a datasheet PDF into things that can actually be read: per-page text,
CSV parameter tables, extracted figures and rendered page images. Everything
runs in the container (see the `eda-environment` guide), offline.

Getting hold of the PDF is out of scope here - download it however you
normally would (the manufacturer's site is the authoritative copy) and keep it
in the repository, e.g. under `docs/datasheets/`, so later sessions can read it
without network access.

## Start by locating, not by dumping

A 60 page datasheet does not belong in the context window. Find the page first,
then read only that page.

```bash
pdf=docs/datasheets/lm321.pdf

./bin/eda.sh datasheet info  "$pdf"                    # pages, metadata, is it scanned?
./bin/eda.sh datasheet find  "$pdf" "absolute maximum" "electrical characteristics"
./bin/eda.sh datasheet text  "$pdf" --pages 3-4        # only what matters
```

`find` returns `page`, `match` and a text `snippet` for each hit; `--regex`
enables patterns (`"\d+\.\d+ *V"`). `datasheet info` also reports
`likely_scanned: true` for image-only PDFs — those need `--ocr`.

## Reading the different kinds of content

| Need | Command |
| --- | --- |
| Text of selected pages | `datasheet text <pdf> --pages 5-7` (`--layout` keeps columns, `--ocr` for scans) |
| Parameter tables as data | `datasheet tables <pdf> --pages 5` → JSON rows |
| Block diagrams / curves / package drawings | `datasheet images <pdf> -o out/img --pages 8-12` |
| Anything vector-drawn (most figures!) | `datasheet pages <pdf> -o out/pages --pages 8 --dpi 200` |
| Everything at once, into a directory | `datasheet parse <pdf> -o out/ --renders` |

**Important:** `images` only recovers *embedded raster* images. Most modern
datasheets draw their pinouts, block diagrams and curves as vectors, so nothing
is extracted. When a figure matters, render the page with `datasheet pages` and
**look at the resulting PNG with the Read tool** — that is how a graph, a
pinout drawing or a package outline gets reviewed.

## Reading a curve or a pinout

```bash
./bin/eda.sh datasheet find  "$pdf" "typical performance"      # -> page 9
./bin/eda.sh datasheet pages "$pdf" -o /tmp/ds --pages 9 --dpi 220
# then: Read /tmp/ds/page-009.png
```

Use ≥200 dpi for graphs with fine gridlines, 150 dpi is enough for text pages.
`datasheet pages` refuses to render more than 40 pages at once — narrow the
range instead of raising the limit.

## What to extract for a design review

When a part is used in a circuit under review, pull these and quote the numbers
with their page:

1. **Absolute maximum ratings** — supply, input voltage, current, junction temp.
2. **Recommended operating conditions** — the range the design must stay inside.
3. **Electrical characteristics** — the spec that the circuit actually relies on
   (offset, bias current, GBW, dropout, RDS(on), leakage …), and its test
   conditions. A number without its conditions is meaningless.
4. **Pin functions** — especially enable/mode pins that must not float.
5. **Application information** — the manufacturer's recommended external
   components (decoupling, compensation, minimum load, soft-start) and layout
   guidance.

Report values as `parameter = value (min/typ/max, conditions) — p.N`, and say
explicitly when a needed parameter is *not* specified in the datasheet.

## Output layout of `datasheet parse`

```
out/
├── index.json        # manifest: pages, tables, images, detected sections
├── full-text.txt     # everything, page delimited
├── text/page-001.txt
├── tables/page-005-table-1.csv
├── images/page-008-img-01.png
└── pages/page-008.png     (only with --renders)
```

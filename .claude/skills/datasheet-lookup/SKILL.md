---
name: datasheet-lookup
description: Find and download the datasheet PDF for a part number (op-amps, regulators, MCUs, passives, connectors) from distributor APIs or web search, with a local cache. Use when a part number needs its datasheet, when a component has to be selected or verified, or before reviewing a schematic that uses an unfamiliar part.
---

# Datasheet lookup

Finds the datasheet for a manufacturer part number and downloads the PDF. All
work happens in the container (see the `eda-environment` skill); this is one of
the two commands that gets network access automatically.

## Workflow

```bash
# 1. what is out there (ranked, best first)
./bin/eda datasheet search LM321 --limit 5

# 2. download the best candidate into the project
./bin/eda datasheet fetch LM321 -o docs/datasheets/

# 3. or, when the URL is already known (the reliable path)
./bin/eda datasheet fetch --url https://www.ti.com/lit/ds/symlink/lm321.pdf -o docs/datasheets/
```

`fetch` returns JSON with the local `path`, `sha256`, `bytes` and the candidate
it picked. Hand that `path` to the **datasheet-analysis** skill to read the
contents.

## Search sources

Providers are tried in order and their results merged and re-ranked. Ones that
need credentials are skipped silently when the credentials are absent:

| Provider | Enabled by | Notes |
| --- | --- | --- |
| `mouser` | `MOUSER_API_KEY` | best signal-to-noise, exact MPN matching |
| `digikey` | `DIGIKEY_CLIENT_ID` + `DIGIKEY_CLIENT_SECRET` | OAuth client credentials |
| `nexar` | `NEXAR_TOKEN` | Octopart backend |
| `searxng` | `SEARXNG_URL` | self-hosted meta search |
| `duckduckgo` | always | no key, but rate limited and easily blocked |

Restrict the chain with `--provider mouser --provider duckduckgo`.

Ranking prefers: the exact part number in the URL, a `.pdf` suffix, manufacturer
domains (ti.com, analog.com, st.com, …) over distributors, and penalises the
usual datasheet-farm domains.

## Rules that keep this honest

* **Verify what you downloaded.** The tool checks the `%PDF` magic bytes and
  refuses HTML error pages, but it cannot tell whether the PDF is really the
  right part. Open it (`./bin/eda datasheet info <pdf>`) and confirm the part
  number appears on page 1 before quoting numbers from it.
* **Prefer the manufacturer's own copy.** Distributor mirrors are often several
  revisions behind. Record the revision/date printed on the datasheet.
* **When search fails, do not guess a URL.** Ask the user for a link, or use a
  distributor API key. The `hint` field in the JSON output says what to try.
* Cache: repeated fetches of the same URL are served from `/cache` inside the
  container (a docker volume). Use `--force` to re-download.

## Typical use inside a bigger task

```bash
part=OPA2340UA
./bin/eda datasheet fetch "$part" -o docs/datasheets/ > /tmp/ds.json
pdf=$(python3 -c "import json;print(json.load(open('/tmp/ds.json'))['path'])")
./bin/eda datasheet find "$pdf" "absolute maximum" "supply voltage"
```

Store the PDF in the repository next to the design (`docs/datasheets/`) so the
review skills and future sessions can reach it without network access.

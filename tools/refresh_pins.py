#!/usr/bin/env python3
"""Check (or refresh) the pins that no dependency bot understands.

Dependabot keeps `uv.lock` and the GitHub Actions up to date. The remaining
pins are hand-rolled and live in this repository's own files:

* the KiCad base image digest        -> docker/kicad-digests.txt + Dockerfile ARGs
* the pip bootstrap wheel            -> docker/Dockerfile (ARG PIP_*)
* the uv bootstrap wheel             -> docker/uv-bootstrap.txt

    tools/refresh_pins.py            # report drift, exit 1 when something is stale
    tools/refresh_pins.py --write    # rewrite the files in place
    tools/refresh_pins.py --write --set-default-kicad  # also bump the default release

Only PyPI and the Docker Hub registry API are contacted, both read only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = ROOT / "docker" / "Dockerfile"
DIGESTS = ROOT / "docker" / "kicad-digests.txt"
UV_BOOTSTRAP = ROOT / "docker" / "uv-bootstrap.txt"
MAKEFILE = ROOT / "Makefile"
WRAPPER = ROOT / "bin" / "eda.sh"
WORKFLOWS = ROOT / ".github" / "workflows"

HUB_TAGS = (
    "https://hub.docker.com/v2/repositories/kicad/kicad/tags?page_size=100&ordering=last_updated"
)
PYPI = "https://pypi.org/pypi/{}/json"
TIMEOUT = 60
STABLE_TAG = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def fetch_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.load(response)


# -- KiCad ----------------------------------------------------------------


def kicad_releases() -> dict[str, str]:
    """Stable ``x.y.z`` tags of kicad/kicad mapped to their manifest digest."""
    releases: dict[str, str] = {}
    data = fetch_json(HUB_TAGS)
    for tag in data.get("results", []):
        name, digest = tag.get("name", ""), tag.get("digest", "")
        if STABLE_TAG.match(name) and digest:
            releases.setdefault(name, digest)
    return releases


def parse_digests() -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in DIGESTS.read_text().splitlines():
        line = line.split("#")[0].strip()
        if line:
            version, digest = line.split()
            entries[version] = digest
    return entries


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


# -- PyPI -----------------------------------------------------------------


def pypi_wheel(package: str, matcher) -> tuple[str, list[tuple[str, str]]]:
    data = fetch_json(PYPI.format(package))
    version = data["info"]["version"]
    wheels = [
        (u["filename"], u["url"], u["digests"]["sha256"])
        for u in data["urls"]
        if u["packagetype"] == "bdist_wheel" and matcher(u["filename"])
    ]
    if not wheels:
        raise SystemExit(f"no matching wheel for {package} {version}")
    return version, [(url, sha) for _, url, sha in wheels]


def is_pure_wheel(filename: str) -> bool:
    return filename.endswith("-py3-none-any.whl")


def is_linux_wheel(filename: str) -> bool:
    return (
        "manylinux" in filename
        and ("x86_64" in filename or "aarch64" in filename)
        and "musl" not in filename
    )


# -- reporting ------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--write", action="store_true", help="rewrite the files in place")
    parser.add_argument(
        "--set-default-kicad",
        action="store_true",
        help="also move the default KICAD_VERSION to the newest release",
    )
    args = parser.parse_args()

    changes: list[str] = []
    dockerfile = DOCKERFILE.read_text()

    # -- KiCad releases ---------------------------------------------------
    known = parse_digests()
    available = kicad_releases()
    # only releases newer than everything we already pin are interesting; the
    # historical ones are simply not supported and never will be
    floor = max((version_key(v) for v in known), default=(0,))
    missing = {v: d for v, d in available.items() if v not in known and version_key(v) > floor}
    for version, digest in sorted(missing.items(), key=lambda kv: version_key(kv[0])):
        changes.append(f"kicad {version} released ({digest[:19]}...), not pinned yet")
    for version, digest in sorted(known.items()):
        upstream = available.get(version)
        if upstream and upstream != digest:
            changes.append(
                f"kicad {version} digest changed upstream: {digest[:19]}... -> {upstream[:19]}..."
            )
    if missing and args.write:
        text = DIGESTS.read_text().rstrip("\n")
        for version, digest in sorted(missing.items(), key=lambda kv: version_key(kv[0])):
            text += f"\n{version} {digest}"
        DIGESTS.write_text(text + "\n")

    newest = max(available, key=version_key) if available else None
    current_default = re.search(r"^ARG KICAD_VERSION=(\S+)", dockerfile, re.MULTILINE).group(1)
    if newest and version_key(newest) > version_key(current_default):
        changes.append(f"default KiCad {current_default} -> {newest} available")
        if args.write and args.set_default_kicad:
            digest = available[newest]
            dockerfile = re.sub(
                r"^ARG KICAD_VERSION=\S+",
                f"ARG KICAD_VERSION={newest}",
                dockerfile,
                flags=re.MULTILINE,
            )
            dockerfile = re.sub(
                r"^ARG KICAD_DIGEST=\S+",
                f"ARG KICAD_DIGEST={digest}",
                dockerfile,
                flags=re.MULTILINE,
            )
            _sub_file(MAKEFILE, r"^KICAD_VERSION \?= \S+", f"KICAD_VERSION ?= {newest}")
            _sub_file(
                WRAPPER,
                r'KICAD_VERSION="\$\{KICAD_VERSION:-[^}]+\}"',
                f'KICAD_VERSION="${{KICAD_VERSION:-{newest}}}"',
            )
            for workflow in sorted(WORKFLOWS.glob("*.yml")):
                _sub_file(workflow, r"KICAD_VERSION: \S+", f"KICAD_VERSION: {newest}")

    # -- pip --------------------------------------------------------------
    pip_version, pip_wheels = pypi_wheel("pip", is_pure_wheel)
    pip_url, pip_sha = pip_wheels[0]
    current_pip = re.search(r"^ARG PIP_VERSION=(\S+)", dockerfile, re.MULTILINE).group(1)
    if current_pip != pip_version:
        changes.append(f"pip {current_pip} -> {pip_version}")
        if args.write:
            dockerfile = re.sub(
                r"^ARG PIP_VERSION=\S+",
                f"ARG PIP_VERSION={pip_version}",
                dockerfile,
                flags=re.MULTILINE,
            )
            dockerfile = re.sub(
                r"^ARG PIP_URL=\S+", f"ARG PIP_URL={pip_url}", dockerfile, flags=re.MULTILINE
            )
            dockerfile = re.sub(
                r"^ARG PIP_SHA256=\S+", f"ARG PIP_SHA256={pip_sha}", dockerfile, flags=re.MULTILINE
            )

    if args.write:
        DOCKERFILE.write_text(dockerfile)

    # -- uv ---------------------------------------------------------------
    uv_version, uv_wheels = pypi_wheel("uv", is_linux_wheel)
    bootstrap = UV_BOOTSTRAP.read_text()
    current_uv = re.search(r"^uv==(\S+)", bootstrap, re.MULTILINE).group(1)
    if current_uv != uv_version:
        changes.append(f"uv {current_uv} -> {uv_version}")
        if args.write:
            header = bootstrap.split("uv==")[0]
            hashes = " \\\n".join(f"    --hash=sha256:{sha}" for _, sha in uv_wheels)
            UV_BOOTSTRAP.write_text(f"{header}uv=={uv_version} \\\n{hashes}\n")

    if not changes:
        print("all pins are current")
        return 0
    print("rewritten:" if args.write else "stale pins:")
    for change in changes:
        print(f"  - {change}")
    if args.write:
        print("\nRemember to run `make lock` and rebuild before committing.")
        return 0
    return 1


def _sub_file(path: Path, pattern: str, replacement: str) -> None:
    text = path.read_text()
    path.write_text(re.sub(pattern, replacement, text, flags=re.MULTILINE))


if __name__ == "__main__":
    sys.exit(main())

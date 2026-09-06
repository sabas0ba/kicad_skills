#!/usr/bin/env python3
"""Render the pictures ``examples/README.md`` shows, from the committed projects.

Each design's ``as-generated/`` and ``reviewed/`` variant is rendered with the
toolkit's own ``sch render`` and ``pcb render`` - the sheet at 150 dpi, the board
front and back at 300 dpi - and written as JPEG next to the design under
``images/``. The ``*-first.jpg`` pictures are not touched: they are the first
editions, recovered from history, and nothing regenerates them.

Run it inside the container, after ``tools/make_examples.py``:

    docker run --rm -u $(id -u):$(id -g) -v "$PWD:/work" -w /work \\
      -e PYTHONPATH=/work/src -e HOME=/tmp/eda-home \\
      --entrypoint python3 eda-toolkit:10.0.4 tools/example_images.py examples/
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

VARIANTS = ("as-generated", "reviewed")
SHEET_DPI = 150
BOARD_DPI = 300
JPEG_QUALITY = 85


def render(args: list[str]) -> None:
    subprocess.run(["eda", *args], check=True, stdout=subprocess.DEVNULL)


def to_jpeg(png: Path, jpg: Path) -> None:
    with Image.open(png) as image:
        image.convert("RGB").save(jpg, quality=JPEG_QUALITY, optimize=True)


def make_images(design: Path, variant: str, scratch: Path) -> list[Path]:
    project = design / variant
    images = design / "images"
    images.mkdir(exist_ok=True)
    sheet_out = scratch / design.name / variant / "sch"
    board_out = scratch / design.name / variant / "pcb"
    render(["sch", "render", str(project), "-o", str(sheet_out), "--dpi", str(SHEET_DPI)])
    render(
        [
            "pcb",
            "render",
            str(project),
            "-o",
            str(board_out),
            "--dpi",
            str(BOARD_DPI),
            "--views",
            "front",
            "back",
            "--no-3d",
            "--no-sheet",
        ]
    )
    written = []
    for png, name in (
        (sheet_out / "sheet.png", f"schematic-{variant}.jpg"),
        (board_out / "front.png", f"board-front-{variant}.jpg"),
        (board_out / "back.png", f"board-back-{variant}.jpg"),
    ):
        target = images / name
        to_jpeg(png, target)
        written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("examples", help="the examples directory")
    parser.add_argument("--only", help="render just this design")
    args = parser.parse_args(argv)
    root = Path(args.examples)
    designs = sorted(p for p in root.iterdir() if (p / "reviewed").is_dir())
    with tempfile.TemporaryDirectory(prefix="example-images-") as tmp:
        for design in designs:
            if args.only and args.only != design.name:
                continue
            for variant in VARIANTS:
                for path in make_images(design, variant, Path(tmp)):
                    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())

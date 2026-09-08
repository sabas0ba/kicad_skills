#!/usr/bin/env python3
"""Render the pictures ``examples/README.md`` shows, from the committed projects.

Each design's ``as-generated/`` and ``reviewed/`` variant is rendered with the
toolkit's own ``sch render`` and ``pcb render`` - the sheet at 150 dpi, the board
front and back at 300 dpi - cropped to its ink and written as JPEG next to the
design under ``images/``. A reviewed variant that has inner copper layers gets
one picture per layer as well. The ``*-first.jpg`` pictures are not touched:
they are the first editions, recovered from history, and nothing regenerates
them.

Cropping is what keeps a picture readable at README width: KiCad renders the
whole sheet, most of which is margin, and scaling that down to a column shrinks
the copper with it. The crop removes the margin instead, so the copper arrives
at the size it was rendered.

Run it inside the container, after ``tools/make_examples.py``:

    docker run --rm -u $(id -u):$(id -g) -v "$PWD:/work" -w /work \\
      -e PYTHONPATH=/work/src -e HOME=/tmp/eda-home \\
      --entrypoint python3 eda-toolkit:9.0.9 tools/example_images.py examples/
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageChops, ImageOps

VARIANTS = ("as-generated", "reviewed")
SHEET_DPI = 150
BOARD_DPI = 300
JPEG_QUALITY = 95
# The inner copper layers a four-layer example would have. Two-layer boards
# render none of these, and the list stays empty.
INNER_LAYERS = (1, 2)


def crop_page(image: Image.Image) -> Image.Image:
    """The rendered page trimmed to its ink, with a small border put back."""
    image = image.convert("RGB")
    ink = ImageChops.difference(image, Image.new("RGB", image.size, "white"))
    bbox = ink.convert("L").point(lambda value: 255 if value > 12 else 0).getbbox()
    if bbox is None:
        raise ValueError("refusing an empty render")
    return ImageOps.expand(image.crop(bbox), border=12, fill="white")


def render(args: list[str]) -> None:
    subprocess.run(["eda", *args], check=True, stdout=subprocess.DEVNULL)


def to_jpeg(png: Path, jpg: Path) -> None:
    with Image.open(png) as image:
        cropped = crop_page(image)
    cropped.save(jpg, quality=JPEG_QUALITY, subsampling=0, optimize=True)


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
            "--per-layer",
            "--no-3d",
            "--no-sheet",
        ]
    )
    wanted = [
        (sheet_out / "sheet.png", f"schematic-{variant}.jpg"),
        (board_out / "front.png", f"board-front-{variant}.jpg"),
        (board_out / "back.png", f"board-back-{variant}.jpg"),
    ]
    for index in INNER_LAYERS:
        inner = board_out / f"layer-In{index}_Cu.png"
        if inner.is_file():
            wanted.append((inner, f"board-in{index}-{variant}.jpg"))
    written = []
    for png, name in wanted:
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

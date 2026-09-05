#!/usr/bin/env python3
"""Prepare README images from actual KiCad PNG renders, without redrawing copper."""

import argparse
from pathlib import Path

from PIL import Image, ImageChops, ImageOps


def crop_page(image: Image.Image) -> Image.Image:
    image = image.convert("RGB")
    ink = ImageChops.difference(image, Image.new("RGB", image.size, "white"))
    bbox = ink.convert("L").point(lambda value: 255 if value > 12 else 0).getbbox()
    if bbox is None:
        raise ValueError("refusing an empty render")
    return ImageOps.expand(image.crop(bbox), border=12, fill="white")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "rendered", type=Path, help="variant directory containing pcb/ and schematic/"
    )
    parser.add_argument("images", type=Path)
    parser.add_argument("variant", choices=("reviewed", "as-generated"))
    args = parser.parse_args()
    files = [
        ("schematic/sheet.png", "schematic"),
        ("pcb/front.png", "board-front"),
        ("pcb/back.png", "board-back"),
    ]
    if args.variant == "reviewed":
        files += [
            (f"pcb/layer-In{i}_Cu.png", f"board-in{i}")
            for i in (1, 2)
            if (args.rendered / f"pcb/layer-In{i}_Cu.png").exists()
        ]
    for source, _ in files:
        if not (args.rendered / source).is_file():
            parser.error(f"missing render: {args.rendered / source}")
    args.images.mkdir(parents=True, exist_ok=True)
    for source, name in files:
        with Image.open(args.rendered / source) as image:
            cropped = crop_page(image)
        target = args.images / f"{name}-{args.variant}.jpg"
        cropped.save(target, quality=95, subsampling=0, optimize=True)
        print(target)


if __name__ == "__main__":
    main()

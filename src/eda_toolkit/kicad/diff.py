"""What changed between two revisions of a design.

`eda report` shows the state of a board. Reviewing a change needs the other
question - what is different - and a text diff of `.kicad_pcb` cannot answer it:
moving one component rewrites thousands of coordinates, and a rerouted net looks
identical to a re-ordered file. So compare the meaning instead.

* **Connectivity** by node set, so a net that gained or lost a pin shows up and a
  net that only moved does not. Nets whose connections are unchanged but whose
  name is not are reported as renames rather than as one addition and one
  removal.
* **Components** by reference, with value, footprint and DNP changes named.
* **The board** by its own numbers, plus which footprints moved and how far.
* **The drawings** - both the schematic sheets and the board plots - by rendering
  each revision and comparing what is inked. What only the old revision had is
  drawn in red, what only the new one has in green, so a part that moved reads as
  red where it was and green where it is now. This is the only one of the four
  that catches "the pour changed shape" or "somebody redrew that wire".

Every section is independent: a project with no schematic still gets a board
diff, and a failed render still leaves the connectivity answer intact.
"""

from __future__ import annotations

import math
import os
from pathlib import Path
from typing import Any

from ..util import ensure_dir, write_json

# Views worth diffing by default: the copper that carries the signals and the
# assembled picture. 3D renders are slow and rarely say anything a plot does not.
DEFAULT_VIEWS = ("front", "back", "copper-front", "copper-back")


def _node_key(node: dict[str, Any]) -> str:
    return f"{node.get('ref', '')}.{node.get('pin', '')}"


def _net_map(netlist: dict[str, Any]) -> dict[str, frozenset[str]]:
    return {
        net["name"]: frozenset(_node_key(node) for node in net.get("nodes", []))
        for net in netlist.get("nets", [])
    }


def compare_netlists(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    """Connectivity change, by which pins each net joins."""
    before, after = _net_map(old), _net_map(new)

    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))

    # A net whose connections survived under a different name is a rename, not a
    # net destroyed and another created - saying so keeps the real changes visible.
    renames = []
    by_nodes_removed = {before[name]: name for name in removed if before[name]}
    for name in list(added):
        nodes = after[name]
        origin = by_nodes_removed.get(nodes)
        if origin and origin in removed:
            renames.append({"from": origin, "to": name, "pins": sorted(nodes)})
            added.remove(name)
            removed.remove(origin)
            del by_nodes_removed[nodes]

    changed = []
    for name in sorted(set(before) & set(after)):
        if before[name] == after[name]:
            continue
        changed.append(
            {
                "net": name,
                "added_pins": sorted(after[name] - before[name]),
                "removed_pins": sorted(before[name] - after[name]),
            }
        )

    return {
        "added": [{"net": name, "pins": sorted(after[name])} for name in added],
        "removed": [{"net": name, "pins": sorted(before[name])} for name in removed],
        "renamed": renames,
        "changed": changed,
        "unchanged": len([n for n in set(before) & set(after) if before[n] == after[n]]),
        "identical": not (added or removed or renames or changed),
    }


TRACKED_FIELDS = ("value", "footprint", "dnp", "lib_id")


def compare_components(old: list[dict], new: list[dict]) -> dict[str, Any]:
    """Parts added, removed, or changed in a way that reaches the BOM."""
    before = {c["reference"]: c for c in old if c.get("reference")}
    after = {c["reference"]: c for c in new if c.get("reference")}

    changed = []
    for ref in sorted(set(before) & set(after)):
        fields = {
            field: {"from": before[ref].get(field), "to": after[ref].get(field)}
            for field in TRACKED_FIELDS
            if before[ref].get(field) != after[ref].get(field)
        }
        if fields:
            changed.append({"reference": ref, "fields": fields})

    def describe(ref: str, source: dict[str, dict]) -> dict[str, Any]:
        entry = source[ref]
        return {
            "reference": ref,
            "value": entry.get("value"),
            "footprint": entry.get("footprint"),
        }

    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    return {
        "added": [describe(ref, after) for ref in added],
        "removed": [describe(ref, before) for ref in removed],
        "changed": changed,
        "unchanged": len(set(before) & set(after)) - len(changed),
        "identical": not (added or removed or changed),
    }


def compare_boards(old: Any, new: Any, *, moved_threshold_mm: float = 0.01) -> dict[str, Any]:
    """Board-level numbers, and which footprints moved."""
    from . import pcb

    before, after = pcb.summary(old), pcb.summary(new)
    tracked = (
        "size_mm",
        "layer_count",
        "footprints",
        "nets",
        "pads",
        "tracks",
        "track_length_mm",
        "vias",
        "zones",
    )
    statistics = {
        key: {"from": before.get(key), "to": after.get(key)}
        for key in tracked
        if before.get(key) != after.get(key)
    }

    old_fp = {fp.ref: fp for fp in old.footprints if fp.ref}
    new_fp = {fp.ref: fp for fp in new.footprints if fp.ref}
    moved = []
    for ref in sorted(set(old_fp) & set(new_fp)):
        a, b = old_fp[ref], new_fp[ref]
        distance = math.dist((a.x, a.y), (b.x, b.y))
        if distance > moved_threshold_mm or a.angle != b.angle or a.layer != b.layer:
            entry: dict[str, Any] = {"reference": ref, "moved_mm": round(distance, 3)}
            if a.angle != b.angle:
                entry["angle"] = {"from": a.angle, "to": b.angle}
            if a.layer != b.layer:
                entry["layer"] = {"from": a.layer, "to": b.layer}
            moved.append(entry)
    moved.sort(key=lambda row: row["moved_mm"], reverse=True)

    return {
        "statistics": statistics,
        "placed": sorted(set(new_fp) - set(old_fp)),
        "unplaced": sorted(set(old_fp) - set(new_fp)),
        "moved": moved,
        "identical": not (statistics or moved or set(old_fp) ^ set(new_fp)),
    }


# Anything this far from white counts as drawn-on. Catches dark schematic line
# art and saturated copper alike, while leaving paper alone.
INK_THRESHOLD = 24
REMOVED_COLOUR = (214, 39, 40)
ADDED_COLOUR = (44, 160, 44)


def _ink(image, threshold: int = INK_THRESHOLD):
    """Mask of the pixels that have something drawn on them."""
    from PIL import ImageChops

    red, green, blue = image.split()
    darkest = ImageChops.darker(ImageChops.darker(red, green), blue)
    return darkest.point(lambda v: 255 if (255 - v) > threshold else 0)


def _grew(mask):
    """One pixel of dilation, to absorb anti-aliased edges.

    Renderers anti-alias, so a shape that did not move still lands on slightly
    different subpixels between two runs. Without this, every outline in the
    drawing is fringed in red and green and the real change is lost in it.
    """
    from PIL import ImageFilter

    return mask.filter(ImageFilter.MaxFilter(3))


def compare_images(old_dir: Path, new_dir: Path, out_dir: Path) -> list[dict[str, Any]]:
    """Directional pixel diff of matching renders.

    What is only in the old revision is drawn in red, what is only in the new one
    in green, over a faded copy of the new drawing. A part that moved shows up as
    both: red where it was, green where it is now.
    """
    from PIL import Image

    out = ensure_dir(out_dir)
    results: list[dict[str, Any]] = []
    for old_image in sorted(old_dir.glob("*.png")):
        if old_image.name == "contact-sheet.png":
            continue
        new_image = new_dir / old_image.name
        if not new_image.exists():
            results.append({"view": old_image.stem, "error": "only in the old revision"})
            continue
        with Image.open(old_image) as before_raw, Image.open(new_image) as after_raw:
            before = before_raw.convert("RGB")
            after = after_raw.convert("RGB")
            if before.size != after.size:
                results.append(
                    {
                        "view": old_image.stem,
                        "error": f"plot size changed {before.size} -> {after.size}",
                    }
                )
                continue
            results.append(_compare_one(old_image.stem, before, after, out))
    for new_image in sorted(new_dir.glob("*.png")):
        if new_image.name == "contact-sheet.png" or (old_dir / new_image.name).exists():
            continue
        results.append({"view": new_image.stem, "error": "only in the new revision"})
    return results


def _compare_one(view: str, before, after, out: Path) -> dict[str, Any]:
    from PIL import Image, ImageChops

    old_ink, new_ink = _ink(before), _ink(after)
    removed = ImageChops.subtract(old_ink, _grew(new_ink))
    added = ImageChops.subtract(new_ink, _grew(old_ink))

    removed_pixels = sum(removed.point(lambda v: 1 if v else 0).getdata())
    added_pixels = sum(added.point(lambda v: 1 if v else 0).getdata())
    total = before.width * before.height
    entry: dict[str, Any] = {
        "view": view,
        "removed_pixels": removed_pixels,
        "added_pixels": added_pixels,
        "changed_pixels": removed_pixels + added_pixels,
        "changed_pct": round((removed_pixels + added_pixels) / total * 100.0, 4) if total else 0.0,
    }
    if entry["changed_pixels"]:
        base = after.convert("L").point(lambda v: 200 + v // 5).convert("RGB")
        for mask, colour in ((removed, REMOVED_COLOUR), (added, ADDED_COLOUR)):
            base.paste(Image.new("RGB", before.size, colour), (0, 0), mask)
        dest = out / f"{view}-diff.png"
        base.save(dest)
        entry["image"] = str(dest)

        detail = _detail_crop(base, ImageChops.lighter(removed, added))
        if detail is not None:
            dest = out / f"{view}-diff-detail.png"
            detail.save(dest)
            entry["detail_image"] = str(dest)
    return entry


# Below this share of the page, the change is a speck on an otherwise empty sheet
# and the full view is not worth looking at on its own.
DETAIL_MAX_AREA_FRACTION = 0.4
DETAIL_TARGET_WIDTH = 900
DETAIL_MAX_ZOOM = 4


def _detail_crop(rendered, mask, *, margin_fraction: float = 0.25, min_margin: int = 24):
    """Zoom on where the change is, when the change is small on a big sheet.

    A moved part on an A4 schematic is a few hundred pixels of a two-megapixel
    page. The full view says where it is; this says what it looks like.
    """
    box = mask.getbbox()
    if box is None:
        return None
    left, top, right, bottom = box
    width, height = right - left, bottom - top
    if width * height > DETAIL_MAX_AREA_FRACTION * rendered.width * rendered.height:
        return None  # the change is most of the drawing; the full view is the detail

    margin = max(int(max(width, height) * margin_fraction), min_margin)
    crop = rendered.crop(
        (
            max(0, left - margin),
            max(0, top - margin),
            min(rendered.width, right + margin),
            min(rendered.height, bottom + margin),
        )
    )
    if crop.width < DETAIL_TARGET_WIDTH:  # small crops are worth enlarging
        from PIL import Image as _Image

        # Nearest neighbour, and never past DETAIL_MAX_ZOOM: this is evidence, so
        # it should look like enlarged pixels rather than invent smooth edges.
        scale = min(DETAIL_MAX_ZOOM, max(1, DETAIL_TARGET_WIDTH // max(crop.width, 1)))
        if scale > 1:
            crop = crop.resize((crop.width * scale, crop.height * scale), _Image.Resampling.NEAREST)
    return crop


def build(
    old_target: str | os.PathLike[str],
    new_target: str | os.PathLike[str],
    out_dir: str | os.PathLike[str],
    *,
    images: bool = True,
    dpi: int = 150,
    views: tuple[str, ...] = DEFAULT_VIEWS,
) -> dict[str, Any]:
    """Compare two revisions and write diff.md / diff.json."""
    from . import netlist as netlist_mod
    from . import pcb, render, schematic

    out = ensure_dir(out_dir)
    result: dict[str, Any] = {
        "old": str(old_target),
        "new": str(new_target),
        "out_dir": str(out),
        "sections": {},
        "errors": [],
    }

    def attempt(name: str, func):
        try:
            result["sections"][name] = func()
        except Exception as exc:
            result["errors"].append({"section": name, "error": f"{type(exc).__name__}: {exc}"})

    def both(finder):
        return finder(old_target), finder(new_target)

    if _exists(schematic.find_root_schematic, old_target) and _exists(
        schematic.find_root_schematic, new_target
    ):

        def connectivity():
            old_net = netlist_mod.get(old_target)
            new_net = netlist_mod.get(new_target)
            nets = compare_netlists(old_net, new_net)
            components = compare_components(
                old_net.get("components", []), new_net.get("components", [])
            )
            # Both halves have to agree before the section calls itself
            # unchanged: a re-valued resistor moves no nets at all.
            return {
                "nets": nets,
                "components": components,
                "identical": nets["identical"] and components["identical"],
            }

        attempt("schematic", connectivity)
        if images:

            def sheets():
                old_render = render.render_schematic(old_target, out / "old-sch", dpi=dpi)
                new_render = render.render_schematic(new_target, out / "new-sch", dpi=dpi)
                return {
                    "pages": compare_images(out / "old-sch", out / "new-sch", out / "diff"),
                    "rendered": {
                        "old": len(old_render["images"]),
                        "new": len(new_render["images"]),
                    },
                }

            attempt("schematic_drawing", sheets)

    if _exists(pcb.find_board, old_target) and _exists(pcb.find_board, new_target):
        attempt(
            "board",
            lambda: compare_boards(*(pcb.parse(path) for path in both(pcb.find_board))),
        )
        if images:

            def artwork():
                old_render = render.render_board(
                    old_target, out / "old", views=list(views), dpi=dpi, three_d=False, sheet=False
                )
                new_render = render.render_board(
                    new_target, out / "new", views=list(views), dpi=dpi, three_d=False, sheet=False
                )
                comparisons = compare_images(out / "old", out / "new", out / "diff")
                return {
                    "views": comparisons,
                    "render_errors": old_render["errors"] + new_render["errors"],
                }

            attempt("artwork", artwork)

    drawn = {"artwork", "schematic_drawing"}
    result["identical"] = all(
        section.get("identical", True)
        for name, section in result["sections"].items()
        if name not in drawn
    ) and not any(image.get("changed_pixels") for image in _images(result))

    markdown = render_markdown(result)
    (out / "diff.md").write_text(markdown, encoding="utf-8")
    result["markdown"] = str(out / "diff.md")
    write_json(out / "diff.json", result)
    return result


def _images(result: dict[str, Any]) -> list[dict[str, Any]]:
    """Every rendered comparison, schematic pages and board views alike."""
    sections = result["sections"]
    return list(sections.get("schematic_drawing", {}).get("pages", [])) + list(
        sections.get("artwork", {}).get("views", [])
    )


def _exists(finder, target) -> bool:
    try:
        finder(target)
    except Exception:
        return False
    return True


def _relative(path: str | None, root: Path) -> str | None:
    if not path:
        return None
    try:
        return str(Path(path).resolve().relative_to(root.resolve()))
    except ValueError:
        return path


def render_markdown(result: dict[str, Any]) -> str:
    root = Path(result["out_dir"])
    sections = result["sections"]
    lines = [
        "# Design diff",
        "",
        f"* old: `{result['old']}`",
        f"* new: `{result['new']}`",
        "",
    ]
    if result.get("identical"):
        lines += ["Nothing changed that this compares.", ""]

    schematic = sections.get("schematic")
    if schematic:
        nets = schematic["nets"]
        lines += ["## Connectivity", ""]
        if nets["identical"]:
            lines += [f"Unchanged, across {nets['unchanged']} nets.", ""]
        else:
            for entry in nets["renamed"]:
                lines.append(f"* renamed **{entry['from']}** to **{entry['to']}** (same pins)")
            for entry in nets["added"]:
                lines.append(
                    f"* new net **{entry['net']}**: {', '.join(entry['pins']) or '(none)'}"
                )
            for entry in nets["removed"]:
                lines.append(f"* removed net **{entry['net']}**")
            for entry in nets["changed"]:
                gained = ", ".join(entry["added_pins"]) or "-"
                lost = ", ".join(entry["removed_pins"]) or "-"
                lines.append(f"* **{entry['net']}**: gained {gained}, lost {lost}")
            lines.append("")

        components = schematic["components"]
        lines += ["## Components", ""]
        if components["identical"]:
            lines += [f"Unchanged, across {components['unchanged']} parts.", ""]
        else:
            for entry in components["added"]:
                lines.append(f"* added **{entry['reference']}** ({entry['value']})")
            for entry in components["removed"]:
                lines.append(f"* removed **{entry['reference']}** ({entry['value']})")
            for entry in components["changed"]:
                fields = ", ".join(
                    f"{name} {change['from']!r} -> {change['to']!r}"
                    for name, change in entry["fields"].items()
                )
                lines.append(f"* **{entry['reference']}**: {fields}")
            lines.append("")

    board = sections.get("board")
    if board:
        lines += ["## Board", ""]
        if board["identical"]:
            lines += ["Unchanged.", ""]
        else:
            if board["statistics"]:
                lines += ["| what | before | after |", "| --- | --- | --- |"]
                for key, change in board["statistics"].items():
                    lines.append(f"| {key} | {change['from']} | {change['to']} |")
                lines.append("")
            for ref in board["placed"]:
                lines.append(f"* placed **{ref}**")
            for ref in board["unplaced"]:
                lines.append(f"* removed **{ref}** from the board")
            if board["moved"]:
                shown = board["moved"][:15]
                moved = ", ".join(f"{m['reference']} ({m['moved_mm']} mm)" for m in shown)
                more = len(board["moved"]) - len(shown)
                lines.append(f"* moved: {moved}" + (f", and {more} more" if more > 0 else ""))
            lines.append("")

    for title, images in (
        ("Schematic drawing", sections.get("schematic_drawing", {}).get("pages", [])),
        ("Artwork", sections.get("artwork", {}).get("views", [])),
    ):
        if not images:
            continue
        lines += [
            f"## {title}",
            "",
            "Red is what the old revision had and the new one does not; green is "
            "the other way round. A part that moved appears as both.",
            "",
        ]
        for image in images:
            if image.get("error"):
                lines.append(f"* `{image['view']}`: {image['error']}")
            elif not image["changed_pixels"]:
                lines.append(f"* `{image['view']}`: identical")
            else:
                lines.append(
                    f"* `{image['view']}`: {image['changed_pct']}% of pixels changed "
                    f"({image['removed_pixels']} removed, {image['added_pixels']} added)"
                )
        lines.append("")
        for image in images:
            if image.get("detail_image"):
                lines.append(
                    f"![{image['view']} (detail)]({_relative(image['detail_image'], root)})"
                )
            if image.get("image"):
                lines.append(f"![{image['view']}]({_relative(image['image'], root)})")
        lines.append("")

    if result["errors"]:
        lines += ["## Sections that failed", ""]
        for error in result["errors"]:
            lines.append(f"* **{error['section']}**: {error['error']}")
        lines.append("")

    return "\n".join(lines) + "\n"

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
* **The artwork** by rendering both and comparing pixels, which is the only one
  of the four that catches "the pour changed shape".

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


def compare_images(old_dir: Path, new_dir: Path, out_dir: Path) -> list[dict[str, Any]]:
    """Pixel diff of matching renders: the only check that sees a reshaped pour."""
    from PIL import Image, ImageChops

    out = ensure_dir(out_dir)
    results: list[dict[str, Any]] = []
    for old_image in sorted(old_dir.glob("*.png")):
        if old_image.name == "contact-sheet.png":
            continue
        new_image = new_dir / old_image.name
        if not new_image.exists():
            results.append({"view": old_image.stem, "error": "only in the old revision"})
            continue
        with Image.open(old_image) as before, Image.open(new_image) as after:
            before = before.convert("RGB")
            after = after.convert("RGB")
            if before.size != after.size:
                results.append(
                    {
                        "view": old_image.stem,
                        "error": f"plot size changed {before.size} -> {after.size}",
                    }
                )
                continue
            difference = ImageChops.difference(before, after).convert("L")
            changed = sum(1 for pixel in difference.getdata() if pixel > 16)
            total = difference.width * difference.height
            entry: dict[str, Any] = {
                "view": old_image.stem,
                "changed_pixels": changed,
                "changed_pct": round(changed / total * 100.0, 4) if total else 0.0,
            }
            if changed:
                # Faded original underneath, the difference in red on top, so the
                # change is readable in place rather than as two images to flick
                # between.
                base = after.convert("L").point(lambda v: 190 + v // 6).convert("RGB")
                mask = difference.point(lambda v: 255 if v > 16 else 0)
                overlay = Image.new("RGB", after.size, (220, 30, 30))
                base.paste(overlay, (0, 0), mask)
                dest = out / f"{old_image.stem}-diff.png"
                base.save(dest)
                entry["image"] = str(dest)
            results.append(entry)
    return results


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

    result["identical"] = all(
        section.get("identical", True)
        for name, section in result["sections"].items()
        if name != "artwork"
    ) and not any(view.get("changed_pixels") for view in _views(result))

    markdown = render_markdown(result)
    (out / "diff.md").write_text(markdown, encoding="utf-8")
    result["markdown"] = str(out / "diff.md")
    write_json(out / "diff.json", result)
    return result


def _views(result: dict[str, Any]) -> list[dict[str, Any]]:
    return result["sections"].get("artwork", {}).get("views", [])


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

    views = _views(result)
    if views:
        lines += ["## Artwork", ""]
        for view in views:
            if view.get("error"):
                lines.append(f"* `{view['view']}`: {view['error']}")
            elif not view["changed_pixels"]:
                lines.append(f"* `{view['view']}`: identical")
            else:
                lines.append(f"* `{view['view']}`: {view['changed_pct']}% of pixels changed")
        lines.append("")
        for view in views:
            if view.get("image"):
                lines.append(f"![{view['view']}]({_relative(view['image'], root)})")
        lines.append("")

    if result["errors"]:
        lines += ["## Sections that failed", ""]
        for error in result["errors"]:
            lines.append(f"* **{error['section']}**: {error['error']}")
        lines.append("")

    return "\n".join(lines) + "\n"

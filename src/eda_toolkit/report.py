"""One command that makes a project's state visible.

Headless development has a blind spot: the agent (and the reviewer reading the
transcript afterwards) never *sees* the design. This walks a KiCad project end
to end - schematic, board, reviews, BOM, optional simulation - and writes a
directory with every artefact plus `report.md` and `report.html` that put the
pictures and the numbers on one page.

    eda report hardware/ -o build/report
"""

from __future__ import annotations

import html
import os
from pathlib import Path
from typing import Any

from .util import ensure_dir, write_json

SEVERITY_COLOUR = {"error": "#c0392b", "warning": "#c87f0a", "info": "#2c6fbb"}


def build(
    target: str | os.PathLike[str],
    out_dir: str | os.PathLike[str],
    *,
    dpi: int = 200,
    three_d: bool = True,
    per_layer: bool = True,
    glb: bool = False,
    bom: bool = True,
    simulation: str | os.PathLike[str] | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """Collect everything and write report.md / report.html / report.json."""
    from .kicad import fab, pcb, pcb_review, render, sch_review, schematic

    out = ensure_dir(out_dir)
    report: dict[str, Any] = {
        "target": str(target),
        "out_dir": str(out),
        "title": title or Path(str(target)).resolve().name,
        "sections": {},
        "errors": [],
    }

    def attempt(name: str, func):
        try:
            report["sections"][name] = func()
        except Exception as exc:
            report["errors"].append({"section": name, "error": f"{type(exc).__name__}: {exc}"})

    has_sch = _exists(schematic.find_root_schematic, target)
    has_pcb = _exists(pcb.find_board, target)

    if has_sch:
        attempt("schematic_review", lambda: sch_review.review(target))
        attempt(
            "schematic_images", lambda: render.render_schematic(target, out / "schematic", dpi=dpi)
        )
        if bom:
            attempt("bom", lambda: _bom_summary(fab, target, out / "bom.csv"))
    if has_pcb:
        attempt("board_review", lambda: pcb_review.review(target))
        attempt(
            "board_images",
            lambda: render.render_board(
                target, out / "board", dpi=dpi, three_d=three_d, per_layer=per_layer, glb=glb
            ),
        )
    if simulation:
        attempt("simulation", lambda: _simulation(simulation, out / "simulation"))

    markdown = render_markdown(report)
    (out / "report.md").write_text(markdown, encoding="utf-8")
    (out / "report.html").write_text(render_html(report), encoding="utf-8")
    report["markdown"] = str(out / "report.md")
    report["html"] = str(out / "report.html")
    write_json(out / "report.json", report)
    return report


def _exists(finder, target) -> bool:
    try:
        finder(target)
    except Exception:
        return False
    return True


def _bom_summary(fab_module, target, dest: Path) -> dict[str, Any]:
    result = fab_module.bom(target, dest)
    rows = result.pop("rows", [])
    result["preview"] = rows[:15]
    return result


def _simulation(netlist: str | os.PathLike[str], out: Path) -> dict[str, Any]:
    from .spice import runner

    summary = runner.run_netlist(netlist, out)
    return {
        "netlist": summary["netlist"],
        "ok": summary["ok"],
        "plots": [
            {
                "analysis": p["analysis"],
                "image": p.get("plot"),
                "csv": p.get("csv"),
                "measurements": p.get("measurements", {}),
            }
            for p in summary["plots"]
        ],
    }


# -- rendering -------------------------------------------------------------


def _relative(path: str | None, root: Path) -> str | None:
    if not path:
        return None
    try:
        return str(Path(path).resolve().relative_to(root.resolve()))
    except ValueError:
        return path


def _summary_line(review: dict[str, Any]) -> str:
    counts = review.get("summary", {})
    return ", ".join(f"{counts.get(s, 0)} {s}" for s in ("error", "warning", "info"))


def _findings_rows(review: dict[str, Any], limit: int = 25) -> list[dict[str, str]]:
    rows = []
    for finding in review.get("findings", [])[:limit]:
        rows.append(
            {
                "severity": finding["severity"],
                "rule": finding["rule"],
                "location": finding.get("location", "") or "",
                "message": finding["message"],
            }
        )
    return rows


def render_markdown(report: dict[str, Any]) -> str:
    root = Path(report["out_dir"])
    sections = report["sections"]
    lines: list[str] = [f"# {report['title']}", ""]

    sch = sections.get("schematic_review")
    board = sections.get("board_review")
    if sch or board:
        lines += ["## Verdict", ""]
        if sch:
            lines.append(f"* schematic: {_summary_line(sch)}")
        if board:
            lines.append(f"* board: {_summary_line(board)}")
            stats = board.get("statistics", {})
            size = stats.get("size_mm")
            if size:
                lines.append(
                    f"* {size[0]} x {size[1]} mm, {stats.get('layer_count')} layers, "
                    f"{stats.get('footprints')} footprints, {stats.get('nets')} nets, "
                    f"{stats.get('vias')} vias"
                )
        lines.append("")

    images = sections.get("schematic_images", {})
    if images.get("images"):
        lines += ["## Schematic", ""]
        for image in images["images"]:
            lines.append(f"![schematic]({_relative(image, root)})")
        if images.get("pdf"):
            lines.append(f"\n[schematic PDF]({_relative(images['pdf'], root)})")
        lines.append("")

    board_images = sections.get("board_images", {})
    if board_images.get("images"):
        lines += ["## Board", ""]
        if board_images.get("contact_sheet"):
            lines.append(f"![all layers]({_relative(board_images['contact_sheet'], root)})")
            lines.append("")
        for image in board_images["images"]:
            if image["view"].startswith("3d"):
                lines.append(f"![{image['view']}]({_relative(image['path'], root)})")
        if board_images.get("glb"):
            lines.append(f"\n[3D model (GLB)]({_relative(board_images['glb'], root)})")
        lines.append("")

    for name, review in (("Schematic findings", sch), ("Board findings", board)):
        rows = _findings_rows(review) if review else []
        if not rows:
            continue
        lines += [
            f"## {name}",
            "",
            "| severity | rule | where | message |",
            "| --- | --- | --- | --- |",
        ]
        for row in rows:
            message = row["message"].replace("|", "\\|")
            lines.append(f"| {row['severity']} | `{row['rule']}` | {row['location']} | {message} |")
        total = len(review.get("findings", []))
        if total > len(rows):
            lines.append(f"\n_{total - len(rows)} more in report.json_")
        lines.append("")

    simulation = sections.get("simulation")
    if simulation:
        lines += ["## Simulation", ""]
        for plot in simulation["plots"]:
            if plot.get("image"):
                lines.append(f"![{plot['analysis']}]({_relative(plot['image'], root)})")
        lines.append("")

    bom = sections.get("bom")
    if bom:
        lines += [
            "## Bill of materials",
            "",
            f"{bom['line_items']} line items, {bom['total_parts']} parts "
            f"([CSV]({_relative(bom['csv'], root)}))",
            "",
        ]

    if report["errors"]:
        lines += ["## Sections that failed", ""]
        for error in report["errors"]:
            lines.append(f"* **{error['section']}**: {error['error']}")
        lines.append("")

    return "\n".join(lines) + "\n"


def render_html(report: dict[str, Any]) -> str:
    root = Path(report["out_dir"])
    sections = report["sections"]
    parts: list[str] = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        f"<title>{html.escape(report['title'])}</title>",
        "<style>",
        "body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;margin:0 auto;"
        "max-width:1100px;padding:2rem 1.5rem;color:#1a1a1a;line-height:1.55}",
        "h1{margin-top:0}h2{margin-top:2.5rem;border-bottom:1px solid #e4e4e7;padding-bottom:.3rem}",
        "img{max-width:100%;border:1px solid #e4e4e7;border-radius:6px;margin:.5rem 0;"
        "background:#fff}",
        "table{border-collapse:collapse;width:100%;font-size:.92rem}",
        "th,td{border-bottom:1px solid #e4e4e7;padding:.4rem .5rem;text-align:left;"
        "vertical-align:top}",
        "code{background:#f4f4f5;padding:.1rem .3rem;border-radius:3px}",
        ".pill{display:inline-block;padding:.15rem .55rem;border-radius:999px;color:#fff;"
        "font-size:.8rem}",
        ".grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:1rem}",
        "@media (prefers-color-scheme:dark){body{background:#111;color:#eee}"
        "code{background:#222}th,td{border-color:#333}img{border-color:#333}}",
        "</style></head><body>",
        f"<h1>{html.escape(report['title'])}</h1>",
    ]

    sch = sections.get("schematic_review")
    board = sections.get("board_review")
    if sch or board:
        parts.append("<h2>Verdict</h2><ul>")
        if sch:
            parts.append(f"<li>schematic: {html.escape(_summary_line(sch))}</li>")
        if board:
            stats = board.get("statistics", {})
            size = stats.get("size_mm")
            parts.append(f"<li>board: {html.escape(_summary_line(board))}</li>")
            if size:
                parts.append(
                    f"<li>{size[0]} x {size[1]} mm, {stats.get('layer_count')} layers, "
                    f"{stats.get('footprints')} footprints, {stats.get('nets')} nets</li>"
                )
        parts.append("</ul>")

    images = sections.get("schematic_images", {})
    if images.get("images"):
        parts.append("<h2>Schematic</h2>")
        for image in images["images"]:
            parts.append(f"<img src='{_relative(image, root)}' alt='schematic'>")
        if images.get("pdf"):
            parts.append(f"<p><a href='{_relative(images['pdf'], root)}'>schematic PDF</a></p>")

    board_images = sections.get("board_images", {})
    if board_images.get("images"):
        parts.append("<h2>Board</h2>")
        if board_images.get("contact_sheet"):
            parts.append(
                f"<img src='{_relative(board_images['contact_sheet'], root)}' alt='all layers'>"
            )
        parts.append("<div class='grid'>")
        for image in board_images["images"]:
            if image["view"].startswith("3d"):
                parts.append(f"<img src='{_relative(image['path'], root)}' alt='{image['view']}'>")
        parts.append("</div>")
        if board_images.get("glb"):
            parts.append(
                f"<p><a href='{_relative(board_images['glb'], root)}'>3D model (GLB)</a></p>"
            )

    for name, review in (("Schematic findings", sch), ("Board findings", board)):
        rows = _findings_rows(review) if review else []
        if not rows:
            continue
        parts.append(
            f"<h2>{name}</h2><table><tr><th>severity</th><th>rule</th>"
            "<th>where</th><th>message</th></tr>"
        )
        for row in rows:
            colour = SEVERITY_COLOUR.get(row["severity"], "#666")
            parts.append(
                f"<tr><td><span class='pill' style='background:{colour}'>"
                f"{row['severity']}</span></td>"
                f"<td><code>{html.escape(row['rule'])}</code></td>"
                f"<td>{html.escape(row['location'])}</td>"
                f"<td>{html.escape(row['message'])}</td></tr>"
            )
        parts.append("</table>")

    simulation = sections.get("simulation")
    if simulation:
        parts.append("<h2>Simulation</h2>")
        for plot in simulation["plots"]:
            if plot.get("image"):
                parts.append(
                    f"<img src='{_relative(plot['image'], root)}' alt='{plot['analysis']}'>"
                )

    bom = sections.get("bom")
    if bom:
        parts.append(
            f"<h2>Bill of materials</h2><p>{bom['line_items']} line items, "
            f"{bom['total_parts']} parts "
            f"(<a href='{_relative(bom['csv'], root)}'>CSV</a>)</p>"
        )

    if report["errors"]:
        parts.append("<h2>Sections that failed</h2><ul>")
        for error in report["errors"]:
            parts.append(
                f"<li><strong>{html.escape(error['section'])}</strong>: "
                f"{html.escape(error['error'])}</li>"
            )
        parts.append("</ul>")

    parts.append("</body></html>")
    return "\n".join(parts)

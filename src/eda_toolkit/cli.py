"""``eda`` command line interface - the entry point used by the skills."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .util import EdaError, ensure_dir, emit, write_json


# ---------------------------------------------------------------- rendering


def _render_findings(payload: dict[str, Any]) -> None:
    target = payload.get("schematic") or payload.get("board") or "?"
    print(f"# review of {target}")
    stats = payload.get("statistics", {})
    if stats:
        print("\n## statistics")
        for key, value in stats.items():
            print(f"  {key}: {value}")
    summary = payload.get("summary", {})
    print("\n## summary: " + ", ".join(f"{k}={v}" for k, v in summary.items()))
    print("\n## findings")
    for finding in payload.get("findings", []):
        location = f" [{finding['location']}]" if finding.get("location") else ""
        print(f"  {finding['severity'].upper():7s} {finding['rule']}{location}: {finding['message']}")


def _render_search(payload: dict[str, Any]) -> None:
    print(f"# datasheet candidates for {payload['part']} "
          f"(providers: {', '.join(payload['providers'])})")
    for cand in payload["candidates"]:
        print(f"  [{cand['score']:.1f}] {cand['url']}")
        if cand.get("manufacturer") or cand.get("title"):
            print(f"         {cand.get('manufacturer', '')} {cand.get('title', '')}".rstrip())
    for err in payload.get("errors", []):
        print(f"  ! {err['provider']}: {err['error']}")


# ---------------------------------------------------------------- doctor


def cmd_doctor(args: argparse.Namespace) -> int:
    from . import util
    from .kicad import kicad_cli
    from .spice import runner as spice_runner

    report: dict[str, Any] = {"eda_toolkit": __version__, "python": sys.version.split()[0]}
    report["kicad_cli"] = kicad_cli.version() if kicad_cli.available() else None
    report["ngspice"] = spice_runner.version() if spice_runner.available() else None
    report["tesseract"] = bool(util.which("tesseract"))
    modules = {}
    for name in ("pdfplumber", "pypdfium2", "numpy", "matplotlib", "requests", "bs4", "PIL"):
        try:
            __import__(name)
            modules[name] = True
        except ImportError:
            modules[name] = False
    report["python_modules"] = modules
    report["cache_dir"] = str(_cache_dir())
    report["in_container"] = Path("/.dockerenv").exists()
    report["ok"] = bool(report["kicad_cli"] and report["ngspice"] and all(modules.values()))
    emit(report, as_json=True)
    return 0 if report["ok"] else 1


def _cache_dir():
    from .datasheet.fetch import cache_dir

    return cache_dir()


# ---------------------------------------------------------------- datasheet


def cmd_datasheet_search(args: argparse.Namespace) -> int:
    from .datasheet import providers

    payload = providers.search(args.part, limit=args.limit, providers=args.provider)
    emit(payload, as_json=args.json, text_renderer=_render_search)
    return 0


def cmd_datasheet_fetch(args: argparse.Namespace) -> int:
    from .datasheet import fetch

    if args.url:
        payload = fetch.download(args.url, dest=args.output, force=args.force)
    else:
        if not args.part:
            raise EdaError("give a part number or --url")
        payload = fetch.fetch_part(args.part, dest=args.output, limit=args.limit,
                                   provider_names=args.provider, force=args.force)
    emit(payload, as_json=True)
    return 0


def cmd_datasheet_info(args: argparse.Namespace) -> int:
    from .datasheet import parse

    emit(parse.info(args.pdf), as_json=True)
    return 0


def cmd_datasheet_text(args: argparse.Namespace) -> int:
    from .datasheet import parse

    pages = parse.extract_text(args.pdf, args.pages, layout=args.layout, ocr=args.ocr)
    if args.output:
        dest = Path(args.output)
        ensure_dir(dest.parent)
        dest.write_text(
            "".join(f"\n\n===== page {p['page']} =====\n{p['text']}" for p in pages),
            encoding="utf-8",
        )
        emit({"pdf": args.pdf, "output": str(dest), "pages": len(pages)}, as_json=True)
    elif args.json:
        emit(pages, as_json=True)
    else:
        for page in pages:
            print(f"\n===== page {page['page']} ({page['source']}) =====")
            print(page["text"])
    return 0


def cmd_datasheet_tables(args: argparse.Namespace) -> int:
    from .datasheet import parse

    tables = parse.extract_tables(args.pdf, args.pages)
    emit(tables, as_json=True)
    return 0


def cmd_datasheet_images(args: argparse.Namespace) -> int:
    from .datasheet import parse

    emit(parse.extract_images(args.pdf, args.output, args.pages), as_json=True)
    return 0


def cmd_datasheet_pages(args: argparse.Namespace) -> int:
    from .datasheet import parse

    emit(parse.render_pages(args.pdf, args.output, args.pages, dpi=args.dpi), as_json=True)
    return 0


def cmd_datasheet_find(args: argparse.Namespace) -> int:
    from .datasheet import parse

    emit(parse.find(args.pdf, args.query, regex=args.regex, context=args.context), as_json=True)
    return 0


def cmd_datasheet_parse(args: argparse.Namespace) -> int:
    from .datasheet import parse

    payload = parse.parse_all(
        args.pdf,
        args.output,
        pages=args.pages,
        want_images=not args.no_images,
        want_tables=not args.no_tables,
        want_renders=args.renders,
        dpi=args.dpi,
        ocr=args.ocr,
    )
    if args.quiet:
        payload.pop("outline", None)
    emit(payload, as_json=True)
    return 0


# ---------------------------------------------------------------- simulation


def cmd_sim_run(args: argparse.Namespace) -> int:
    from .spice import runner

    payload = runner.run_netlist(args.netlist, args.output, timeout=args.timeout,
                                 make_plots=not args.no_plots)
    emit(payload, as_json=True)
    return 0 if payload.get("ok") else 2


def cmd_sim_lint(args: argparse.Namespace) -> int:
    from .spice import runner

    text = Path(args.netlist).read_text(encoding="utf-8", errors="replace")
    problems = runner.lint_netlist(text)
    emit({"netlist": args.netlist, "problems": problems, "ok": not problems}, as_json=True)
    return 0 if not problems else 1


def cmd_sim_measure(args: argparse.Namespace) -> int:
    from .spice import measure, rawfile

    plots = rawfile.parse(args.raw)
    payload: list[dict[str, Any]] = []
    for plot in plots:
        entry = plot.to_dict()
        entry["measurements"] = measure.measure(plot)
        if args.thd and plot.analysis == "tran":
            try:
                entry["thd"] = measure.thd(plot, args.thd, args.fundamental,
                                           skip_seconds=args.skip)
            except Exception as exc:
                entry["thd_error"] = str(exc)
        payload.append(entry)
    emit(payload, as_json=True)
    return 0


def cmd_sim_plot(args: argparse.Namespace) -> int:
    from .spice import rawfile, runner

    plots = rawfile.parse(args.raw)
    out_dir = ensure_dir(args.output)
    written = []
    for index, plot in enumerate(plots, start=1):
        dest = out_dir / f"{index:02d}-{plot.analysis}.png"
        runner.plot_png(plot, dest, signals=args.signals)
        written.append(str(dest))
    emit({"raw": args.raw, "images": written}, as_json=True)
    return 0


def cmd_sim_netlist(args: argparse.Namespace) -> int:
    from .spice import runner

    dest = runner.netlist_from_schematic(args.schematic, args.output, fmt=args.format)
    emit({"schematic": args.schematic, "netlist": str(dest), "format": args.format}, as_json=True)
    return 0


# ---------------------------------------------------------------- schematic


def cmd_sch_info(args: argparse.Namespace) -> int:
    from .kicad import sch_review

    emit(sch_review.info(args.target, use_cli=not args.no_cli), as_json=True)
    return 0


def cmd_sch_review(args: argparse.Namespace) -> int:
    from .kicad import sch_review

    payload = sch_review.review(args.target, use_cli=not args.no_cli)
    emit(payload, as_json=args.json, text_renderer=_render_findings)
    if args.output:
        write_json(args.output, payload)
    return 2 if payload["summary"]["error"] else 0


def cmd_sch_erc(args: argparse.Namespace) -> int:
    from .kicad import kicad_cli, schematic

    sch = schematic.find_root_schematic(args.target)
    emit(kicad_cli.erc(sch), as_json=True)
    return 0


def cmd_sch_netlist(args: argparse.Namespace) -> int:
    from .kicad import netlist as netlist_mod

    if args.format == "json":
        payload = netlist_mod.get(args.target, prefer_cli=not args.no_cli)
        if args.output:
            write_json(args.output, payload)
            emit({"output": args.output, "nets": len(payload["nets"])}, as_json=True)
        else:
            emit(payload, as_json=True)
        return 0

    from .kicad import kicad_cli, schematic

    sch = schematic.find_root_schematic(args.target)
    dest = kicad_cli.export_netlist(sch, args.output or "netlist.net", fmt=args.format)
    emit({"schematic": str(sch), "netlist": str(dest), "format": args.format}, as_json=True)
    return 0


def cmd_sch_render(args: argparse.Namespace) -> int:
    from .kicad import render

    emit(render.render_schematic(args.target, args.output, dpi=args.dpi), as_json=True)
    return 0


# ---------------------------------------------------------------- board


def cmd_pcb_info(args: argparse.Namespace) -> int:
    from .kicad import pcb_review

    emit(pcb_review.info(args.target), as_json=True)
    return 0


def cmd_pcb_review(args: argparse.Namespace) -> int:
    from .kicad import pcb_review

    thresholds = {}
    for item in args.threshold or []:
        key, _, value = item.partition("=")
        if not value:
            raise EdaError(f"--threshold expects key=value, got {item!r}")
        thresholds[key.strip()] = float(value)
    payload = pcb_review.review(args.target, use_cli=not args.no_cli, thresholds=thresholds)
    emit(payload, as_json=args.json, text_renderer=_render_findings)
    if args.output:
        write_json(args.output, payload)
    return 2 if payload["summary"]["error"] else 0


def cmd_pcb_drc(args: argparse.Namespace) -> int:
    from .kicad import kicad_cli, pcb

    board = pcb.find_board(args.target)
    emit(kicad_cli.drc(board, schematic_parity=not args.no_parity), as_json=True)
    return 0


def cmd_pcb_render(args: argparse.Namespace) -> int:
    from .kicad import render

    payload = render.render_board(
        args.target,
        args.output,
        views=args.views,
        dpi=args.dpi,
        three_d=not args.no_3d,
        per_layer=args.per_layer,
    )
    emit(payload, as_json=True)
    return 0


def cmd_pcb_stats(args: argparse.Namespace) -> int:
    from .kicad import kicad_cli, pcb

    board = pcb.find_board(args.target)
    emit({"board": str(board), "kicad_stats": kicad_cli.board_stats(board),
          "parsed": pcb.summary(pcb.parse(board))}, as_json=True)
    return 0


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eda",
        description="Circuit design toolkit: datasheets, SPICE simulation, KiCad review.",
    )
    parser.add_argument("--version", action="version", version=f"eda-toolkit {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="report the tool versions available in this environment").set_defaults(
        func=cmd_doctor
    )

    # -- datasheet --------------------------------------------------------
    ds = sub.add_parser("datasheet", help="find, download and parse datasheets").add_subparsers(
        dest="subcommand", required=True
    )

    p = ds.add_parser("search", help="search datasheet URLs for a part number")
    p.add_argument("part")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--provider", action="append",
                   help="restrict to a provider (mouser/digikey/nexar/searxng/duckduckgo)")
    p.add_argument("--json", action="store_true", default=True)
    p.add_argument("--text", dest="json", action="store_false")
    p.set_defaults(func=cmd_datasheet_search)

    p = ds.add_parser("fetch", help="download the datasheet PDF for a part (or a URL)")
    p.add_argument("part", nargs="?")
    p.add_argument("--url")
    p.add_argument("-o", "--output", help="output file or directory (default: cache)")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--provider", action="append")
    p.add_argument("--force", action="store_true", help="ignore the cache")
    p.set_defaults(func=cmd_datasheet_fetch)

    p = ds.add_parser("info", help="page count, metadata and text density")
    p.add_argument("pdf")
    p.set_defaults(func=cmd_datasheet_info)

    p = ds.add_parser("text", help="extract text")
    p.add_argument("pdf")
    p.add_argument("--pages", help="e.g. 1-5,12")
    p.add_argument("--layout", action="store_true", help="keep the visual layout")
    p.add_argument("--ocr", action="store_true", help="OCR pages without a text layer")
    p.add_argument("-o", "--output")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_datasheet_text)

    p = ds.add_parser("tables", help="extract tables as JSON")
    p.add_argument("pdf")
    p.add_argument("--pages")
    p.set_defaults(func=cmd_datasheet_tables)

    p = ds.add_parser("images", help="extract embedded images")
    p.add_argument("pdf")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--pages")
    p.set_defaults(func=cmd_datasheet_images)

    p = ds.add_parser("pages", help="render pages to PNG (for figures and curves)")
    p.add_argument("pdf")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--pages")
    p.add_argument("--dpi", type=int, default=150)
    p.set_defaults(func=cmd_datasheet_pages)

    p = ds.add_parser("find", help="locate text and get the page numbers")
    p.add_argument("pdf")
    p.add_argument("query", nargs="+")
    p.add_argument("--regex", action="store_true")
    p.add_argument("--context", type=int, default=200)
    p.set_defaults(func=cmd_datasheet_find)

    p = ds.add_parser("parse", help="one-shot extraction into a directory")
    p.add_argument("pdf")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--pages")
    p.add_argument("--no-images", action="store_true")
    p.add_argument("--no-tables", action="store_true")
    p.add_argument("--renders", action="store_true", help="also rasterise the pages")
    p.add_argument("--dpi", type=int, default=150)
    p.add_argument("--ocr", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_datasheet_parse)

    # -- sim --------------------------------------------------------------
    sim = sub.add_parser("sim", help="analog simulation with ngspice").add_subparsers(
        dest="subcommand", required=True
    )

    p = sim.add_parser("run", help="run a SPICE deck and summarise the results")
    p.add_argument("netlist")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--no-plots", action="store_true")
    p.set_defaults(func=cmd_sim_run)

    p = sim.add_parser("lint", help="sanity check a SPICE deck without running it")
    p.add_argument("netlist")
    p.set_defaults(func=cmd_sim_lint)

    p = sim.add_parser("measure", help="measurements from an existing rawfile")
    p.add_argument("raw")
    p.add_argument("--thd", help="signal name for THD analysis")
    p.add_argument("--fundamental", type=float, default=1000.0)
    p.add_argument("--skip", type=float, help="seconds to skip before the THD window")
    p.set_defaults(func=cmd_sim_measure)

    p = sim.add_parser("plot", help="plot an existing rawfile")
    p.add_argument("raw")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--signals", nargs="*")
    p.set_defaults(func=cmd_sim_plot)

    p = sim.add_parser("netlist", help="export a SPICE netlist from a KiCad schematic")
    p.add_argument("schematic")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--format", default="spice", choices=["spice", "spicemodel"])
    p.set_defaults(func=cmd_sim_netlist)

    # -- schematic --------------------------------------------------------
    sch = sub.add_parser("sch", help="read and review KiCad schematics").add_subparsers(
        dest="subcommand", required=True
    )

    p = sch.add_parser("info", help="components, nets and hierarchy as JSON")
    p.add_argument("target", help=".kicad_sch, .kicad_pro or project directory")
    p.add_argument("--no-cli", action="store_true", help="skip kicad-cli, parse files only")
    p.set_defaults(func=cmd_sch_info)

    p = sch.add_parser("review", help="ERC plus design heuristics")
    p.add_argument("target")
    p.add_argument("--no-cli", action="store_true")
    p.add_argument("-o", "--output", help="also write the JSON report here")
    p.add_argument("--json", action="store_true", default=True)
    p.add_argument("--text", dest="json", action="store_false")
    p.set_defaults(func=cmd_sch_review)

    p = sch.add_parser("erc", help="raw kicad-cli ERC report")
    p.add_argument("target")
    p.set_defaults(func=cmd_sch_erc)

    p = sch.add_parser("netlist", help="export the netlist")
    p.add_argument("target")
    p.add_argument("-o", "--output")
    p.add_argument("--format", default="json",
                   choices=["json", "kicadxml", "kicadsexpr", "spice", "orcadpcb2", "cadstar"])
    p.add_argument("--no-cli", action="store_true")
    p.set_defaults(func=cmd_sch_netlist)

    p = sch.add_parser("render", help="plot the schematic to PNG")
    p.add_argument("target")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--dpi", type=int, default=200)
    p.set_defaults(func=cmd_sch_render)

    # -- pcb --------------------------------------------------------------
    pcb_p = sub.add_parser("pcb", help="read and review KiCad boards").add_subparsers(
        dest="subcommand", required=True
    )

    p = pcb_p.add_parser("info", help="stackup, footprints, nets and routing as JSON")
    p.add_argument("target", help=".kicad_pcb, .kicad_pro or project directory")
    p.set_defaults(func=cmd_pcb_info)

    p = pcb_p.add_parser("review", help="DRC plus layout heuristics")
    p.add_argument("target")
    p.add_argument("--no-cli", action="store_true")
    p.add_argument("--threshold", action="append", metavar="KEY=VALUE",
                   help="override a review threshold, e.g. min_track_mm=0.2")
    p.add_argument("-o", "--output")
    p.add_argument("--json", action="store_true", default=True)
    p.add_argument("--text", dest="json", action="store_false")
    p.set_defaults(func=cmd_pcb_review)

    p = pcb_p.add_parser("drc", help="raw kicad-cli DRC report")
    p.add_argument("target")
    p.add_argument("--no-parity", action="store_true", help="skip schematic parity checks")
    p.set_defaults(func=cmd_pcb_drc)

    p = pcb_p.add_parser("render", help="plot layers and 3D views to PNG")
    p.add_argument("target")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--views", nargs="*", help="front back copper-front ... or layer:F.Cu")
    p.add_argument("--dpi", type=int, default=300)
    p.add_argument("--no-3d", action="store_true")
    p.add_argument("--per-layer", action="store_true")
    p.set_defaults(func=cmd_pcb_render)

    p = pcb_p.add_parser("stats", help="board statistics")
    p.add_argument("target")
    p.set_defaults(func=cmd_pcb_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except EdaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:  # pragma: no cover
        return 0
    except KeyboardInterrupt:  # pragma: no cover
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

"""``eda`` command line interface - the entry point used by the skills."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .util import COLLAPSE_LIMIT, EdaError, emit, ensure_dir, write_json

# ---------------------------------------------------------------- rendering


def _thresholds(items: list[str] | None) -> dict[str, float]:
    """Parse repeated ``--threshold key=value`` arguments."""
    out: dict[str, float] = {}
    for item in items or []:
        key, _, value = item.partition("=")
        if not value:
            raise EdaError(f"--threshold expects key=value, got {item!r}")
        try:
            out[key.strip()] = float(value)
        except ValueError as exc:
            raise EdaError(f"--threshold expects a number, got {value!r}") from exc
    return out


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
        print(
            f"  {finding['severity'].upper():7s} {finding['rule']}{location}: {finding['message']}"
        )


def _render_gate(payload: dict[str, Any]) -> None:
    verdict = "PASS" if payload["pass"] else "FAIL"
    policy = payload["policy"]
    print(f"# gate {verdict}: {payload['target']} against policy '{policy['name']}'")
    print(f"  {policy['description']}")
    for stage in ("schematic", "board"):
        section = payload[stage]
        if "skipped" in section:
            print(f"\n## {stage}: skipped - {section['skipped']}")
        else:
            counts = ", ".join(f"{k}={v}" for k, v in section["summary"].items())
            print(f"\n## {stage}: {section.get(stage, '')} ({counts} as reported)")
    print("\n## after the policy: " + ", ".join(f"{k}={v}" for k, v in payload["counts"].items()))
    for severity, state in payload["exceeded"].items():
        print(f"  over the limit: {state['count']} {severity}(s), {state['limit']} allowed")
    if payload["blocking"]:
        print("\n## blocking")
        for finding in payload["blocking"]:
            location = f" [{finding['location']}]" if finding.get("location") else ""
            promoted = (
                f" (reported as {finding['reported_severity']})"
                if finding["reported_severity"] != finding["severity"]
                else ""
            )
            print(
                f"  {finding['severity'].upper():7s} {finding['origin']}/{finding['rule']}"
                f"{location}: {finding['message']}{promoted}"
            )
    if payload["waived"]:
        print("\n## waived")
        for finding in payload["waived"]:
            print(f"  {finding['rule']}: {finding['waiver']['reason']}")


def _render_rules(catalogue: dict[str, Any]) -> None:
    print("# every rule the reviews can produce\n")
    for origin in ("schematic", "board", "schematic + board"):
        entries = {k: v for k, v in catalogue.items() if v["origin"] == origin}
        if not entries:
            continue
        print(f"## {origin}")
        for rule_id, entry in sorted(entries.items()):
            blocks = ", ".join(entry["blocks_under"]) or "nothing"
            tune = (
                f" [--threshold {entry['threshold']}={entry.get('threshold_default')}]"
                if entry.get("threshold")
                else ""
            )
            context = " (context only: never promoted)" if entry["context_only"] else ""
            print(f"  {rule_id}  -  {entry['severity']}{tune}{context}")
            print(f"      checks: {entry['checks']}")
            print(f"      blocks under: {blocks}")
        print()


def cmd_gate(args: argparse.Namespace) -> int:
    from . import gate as gate_mod

    if args.list_policies:
        emit(
            {name: p.description for name, p in sorted(gate_mod.BUILTIN_POLICIES.items())},
            as_json=True,
        )
        return 0
    if args.list_rules:
        emit(gate_mod.catalogue(), as_json=args.json, text_renderer=_render_rules)
        return 0
    if not args.target:
        raise EdaError("gate needs a target (or --list-policies)")
    policy = gate_mod.load_policy(args.policy)
    payload = gate_mod.run(
        args.target,
        policy=policy,
        use_cli=not args.no_cli,
        collapse=args.collapse,
        thresholds=_thresholds(args.threshold),
    )
    emit(payload, as_json=args.json, text_renderer=_render_gate)
    if args.output:
        write_json(args.output, payload)
    return 0 if payload["pass"] else 2


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
    for name in ("pdfplumber", "pypdfium2", "numpy", "matplotlib", "PIL"):
        try:
            __import__(name)
            modules[name] = True
        except ImportError:
            modules[name] = False
    report["python_modules"] = modules
    report["in_container"] = Path("/.dockerenv").exists()
    report["ok"] = bool(report["kicad_cli"] and report["ngspice"] and all(modules.values()))
    emit(report, as_json=True)
    return 0 if report["ok"] else 1


# ---------------------------------------------------------------- datasheet


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

    payload = runner.run_netlist(
        args.netlist, args.output, timeout=args.timeout, make_plots=not args.no_plots
    )
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
                entry["thd"] = measure.thd(plot, args.thd, args.fundamental, skip_seconds=args.skip)
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

    payload = sch_review.review(
        args.target,
        use_cli=not args.no_cli,
        thresholds=_thresholds(args.threshold),
        collapse=args.collapse,
    )
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

    payload = pcb_review.review(
        args.target,
        use_cli=not args.no_cli,
        thresholds=_thresholds(args.threshold),
        collapse=args.collapse,
    )
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
        glb=args.glb,
        sheet=not args.no_sheet,
    )
    emit(payload, as_json=True)
    return 0


def cmd_pcb_fab(args: argparse.Namespace) -> int:
    from .kicad import fab

    payload = fab.export_package(
        args.target,
        args.output,
        include_fab_layers=args.fab_layers,
        pos_format=args.pos_format,
        step=args.step,
        ipc2581=args.ipc2581,
        exclude_dnp=not args.include_dnp,
        make_zip=not args.no_zip,
    )
    emit(payload, as_json=True)
    return 0 if payload.get("ok") else 2


def cmd_sch_bom(args: argparse.Namespace) -> int:
    from .kicad import fab

    payload = fab.bom(
        args.target,
        args.output,
        group_by=args.group_by,
        exclude_dnp=not args.include_dnp,
        fields=args.fields.split(",") if args.fields else None,
    )
    if not args.rows:
        payload.pop("rows", None)
    emit(payload, as_json=True)
    return 0


def cmd_sim_montecarlo(args: argparse.Namespace) -> int:
    from .spice import sweep

    tolerances = dict(sweep.parse_tolerance(spec) for spec in args.vary)
    payload = sweep.monte_carlo(
        args.netlist,
        args.output,
        tolerances=tolerances,
        metric=args.metric,
        trials=args.trials,
        distribution=args.distribution,
        seed=args.seed,
        timeout=args.timeout,
        keep_runs=args.keep_runs,
    )
    emit(payload, as_json=True)
    return 0 if payload.get("ok") else 2


def cmd_sim_temperature(args: argparse.Namespace) -> int:
    from .spice import sweep

    payload = sweep.temperature_sweep(
        args.netlist,
        args.output,
        temperatures=args.temperatures,
        metric=args.metric,
        timeout=args.timeout,
    )
    emit(payload, as_json=True)
    return 0 if payload.get("ok") else 2


def cmd_report(args: argparse.Namespace) -> int:
    from . import report

    payload = report.build(
        args.target,
        args.output,
        dpi=args.dpi,
        three_d=not args.no_3d,
        per_layer=not args.no_per_layer,
        glb=args.glb,
        bom=not args.no_bom,
        simulation=args.simulation,
        title=args.title,
    )
    emit(payload, as_json=True)
    return 0 if not payload["errors"] else 2


def cmd_pcb_glb(args: argparse.Namespace) -> int:
    from .kicad import kicad_cli, pcb

    board = pcb.find_board(args.target)
    dest = kicad_cli.export_glb(board, args.output)
    emit({"board": str(board), "glb": str(dest), "bytes": dest.stat().st_size}, as_json=True)
    return 0


def cmd_sch_pdf(args: argparse.Namespace) -> int:
    from .kicad import kicad_cli, schematic

    sch = schematic.find_root_schematic(args.target)
    dest = kicad_cli.export_sch_pdf(sch, args.output)
    emit({"schematic": str(sch), "pdf": str(dest), "bytes": dest.stat().st_size}, as_json=True)
    return 0


def cmd_diff(args: argparse.Namespace) -> int:
    from .kicad import diff

    payload = diff.build(
        args.old,
        args.new,
        args.output,
        images=not args.no_images,
        dpi=args.dpi,
    )
    emit(payload, as_json=True)
    return 0 if not payload["errors"] else 2


def cmd_pcb_electrical(args: argparse.Namespace) -> int:
    from .kicad import electrical, pcb

    board_path = pcb.find_board(args.target)
    payload = electrical.analyse(pcb.parse(board_path), temperature_rise_c=args.temperature_rise)
    payload["board"] = str(board_path)
    if args.top:
        payload["nets"] = payload["nets"][: args.top]
    emit(payload, as_json=True)
    return 0


def cmd_pcb_stats(args: argparse.Namespace) -> int:
    from .kicad import kicad_cli, pcb

    board = pcb.find_board(args.target)
    emit(
        {
            "board": str(board),
            "kicad_stats": kicad_cli.board_stats(board),
            "parsed": pcb.summary(pcb.parse(board)),
        },
        as_json=True,
    )
    return 0


# ---------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="eda",
        description="Circuit design toolkit: datasheets, SPICE simulation, KiCad review.",
    )
    parser.add_argument("--version", action="version", version=f"eda-toolkit {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "doctor", help="report the tool versions available in this environment"
    ).set_defaults(func=cmd_doctor)

    # ------------------------------------------------------------ gate
    p = sub.add_parser(
        "gate",
        help="one pass/fail verdict for the whole design, against a stated policy",
        description=(
            "Reviews the schematic and the board, applies a policy that says which "
            "findings block and which are waived (with a reason), and exits 2 when "
            "the design does not pass."
        ),
    )
    p.add_argument("target", nargs="?", help=".kicad_pro or project directory")
    p.add_argument(
        "--policy",
        help="a built-in policy name or a path to a JSON/TOML policy file "
        "(default: 'default'; --list-policies shows the built-in ones)",
    )
    p.add_argument(
        "--list-policies", action="store_true", help="print the built-in policies and exit"
    )
    p.add_argument(
        "--list-rules",
        action="store_true",
        help="print every rule, what it checks, what tunes it and which policies "
        "block on it, then exit",
    )
    p.add_argument("--no-cli", action="store_true")
    p.add_argument(
        "--collapse",
        type=int,
        default=COLLAPSE_LIMIT,
        metavar="N",
        help="fold a rule that fires more than N times into one finding "
        "(0 disables, default: %(default)s)",
    )
    p.add_argument(
        "--threshold", action="append", metavar="KEY=VALUE", help="override a review threshold"
    )
    p.add_argument("-o", "--output", help="also write the JSON verdict here")
    p.add_argument("--json", action="store_true", default=True)
    p.add_argument("--text", dest="json", action="store_false")
    p.set_defaults(func=cmd_gate)

    # ------------------------------------------------------------ diff
    p = sub.add_parser("diff", help="what changed between two revisions of a design")
    p.add_argument("old", help="the earlier project (directory, .kicad_pro, or file)")
    p.add_argument("new", help="the later one")
    p.add_argument("-o", "--output", required=True, help="directory to write the diff into")
    p.add_argument("--dpi", type=int, default=150)
    p.add_argument("--no-images", action="store_true", help="skip rendering and the pixel diff")
    p.set_defaults(func=cmd_diff)

    # ------------------------------------------------------------ report
    p = sub.add_parser(
        "report", help="one visual report: schematic, board, reviews, BOM, simulation"
    )
    p.add_argument("target", help=".kicad_pro, project directory, schematic or board")
    p.add_argument("-o", "--output", required=True, help="directory to write the report into")
    p.add_argument("--dpi", type=int, default=200)
    p.add_argument("--title", help="heading for the report (default: project name)")
    p.add_argument(
        "--simulation", metavar="NETLIST", help="also run this SPICE deck and include its plots"
    )
    p.add_argument("--glb", action="store_true", help="also export a GLB 3D model")
    p.add_argument("--no-3d", action="store_true", help="skip the 3D renders (much faster)")
    p.add_argument("--no-per-layer", action="store_true", help="skip the per-copper-layer plots")
    p.add_argument("--no-bom", action="store_true")
    p.set_defaults(func=cmd_report)

    # -- datasheet --------------------------------------------------------
    ds = sub.add_parser("datasheet", help="read datasheet PDFs").add_subparsers(
        dest="subcommand", required=True
    )

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

    p = sim.add_parser("montecarlo", help="component tolerance analysis")
    p.add_argument("netlist")
    p.add_argument("-o", "--output", required=True)
    p.add_argument(
        "--vary",
        action="append",
        required=True,
        metavar="NAME=TOL",
        help="component or .param to vary, e.g. R1=1%% (repeatable)",
    )
    p.add_argument(
        "--metric", required=True, help="measurement to collect, e.g. ac.v(out).f_minus_3db_hz"
    )
    p.add_argument("--trials", type=int, default=100)
    p.add_argument("--distribution", default="normal", choices=["normal", "uniform", "worst"])
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--timeout", type=int, default=600)
    p.add_argument("--keep-runs", action="store_true", help="keep every trial's raw output")
    p.set_defaults(func=cmd_sim_montecarlo)

    p = sim.add_parser("temperature", help="run the deck at several temperatures")
    p.add_argument("netlist")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--temperatures", type=float, nargs="+", default=[-40, 25, 85], metavar="C")
    p.add_argument("--metric", help="measurement to collect at each temperature")
    p.add_argument("--timeout", type=int, default=600)
    p.set_defaults(func=cmd_sim_temperature)

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
    p.add_argument(
        "--collapse",
        type=int,
        default=COLLAPSE_LIMIT,
        metavar="N",
        help="fold a rule that fires more than N times into one finding "
        "(0 disables, default: %(default)s)",
    )
    p.add_argument(
        "--threshold",
        action="append",
        metavar="KEY=VALUE",
        help="override a review threshold, e.g. grid_mm=2.54",
    )
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
    p.add_argument(
        "--format",
        default="json",
        choices=["json", "kicadxml", "kicadsexpr", "spice", "orcadpcb2", "cadstar"],
    )
    p.add_argument("--no-cli", action="store_true")
    p.set_defaults(func=cmd_sch_netlist)

    p = sch.add_parser("bom", help="grouped bill of materials as CSV")
    p.add_argument("target")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--group-by", default="Value,Footprint")
    p.add_argument("--fields", help="comma separated field list to export")
    p.add_argument("--include-dnp", action="store_true", help="keep DNP parts")
    p.add_argument("--rows", action="store_true", help="include every row in the JSON")
    p.set_defaults(func=cmd_sch_bom)

    p = sch.add_parser("render", help="plot the schematic to PNG (and PDF)")
    p.add_argument("target")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--dpi", type=int, default=200)
    p.set_defaults(func=cmd_sch_render)

    p = sch.add_parser("pdf", help="export the schematic as a PDF")
    p.add_argument("target")
    p.add_argument("-o", "--output", required=True)
    p.set_defaults(func=cmd_sch_pdf)

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
    p.add_argument(
        "--collapse",
        type=int,
        default=COLLAPSE_LIMIT,
        metavar="N",
        help="fold a rule that fires more than N times into one finding "
        "(0 disables, default: %(default)s)",
    )
    p.add_argument(
        "--threshold",
        action="append",
        metavar="KEY=VALUE",
        help="override a review threshold, e.g. min_track_mm=0.2",
    )
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
    p.add_argument("--per-layer", action="store_true", help="one plot per copper layer")
    p.add_argument("--glb", action="store_true", help="also export a GLB 3D model")
    p.add_argument("--no-sheet", action="store_true", help="skip the tiled contact sheet")
    p.set_defaults(func=cmd_pcb_render)

    p = pcb_p.add_parser("glb", help="export a GLB 3D model (viewable in a browser)")
    p.add_argument("target")
    p.add_argument("-o", "--output", required=True)
    p.set_defaults(func=cmd_pcb_glb)

    p = pcb_p.add_parser("fab", help="gerbers, drill, pick-and-place, BOM and a zip")
    p.add_argument("target")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--pos-format", default="csv", choices=["csv", "ascii", "gerber"])
    p.add_argument("--fab-layers", action="store_true", help="also plot F.Fab/B.Fab")
    p.add_argument("--step", action="store_true", help="also export a STEP model")
    p.add_argument("--ipc2581", action="store_true", help="also export IPC-2581")
    p.add_argument("--include-dnp", action="store_true", help="keep DNP parts")
    p.add_argument("--no-zip", action="store_true")
    p.set_defaults(func=cmd_pcb_fab)

    p = pcb_p.add_parser(
        "electrical", help="track resistance, current capacity and impedance widths"
    )
    p.add_argument("target")
    p.add_argument(
        "--temperature-rise",
        type=float,
        default=10.0,
        metavar="K",
        help="temperature rise the current rating is quoted at (default: 10)",
    )
    p.add_argument("--top", type=int, metavar="N", help="only the N most current-limited nets")
    p.set_defaults(func=cmd_pcb_electrical)

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

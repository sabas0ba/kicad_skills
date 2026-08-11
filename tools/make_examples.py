#!/usr/bin/env python3
"""Generate the worked example projects under ``examples/``.

Each design is described once - parts, nets, placement, routing - and emitted
twice from that one description:

* ``as-generated/`` is what falls out of a generator that got the connectivity
  roughly right and thought about nothing else. The defects are injected by
  :func:`degrade`, so they are listed in one place and are the same every time.
* ``reviewed/`` is the same circuit after the ``eda gate`` loop has been closed.

Committing both is the point. A repository full of good designs proves only that
good designs pass; the *pair* is what shows that the rules catch what they claim
to catch, and that the loop converges. ``examples/README.md`` puts the two
verdicts side by side.

Run it inside the **KiCad 9** image: it reads KiCad's own symbol and footprint
libraries so the projects use real parts, and it saves boards through pcbnew, so
the file format has to be the oldest one the CI matrix covers - KiCad never
reads a file newer than itself.

    docker run --rm -u $(id -u):$(id -g) -v "$PWD:/work" -w /work \\
      -e PYTHONPATH=/work/src -e HOME=/tmp/eda-home \\
      --entrypoint python3 eda-toolkit:9.0.9 tools/make_examples.py examples/
"""

from __future__ import annotations

import argparse
import copy
import math
import re
import sys
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from eda_toolkit.kicad import s_expression as sexp
from eda_toolkit.kicad.s_expression import SNode
from eda_toolkit.kicad.schematic import transform_pin

SYMBOL_DIR = Path("/usr/share/kicad/symbols")
FOOTPRINT_DIR = Path("/usr/share/kicad/footprints")

GRID = 1.27  # KiCad's default schematic grid, 50 mil
STUB = 2.54  # how far a wire runs from a pin before its label
SCH_VERSION = 20231120  # KiCad 8 format: read by every version in the CI matrix
NAMESPACE = uuid.UUID("6f1a0f3e-0000-4000-8000-000000000000")


def stable_uuid(*parts: object) -> str:
    """A uuid that depends only on what it names.

    The output is committed, so regenerating an unchanged design has to produce
    an unchanged file - a random uuid would rewrite every line every time.
    """
    return str(uuid.uuid5(NAMESPACE, "/".join(str(p) for p in parts)))


# ---------------------------------------------------------------------------
# the design description
# ---------------------------------------------------------------------------


@dataclass
class Part:
    ref: str
    lib_id: str
    value: str
    footprint: str
    sheet: tuple[float, float]  # symbol origin on the sheet
    board: tuple[float, float, float]  # x, y, rotation on the board
    angle: float = 0.0  # symbol rotation on the sheet
    fields: dict[str, str] = field(default_factory=dict)

    @property
    def library(self) -> str:
        return self.lib_id.split(":", 1)[0]


@dataclass
class Track:
    net: str
    layer: str
    width: float
    points: list[tuple[float, float]]


@dataclass
class Via:
    net: str
    x: float
    y: float
    drill: float = 0.4
    size: float = 0.8


@dataclass
class Design:
    name: str
    title: str
    rev: str
    company: str
    notes: list[str]
    parts: list[Part]
    nets: dict[str, list[str]]  # net name -> ["U1.1", "C2.2", ...]
    power_flags: list[str]  # nets that need a PWR_FLAG to satisfy ERC
    board_size: tuple[float, float]
    tracks: list[Track]
    vias: list[Via] = field(default_factory=list)
    ground_pour: bool = True
    origin: tuple[float, float] = (100.0, 60.0)  # top-left of the board on the sheet

    def part(self, ref: str) -> Part:
        return next(p for p in self.parts if p.ref == ref)


# ---------------------------------------------------------------------------
# KiCad's libraries
# ---------------------------------------------------------------------------

_lib_cache: dict[str, dict[str, SNode]] = {}


def _library(lib: str) -> dict[str, SNode]:
    if lib not in _lib_cache:
        path = SYMBOL_DIR / f"{lib}.kicad_sym"
        if not path.exists():
            raise SystemExit(f"no such symbol library: {path}")
        root = sexp.load(path)
        _lib_cache[lib] = {str(n.atom(0, "")): n for n in root.children("symbol")}
    return _lib_cache[lib]


def symbol_definition(lib_id: str) -> SNode:
    """The symbol as a schematic's ``lib_symbols`` wants it, ``extends`` resolved.

    Most of the interesting parts in KiCad's libraries are derived symbols -
    ``LM2596S-5`` carries only its own fields and inherits every pin from
    ``LM2596S-12``. A schematic embeds the flattened result, which is what this
    reproduces: the parent's graphics and pins, with the child's fields on top.
    """
    lib, _, name = lib_id.partition(":")
    node = _library(lib).get(name)
    if node is None:
        raise SystemExit(f"symbol {lib_id} is not in {lib}.kicad_sym")

    parent_name = node.value("extends")
    if parent_name:
        flat = copy.deepcopy(symbol_definition(f"{lib}:{parent_name}"))
        stem = str(parent_name)
        for sub in flat.children("symbol"):
            sub.args[0] = re.sub(rf"^{re.escape(stem)}", name, str(sub.atom(0, "")))
        _merge_over(flat, node)
    else:
        flat = copy.deepcopy(node)

    flat.args[0] = lib_id
    return flat


def _merge_over(base: SNode, child: SNode) -> None:
    """Let the derived symbol's own settings win over the ones it inherited."""
    for node in child.children():
        if node.name == "symbol":
            continue  # graphics and pins always come from the parent
        key = str(node.atom(0, "")) if node.name == "property" else None
        for index, existing in enumerate(base.args):
            same = (
                isinstance(existing, SNode)
                and existing.name == node.name
                and (key is None or str(existing.atom(0, "")) == key)
            )
            if same:
                base.args[index] = copy.deepcopy(node)
                break
        else:
            base.args.append(copy.deepcopy(node))


@dataclass(frozen=True)
class PinDef:
    number: str
    name: str
    etype: str
    x: float
    y: float
    angle: float


def symbol_pins(lib_id: str) -> list[PinDef]:
    out: list[PinDef] = []
    for sub in symbol_definition(lib_id).children("symbol"):
        for pin in sub.children("pin"):
            at = pin.child("at")
            atoms = at.atoms() if at else [0, 0, 0]
            number = pin.child("number")
            pname = pin.child("name")
            out.append(
                PinDef(
                    number=str(number.atom(0, "")) if number else "",
                    name=str(pname.atom(0, "")) if pname else "",
                    etype=str(pin.atom(0, "unspecified")),
                    x=float(atoms[0]),
                    y=float(atoms[1]),
                    angle=float(atoms[2]) if len(atoms) > 2 else 0.0,
                )
            )
    return out


def pin_geometry(part: Part, pin: PinDef) -> tuple[tuple[float, float], tuple[float, float]]:
    """Where the pin ends on the sheet, and where a stub off it would end.

    A symbol pin's ``at`` is its connection point and its angle points *into* the
    body, so a wire leaves in the opposite direction. Running both points through
    the same transform means rotation and mirroring need no separate handling.
    """
    sx, sy = part.sheet
    end = transform_pin(pin.x, pin.y, sx, sy, part.angle, "")
    away = math.radians(pin.angle + 180)
    out = transform_pin(
        pin.x + math.cos(away) * STUB, pin.y + math.sin(away) * STUB, sx, sy, part.angle, ""
    )
    return (round(end[0], 4), round(end[1], 4)), (round(out[0], 4), round(out[1], 4))


# ---------------------------------------------------------------------------
# schematic emission
# ---------------------------------------------------------------------------

POWER_SYMBOLS = {"GND": "power:GND", "+5V": "power:+5V", "+12V": "power:+12V"}


def _effects(hide: bool = False, justify: str = "") -> str:
    parts = ["(font (size 1.27 1.27))"]
    if justify:
        parts.append(f"(justify {justify})")
    if hide:
        parts.append("hide")
    return "(effects " + " ".join(parts) + ")"


def _property(name: str, value: str, x: float, y: float, hide: bool) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'    (property "{name}" "{escaped}" (at {x} {y} 0) {_effects(hide)})'


def emit_schematic(design: Design) -> str:
    root_uuid = stable_uuid(design.name, "sheet")
    lines = [
        f'(kicad_sch (version {SCH_VERSION}) (generator "eda-toolkit") (generator_version "8.0")',
        f'  (uuid "{root_uuid}")',
        '  (paper "A4")',
    ]

    if design.title or design.rev or design.company:
        lines += [
            "  (title_block",
            f'    (title "{design.title}")',
            '    (date "2024-01-01")',
            f'    (rev "{design.rev}")',
            f'    (company "{design.company}")',
            "  )",
        ]

    used: dict[str, SNode] = {}
    for part in design.parts:
        used.setdefault(part.lib_id, symbol_definition(part.lib_id))
    for lib_id in sorted({POWER_SYMBOLS[n] for n in design.nets if n in POWER_SYMBOLS}):
        used.setdefault(lib_id, symbol_definition(lib_id))
    if design.power_flags:
        used.setdefault("power:PWR_FLAG", symbol_definition("power:PWR_FLAG"))

    lines.append("  (lib_symbols")
    for lib_id in sorted(used):
        lines.append(sexp.dumps(used[lib_id], indent=2))
    lines.append("  )")

    net_of: dict[tuple[str, str], str] = {}
    for net, nodes in design.nets.items():
        for node in nodes:
            ref, _, number = node.partition(".")
            net_of[(ref, number)] = net

    body: list[str] = []
    power_index = 0

    for part in design.parts:
        pins = symbol_pins(part.lib_id)
        for pin in pins:
            net = net_of.get((part.ref, pin.number))
            if net is None:
                continue
            end, out = pin_geometry(part, pin)
            body.append(_wire(design, part.ref, pin.number, end, out))
            if net in POWER_SYMBOLS:
                power_index += 1
                body.append(_power_symbol(design, POWER_SYMBOLS[net], net, out, power_index))
            else:
                body.append(_label(design, net, part.ref, pin.number, out))
        body.append(_symbol_instance(design, part, pins))

    for index, net in enumerate(design.power_flags, start=1):
        body.append(_power_flag(design, net, index))

    for index, note in enumerate(design.notes, start=1):
        y = 24.0 + index * 5.08
        escaped = note.replace('"', '\\"')
        body.append(
            f'  (text "{escaped}" (at 25.4 {round(y, 2)} 0) '
            f'{_effects(justify="left top")} (uuid "{stable_uuid(design.name, "note", index)}"))'
        )

    lines += body
    lines += ["  (sheet_instances", '    (path "/" (page "1"))', "  )", ")"]
    return "\n".join(lines) + "\n"


def _wire(design: Design, ref: str, number: str, a, b) -> str:
    return (
        f"  (wire (pts (xy {a[0]} {a[1]}) (xy {b[0]} {b[1]})) "
        f"(stroke (width 0) (type default)) "
        f'(uuid "{stable_uuid(design.name, "wire", ref, number)}"))'
    )


def _label(design: Design, net: str, ref: str, number: str, at) -> str:
    return (
        f'  (label "{net}" (at {at[0]} {at[1]} 0) '
        f"{_effects(justify='left bottom')} "
        f'(uuid "{stable_uuid(design.name, "label", ref, number)}"))'
    )


def _power_symbol(design: Design, lib_id: str, net: str, at, index: int) -> str:
    ref = f"#PWR{index:02d}"
    uid = stable_uuid(design.name, "power", index)
    root = stable_uuid(design.name, "sheet")
    return "\n".join(
        [
            f'  (symbol (lib_id "{lib_id}") (at {at[0]} {at[1]} 0) (unit 1)',
            "    (exclude_from_sim no) (in_bom no) (on_board yes) (dnp no)",
            f'    (uuid "{uid}")',
            _property("Reference", ref, at[0], at[1], True),
            _property("Value", net, at[0], at[1] + 3.81, False),
            _property("Footprint", "", at[0], at[1], True),
            _property("Datasheet", "", at[0], at[1], True),
            f'    (pin "1" (uuid "{uid}-p"))',
            f'    (instances (project "{design.name}" '
            f'(path "/{root}" (reference "{ref}") (unit 1))))',
            "  )",
        ]
    )


def _power_flag(design: Design, net: str, index: int) -> str:
    """A PWR_FLAG, parked on the net's label so it drives it.

    Without one, ERC reports every power_in pin on an externally supplied rail as
    undriven - which is exactly what the as-generated variant leaves behind.
    """
    x = 38.1 + index * 12.7
    y = 118.11
    ref = f"#FLG{index:02d}"
    uid = stable_uuid(design.name, "flag", index)
    root = stable_uuid(design.name, "sheet")
    lines = [
        f"  (wire (pts (xy {x} {y}) (xy {x} {y + 2.54})) (stroke (width 0) (type default)) "
        f'(uuid "{uid}-w"))',
        f'  (label "{net}" (at {x} {y + 2.54} 0) {_effects(justify="left bottom")} '
        f'(uuid "{uid}-l"))',
        f'  (symbol (lib_id "power:PWR_FLAG") (at {x} {y} 0) (unit 1)',
        "    (exclude_from_sim no) (in_bom no) (on_board yes) (dnp no)",
        f'    (uuid "{uid}")',
        _property("Reference", ref, x, y, True),
        _property("Value", "PWR_FLAG", x, y - 3.81, False),
        _property("Footprint", "", x, y, True),
        _property("Datasheet", "", x, y, True),
        f'    (pin "1" (uuid "{uid}-p"))',
        f'    (instances (project "{design.name}" (path "/{root}" (reference "{ref}") (unit 1))))',
        "  )",
    ]
    return "\n".join(lines)


def _symbol_instance(design: Design, part: Part, pins: list[PinDef]) -> str:
    x, y = part.sheet
    uid = stable_uuid(design.name, "symbol", part.ref)
    root = stable_uuid(design.name, "sheet")
    lines = [
        f'  (symbol (lib_id "{part.lib_id}") (at {x} {y} {part.angle}) (unit 1)',
        "    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)",
        f'    (uuid "{uid}")',
        _property("Reference", part.ref, x, y - 6.35, False),
        _property("Value", part.value, x, y + 6.35, False),
        _property("Footprint", part.footprint, x, y, True),
        _property("Datasheet", part.fields.get("Datasheet", "~"), x, y, True),
    ]
    for name, value in part.fields.items():
        if name != "Datasheet":
            lines.append(_property(name, value, x, y, True))
    for pin in pins:
        lines.append(f'    (pin "{pin.number}" (uuid "{uid}-p{pin.number}"))')
    lines.append(
        f'    (instances (project "{design.name}" '
        f'(path "/{root}" (reference "{part.ref}") (unit 1))))'
    )
    lines.append("  )")
    return "\n".join(lines)


def emit_project(design: Design) -> str:
    root = stable_uuid(design.name, "sheet")
    return (
        "{\n"
        '  "board": {"design_settings": {"rules": {"min_track_width": 0.15}}},\n'
        f'  "meta": {{"filename": "{design.name}.kicad_pro", "version": 1}},\n'
        f'  "sheets": [["{root}", "Root"]]\n'
        "}\n"
    )


# ---------------------------------------------------------------------------
# board emission (through pcbnew, so the geometry and the fill are KiCad's own)
# ---------------------------------------------------------------------------


def emit_board(design: Design, path: Path) -> None:
    import pcbnew

    def mm(value: float) -> int:
        return pcbnew.FromMM(value)

    def point(x: float, y: float):
        return pcbnew.VECTOR2I(mm(x), mm(y))

    # A bare BOARD() has no design settings and segfaults as soon as anything
    # is added to it; CreateEmptyBoard is the constructor that sets one up.
    board = pcbnew.CreateEmptyBoard()
    board.SetCopperLayerCount(2)

    nets = {}
    for name in sorted(design.nets):
        info = pcbnew.NETINFO_ITEM(board, name)
        board.Add(info)
        nets[name] = info

    net_of: dict[tuple[str, str], str] = {}
    for name, nodes in design.nets.items():
        for node in nodes:
            ref, _, number = node.partition(".")
            net_of[(ref, number)] = name

    ox, oy = design.origin
    for part in design.parts:
        lib, _, fp_name = part.footprint.partition(":")
        footprint = pcbnew.FootprintLoad(str(FOOTPRINT_DIR / f"{lib}.pretty"), fp_name)
        if footprint is None:
            raise SystemExit(f"footprint {part.footprint} not found")
        footprint.SetReference(part.ref)
        footprint.SetValue(part.value)
        bx, by, angle = part.board
        footprint.SetPosition(point(ox + bx, oy + by))
        footprint.SetOrientationDegrees(angle)
        for pad in footprint.Pads():
            name = net_of.get((part.ref, pad.GetNumber()))
            if name:
                pad.SetNet(nets[name])
        board.Add(footprint)

    width, height = design.board_size
    for a, b in (
        ((0, 0), (width, 0)),
        ((width, 0), (width, height)),
        ((width, height), (0, height)),
        ((0, height), (0, 0)),
    ):
        edge = pcbnew.PCB_SHAPE(board)
        edge.SetShape(pcbnew.SHAPE_T_SEGMENT)
        edge.SetStart(point(ox + a[0], oy + a[1]))
        edge.SetEnd(point(ox + b[0], oy + b[1]))
        edge.SetLayer(pcbnew.Edge_Cuts)
        edge.SetWidth(mm(0.1))
        board.Add(edge)

    layers = {"F.Cu": pcbnew.F_Cu, "B.Cu": pcbnew.B_Cu}
    for track in design.tracks:
        for a, b in zip(track.points, track.points[1:], strict=False):
            segment = pcbnew.PCB_TRACK(board)
            segment.SetStart(point(ox + a[0], oy + a[1]))
            segment.SetEnd(point(ox + b[0], oy + b[1]))
            segment.SetWidth(mm(track.width))
            segment.SetLayer(layers[track.layer])
            segment.SetNet(nets[track.net])
            board.Add(segment)

    for via in design.vias:
        item = pcbnew.PCB_VIA(board)
        item.SetPosition(point(ox + via.x, oy + via.y))
        item.SetDrill(mm(via.drill))
        item.SetWidth(mm(via.size))
        item.SetNet(nets[via.net])
        board.Add(item)

    if design.ground_pour:
        outline = pcbnew.SHAPE_POLY_SET()
        outline.NewOutline()
        for x, y in ((0, 0), (width, 0), (width, height), (0, height)):
            outline.Append(mm(ox + x), mm(oy + y))
        zone = pcbnew.ZONE(board)
        zone.SetOutline(outline)
        zone.SetLayer(pcbnew.B_Cu)
        zone.SetNet(nets["GND"])
        zone.SetIsFilled(False)
        board.Add(zone)
        # Fill it here rather than leaving it to whoever opens the board: KiCad 9
        # has no `pcb drc --refill-zones`, so an unfilled pour reads as an
        # unconnected GND on half the CI matrix.
        pcbnew.ZONE_FILLER(board).Fill(board.Zones())

    pcbnew.SaveBoard(str(path), board)


# ---------------------------------------------------------------------------
# the degradation
# ---------------------------------------------------------------------------

# What a generator that thought about connectivity and nothing else leaves
# behind. Every entry is a defect one of the review rules exists to catch, and
# naming them here rather than hand-editing a second copy of the design is what
# keeps the two variants the same circuit.
DEGRADATIONS = """\
symbols and wires land off the 1.27 mm grid       -> readability.off_grid_*
two symbols are placed on top of each other       -> readability.overlapping_symbols
no title block, no design notes                   -> readability.title_block, spec.no_design_notes
no tolerance / voltage / power ratings, no MPN    -> spec.missing_rating, spec.missing_part_number
capacitors picked without derating the rail       -> spec.voltage_derating
no PWR_FLAG on the externally supplied rails      -> erc.power_pin_not_driven
footprints off the placement grid, odd rotations  -> layout.off_grid_placement, layout.odd_rotation
power routed at signal width                      -> track.thin_power
no ground pour, and no via near the decoupling    -> layout.no_ground_plane, layout.decoupling_via
"""

OFF_GRID = (0.31, -0.19)  # enough to break a connection, too little to see


def degrade(design: Design) -> Design:
    """The same circuit, drawn and laid out the way a generator leaves it."""
    parts = []
    for index, part in enumerate(design.parts):
        sx, sy = part.sheet
        bx, by, angle = part.board
        parts.append(
            replace(
                part,
                sheet=(round(sx + OFF_GRID[0], 4), round(sy + OFF_GRID[1], 4)),
                board=(round(bx + 0.23, 3), round(by - 0.17, 3), 37.0 if index % 4 == 0 else angle),
                fields={"Datasheet": part.fields.get("Datasheet", "~")},
                value=UNDERRATED.get(part.ref, part.value),
            )
        )
    # ... and one pair of symbols simply dropped on the same spot
    parts[-1] = replace(parts[-1], sheet=parts[-2].sheet)

    return replace(
        design,
        name=design.name,
        title="",
        rev="",
        company="",
        notes=[],
        parts=parts,
        power_flags=[],
        tracks=[replace(t, width=0.25) for t in design.tracks],
        vias=[],
        ground_pour=False,
    )


# Values a generator picks by capacitance alone, ignoring the rail they sit on.
UNDERRATED = {"C1": "220u", "C3": "220u"}


# ---------------------------------------------------------------------------
# the designs
# ---------------------------------------------------------------------------


def buck_5v() -> Design:
    """12 V to 5 V at 2 A, LM2596S-5 with a catch diode and an output inductor."""
    RIGHT = 168.91  # where the output half of the sheet starts
    parts = [
        Part(
            "J1",
            "Connector:Screw_Terminal_01x02",
            "12V IN",
            "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2_1x02_P5.00mm_Horizontal",
            sheet=(38.1, 66.04),
            board=(6.0, 17.5, 180.0),
            fields={"MPN": "1729128", "Manufacturer": "Phoenix Contact"},
        ),
        Part(
            "C1",
            "Device:C_Polarized",
            "220u",
            "Capacitor_THT:CP_Radial_D8.0mm_P3.50mm",
            sheet=(63.5, 71.12),
            board=(17.0, 9.0, 0.0),
            fields={
                "Voltage": "35V",
                "Tolerance": "20%",
                "MPN": "UVR1V221MPD",
                "Manufacturer": "Nichicon",
            },
        ),
        Part(
            "C2",
            "Device:C",
            "100n",
            "Capacitor_SMD:C_0805_2012Metric",
            sheet=(78.74, 71.12),
            board=(17.0, 25.0, 0.0),
            fields={
                "Voltage": "50V",
                "Tolerance": "10%",
                "MPN": "CL21B104KBCNNNC",
                "Manufacturer": "Samsung",
            },
        ),
        Part(
            "U1",
            "Regulator_Switching:LM2596S-5",
            "LM2596S-5",
            "Package_TO_SOT_SMD:TO-263-5_TabPin3",
            sheet=(109.22, 66.04),
            board=(30.0, 17.0, 0.0),
            fields={
                "MPN": "LM2596SX-5.0/NOPB",
                "Manufacturer": "Texas Instruments",
                "Datasheet": "https://www.ti.com/lit/ds/symlink/lm2596.pdf",
            },
        ),
        Part(
            "D1",
            "Diode:SS34",
            "SS34",
            "Diode_SMD:D_SMA",
            sheet=(140.97, 76.2),
            board=(41.0, 25.0, 90.0),
            fields={
                "MPN": "SS34",
                "Manufacturer": "Vishay",
                "Datasheet": "https://www.vishay.com/docs/88946/ss32.pdf",
            },
        ),
        Part(
            "L1",
            "Device:L",
            "33u",
            "Inductor_SMD:L_12x12mm_H8mm",
            sheet=(154.94, 66.04),
            board=(53.0, 17.0, 0.0),
            fields={
                "Current": "3A",
                "Tolerance": "20%",
                "MPN": "SRR1260-330M",
                "Manufacturer": "Bourns",
            },
        ),
        Part(
            "C3",
            "Device:C_Polarized",
            "220u",
            "Capacitor_THT:CP_Radial_D8.0mm_P3.50mm",
            sheet=(RIGHT, 71.12),
            board=(65.0, 9.0, 0.0),
            fields={
                "Voltage": "16V",
                "Tolerance": "20%",
                "MPN": "UVR1C221MPD",
                "Manufacturer": "Nichicon",
            },
        ),
        Part(
            "C4",
            "Device:C",
            "100n",
            "Capacitor_SMD:C_0805_2012Metric",
            sheet=(RIGHT + 15.24, 71.12),
            board=(65.0, 25.0, 0.0),
            fields={
                "Voltage": "25V",
                "Tolerance": "10%",
                "MPN": "CL21B104KBCNNNC",
                "Manufacturer": "Samsung",
            },
        ),
        Part(
            "R1",
            "Device:R",
            "1k",
            "Resistor_SMD:R_0805_2012Metric",
            sheet=(RIGHT + 30.48, 71.12),
            board=(76.0, 9.0, 0.0),
            fields={
                "Tolerance": "1%",
                "Power": "0.125W",
                "MPN": "RC0805FR-071KL",
                "Manufacturer": "Yageo",
            },
        ),
        Part(
            "D2",
            "Device:LED",
            "green",
            "LED_SMD:LED_0805_2012Metric",
            sheet=(RIGHT + 30.48, 81.28),
            board=(76.0, 17.0, 0.0),
            angle=270.0,
            fields={
                "Voltage": "2.1V",
                "Current": "3mA",
                "MPN": "LTST-C170KGKT",
                "Manufacturer": "Lite-On",
            },
        ),
        Part(
            "J2",
            "Connector:Screw_Terminal_01x02",
            "5V OUT",
            "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2_1x02_P5.00mm_Horizontal",
            sheet=(RIGHT + 45.72, 66.04),
            board=(84.0, 17.5, 0.0),
            fields={"MPN": "1729128", "Manufacturer": "Phoenix Contact"},
        ),
    ]

    nets = {
        "+12V": ["J1.1", "C1.1", "C2.1", "U1.1"],
        "GND": ["J1.2", "C1.2", "C2.2", "U1.3", "U1.5", "D1.2", "C3.2", "C4.2", "J2.2", "D2.1"],
        "SW": ["U1.2", "D1.1", "L1.1"],
        "+5V": ["L1.2", "U1.4", "C3.1", "C4.1", "J2.1", "R1.1"],
        "LED_A": ["R1.2", "D2.2"],
    }

    # 2 A of output current needs copper, not a signal trace: 1.0 mm of 35 um
    # outer-layer copper carries about 2.7 A at a 10 C rise (IPC-2221).
    power = 1.0
    tracks = [
        Track("+12V", "F.Cu", power, [(6.0, 15.0), (17.0, 15.0), (17.0, 11.2), (30.0, 11.2)]),
        Track("+12V", "F.Cu", power, [(17.0, 15.0), (17.0, 23.0)]),
        Track("SW", "F.Cu", power, [(35.0, 17.0), (41.0, 17.0), (41.0, 23.0)]),
        Track("SW", "F.Cu", power, [(41.0, 17.0), (53.0, 17.0)]),
        Track("+5V", "F.Cu", power, [(57.0, 17.0), (65.0, 17.0), (65.0, 11.2)]),
        Track("+5V", "F.Cu", power, [(65.0, 17.0), (76.0, 17.0), (84.0, 17.0)]),
        Track("+5V", "F.Cu", power, [(65.0, 17.0), (65.0, 23.0)]),
        Track("+5V", "F.Cu", 0.3, [(76.0, 11.2), (76.0, 15.0)]),
        Track("LED_A", "F.Cu", 0.3, [(76.0, 7.0), (79.0, 7.0), (79.0, 19.0), (76.0, 19.0)]),
    ]
    # Every ground pad reaches the pour on B.Cu through a via of its own.
    vias = [
        Via("GND", 6.0, 20.0),
        Via("GND", 17.0, 7.0),
        Via("GND", 17.0, 27.0),
        Via("GND", 30.0, 21.0),
        Via("GND", 41.0, 27.0),
        Via("GND", 65.0, 7.0),
        Via("GND", 65.0, 27.0),
        Via("GND", 76.0, 21.0),
        Via("GND", 84.0, 20.0),
    ]

    return Design(
        name="buck-5v",
        title="12 V to 5 V buck converter, 2 A",
        rev="A",
        company="kicad_skills examples",
        notes=[
            "LM2596S-5 fixed 5 V: FB ties straight to the output, no divider.",
            "C1 35 V on a 12 V rail and C3 16 V on a 5 V rail: both keep the 1.5x",
            "headroom a ceramic-adjacent electrolytic wants over its working voltage.",
            "L1 33 uH / 3 A: ripple is about 0.6 A pk-pk at 2 A out, and the",
            "saturation rating stays above the peak.",
            "Power copper is 1.0 mm, good for 2.7 A at a 10 C rise (IPC-2221).",
            "D1 catches the inductor current: SS34 is 3 A / 40 V, both above the",
            "2 A load and the 12 V input.",
        ],
        parts=parts,
        nets=nets,
        power_flags=["+12V", "GND"],
        board_size=(92.0, 34.0),
        tracks=tracks,
        vias=vias,
    )


DESIGNS = {"buck-5v": buck_5v}


# ---------------------------------------------------------------------------


def write_variant(design: Design, root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{design.name}.kicad_sch").write_text(emit_schematic(design), encoding="utf-8")
    (root / f"{design.name}.kicad_pro").write_text(emit_project(design), encoding="utf-8")
    emit_board(design, root / f"{design.name}.kicad_pcb")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", help="directory to write the examples into")
    parser.add_argument("--only", help="generate just this design")
    args = parser.parse_args(argv)

    out = Path(args.output)
    for name, builder in sorted(DESIGNS.items()):
        if args.only and args.only != name:
            continue
        design = builder()
        write_variant(design, out / name / "reviewed")
        write_variant(degrade(design), out / name / "as-generated")
        print(f"{name}: wrote as-generated/ and reviewed/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

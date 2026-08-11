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

Run it inside the container: it reads KiCad's own symbol and footprint
libraries, so the projects are built from the real parts rather than from
simplified copies. The files it writes are in the oldest format the CI matrix
covers, because KiCad never reads a file newer than itself.

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
from itertools import pairwise
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import autoroute

from eda_toolkit.kicad import s_expression as sexp
from eda_toolkit.kicad.s_expression import Bare, SNode
from eda_toolkit.kicad.schematic import transform_pin

SYMBOL_DIR = Path("/usr/share/kicad/symbols")
FOOTPRINT_DIR = Path("/usr/share/kicad/footprints")

GRID = 1.27  # KiCad's default schematic grid, 50 mil
BOARD_GRID = 0.5  # and its default placement grid on the board
STUB = 2.54  # how far a wire runs from a pin before its label
# The oldest format in the CI matrix, which is KiCad 9's. It cannot be older:
# the symbols are copied verbatim out of that release's libraries, so a file
# stamped KiCad 8 is parsed as KiCad 8 and rejected for tokens it now contains.
SCH_VERSION = 20250114
NAMESPACE = uuid.UUID("6f1a0f3e-0000-4000-8000-000000000000")

# What produced the committed output, and when. Both are frozen here rather than
# read from the clock so that regenerating an unchanged design still produces an
# unchanged file - bump them when you regenerate, or pass --generated-on /
# --generated-by. They matter most on the `as-generated` variant: it is a record
# of what a generator of this vintage actually wrote, and a year from now that
# is the only thing that dates it.
GENERATED_ON = "2026-08-11"
GENERATED_BY = "Claude Code"

GEOM_EPS = 1e-6
VIA_SIZE = 0.8  # what the router drops when it has to change layer
POUR_NET = "GND"  # the net every ground pour in these examples belongs to


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
    # How far a wire runs off each pin before its label. The default clears a
    # two-pin symbol; a forty-pin one draws its pin numbers just outside the
    # body, and a label parked 2.54 mm out lands on top of them.
    stub: float = STUB
    # Whether a pin this design does not use gets a no-connect flag. Off by
    # default: on a two-pin part an unused pin is a mistake, and on a 48 pin one
    # it is most of them.
    no_connect: bool = False

    @property
    def library(self) -> str:
        return self.lib_id.split(":", 1)[0]


@dataclass
class Track:
    net: str
    layer: str
    width: float
    # Each point is either a board coordinate or "REF.PAD". Naming the pad rather
    # than copying its coordinate is what keeps a track actually landing on it
    # when the part moves or turns.
    points: list[tuple[float, float] | str]
    # When set, the two points are endpoints to find a path between rather than
    # a path already chosen. Power nets are never routed this way - where the
    # copper goes is the design - but a logic input that simply has to arrive
    # is exactly what a maze router is for.
    auto: bool = False
    # Which layer an auto route has to *end* on. A ground stub asks for B.Cu at a
    # point inside the pour, which is how it reaches the plane: the router has to
    # spend a via, and where it spends it is not worth choosing by hand.
    goal_layer: str | None = None


@dataclass
class Via:
    """A via, placed either at a coordinate or beside the pad it drains.

    Anchoring to ``pad`` is what keeps a stitching via next to its ground pad
    when the part moves: a typed coordinate silently becomes a via in the middle
    of nowhere and a pad with no way down to the plane.
    """

    net: str
    x: float = 0.0
    y: float = 0.0
    pad: str | None = None
    offset: tuple[float, float] = (0.0, 0.0)
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
    # The ground pour, as (x0, y0, x1, y1) in board coordinates. It is inset
    # from the board edge rather than being the whole board, because KiCad's own
    # zone-to-edge clearance is not something the hand-written fill applies.
    pour: tuple[float, float, float, float] | None = None
    origin: tuple[float, float] = (100.0, 60.0)  # top-left of the board on the sheet
    # Free-text lines stamped into both title blocks as comments. `date`, `title`
    # and the rest are the *design's* - a generator that leaves them blank is a
    # finding - so provenance goes in the comment fields, which no rule reads and
    # which survive being opened in KiCad.
    provenance: tuple[str, ...] = ()
    date: str = ""
    # Where the design notes start on the sheet. Below the circuit, never beside
    # it - and how far below depends on how tall the circuit is.
    notes_at: tuple[float, float] = (25.4, 110.0)
    # Where the row of PWR_FLAGs starts. They need a clear strip of sheet, and
    # which strip is clear depends on how big the circuit is.
    flags_at: tuple[float, float] = (38.1, 118.11)
    # The grid `snapped` puts footprints on. A board whose placement is set by a
    # module's own 2.54 mm pad pitch cannot also sit on 0.5 mm, and pretending
    # otherwise moves the pads off the pins they have to land on.
    board_grid: float | None = BOARD_GRID

    def part(self, ref: str) -> Part:
        return next(p for p in self.parts if p.ref == ref)

    def snapped(self) -> Design:
        """The same design on the two grids: 1.27 mm on the sheet, 0.5 mm on the board.

        Placing a symbol at a round millimetre puts every one of its pins off
        the 1.27 mm grid, and KiCad's own ERC says so 65 times. The board has
        the same problem in reverse: a part placed to line up with something
        else - the end of a fan-out, the pin it bypasses - lands wherever that
        was, and `layout.off_grid_placement` counts it. Snapping here rather
        than asking each design to spell out multiples of 1.27 and 0.5 means the
        reviewed variant is on both grids by construction, and `degrade` is the
        only thing that can take it off either.

        Tracks are unaffected: every one of them names the pad it lands on
        rather than repeating its coordinate, so moving the part moves the
        copper with it.
        """
        return replace(
            self,
            # The PWR_FLAG row is wire and pin like anything else, so it is on
            # the same grid as everything else or it is three off-grid findings.
            flags_at=(
                round(round(self.flags_at[0] / GRID) * GRID, 4),
                round(round(self.flags_at[1] / GRID) * GRID, 4),
            ),
            parts=[
                replace(
                    part,
                    sheet=(
                        round(round(part.sheet[0] / GRID) * GRID, 4),
                        round(round(part.sheet[1] / GRID) * GRID, 4),
                    ),
                    board=part.board
                    if self.board_grid is None
                    else (
                        round(round(part.board[0] / self.board_grid) * self.board_grid, 4),
                        round(round(part.board[1] / self.board_grid) * self.board_grid, 4),
                        part.board[2],
                    ),
                )
                for part in self.parts
            ],
        )


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

    # `extends` names a parent that exists in the library and not in the
    # schematic, so it has to go once the parent has been folded in. KiCad 10
    # ignores the leftover; KiCad 9 refuses to open the file at all, with no
    # message beyond "Failed to load schematic".
    flat.args = [a for a in flat.args if not (isinstance(a, SNode) and a.name == "extends")]
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
        pin.x + math.cos(away) * part.stub,
        pin.y + math.sin(away) * part.stub,
        sx,
        sy,
        part.angle,
        "",
    )
    return (round(end[0], 4), round(end[1], 4)), (round(out[0], 4), round(out[1], 4))


# ---------------------------------------------------------------------------
# schematic emission
# ---------------------------------------------------------------------------

POWER_SYMBOLS = {
    "GND": "power:GND",
    "+3V3": "power:+3V3",
    "+5V": "power:+5V",
    "+12V": "power:+12V",
}


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


def _title_block(design: Design, indent: str) -> list[str]:
    """The title block both files share, with anything unset left out.

    A field the design does not state is *absent* rather than empty, because
    `readability.title_block` reads exactly these four and the degraded variant
    is supposed to fail it. The provenance comments are not among them.
    """
    fields = [
        ("title", design.title),
        ("date", design.date),
        ("rev", design.rev),
        ("company", design.company),
    ]
    body = [f'{indent}\t({name} "{value}")' for name, value in fields if value]
    body += [
        f'{indent}\t(comment {number} "{text}")'
        for number, text in enumerate(design.provenance, start=1)
    ]
    return [f"{indent}(title_block", *body, f"{indent})"] if body else []


def emit_schematic(design: Design) -> str:
    root_uuid = stable_uuid(design.name, "sheet")
    lines = [
        f'(kicad_sch (version {SCH_VERSION}) (generator "eda-toolkit") (generator_version "9.0")',
        f'  (uuid "{root_uuid}")',
        '  (paper "A4")',
    ]

    block = _title_block(design, "  ")
    if block:
        lines += block

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
    # Two stubs that happen to end on the same coordinate silently become one
    # net, and the design is quietly not the design any more. Catch it here
    # rather than in ERC, where it surfaces as a puzzle about net names.
    claimed: dict[tuple[float, float], tuple[str, str]] = {}

    for part in design.parts:
        pins = symbol_pins(part.lib_id)
        # A symbol may bring several pins out at one point - the Pico draws its
        # seven grounds that way, and KiCad calls them stacked. One wire and one
        # ground symbol is what that means; seven of each on the same coordinate
        # is a drawing that reads as one and reviews as seven.
        drawn: set[tuple[float, float]] = set()
        for pin in pins:
            net = net_of.get((part.ref, pin.number))
            end, out = pin_geometry(part, pin)
            if net is None:
                # A pin the design does not use is a decision, and KiCad wants
                # to see it made: without the flag, ERC reports every one of
                # them and the ones that matter are lost in the ones that do
                # not. Drawn once per point, like the wires above.
                if part.no_connect and end not in drawn:
                    drawn.add(end)
                    body.append(
                        f"  (no_connect (at {end[0]} {end[1]}) "
                        f'(uuid "{stable_uuid(design.name, "nc", part.ref, pin.number)}"))'
                    )
                continue
            if end in drawn:
                if claimed[end][0] != net:
                    raise SystemExit(
                        f"{design.name}: {part.ref}.{pin.number} is stacked on "
                        f"{claimed[end][1]} but is on {net}, not {claimed[end][0]}"
                    )
                continue
            drawn.add(end)
            # Both ends matter: a pin landing on someone else's stub joins the
            # two nets just as surely as two stubs meeting.
            for point in (end, out):
                owner = claimed.setdefault(point, (net, f"{part.ref}.{pin.number}"))
                if owner[0] != net:
                    raise SystemExit(
                        f"{design.name}: {part.ref}.{pin.number} ({net}) and {owner[1]} "
                        f"({owner[0]}) both touch {point} - move one of them"
                    )
            body.append(_wire(design, part.ref, pin.number, end, out))
            if net in POWER_SYMBOLS:
                power_index += 1
                step = (out[0] - end[0], out[1] - end[1])
                length = math.hypot(*step) or 1.0
                body.append(
                    _power_symbol(
                        design,
                        POWER_SYMBOLS[net],
                        net,
                        out,
                        power_index,
                        (round(step[0] / length), round(step[1] / length)),
                    )
                )
            else:
                body.append(_label(design, net, part.ref, pin.number, out))
        body.append(_symbol_instance(design, part, pins))

    for index, net in enumerate(design.power_flags, start=1):
        body.append(_power_flag(design, net, index))

    for index, note in enumerate(design.notes, start=1):
        # Below the circuit, not beside it. Started at the top of the sheet the
        # notes ran straight through the input section - which no rule catches,
        # because nothing about it changes the netlist. It is only visible by
        # looking at the plot, which is why the plot is in the documentation.
        y = design.notes_at[1] + index * 5.08
        escaped = note.replace('"', '\\"')
        body.append(
            f'  (text "{escaped}" (at {design.notes_at[0]} {round(y, 2)} 0) '
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


def _power_symbol(design: Design, lib_id: str, net: str, at, index: int, away=(0.0, 1.0)) -> str:
    """A ground or rail symbol, turned to point the way the wire left the pin.

    KiCad draws these pointing down and puts the rail name below them. On a pin
    that leaves sideways that name lands on the next pin's label - which on a
    twenty-pin header is most of them. Turning the symbol takes the name with it.
    """
    ref = f"#PWR{index:02d}"
    uid = stable_uuid(design.name, "power", index)
    root = stable_uuid(design.name, "sheet")
    angle = {(0.0, 1.0): 0, (-1.0, 0.0): 90, (0.0, -1.0): 180, (1.0, 0.0): 270}.get(away, 0)
    label = (round(at[0] + away[0] * 3.81, 4), round(at[1] + away[1] * 3.81, 4))
    return "\n".join(
        [
            f'  (symbol (lib_id "{lib_id}") (at {at[0]} {at[1]} {angle}) (unit 1)',
            "    (exclude_from_sim no) (in_bom no) (on_board yes) (dnp no)",
            f'    (uuid "{uid}")',
            _property("Reference", ref, at[0], at[1], True),
            _property("Value", net, label[0], label[1], False),
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
    x = design.flags_at[0] + index * 15.24
    y = design.flags_at[1]
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
    """The symbol, with its two visible fields clear of everything else.

    A fixed 6.35 mm above and below the origin is right for a two-pin part and
    lands in the middle of the pin labels of a forty-pin one. Measuring the pins
    instead puts the reference above the symbol and the value below it whatever
    size it is - which is where a reader looks for them anyway.
    """
    x, y = part.sheet
    ends = [pin_geometry(part, pin)[0][1] for pin in pins] or [y]
    top, bottom = min(*ends, y), max(*ends, y)
    uid = stable_uuid(design.name, "symbol", part.ref)
    root = stable_uuid(design.name, "sheet")
    lines = [
        f'  (symbol (lib_id "{part.lib_id}") (at {x} {y} {part.angle}) (unit 1)',
        "    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)",
        f'    (uuid "{uid}")',
        _property("Reference", part.ref, x, round(top - 2.54, 4), False),
        _property("Value", part.value, x, round(bottom + 2.54, 4), False),
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
# board emission
# ---------------------------------------------------------------------------
#
# The board is written as s-expressions rather than built through pcbnew. That
# is not a shortcut: pcbnew's ZONE_FILLER segfaults headlessly on both KiCad 9
# and 10 (it wants a wx display, and the image has no framebuffer), and a pour
# with no computed fill reads as an unconnected GND on the half of the CI matrix
# that has no `pcb drc --refill-zones`. Writing the file directly means the fill
# is ours to state - which is only honest because the pour is deliberately
# placed where nothing else can be inside it. See `Design.pour`.

BOARD_LAYERS = """\
	(layers
		(0 "F.Cu" signal)
		(2 "B.Cu" signal)
		(9 "F.Adhes" user "F.Adhesive")
		(11 "B.Adhes" user "B.Adhesive")
		(13 "F.Paste" user)
		(15 "B.Paste" user)
		(5 "F.SilkS" user "F.Silkscreen")
		(7 "B.SilkS" user "B.Silkscreen")
		(1 "F.Mask" user)
		(3 "B.Mask" user)
		(17 "Dwgs.User" user "User.Drawings")
		(19 "Cmts.User" user "User.Comments")
		(21 "Eco1.User" user "User.Eco1")
		(23 "Eco2.User" user "User.Eco2")
		(25 "Edge.Cuts" user)
		(27 "Margin" user)
		(31 "F.CrtYd" user "F.Courtyard")
		(29 "B.CrtYd" user "B.Courtyard")
		(35 "F.Fab" user)
		(33 "B.Fab" user)
	)"""

_footprint_cache: dict[str, SNode] = {}


def footprint_definition(spec: str) -> SNode:
    """One of KiCad's own footprints, ready to drop into a board."""
    if spec not in _footprint_cache:
        lib, _, name = spec.partition(":")
        path = FOOTPRINT_DIR / f"{lib}.pretty" / f"{name}.kicad_mod"
        if not path.exists():
            raise SystemExit(f"no such footprint: {path}")
        node = sexp.load(path)
        node.args[0] = spec
        # A board carries no per-footprint format stamp; the document has one.
        node.args = [
            a
            for a in node.args
            if not (
                isinstance(a, SNode) and a.name in ("version", "generator", "generator_version")
            )
        ]
        _footprint_cache[spec] = node
    return copy.deepcopy(_footprint_cache[spec])


def _set_property(node: SNode, name: str, value: str, *, add: bool = False) -> None:
    for prop in node.children("property"):
        if str(prop.atom(0, "")) == name:
            # a property is (property "Name" "Value" ...): the value is its
            # second bare atom, whatever nodes are interleaved after it
            bare = [i for i, a in enumerate(prop.args) if not isinstance(a, SNode)]
            if len(bare) >= 2:
                prop.args[bare[1]] = value
            return
    if not add:
        return
    # KiCad's own "update PCB from schematic" copies every symbol field onto the
    # footprint, and its parity check then compares the two. A footprint without
    # them is one `footprint_symbol_field_mismatch` per field per part.
    template = node.child("property")
    at = template.child("at") if template else None
    prop = SNode("property", [name, value])
    if at is not None:
        prop.args.append(SNode("at", list(at.atoms())))
    prop.args.append(SNode("layer", ["F.Fab"]))
    prop.args.append(_uuid_node(stable_uuid(str(node.atom(0, "")), "prop", name)))
    prop.args.append(SNode("hide", [Bare("yes")]))
    prop.args.append(SNode("effects", [SNode("font", [SNode("size", [1.0, 1.0])])]))
    node.args.append(prop)


def _uuid_node(value: str) -> SNode:
    return SNode("uuid", [value])


def _rotate(x: float, y: float, angle: float) -> tuple[float, float]:
    """KiCad's RotatePoint: positive angles turn counter-clockwise on screen."""
    rad = math.radians(angle)
    return (x * math.cos(rad) + y * math.sin(rad), -x * math.sin(rad) + y * math.cos(rad))


def pad_position(design: Design, spec: str) -> tuple[float, float]:
    """Board coordinates of ``REF.PAD``, footprint rotation included."""
    ref, _, number = spec.partition(".")
    part = design.part(ref)
    node = footprint_definition(part.footprint)
    for pad in node.children("pad"):
        if str(pad.atom(0, "")) != number:
            continue
        at = pad.child("at")
        atoms = [a for a in (at.atoms() if at else []) if isinstance(a, (int, float))]
        px, py = (float(atoms[0]), float(atoms[1])) if len(atoms) >= 2 else (0.0, 0.0)
        bx, by, angle = part.board
        rx, ry = _rotate(px, py, angle)
        return (round(bx + rx, 4), round(by + ry, 4))
    raise SystemExit(f"{part.footprint} has no pad {number!r} (wanted by {spec})")


def resolve(design: Design, point: tuple[float, float] | str) -> tuple[float, float]:
    return pad_position(design, point) if isinstance(point, str) else point


def _segment_distance(a1, a2, b1, b2) -> float:
    """Shortest distance between two segments, 0 when they cross."""

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    d1, d2 = cross(b1, b2, a1), cross(b1, b2, a2)
    d3, d4 = cross(a1, a2, b1), cross(a1, a2, b2)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)):
        return 0.0

    def point_to_segment(p, s, e):
        dx, dy = e[0] - s[0], e[1] - s[1]
        length2 = dx * dx + dy * dy
        if length2 == 0:
            return math.dist(p, s)
        u = max(0.0, min(1.0, ((p[0] - s[0]) * dx + (p[1] - s[1]) * dy) / length2))
        return math.dist(p, (s[0] + u * dx, s[1] + u * dy))

    return min(
        point_to_segment(a1, b1, b2),
        point_to_segment(a2, b1, b2),
        point_to_segment(b1, a1, a2),
        point_to_segment(b2, a1, a2),
    )


def check_board(design: Design, clearance: float = 0.2) -> list[str]:
    """Everything KiCad's DRC would call a short, found without leaving Python.

    Each round trip through the real DRC costs a container start and the best
    part of a minute; the routing needs dozens. This is the same question asked
    of the same geometry - does copper of one net come within `clearance` of
    another - so the slow check confirms the layout rather than discovering it.
    """
    segments = []
    for track in design.tracks:
        points = [resolve(design, point) for point in track.points]
        for a, b in pairwise(points):
            segments.append((track.net, track.layer, track.width, a, b))

    # A via is copper on every layer, so it is checked as a square pad on both.
    pads = []
    for index, via in enumerate(design.vias):
        vx, vy = via_position(design, via)
        half = via.size / 2
        pads.append(
            (
                via.net,
                None,
                f"via{index} at ({vx}, {vy})",
                (vx - half, vy - half, vx + half, vy + half),
            )
        )
    for part in design.parts:
        node = footprint_definition(part.footprint)
        for pad in node.children("pad"):
            number = str(pad.atom(0, ""))
            net = None
            for name, nodes in design.nets.items():
                if f"{part.ref}.{number}" in nodes:
                    net = name
            pads.append((net, pad_layer(pad), f"{part.ref}.{number}", pad_box(design, part, pad)))

    def shares(one: str | None, other: str | None) -> bool:
        return one is None or other is None or one == other

    problems = []
    for i, (net, layer, width, a, b) in enumerate(segments):
        for other_net, other_layer, other_width, c, d in segments[i + 1 :]:
            if net == other_net or layer != other_layer:
                continue
            gap = _segment_distance(a, b, c, d) - width / 2 - other_width / 2
            if gap < clearance:
                problems.append(
                    f"{net} and {other_net} come within {max(gap, 0):.2f} mm: "
                    f"{a}-{b} against {c}-{d}"
                )
        for pad_net, pad_on, label, box in pads:
            if pad_net is None or pad_net == net or not shares(layer, pad_on):
                continue
            gap = _segment_to_box(a, b, box) - width / 2
            if gap < clearance:
                problems.append(
                    f"{net} track {a}-{b} comes within {max(gap, 0):.2f} mm of {label} ({pad_net})"
                )
    for i, (net, pad_on, label, box) in enumerate(pads):
        for other_net, other_on, other_label, other_box in pads[i + 1 :]:
            if net is None or other_net is None or net == other_net:
                continue
            if not shares(pad_on, other_on) or not _boxes_near(box, other_box, clearance):
                continue
            problems.append(f"{label} ({net}) is too close to {other_label} ({other_net})")

    return sorted(set(problems))


def _boxes_near(one, other, clearance: float) -> bool:
    return (
        one[0] - clearance < other[2]
        and other[0] - clearance < one[2]
        and one[1] - clearance < other[3]
        and other[1] - clearance < one[3]
    )


def _segment_to_box(a, b, box) -> float:
    """Distance from a segment to an axis-aligned pad rectangle."""
    x0, y0, x1, y1 = box
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    inside = all(x0 <= p[0] <= x1 and y0 <= p[1] <= y1 for p in (a, b))
    if inside:
        return 0.0
    return min(_segment_distance(a, b, c, d) for c, d in pairwise([*corners, corners[0]]))


def pad_box(design: Design, part: Part, pad: SNode) -> tuple[float, float, float, float]:
    """A pad's extent on the board, the footprint's own rotation included."""
    cx, cy = pad_position_of(design, part, pad)
    size = [a for a in pad.child("size").atoms() if isinstance(a, (int, float))]
    w, h = (float(size[0]), float(size[1] if len(size) > 1 else size[0]))
    angle = part.board[2]
    at = pad.child("at")
    atoms = [a for a in (at.atoms() if at else []) if isinstance(a, (int, float))]
    if len(atoms) > 2:
        angle += float(atoms[2])
    if round(abs(angle) % 180) == 90:
        w, h = h, w
    return (cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2)


def pad_position_of(design: Design, part: Part, pad: SNode) -> tuple[float, float]:
    at = pad.child("at")
    atoms = [a for a in (at.atoms() if at else []) if isinstance(a, (int, float))]
    px, py = (float(atoms[0]), float(atoms[1])) if len(atoms) >= 2 else (0.0, 0.0)
    bx, by, angle = part.board
    rx, ry = _rotate(px, py, angle)
    return (round(bx + rx, 4), round(by + ry, 4))


def pad_layer(pad: SNode) -> str | None:
    """Which copper layer a pad is on, or None when it is on all of them."""
    node = pad.child("layers")
    names = [str(a) for a in (node.atoms() if node else [])]
    if any(name.startswith("*") for name in names):
        return None
    copper = [name for name in names if name.endswith(".Cu")]
    return copper[0] if len(copper) == 1 else None


def through_hole(design: Design, point: tuple[float, float] | str) -> bool:
    """Whether a route endpoint is a pad that exists on every copper layer."""
    if not isinstance(point, str):
        return False
    ref, _, number = point.partition(".")
    node = footprint_definition(design.part(ref).footprint)
    pad = next((p for p in node.children("pad") if str(p.atom(0, "")) == number), None)
    return pad is not None and pad_layer(pad) is None


def via_position(design: Design, via: Via) -> tuple[float, float]:
    if not via.pad:
        return (via.x, via.y)
    px, py = pad_position(design, via.pad)
    return (round(px + via.offset[0], 4), round(py + via.offset[1], 4))


def fan(
    design: Design,
    ref: str,
    pins: list[str],
    *,
    lead: float,
    column: float,
    pitch: float,
    centre: float,
    axis: str = "x",
    widths: dict[str, float] | None = None,
    width: float = 0.3,
    slope: float = 2.2,
    clearance: float = 0.25,
) -> tuple[list[Track], dict[str, tuple[float, float]]]:
    """Take one row of a fine-pitch package out to a pitch a router can use.

    At 0.65 mm there is nothing for a search to find: two 0.3 mm tracks and the
    clearance between them already fill the gap, and a grid coarse enough to
    finish in this decade cannot see it. So the escape is stated rather than
    searched for - every pin leaves straight, then all of them turn together at
    the same shallow angle, which keeps the perpendicular spacing at
    ``row pitch * cos(angle)`` instead of letting one track cut the corner into
    its neighbour. ``slope`` is dx/dy of that turn.

    That is also what sets the width. Two neighbours in the turn are only
    ``cos(angle)`` of the row pitch apart, so on a 0.65 mm row nothing wider
    than 0.3 mm fits however gentle the angle is made - a pair of 0.4 mm tracks
    would need the full 0.65 mm and so could only ever run parallel. Every pin
    therefore leaves narrow and widens once it is clear, which is what the
    assertion below is checking rather than trusting.

    ``axis`` is the direction the escape runs in: "x" for a row of pads down the
    side of a package, "y" for one along its top or bottom. ``lead`` and
    ``column`` are coordinates along it; ``centre`` and ``pitch`` are across it.

    Returns the escape tracks and, per pin, the point the router picks up from.
    """
    widths = widths or {}
    across = 1 if axis == "x" else 0

    def at(along: float, offset: float) -> tuple[float, float]:
        return (along, offset) if axis == "x" else (offset, along)

    direction = -1.0 if column < lead else 1.0
    row = min(
        abs(pad_position(design, f"{ref}.{a}")[across] - pad_position(design, f"{ref}.{b}")[across])
        for a, b in pairwise(pins)
    )
    span = row * math.cos(math.atan2(1.0, slope))
    for a, b in pairwise(pins):
        need = (widths.get(a, width) + widths.get(b, width)) / 2 + clearance
        if span < need - GEOM_EPS:
            raise SystemExit(
                f"{design.name}: {ref} pins {a} and {b} are {row:.3f} mm apart, which at "
                f"slope {slope} leaves {span:.3f} mm across the turn and they need {need:.3f}"
            )

    tracks: list[Track] = []
    ends: dict[str, tuple[float, float]] = {}
    for index, number in enumerate(pins):
        pad = f"{ref}.{number}"
        offset = pad_position(design, pad)[across]
        target = round(centre + (index - (len(pins) - 1) / 2) * pitch, 4)
        bend = round(lead + direction * abs(target - offset) * slope, 4)
        points: list[tuple[float, float] | str] = [pad, at(lead, offset)]
        if abs(target - offset) > GEOM_EPS:
            points.append(at(bend, target))
        if abs(column - bend) > GEOM_EPS:
            points.append(at(column, target))
        net = next(name for name, nodes in design.nets.items() if pad in nodes)
        tracks.append(Track(net, "F.Cu", widths.get(number, width), points))
        ends[number] = at(column, target)
    return tracks, ends


class Blocked(Exception):
    """The router could not place one track, given everything already placed."""

    def __init__(self, track: Track) -> None:
        super().__init__(track)
        self.track = track


def _route_all(design: Design, order: list[Track]) -> tuple[list[tuple[int, Track]], list[Via]]:
    """Route every ``auto`` track in ``order``, or say which one had no room."""
    router = autoroute.Router(*design.board_size)
    for part in design.parts:
        node = footprint_definition(part.footprint)
        for pad in node.children("pad"):
            number = str(pad.atom(0, ""))
            net = next(
                (name for name, nodes in design.nets.items() if f"{part.ref}.{number}" in nodes),
                "",
            )
            x0, y0, x1, y1 = pad_box(design, part, pad)
            router.add(autoroute.Obstacle(x0, y0, x1, y1, net, pad_layer(pad)))
    for via in design.vias:
        router.add_via(via.net, via_position(design, via), via.size)
    for track in design.tracks:
        if track.auto:
            continue
        points = [resolve(design, point) for point in track.points]
        for a, b in pairwise(points):
            router.add_track(track.net, a, b, track.width, track.layer)

    # Keyed by where the design listed the track, so that the emitted board does
    # not shuffle when a retry changes the order things are routed in.
    place = {id(track): index for index, track in enumerate(design.tracks)}
    routed = [(place[id(t)], t) for t in design.tracks if not t.auto]
    vias = list(design.vias)
    for index, track in enumerate(order):
        a, b = (resolve(design, point) for point in track.points)
        path = router.route(
            track.net,
            a,
            b,
            track.width,
            start_layer=None if through_hole(design, track.points[0]) else track.layer,
            goal_layer=(
                track.goal_layer
                or (None if through_hole(design, track.points[-1]) else track.layer)
            ),
            crowd=[
                resolve(design, point)
                for later in order[index + 1 :]
                if later.net != track.net
                for point in later.points
            ],
        )
        if path is None:
            raise Blocked(track)
        for layer, points in path.runs:
            for start, end in pairwise(points):
                router.add_track(track.net, start, end, track.width, layer)
            routed.append(
                (place[id(track)], replace(track, points=list(points), layer=layer, auto=False))
            )
        for point in path.vias:
            router.add_via(track.net, point, VIA_SIZE)
            vias.append(Via(track.net, x=point[0], y=point[1], size=VIA_SIZE))
    return routed, vias


def resolve_routes(design: Design) -> Design:
    """Replace every ``auto`` track with the path a router found for it.

    Done once, before anything looks at the geometry, so the clearance check and
    the emitted board see the same copper. A path that changed layer comes back
    as one track per layer plus the vias between them, which is what the board
    file wants anyway.

    Routing one net at a time means an early net can take the only lane a later
    one had, and no amount of care over the order avoids that in general. So a
    net that finds no room is moved to the front and the whole set is routed
    again - the cheapest form of rip-up there is, and enough for boards this
    size. A net that fails twice is a floorplan that does not work, and says so.
    """
    order = [track for track in design.tracks if track.auto]
    if not order:
        return design
    ripped: list[Track] = []
    while True:
        try:
            routed, vias = _route_all(design, order)
        except Blocked as blocked:
            if blocked.track in ripped:
                raise SystemExit(
                    f"{design.name}: no route for {blocked.track.net} between "
                    f"{blocked.track.points} even with first pick of the board - "
                    "the floorplan has no lane for it"
                ) from None
            ripped.append(blocked.track)
            order.remove(blocked.track)
            order.insert(0, blocked.track)
            continue
        return replace(
            design, tracks=[track for _, track in sorted(routed, key=lambda p: p[0])], vias=vias
        )


def _reuuid(node: SNode, *salt: object) -> None:
    """Give every uuid in a footprint a new, stable value - references included.

    A footprint placed twice cannot keep the library's uuids, and a footprint
    whose uuids are simply replaced cannot keep its groups: KiCad's `(group ...)`
    lists its members by uuid, and a group that names uuids the file no longer
    contains is a footprint that no longer matches its library copy. The Pico
    module has six of them. So the whole subtree is remapped at once, and the
    member lists are remapped with it.
    """
    mapping: dict[str, str] = {}

    def collect(current: SNode) -> None:
        if current.name == "uuid":
            for atom in current.atoms():
                mapping.setdefault(str(atom), stable_uuid(*salt, atom))
        for child in current.children():
            collect(child)

    def rewrite(current: SNode) -> None:
        if current.name in ("uuid", "members"):
            current.args = [mapping.get(str(a), a) for a in current.args]
        for child in current.children():
            rewrite(child)

    collect(node)
    rewrite(node)


def emit_board(design: Design, path: Path) -> None:
    ox, oy = design.origin
    net_of: dict[tuple[str, str], str] = {}
    for name, nodes in design.nets.items():
        for entry in nodes:
            ref, _, number = entry.partition(".")
            net_of[(ref, number)] = name

    order = ["GND", *sorted(n for n in design.nets if n != "GND")]
    codes = {name: index for index, name in enumerate(order, start=1)}
    # KiCad names a net after the sheet path of the label that drives it, so a
    # plain label on the root sheet becomes "/NAME"; only a power symbol keeps
    # its bare name. Getting this wrong costs one `net_conflict` per pad in the
    # schematic-parity check, and nothing else notices.
    labels = {name: (name if name in POWER_SYMBOLS else f"/{name}") for name in order}

    lines = [
        "(kicad_pcb",
        "\t(version 20241229)",
        '\t(generator "eda-toolkit")',
        '\t(generator_version "9.0")',
        "\t(general",
        "\t\t(thickness 1.6)",
        "\t\t(legacy_teardrops no)",
        "\t)",
        '\t(paper "A4")',
        *_title_block(design, "\t"),
        BOARD_LAYERS,
        "\t(setup",
        "\t\t(pad_to_mask_clearance 0)",
        "\t\t(allow_soldermask_bridges_in_footprints no)",
        "\t)",
        '\t(net 0 "")',
    ]
    for name in order:
        lines.append(f'\t(net {codes[name]} "{labels[name]}")')

    for part in design.parts:
        node = footprint_definition(part.footprint)
        bx, by, angle = part.board
        node.args.insert(
            1, SNode("at", [round(ox + bx, 4), round(oy + by, 4)] + ([angle] if angle else []))
        )
        _reuuid(node, design.name, "fp", part.ref)
        node.args.insert(2, _uuid_node(stable_uuid(design.name, "fp", part.ref)))
        _set_property(node, "Reference", part.ref)
        _set_property(node, "Value", part.value)
        for key, value in part.fields.items():
            _set_property(node, key, value, add=True)
        for index, pad in enumerate(node.children("pad")):
            number = str(pad.atom(0, ""))
            # A pad carries its own absolute orientation, so turning a footprint
            # turns its pads too. Leaving them at zero looks identical on the
            # board - the geometry is the same either way - and is one
            # `lib_footprint_mismatch` per rotated footprint.
            if angle:
                at = pad.child("at")
                bare = [a for a in at.atoms() if isinstance(a, (int, float))]
                at.args = [*bare[:2], round((bare[2] if len(bare) > 2 else 0.0) + angle, 4)]
            name = net_of.get((part.ref, number))
            if name:
                pad.args.append(SNode("net", [codes[name], labels[name]]))
            if pad.child("uuid") is None:
                pad.args.append(
                    _uuid_node(stable_uuid(design.name, "pad", part.ref, number, index))
                )
        lines.append(sexp.dumps(node, indent=1))

    width, height = design.board_size
    corners = [(0, 0), (width, 0), (width, height), (0, height)]
    for index, (a, b) in enumerate(zip(corners, corners[1:] + corners[:1], strict=False)):
        lines.append(
            f"\t(gr_line (start {ox + a[0]} {oy + a[1]}) (end {ox + b[0]} {oy + b[1]}) "
            f'(stroke (width 0.1) (type default)) (layer "Edge.Cuts") '
            f'(uuid "{stable_uuid(design.name, "edge", index)}"))'
        )

    for index, track in enumerate(design.tracks):
        points = [resolve(design, p) for p in track.points]
        for step, (a, b) in enumerate(pairwise(points)):
            lines.append(
                f"\t(segment (start {round(ox + a[0], 4)} {round(oy + a[1], 4)}) "
                f"(end {round(ox + b[0], 4)} {round(oy + b[1], 4)}) (width {track.width}) "
                f'(layer "{track.layer}") (net {codes[track.net]}) '
                f'(uuid "{stable_uuid(design.name, "seg", index, step)}"))'
            )

    for index, via in enumerate(design.vias):
        vx, vy = via.x, via.y
        if via.pad:
            px, py = pad_position(design, via.pad)
            vx, vy = px + via.offset[0], py + via.offset[1]
        lines.append(
            f"\t(via (at {round(ox + vx, 4)} {round(oy + vy, 4)}) (size {via.size}) "
            f'(drill {via.drill}) (layers "F.Cu" "B.Cu") (net {codes[via.net]}) '
            f'(uuid "{stable_uuid(design.name, "via", index)}"))'
        )

    if design.pour:
        lines.append(_zone(design, codes["GND"]))

    lines.append(")")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


ZONE_CLEARANCE = 0.4  # what the pour keeps away from copper of another net
ZONE_SLIVER = 0.35  # a strip of plane thinner than this is not worth filling
ZONE_WELD = 0.05  # how far neighbouring islands are grown into each other
# Every hole is rounded outward onto this grid. Without it a diagonal track puts
# an x edge every fraction of a millimetre, the sweep below never sees two
# neighbouring columns agree, and the plane comes out as a thousand slivers
# instead of a dozen rectangles. Rounding outward only ever adds clearance.
ZONE_GRID = 0.1


def _hole_boxes(design: Design, layer: str, net: str) -> list[tuple[float, float, float, float]]:
    """Everything of another net that this layer's pour has to keep clear of."""
    keep = ZONE_CLEARANCE + ZONE_WELD

    def grown(box, half=0.0):
        return (
            math.floor((box[0] - keep - half) / ZONE_GRID) * ZONE_GRID,
            math.floor((box[1] - keep - half) / ZONE_GRID) * ZONE_GRID,
            math.ceil((box[2] + keep + half) / ZONE_GRID) * ZONE_GRID,
            math.ceil((box[3] + keep + half) / ZONE_GRID) * ZONE_GRID,
        )

    holes = []
    for part in design.parts:
        node = footprint_definition(part.footprint)
        for pad in node.children("pad"):
            number = str(pad.atom(0, ""))
            owner = next(
                (n for n, nodes in design.nets.items() if f"{part.ref}.{number}" in nodes), None
            )
            if owner == net or pad_layer(pad) not in (None, layer):
                continue
            holes.append(grown(pad_box(design, part, pad)))
    for via in design.vias:
        if via.net == net:
            continue
        vx, vy = via_position(design, via)
        holes.append(grown((vx, vy, vx, vy), via.size / 2))
    for track in design.tracks:
        if track.net == net or track.layer != layer:
            continue
        points = [resolve(design, point) for point in track.points]
        for a, b in pairwise(points):
            # A diagonal is walked rather than boxed, so its clearance channel
            # follows the copper instead of swallowing the square it sits in.
            steps = max(1, math.ceil(math.dist(a, b) / 0.5))
            for index in range(steps):
                t0, t1 = index / steps, (index + 1) / steps
                p = (a[0] + (b[0] - a[0]) * t0, a[1] + (b[1] - a[1]) * t0)
                q = (a[0] + (b[0] - a[0]) * t1, a[1] + (b[1] - a[1]) * t1)
                box = (min(p[0], q[0]), min(p[1], q[1]), max(p[0], q[0]), max(p[1], q[1]))
                holes.append(grown(box, track.width / 2))
    return holes


def _fill_rectangles(
    design: Design, layer: str, net: str
) -> list[tuple[float, float, float, float]]:
    """The pour outline minus the holes, as a set of rectangles.

    KiCad stores a zone's fill as polygons and expects each to be a single
    outline, so a plane with a hole in the middle of it has to be cut open
    somewhere. Sweeping x instead and emitting one rectangle per gap avoids the
    question: every piece is convex, and neighbouring pieces are grown into each
    other by ``ZONE_WELD`` so that the connectivity that used to come from being
    one polygon now comes from overlapping.

    The alternative - forbidding copper of another net inside the pour, so that
    the fill is the outline itself - is what this replaced. It cost two of the
    four example boards a routable back layer.
    """
    x0, y0, x1, y1 = design.pour
    holes = [
        hole
        for hole in _hole_boxes(design, layer, net)
        if hole[0] < x1 and hole[2] > x0 and hole[1] < y1 and hole[3] > y0
    ]
    edges = sorted({x0, x1} | {min(max(v, x0), x1) for hole in holes for v in (hole[0], hole[2])})

    slabs: list[tuple[float, float, tuple[tuple[float, float], ...]]] = []
    for left, right in pairwise(edges):
        if right - left < GEOM_EPS:
            continue
        middle = (left + right) / 2
        spans = sorted((h[1], h[3]) for h in holes if h[0] <= middle <= h[2])
        free, cursor = [], y0
        for top, bottom in spans:
            if top > cursor:
                free.append((cursor, min(top, y1)))
            cursor = max(cursor, bottom)
            if cursor >= y1:
                break
        if cursor < y1:
            free.append((cursor, y1))
        keep = tuple((a, b) for a, b in free if b - a >= ZONE_SLIVER)
        if slabs and slabs[-1][2] == keep and abs(slabs[-1][1] - left) < GEOM_EPS:
            slabs[-1] = (slabs[-1][0], right, keep)
        else:
            slabs.append((left, right, keep))

    islands = [
        (
            max(left - ZONE_WELD, x0),
            max(top - ZONE_WELD, y0),
            min(right + ZONE_WELD, x1),
            min(bottom + ZONE_WELD, y1),
        )
        for left, right, free in slabs
        for top, bottom in free
    ]
    return _connected_islands(design, islands, layer, net)


def _connected_islands(design, islands, layer: str, net: str):
    """Drop the pieces of plane that no longer reach the net.

    A track laid across the pour can fence a corner of it off. KiCad's own
    filler calls that an island and removes it; leaving it in the file instead
    is one `unconnected_items` error per orphan, because a zone that is two
    separate shapes is two separate pieces of copper.
    """
    parent = list(range(len(islands)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        parent[find(i)] = find(j)

    for i, one in enumerate(islands):
        for j in range(i + 1, len(islands)):
            other = islands[j]
            if one[0] < other[2] and other[0] < one[2] and one[1] < other[3] and other[1] < one[3]:
                union(i, j)

    anchors: list[tuple[float, float, float, float]] = []
    for via in design.vias:
        if via.net == net:
            vx, vy = via_position(design, via)
            anchors.append((vx, vy, vx, vy))
    for part in design.parts:
        node = footprint_definition(part.footprint)
        for pad in node.children("pad"):
            number = str(pad.atom(0, ""))
            if f"{part.ref}.{number}" not in design.nets.get(net, ()):
                continue
            if pad_layer(pad) in (None, layer):
                anchors.append(pad_box(design, part, pad))
    for track in design.tracks:
        if track.net != net or track.layer != layer:
            continue
        for point in (resolve(design, p) for p in track.points):
            anchors.append((*point, *point))

    live = {
        find(i)
        for i, box in enumerate(islands)
        for anchor in anchors
        if box[0] <= anchor[2]
        and anchor[0] <= box[2]
        and box[1] <= anchor[3]
        and anchor[1] <= box[3]
    }
    return [box for i, box in enumerate(islands) if find(i) in live]


def _zone(design: Design, code: int) -> str:
    """The ground pour, and the fill that goes with it.

    The fill is computed here rather than left for KiCad because the committed
    board has to be complete on its own: an unfilled zone means DRC reports
    every ground pad unconnected, and the fabrication output ships without a
    plane. KiCad's own filler is not available - it needs a display, and the
    container has none - so this is the same subtraction done by hand.
    """
    ox, oy = design.origin
    x0, y0, x1, y1 = design.pour
    outline = [(ox + x0, oy + y0), (ox + x1, oy + y0), (ox + x1, oy + y1), (ox + x0, oy + y1)]
    pts = " ".join(f"(xy {round(x, 4)} {round(y, 4)})" for x, y in outline)
    lines = [
        "\t(zone",
        f"\t\t(net {code})",
        f'\t\t(net_name "{POUR_NET}")',
        '\t\t(layer "B.Cu")',
        f'\t\t(uuid "{stable_uuid(design.name, "zone")}")',
        "\t\t(hatch edge 0.5)",
        "\t\t(connect_pads",
        f"\t\t\t(clearance {ZONE_CLEARANCE})",
        "\t\t)",
        f"\t\t(min_thickness {ZONE_SLIVER / 2})",
        "\t\t(filled_areas_thickness no)",
        "\t\t(fill yes",
        "\t\t\t(thermal_gap 0.5)",
        "\t\t\t(thermal_bridge_width 0.5)",
        "\t\t)",
        f"\t\t(polygon (pts {pts}))",
    ]
    for left, top, right, bottom in _fill_rectangles(design, "B.Cu", POUR_NET):
        corners = [(left, top), (right, top), (right, bottom), (left, bottom)]
        island = " ".join(f"(xy {round(ox + x, 4)} {round(oy + y, 4)})" for x, y in corners)
        lines.append(f'\t\t(filled_polygon (layer "B.Cu") (pts {island}))')
    lines.append("\t)")
    return "\n".join(lines)


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
        date="",
        notes=[],
        parts=parts,
        power_flags=[],
        tracks=[replace(t, width=0.25) for t in design.tracks],
        vias=[],
        pour=None,
    )


# Values a generator picks by capacitance alone, ignoring the rail they sit on.
UNDERRATED = {"C1": "220u", "C3": "220u"}


# ---------------------------------------------------------------------------
# the designs
# ---------------------------------------------------------------------------


def buck_5v() -> Design:
    """12 V to 5 V at 2 A: LM2596S-5, catch diode, output inductor.

    Two things drive the floorplan.

    The TO-263 brings all five pins out of one edge on a 1.7 mm pitch, so nothing
    can be routed *past* the pin field - a track crossing it lands on the pads
    either side. Each pin leaves at its own y into a channel of its own. The
    circuit is then folded into two rows, input above and output below, so the
    switch node and the feedback trace run *parallel* down the board instead of
    having to cross: laid out in one row they cannot both get from the pin field
    to the output without one going over the other.

    The second is the pour. Only the two screw terminals are through-hole and
    both sit outside it, so B.Cu carries nothing but the plane and the vias into
    it - which is what lets the filled area be the pour outline itself, with
    nothing to subtract.
    """
    RIGHT = 168.91  # where the output half of the sheet starts
    parts = [
        Part(
            "J1",
            "Connector:Screw_Terminal_01x02",
            "12V IN",
            "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2_1x02_P5.00mm_Horizontal",
            sheet=(38.1, 66.04),
            board=(10.0, 12.0, 180.0),
            fields={
                "MPN": "1729128",
                "Manufacturer": "Phoenix Contact",
                "Datasheet": "https://www.phoenixcontact.com/product/1729128",
            },
        ),
        Part(
            "C1",
            "Device:C_Polarized",
            "220u",
            "Capacitor_SMD:CP_Elec_8x10.5",
            sheet=(63.5, 71.12),
            board=(32.0, 12.0, 0.0),
            fields={
                "Voltage": "35V",
                "Tolerance": "20%",
                "MPN": "EEE-FK1V221AP",
                "Manufacturer": "Panasonic",
                "Datasheet": "https://industrial.panasonic.com/cdbs/www-data/pdf/RDF0000/ABA0000C1053.pdf",
            },
        ),
        Part(
            "C2",
            "Device:C",
            "100n",
            "Capacitor_SMD:C_0805_2012Metric",
            sheet=(78.74, 71.12),
            # stood on end beside U1's VIN pin: the input loop is the one that
            # has to be short, and this is the only spot the fan-out leaves free
            board=(50.0, 7.0, 90.0),
            fields={
                "Voltage": "50V",
                "Tolerance": "10%",
                "MPN": "CL21B104KBCNNNC",
                "Manufacturer": "Samsung",
                "Datasheet": "https://product.samsungsem.com/mlcc/CL21B104KBCNNNC.do",
            },
        ),
        Part(
            "U1",
            "Regulator_Switching:LM2596S-5",
            "LM2596S-5",
            "Package_TO_SOT_SMD:TO-263-5_TabPin3",
            sheet=(109.22, 66.04),
            board=(62.0, 12.0, 0.0),
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
            board=(58.0, 36.0, 0.0),
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
            board=(76.0, 36.0, 0.0),
            fields={
                "Current": "3A",
                "Tolerance": "20%",
                "MPN": "SRR1260-330M",
                "Manufacturer": "Bourns",
                "Datasheet": "https://www.bourns.com/docs/product-datasheets/srr1260.pdf",
            },
        ),
        Part(
            "C4",
            "Device:C",
            "100n",
            "Capacitor_SMD:C_0805_2012Metric",
            sheet=(RIGHT + 15.24, 71.12),
            board=(90.0, 36.0, 0.0),
            fields={
                "Voltage": "25V",
                "Tolerance": "10%",
                "MPN": "CL21B104KBCNNNC",
                "Manufacturer": "Samsung",
                "Datasheet": "https://product.samsungsem.com/mlcc/CL21B104KBCNNNC.do",
            },
        ),
        Part(
            "C3",
            "Device:C_Polarized",
            "220u",
            "Capacitor_SMD:CP_Elec_8x10.5",
            sheet=(RIGHT, 71.12),
            board=(100.0, 36.0, 0.0),
            fields={
                "Voltage": "16V",
                "Tolerance": "20%",
                "MPN": "EEE-FK1C221P",
                "Manufacturer": "Panasonic",
                "Datasheet": "https://industrial.panasonic.com/cdbs/www-data/pdf/RDF0000/ABA0000C1053.pdf",
            },
        ),
        Part(
            "R1",
            "Device:R",
            "1k",
            "Resistor_SMD:R_0805_2012Metric",
            sheet=(RIGHT + 30.48, 71.12),
            board=(100.0, 46.0, 0.0),
            fields={
                "Tolerance": "1%",
                "Power": "0.125W",
                "MPN": "RC0805FR-071KL",
                "Manufacturer": "Yageo",
                "Datasheet": "https://www.yageo.com/upload/media/product/productsearch/datasheet/rchip/PYu-RC_Group_51_RoHS_L_12.pdf",
            },
        ),
        Part(
            "D2",
            "Device:LED",
            "green",
            "LED_SMD:LED_0805_2012Metric",
            sheet=(RIGHT + 30.48, 88.9),
            board=(107.0, 46.0, 180.0),
            angle=270.0,
            fields={
                "Voltage": "2.1V",
                "Current": "3mA",
                "MPN": "LTST-C170KGKT",
                "Manufacturer": "Lite-On",
                "Datasheet": "https://optoelectronics.liteon.com/upload/download/DS-22-98-0002/LTST-C170KGKT.pdf",
            },
        ),
        Part(
            "J2",
            "Connector:Screw_Terminal_01x02",
            "5V OUT",
            "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2_1x02_P5.00mm_Horizontal",
            sheet=(RIGHT + 45.72, 66.04),
            board=(116.0, 36.0, 0.0),
            fields={
                "MPN": "1729128",
                "Manufacturer": "Phoenix Contact",
                "Datasheet": "https://www.phoenixcontact.com/product/1729128",
            },
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
    # outer-layer copper carries about 2.7 A at a 10 C rise (IPC-2221). Feedback
    # and the LED branch carry nothing and stay narrow, but not below 0.4 mm,
    # because they hang off a rail.
    W, SIG = 1.0, 0.4
    tracks = [
        # Input rail across the top, stepping over each capacitor's ground pad.
        Track("+12V", "F.Cu", W, ["J1.1", (10.0, 4.0), (28.3, 4.0), "C1.1"]),
        Track("+12V", "F.Cu", W, [(28.3, 4.0), (47.0, 4.0), (47.0, 7.95), "C2.1"]),
        Track("+12V", "F.Cu", W, ["C2.1", (52.0, 7.95), (52.0, 8.6), "U1.1"]),
        # SW and FB leave the pin field into parallel channels and run down the
        # board together - never across each other.
        Track("SW", "F.Cu", W, ["U1.2", (44.0, 10.3), (44.0, 36.0), "D1.1"]),
        Track("SW", "F.Cu", W, ["D1.1", (56.0, 44.0), (71.05, 44.0), "L1.1"]),
        Track("+5V", "F.Cu", SIG, ["U1.4", (47.0, 13.7), (47.0, 26.0), (88.0, 26.0), (88.0, 36.0)]),
        # Output rail across the bottom row.
        Track("+5V", "F.Cu", W, ["L1.2", "C4.1"]),
        Track("+5V", "F.Cu", W, ["C4.1", (89.05, 30.0), (96.3, 30.0), "C3.1"]),
        Track("+5V", "F.Cu", W, [(96.3, 30.0), (116.0, 30.0), "J2.1"]),
        Track("+5V", "F.Cu", SIG, ["C3.1", (96.3, 42.0), (99.088, 42.0), "R1.1"]),
        Track("LED_A", "F.Cu", SIG, ["R1.2", "D2.2"]),
        # Ground: a stub from each pad to a via of its own, straight into the
        # pour. Only the two through-hole terminals, outside the pour, run far.
        Track("GND", "F.Cu", W, ["J1.2", (5.0, 50.0), (16.0, 50.0)]),
        Track("GND", "F.Cu", W, ["J2.2", (121.0, 50.0), (110.0, 50.0)]),
        Track("GND", "F.Cu", W, ["U1.3", (51.0, 12.0)]),
        Track("GND", "F.Cu", W, ["U1.5", (54.35, 20.0)]),
        Track("GND", "F.Cu", W, [(63.5, 12.0), (63.5, 20.0)]),  # the TO-263 tab
        Track("GND", "F.Cu", W, ["C1.2", (35.7, 14.6)]),
        Track("GND", "F.Cu", W, ["C2.2", (50.0, 4.4)]),
        Track("GND", "F.Cu", W, ["D1.2", (60.0, 31.0)]),
        Track("GND", "F.Cu", W, ["C4.2", (90.95, 38.0)]),
        Track("GND", "F.Cu", W, ["C3.2", (103.7, 38.6)]),
        Track("GND", "F.Cu", SIG, ["D2.1", (107.938, 50.0)]),
    ]
    vias = [
        Via("GND", x=16.0, y=50.0),
        Via("GND", x=110.0, y=50.0),
        Via("GND", x=51.0, y=12.0),
        Via("GND", x=54.35, y=20.0),
        Via("GND", x=63.5, y=20.0),
        Via("GND", x=35.7, y=14.6),
        Via("GND", x=50.0, y=4.4),
        Via("GND", x=60.0, y=31.0),
        Via("GND", x=90.95, y=38.0),
        Via("GND", x=103.7, y=38.6),
        Via("GND", x=107.938, y=50.0),
    ]

    return Design(
        name="buck-5v",
        title="12 V to 5 V buck converter, 2 A",
        rev="A",
        company="kicad_skills examples",
        notes=[
            "LM2596S-5 is the fixed 5 V part: FB ties straight to the output, no divider.",
            "C1 35 V on a 12 V rail and C3 16 V on a 5 V rail - both keep more than the",
            "1.5x headroom the gate asks for over their working voltage.",
            "L1 33 uH / 3 A saturation: ripple is about 0.6 A pk-pk at 2 A out, so the",
            "peak stays under the rating.",
            "D1 catches the inductor current. SS34 is 3 A / 40 V, above both the 2 A load",
            "and the 12 V input.",
            "Power copper is 1.0 mm, good for 2.7 A at a 10 C rise (IPC-2221).",
            "Input row above, output row below: SW and FB then run down the board in",
            "parallel channels instead of having to cross each other.",
        ],
        parts=parts,
        nets=nets,
        power_flags=["+12V", "GND", "+5V"],
        board_size=(126.0, 56.0),
        tracks=tracks,
        vias=vias,
        pour=(15.0, 2.0, 112.0, 54.0),
    )


def motor_driver() -> Design:
    """Dual H-bridge for two brushed DC motors: DRV8833PW, logic on a header.

    Three things decide the floorplan.

    *The package.* A TSSOP-16 brings its pins out on a 0.65 mm pitch, and the
    gap between two adjacent pads is narrower than a track and its clearance.
    Nothing is routed between them and nothing is searched for there either: the
    two rows leave as a stated fan (:func:`fan`), which walks the pitch out to
    1.3 mm at a shallow enough angle that no track cuts into its neighbour. The
    router picks the nets up from the far end of that fan.

    *The pin order.* AOUT1/AOUT2 come out of the package in the opposite order
    to BOUT1/BOUT2, so a fan that does not cross itself lands motor A on the
    terminal block one way round and motor B the other. Both terminals are
    unpolarised - the silk says A and B - so the layout is allowed to decide,
    and the schematic says so rather than leaving a reviewer to wonder.

    *The plane.* The ground pour is the back of the left two thirds, under the
    driver and the four output tracks, which is where the current and the heat
    are. It stops at x = 64 because the fill these examples write is the pour
    outline itself - so no foreign copper may be inside it, and the back of the
    right hand third is where the router is allowed to cross something.
    """
    parts = [
        Part(
            "J1",
            "Connector:Screw_Terminal_01x02",
            "VM 2.7-10.8V",
            "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2_1x02_P5.00mm_Horizontal",
            sheet=(30.0, 80.0),
            board=(92.0, 12.0, 270.0),
            fields={
                "MPN": "1729128",
                "Manufacturer": "Phoenix Contact",
                "Datasheet": "https://www.phoenixcontact.com/product/1729128",
            },
        ),
        Part(
            "C1",
            "Device:C_Polarized",
            "100u",
            "Capacitor_SMD:CP_Elec_6.3x7.7",
            sheet=(55.0, 85.0),
            board=(70.0, 17.0, 0.0),
            fields={
                "Voltage": "25V",
                "Tolerance": "20%",
                "MPN": "EEE-FK1E101P",
                "Manufacturer": "Panasonic",
                "Datasheet": "https://industrial.panasonic.com/cdbs/www-data/pdf/RDF0000/ABA0000C1053.pdf",
            },
        ),
        Part(
            "C2",
            "Device:C",
            "100n",
            "Capacitor_SMD:C_0805_2012Metric",
            sheet=(70.0, 85.0),
            board=(57.5, 26.5, 90.0),
            fields={
                "Voltage": "50V",
                "Tolerance": "10%",
                "MPN": "CL21B104KBCNNNC",
                "Manufacturer": "Samsung",
                "Datasheet": "https://product.samsungsem.com/mlcc/CL21B104KBCNNNC.do",
            },
        ),
        Part(
            "C3",
            "Device:C",
            "10n",
            "Capacitor_SMD:C_0805_2012Metric",
            sheet=(85.0, 85.0),
            board=(63.0, 27.0, 270.0),
            fields={
                "Voltage": "50V",
                "Tolerance": "10%",
                "MPN": "CL21B103KBANNNC",
                "Manufacturer": "Samsung",
                "Datasheet": "https://product.samsungsem.com/mlcc/CL21B103KBANNNC.do",
            },
        ),
        Part(
            "U1",
            "Driver_Motor:DRV8833PW",
            "DRV8833PW",
            "Package_SO:TSSOP-16_4.4x5mm_P0.65mm",
            sheet=(130.0, 80.0),
            board=(44.0, 26.0, 0.0),
            fields={
                "MPN": "DRV8833PWR",
                "Manufacturer": "Texas Instruments",
                "Datasheet": "https://www.ti.com/lit/ds/symlink/drv8833.pdf",
            },
        ),
        Part(
            "C4",
            "Device:C",
            "1u",
            "Capacitor_SMD:C_0805_2012Metric",
            sheet=(175.0, 85.0),
            board=(61.0, 24.0, 0.0),
            fields={
                "Voltage": "16V",
                "Tolerance": "10%",
                "MPN": "CL21A105KBFNNNE",
                "Manufacturer": "Samsung",
                "Datasheet": "https://product.samsungsem.com/mlcc/CL21A105KBFNNNE.do",
            },
        ),
        Part(
            "R1",
            "Device:R",
            "10k",
            "Resistor_SMD:R_0805_2012Metric",
            sheet=(190.0, 85.0),
            board=(60.0, 40.0, 0.0),
            fields={
                "Tolerance": "1%",
                "Power": "0.125W",
                "MPN": "RC0805FR-0710KL",
                "Manufacturer": "Yageo",
                "Datasheet": "https://www.yageo.com/upload/media/product/productsearch/datasheet/rchip/PYu-RC_Group_51_RoHS_L_12.pdf",
            },
        ),
        Part(
            "R2",
            "Device:R",
            "4k7",
            "Resistor_SMD:R_0805_2012Metric",
            sheet=(205.0, 85.0),
            board=(72.0, 6.0, 0.0),
            fields={
                "Tolerance": "1%",
                "Power": "0.125W",
                "MPN": "RC0805FR-074K7L",
                "Manufacturer": "Yageo",
                "Datasheet": "https://www.yageo.com/upload/media/product/productsearch/datasheet/rchip/PYu-RC_Group_51_RoHS_L_12.pdf",
            },
        ),
        Part(
            "D2",
            "Device:LED",
            "green",
            "LED_SMD:LED_0805_2012Metric",
            sheet=(205.0, 100.0),
            board=(80.0, 6.0, 180.0),
            fields={
                "Voltage": "2.1V",
                "Current": "1.5mA",
                "MPN": "LTST-C170KGKT",
                "Manufacturer": "Lite-On",
                "Datasheet": "https://optoelectronics.liteon.com/upload/download/DS-22-98-0002/LTST-C170KGKT.pdf",
            },
        ),
        Part(
            "J2",
            "Connector:Screw_Terminal_01x02",
            "MOTOR A",
            "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2_1x02_P5.00mm_Horizontal",
            sheet=(240.0, 70.0),
            board=(8.0, 20.0, 90.0),
            fields={
                "MPN": "1729128",
                "Manufacturer": "Phoenix Contact",
                "Datasheet": "https://www.phoenixcontact.com/product/1729128",
            },
        ),
        Part(
            "J3",
            "Connector:Screw_Terminal_01x02",
            "MOTOR B",
            "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2_1x02_P5.00mm_Horizontal",
            sheet=(240.0, 95.0),
            board=(8.0, 38.0, 90.0),
            fields={
                "MPN": "1729128",
                "Manufacturer": "Phoenix Contact",
                "Datasheet": "https://www.phoenixcontact.com/product/1729128",
            },
        ),
        Part(
            "J4",
            "Connector:Conn_01x08_Pin",
            "LOGIC",
            "Connector_PinHeader_2.54mm:PinHeader_1x08_P2.54mm_Vertical",
            sheet=(40.0, 45.0),
            board=(92.0, 26.0, 0.0),
            fields={
                "MPN": "61300811121",
                "Manufacturer": "Wurth Elektronik",
                "Datasheet": "https://www.we-online.com/components/products/datasheet/61300811121.pdf",
            },
        ),
    ]

    nets = {
        "VM": ["J1.1", "C1.1", "C2.1", "C3.1", "U1.12", "R2.1"],
        # J4 is in the order the tracks arrive, so that nothing has to cross to
        # reach it: ground at both ends, then the two signals that come round
        # the outside of the package and the four that come straight out of it.
        "GND": [
            "J1.2",
            "C1.2",
            "C2.2",
            "U1.13",
            "U1.3",
            "U1.6",
            "C4.2",
            "D2.1",
            "J4.1",
            "J4.8",
        ],
        "VCP": ["U1.11", "C3.2"],
        "VINT": ["U1.14", "C4.1", "R1.1"],
        "nFAULT": ["U1.8", "R1.2", "J4.7"],
        "nSLEEP": ["U1.1", "J4.2"],
        "AIN1": ["U1.16", "J4.3"],
        "AIN2": ["U1.15", "J4.4"],
        "BIN2": ["U1.10", "J4.5"],
        "BIN1": ["U1.9", "J4.6"],
        # The package brings A out 1-then-2 down the row and B out 2-then-1, so
        # a fan that does not cross itself lands them on opposite terminals.
        "AOUT1": ["U1.2", "J2.2"],
        "AOUT2": ["U1.4", "J2.1"],
        "BOUT1": ["U1.7", "J3.1"],
        "BOUT2": ["U1.5", "J3.2"],
        "LED_A": ["R2.2", "D2.2"],
    }

    design = Design(
        name="motor-driver",
        title="Dual H-bridge, DRV8833, 2 x 1.5 A",
        rev="A",
        company="kicad_skills examples",
        notes=[
            "DRV8833PW: 1.5 A per bridge continuous, 2 A peak, VM 2.7 - 10.8 V.",
            "AISEN and BISEN are tied to ground - no current sensing, so the",
            "current limit is the part's own internal one.",
            "C3 10 nF is the charge pump flying capacitor between VM and VCP;",
            "the datasheet asks for 10 nF and nothing else will do.",
            "C4 1 uF bypasses VINT, the internal 3.3 V regulator, which also",
            "pulls up the open-drain nFAULT through R1.",
            "C1 100 uF / 25 V on a rail that can reach 10.8 V - more than the",
            "1.5x headroom, and enough bulk for the motor current steps.",
            "The motor terminals are unpolarised. The package brings AOUT1/AOUT2",
            "out in the opposite order to BOUT1/BOUT2, so the fan-out lands A on",
            "J2 one way round and B on J3 the other; the silk says A and B.",
            "The ground pour covers the driver and the output tracks. It stops",
            "short of the connectors so the back of the right hand third is free",
            "for the two crossings the logic needs.",
        ],
        parts=parts,
        nets=nets,
        power_flags=["VM", "GND", "VINT"],
        board_size=(100.0, 50.0),
        tracks=[],
        vias=[],
        pour=(3.0, 3.0, 97.0, 47.0),
    )

    # Snap before the fan-out is worked out: it measures from where the pads
    # actually are, so the placement has to be final first.
    design = design.snapped()

    # -- the escape from the package ---------------------------------------
    # 1.5 A a bridge, so an output leaves at the width of its own pad and is
    # widened by the router once it is clear; logic carries nothing.
    SIG, POWER = 0.3, 0.8
    # The output side spreads to 2.0 mm because four 0.8 mm tracks start there
    # and two of them are 0.8 mm apart from a ground via.
    left, west = fan(
        design,
        "U1",
        ["1", "2", "3", "4", "5", "6", "7", "8"],
        lead=39.6,
        column=29.0,
        pitch=2.0,
        centre=26.0,
        width=SIG,
    )
    right, east = fan(
        design,
        "U1",
        ["16", "15", "14", "13", "12", "11", "10", "9"],
        lead=48.4,
        column=53.6,
        pitch=1.3,
        centre=26.0,
        width=SIG,
    )

    tracks = [*left, *right]
    # AISEN, BISEN and GND stop at the end of the fan, on top of a via into the
    # plane. Nothing else on this board asks the plane for anything.
    vias = [
        Via("GND", x=west["3"][0], y=west["3"][1]),
        Via("GND", x=west["6"][0], y=west["6"][1]),
        Via("GND", x=east["13"][0], y=east["13"][1]),
    ]

    # -- the supply, placed by hand ----------------------------------------
    # Bulk, then bypass, then the pin: the loop closes at the part, so the
    # bypass sits hard against the end of pin 12's escape.
    tracks += [
        Track("VM", "F.Cu", POWER, [east["12"], "C2.1"]),
        Track("VM", "F.Cu", POWER, ["J1.1", "C1.1"], auto=True),
        Track("VM", "F.Cu", POWER, ["C1.1", "C3.1"], auto=True),
        Track("VM", "F.Cu", POWER, ["C3.1", "C2.1"], auto=True),
        Track("VM", "F.Cu", SIG, ["C1.1", "R2.1"], auto=True),
        Track("VCP", "F.Cu", SIG, [east["11"], "C3.2"], auto=True),
        Track("VINT", "F.Cu", SIG, [east["14"], "C4.1"], auto=True),
        Track("VINT", "F.Cu", SIG, ["C4.1", "R1.1"], auto=True),
        Track("LED_A", "F.Cu", SIG, ["R2.2", "D2.2"], auto=True),
    ]

    # -- ground ------------------------------------------------------------
    # Routed before the signals, because a ground pad that has to walk to find a
    # via has already lost the loop it was there to close. Each one asks for the
    # back of the board a couple of millimetres away and the router spends the
    # via; the plane is under all of it.
    tracks += [
        Track("GND", "F.Cu", 0.5, ["C2.2", (57.5, 22.0)], auto=True, goal_layer="B.Cu"),
        Track("GND", "F.Cu", 0.5, ["C4.2", (61.5, 24.05)], auto=True, goal_layer="B.Cu"),
        Track("GND", "F.Cu", 0.5, ["C1.2", (68.0, 20.5)], auto=True, goal_layer="B.Cu"),
        Track("GND", "F.Cu", 0.5, ["J1.2", (88.0, 18.5)], auto=True, goal_layer="B.Cu"),
        Track("GND", "F.Cu", 0.5, ["J4.1", (88.0, 24.0)], auto=True, goal_layer="B.Cu"),
        Track("GND", "F.Cu", 0.5, ["J4.8", (88.0, 45.0)], auto=True, goal_layer="B.Cu"),
        Track("GND", "F.Cu", SIG, ["D2.1", (83.0, 8.5)], auto=True, goal_layer="B.Cu"),
    ]

    # -- everything that simply has to arrive ------------------------------
    tracks += [
        Track("AOUT1", "F.Cu", POWER, [west["2"], "J2.2"], auto=True),
        Track("AOUT2", "F.Cu", POWER, [west["4"], "J2.1"], auto=True),
        Track("BOUT2", "F.Cu", POWER, [west["5"], "J3.2"], auto=True),
        Track("BOUT1", "F.Cu", POWER, [west["7"], "J3.1"], auto=True),
        Track("nSLEEP", "F.Cu", SIG, [west["1"], "J4.2"], auto=True),
        Track("nFAULT", "F.Cu", SIG, [west["8"], "R1.2"], auto=True),
        Track("nFAULT", "F.Cu", SIG, ["R1.2", "J4.7"], auto=True),
        Track("AIN1", "F.Cu", SIG, [east["16"], "J4.3"], auto=True),
        Track("AIN2", "F.Cu", SIG, [east["15"], "J4.4"], auto=True),
        Track("BIN2", "F.Cu", SIG, [east["10"], "J4.5"], auto=True),
        Track("BIN1", "F.Cu", SIG, [east["9"], "J4.6"], auto=True),
    ]

    return replace(design, tracks=tracks, vias=vias)


PICO = "MCU_Module:RaspberryPi_Pico"
# What the module's own pin names become as net names. Everything else keeps the
# symbol's name, which is already what a Pico datasheet calls it. AGND is not
# among them: it is the ADC's return and the module already joins it to GND
# internally, so a carrier that ties the two again has two power outputs wired
# together - which is what KiCad's ERC says, and it is right. It goes to the
# header on its own, and what to do with it is the user's decision.
PICO_NET = {"GND": "GND", "3V3": "+3V3"}


def pico_net(name: str) -> str:
    """The net a Pico pin belongs on, from the name the symbol gives it.

    Reading the pinout out of the symbol rather than typing it again is not
    laziness: forty pins typed twice is forty chances to swap two of them, and
    nothing downstream would notice - the netlist would simply be a different,
    self-consistent board.
    """
    if name in PICO_NET:
        return PICO_NET[name]
    if name.startswith("GPIO"):
        return "GP" + name.removeprefix("GPIO").split("_")[0]
    return name


def pico_carrier() -> Design:
    """A Raspberry Pi Pico carrier: every pin broken out, and a supply for it.

    A carrier is mostly one job done forty times - each module pad to the header
    pad beside it - and the interesting parts are at the edges of that.

    *The pinout is read, not typed.* The nets come from the symbol's own pin
    names, so the header is wired to the module by construction. The right hand
    header runs the other way up, because the module numbers its right side from
    the bottom and a header numbers itself from the top; that reversal is the
    one place a carrier board is easy to get wrong, and it is one line here.

    *Seven grounds, one wire.* The Pico symbol stacks its ground pins at a single
    point. Drawing seven wires and seven ground symbols on that point reads as
    one and reviews as seven, so :func:`emit_schematic` draws it once.

    *The supply.* An external 5 V feeds VSYS through a Schottky, which is what
    the Pico datasheet asks for - the diode is what keeps USB and the external
    supply from fighting when both are present. C1 is the bulk that goes with
    it, C2 bypasses the module's own 3.3 V, and D3 says the rail is up.
    """
    pins = {pin.number: pin.name for pin in symbol_pins(PICO)}
    left = [str(n) for n in range(1, 21)]  # module pins 1-20, top to bottom
    right = [str(n) for n in range(40, 20, -1)]  # 40 down to 21, top to bottom

    parts = [
        Part(
            "U1",
            PICO,
            "Pico",
            "Module:RaspberryPi_Pico_SMD",
            sheet=(150.0, 100.0),
            board=(26.0, 34.13, 0.0),
            stub=7.62,
            fields={
                "MPN": "SC0915",
                "Manufacturer": "Raspberry Pi",
                "Datasheet": "https://datasheets.raspberrypi.com/pico/pico-datasheet.pdf",
            },
        ),
        Part(
            "J3",
            "Connector:Conn_01x20_Pin",
            "GP0-GP15",
            "Connector_PinHeader_2.54mm:PinHeader_1x20_P2.54mm_Vertical",
            sheet=(60.0, 100.0),
            board=(10.0, 10.0, 0.0),
            stub=7.62,
            fields={
                "MPN": "61302011121",
                "Manufacturer": "Wurth Elektronik",
                "Datasheet": "https://www.we-online.com/components/products/datasheet/61302011121.pdf",
            },
        ),
        Part(
            "J4",
            "Connector:Conn_01x20_Pin",
            "GP16-GP28, PWR",
            "Connector_PinHeader_2.54mm:PinHeader_1x20_P2.54mm_Vertical",
            sheet=(240.0, 100.0),
            board=(42.0, 10.0, 0.0),
            stub=7.62,
            fields={
                "MPN": "61302011121",
                "Manufacturer": "Wurth Elektronik",
                "Datasheet": "https://www.we-online.com/components/products/datasheet/61302011121.pdf",
            },
        ),
        Part(
            "J1",
            "Connector:Screw_Terminal_01x02",
            "5V IN",
            "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2_1x02_P5.00mm_Horizontal",
            sheet=(60.0, 40.0),
            board=(72.0, 11.0, 270.0),
            fields={
                "MPN": "1729128",
                "Manufacturer": "Phoenix Contact",
                "Datasheet": "https://www.phoenixcontact.com/product/1729128",
            },
        ),
        Part(
            "D1",
            "Device:D_Schottky",
            "SS14",
            "Diode_SMD:D_SMA",
            sheet=(90.0, 40.0),
            board=(56.0, 11.0, 180.0),
            fields={
                "Voltage": "40V",
                "Current": "1A",
                "MPN": "SS14",
                "Manufacturer": "Vishay",
                "Datasheet": "https://www.vishay.com/docs/88746/ss12.pdf",
            },
        ),
        Part(
            "C1",
            "Device:C",
            "22u",
            "Capacitor_SMD:C_1210_3225Metric",
            sheet=(115.0, 45.0),
            board=(46.0, 12.54, 90.0),
            fields={
                "Voltage": "16V",
                "Tolerance": "20%",
                "MPN": "CL32A226KAJNNNE",
                "Manufacturer": "Samsung",
                "Datasheet": "https://product.samsungsem.com/mlcc/CL32A226KAJNNNE.do",
            },
        ),
        Part(
            "C2",
            "Device:C",
            "100n",
            "Capacitor_SMD:C_0805_2012Metric",
            sheet=(200.0, 45.0),
            board=(46.0, 20.0, 0.0),
            fields={
                "Voltage": "25V",
                "Tolerance": "10%",
                "MPN": "CL21B104KBCNNNC",
                "Manufacturer": "Samsung",
                "Datasheet": "https://product.samsungsem.com/mlcc/CL21B104KBCNNNC.do",
            },
        ),
        Part(
            "R1",
            "Device:R",
            "1k",
            "Resistor_SMD:R_0805_2012Metric",
            sheet=(225.0, 45.0),
            board=(56.0, 20.0, 0.0),
            fields={
                "Tolerance": "1%",
                "Power": "0.125W",
                "MPN": "RC0805FR-071KL",
                "Manufacturer": "Yageo",
                "Datasheet": "https://www.yageo.com/upload/media/product/productsearch/datasheet/rchip/PYu-RC_Group_51_RoHS_L_12.pdf",
            },
        ),
        Part(
            "D3",
            "Device:LED",
            "green",
            "LED_SMD:LED_0805_2012Metric",
            sheet=(225.0, 60.0),
            board=(64.0, 20.0, 180.0),
            fields={
                "Voltage": "2.1V",
                "Current": "1.2mA",
                "MPN": "LTST-C170KGKT",
                "Manufacturer": "Lite-On",
                "Datasheet": "https://optoelectronics.liteon.com/upload/download/DS-22-98-0002/LTST-C170KGKT.pdf",
            },
        ),
    ]

    # Every module pin, on the net its own name says it is on, and on the header
    # pad beside it. J4 counts down because the module counts its right hand
    # side up.
    nets: dict[str, list[str]] = {}
    for index, number in enumerate(left, start=1):
        nets.setdefault(pico_net(pins[number]), []).extend([f"U1.{number}", f"J3.{index}"])
    for index, number in enumerate(right, start=1):
        nets.setdefault(pico_net(pins[number]), []).extend([f"U1.{number}", f"J4.{index}"])

    nets["+5V"] = ["J1.1", "D1.1"]
    nets["VSYS"] += ["D1.2", "C1.1"]
    nets["+3V3"] += ["C2.1", "R1.1"]
    nets["GND"] += ["J1.2", "C1.2", "C2.2", "D3.1"]
    nets["LED_P"] = ["R1.2", "D3.2"]

    design = Design(
        name="pico-carrier",
        title="Raspberry Pi Pico carrier, 5 V in",
        rev="A",
        company="kicad_skills examples",
        notes=[
            "Every module pin is brought out 1:1 to the header beside it. J4 counts",
            "down against the module: the Pico numbers its right hand side from the",
            "bottom and a pin header numbers itself from the top.",
            "5 V reaches VSYS through D1, which is what the Pico datasheet asks for",
            "- it stops USB and the external supply fighting when both are plugged",
            "in, at the cost of a diode drop. C1 22 uF / 16 V is the bulk that goes",
            "with it, small enough to sit against the pin, which an electrolytic is",
            "not. C2 bypasses the module's own 3.3 V and D3 says that rail is up.",
            "AGND goes to the header on its own: the module already joins it to GND,",
            "and doing it again here is two power outputs wired together.",
        ],
        parts=parts,
        nets=nets,
        power_flags=["+5V", "VSYS", "ADC_VREF"],
        board_size=(82.0, 68.0),
        tracks=[],
        vias=[],
        pour=(3.0, 3.0, 79.0, 65.0),
        notes_at=(20.0, 152.0),
        flags_at=(133.0, 30.0),
        # The module's 2.54 mm pad pitch decides where everything goes; snapping
        # to 0.5 mm would move the headers off the pins they exist to reach.
        board_grid=None,
    ).snapped()

    SIG, POWER = 0.3, 0.8
    tracks: list[Track] = []
    # The breakout itself: each module pad straight across to its header pad.
    for header, numbers in (("J3", left), ("J4", right)):
        for index, number in enumerate(numbers, start=1):
            net = pico_net(pins[number])
            width = POWER if net in ("GND", "VSYS", "VBUS", "+3V3") else SIG
            tracks.append(Track(net, "F.Cu", width, [f"U1.{number}", f"{header}.{index}"]))

    # The supply, placed by hand.
    tracks += [
        Track("+5V", "F.Cu", POWER, ["J1.1", "D1.1"], auto=True),
        Track("VSYS", "F.Cu", POWER, ["D1.2", "J4.2"], auto=True),
        Track("VSYS", "F.Cu", POWER, ["C1.1", "D1.2"], auto=True),
        Track("+3V3", "F.Cu", POWER, ["C2.1", "J4.5"], auto=True),
        Track("+3V3", "F.Cu", SIG, ["C2.1", "R1.1"], auto=True),
        Track("LED_P", "F.Cu", SIG, ["R1.2", "D3.2"], auto=True),
    ]
    # Ground: every pad drops straight through to the plane under it.
    for pad, target in (
        ("C1.2", (49.0, 17.0)),
        ("C2.2", (48.5, 20.0)),
        ("D3.1", (68.0, 24.0)),
        ("J1.2", (66.0, 17.0)),
    ):
        tracks.append(Track("GND", "F.Cu", 0.5, [pad, target], auto=True, goal_layer="B.Cu"))
    # The module's own ground pads are surface mount and reach the plane through
    # the header pins they are wired to, which are through-hole and sit in it
    # already - so those need no via of their own. J1 is the same. Only the four
    # surface mount parts out on the right have to drill down.
    return replace(design, tracks=tracks)


def _passive(ref, lib, value, footprint, sheet, board, angle=0.0, **fields):
    return Part(ref, lib, value, footprint, sheet, board, angle=angle, fields=fields)


OPAMP = "Amplifier_Operational:MCP6001R"
YAGEO = "https://www.yageo.com/upload/media/product/productsearch/datasheet/rchip/PYu-RC_Group_51_RoHS_L_12.pdf"
SAMSUNG = "https://product.samsungsem.com/mlcc/{}.do"


def opamp_filter() -> Design:
    """A 1 kHz Sallen-Key low pass on a single 5 V supply.

    The part that makes this an analogue example rather than an arrangement of
    parts is what "ground" means. On one supply the signal has to sit somewhere
    in the middle of the rail, so there are two grounds: GND, which is the
    supply return, and VREF at half the rail, which is what the filter is
    referenced to. R3/R4 make VREF and U2 buffers it, because the filter's
    return current flows into that node through C2 - into a bare divider that is
    a 50 kohm source impedance and the filter is not the filter any more.

    The values are a Butterworth-ish pair rather than the textbook one: equal
    10 k resistors with 22 nF and 10 nF give f = 1073 Hz and Q = 0.742, where
    Butterworth wants Q = 0.707 and 11 nF. 10 nF is a value one can buy in C0G
    and 11 nF is not, and the sheet says so rather than implying the arithmetic
    came out exactly.

    C0G matters more than the arithmetic: an X7R of the same value loses a third
    of its capacitance over the rail's range and the corner moves with it, which
    is why the two filter capacitors state a dielectric and nothing else does.
    """
    parts = [
        _passive(
            "J1",
            "Connector:Conn_01x02_Pin",
            "IN",
            "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
            (30.0, 100.0),
            (8.0, 18.0, 0.0),
            MPN="61300211121",
            Manufacturer="Wurth Elektronik",
            Datasheet="https://www.we-online.com/components/products/datasheet/61300211121.pdf",
        ),
        _passive(
            "C3",
            "Device:C",
            "1u",
            "Capacitor_SMD:C_0805_2012Metric",
            (55.0, 100.0),
            (16.0, 18.0, 90.0),
            Voltage="25V",
            Tolerance="10%",
            MPN="CL21B105KBFNNNE",
            Manufacturer="Samsung",
            Datasheet=SAMSUNG.format("CL21B105KBFNNNE"),
        ),
        _passive(
            "R5",
            "Device:R",
            "100k",
            "Resistor_SMD:R_0805_2012Metric",
            (70.0, 130.0),
            (24.0, 26.0, 0.0),
            Tolerance="1%",
            Power="0.125W",
            MPN="RC0805FR-07100KL",
            Manufacturer="Yageo",
            Datasheet=YAGEO,
        ),
        _passive(
            "R1",
            "Device:R",
            "10k",
            "Resistor_SMD:R_0805_2012Metric",
            (85.0, 100.0),
            (26.0, 18.0, 0.0),
            angle=90.0,
            Tolerance="1%",
            Power="0.125W",
            MPN="RC0805FR-0710KL",
            Manufacturer="Yageo",
            Datasheet=YAGEO,
        ),
        _passive(
            "R2",
            "Device:R",
            "10k",
            "Resistor_SMD:R_0805_2012Metric",
            (115.0, 100.0),
            (36.0, 18.0, 0.0),
            angle=90.0,
            Tolerance="1%",
            Power="0.125W",
            MPN="RC0805FR-0710KL",
            Manufacturer="Yageo",
            Datasheet=YAGEO,
        ),
        _passive(
            "C1",
            "Device:C",
            "22n",
            "Capacitor_SMD:C_0805_2012Metric",
            (100.0, 70.0),
            (42.0, 11.0, 0.0),
            Voltage="50V",
            Tolerance="1%",
            Dielectric="C0G",
            MPN="CL21C223JBFNNNE",
            Manufacturer="Samsung",
            Datasheet=SAMSUNG.format("CL21C223JBFNNNE"),
        ),
        _passive(
            "C2",
            "Device:C",
            "10n",
            "Capacitor_SMD:C_0805_2012Metric",
            (130.0, 130.0),
            (40.0, 24.0, 0.0),
            Voltage="50V",
            Tolerance="1%",
            Dielectric="C0G",
            MPN="CL21C103JBFNNNE",
            Manufacturer="Samsung",
            Datasheet=SAMSUNG.format("CL21C103JBFNNNE"),
        ),
        Part(
            "U1",
            OPAMP,
            "MCP6001R",
            "Package_TO_SOT_SMD:SOT-23-5",
            sheet=(160.0, 100.0),
            board=(48.0, 18.0, 0.0),
            stub=6.35,
            fields={
                "MPN": "MCP6001RT-I/OT",
                "Manufacturer": "Microchip",
                "Datasheet": "https://ww1.microchip.com/downloads/en/DeviceDoc/21733j.pdf",
            },
        ),
        _passive(
            "C5",
            "Device:C",
            "100n",
            "Capacitor_SMD:C_0805_2012Metric",
            (190.0, 65.0),
            (52.0, 12.0, 0.0),
            Voltage="25V",
            Tolerance="10%",
            MPN="CL21B104KBCNNNC",
            Manufacturer="Samsung",
            Datasheet=SAMSUNG.format("CL21B104KBCNNNC"),
        ),
        _passive(
            "C6",
            "Device:C",
            "1u",
            "Capacitor_SMD:C_0805_2012Metric",
            (195.0, 100.0),
            (60.0, 18.0, 90.0),
            Voltage="25V",
            Tolerance="10%",
            MPN="CL21B105KBFNNNE",
            Manufacturer="Samsung",
            Datasheet=SAMSUNG.format("CL21B105KBFNNNE"),
        ),
        _passive(
            "J3",
            "Connector:Conn_01x02_Pin",
            "OUT",
            "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
            (225.0, 100.0),
            (72.0, 18.0, 0.0),
            MPN="61300211121",
            Manufacturer="Wurth Elektronik",
            Datasheet="https://www.we-online.com/components/products/datasheet/61300211121.pdf",
        ),
        _passive(
            "J2",
            "Connector:Screw_Terminal_01x02",
            "5V",
            "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2_1x02_P5.00mm_Horizontal",
            (205.0, 35.0),
            (70.0, 8.0, 0.0),
            MPN="1729128",
            Manufacturer="Phoenix Contact",
            Datasheet="https://www.phoenixcontact.com/product/1729128",
        ),
        _passive(
            "R3",
            "Device:R",
            "100k",
            "Resistor_SMD:R_0805_2012Metric",
            (75.0, 165.0),
            (30.0, 34.0, 0.0),
            Tolerance="1%",
            Power="0.125W",
            MPN="RC0805FR-07100KL",
            Manufacturer="Yageo",
            Datasheet=YAGEO,
        ),
        _passive(
            "R4",
            "Device:R",
            "100k",
            "Resistor_SMD:R_0805_2012Metric",
            (75.0, 190.0),
            (36.0, 34.0, 0.0),
            Tolerance="1%",
            Power="0.125W",
            MPN="RC0805FR-07100KL",
            Manufacturer="Yageo",
            Datasheet=YAGEO,
        ),
        _passive(
            "C4",
            "Device:C",
            "10u",
            "Capacitor_SMD:C_0805_2012Metric",
            (105.0, 180.0),
            (42.0, 34.0, 0.0),
            Voltage="16V",
            Tolerance="20%",
            MPN="CL21A106KOQNNNE",
            Manufacturer="Samsung",
            Datasheet=SAMSUNG.format("CL21A106KOQNNNE"),
        ),
        Part(
            "U2",
            OPAMP,
            "MCP6001R",
            "Package_TO_SOT_SMD:SOT-23-5",
            sheet=(150.0, 175.0),
            board=(52.0, 34.0, 0.0),
            stub=6.35,
            fields={
                "MPN": "MCP6001RT-I/OT",
                "Manufacturer": "Microchip",
                "Datasheet": "https://ww1.microchip.com/downloads/en/DeviceDoc/21733j.pdf",
            },
        ),
        _passive(
            "C7",
            "Device:C",
            "100n",
            "Capacitor_SMD:C_0805_2012Metric",
            (190.0, 150.0),
            (58.0, 30.0, 0.0),
            Voltage="25V",
            Tolerance="10%",
            MPN="CL21B104KBCNNNC",
            Manufacturer="Samsung",
            Datasheet=SAMSUNG.format("CL21B104KBCNNNC"),
        ),
    ]

    nets = {
        "+5V": ["J2.1", "R3.1", "C5.1", "C7.1", "U1.2", "U2.2"],
        "GND": ["J2.2", "J1.2", "J3.2", "R4.2", "C4.2", "C5.2", "C7.2", "U1.5", "U2.5"],
        "IN": ["J1.1", "C3.1"],
        "IN_DC": ["C3.2", "R5.1", "R1.1"],
        "X": ["R1.2", "R2.1", "C1.1"],
        "FILT_IN": ["R2.2", "C2.1", "U1.3"],
        "OUT": ["U1.1", "U1.4", "C1.2", "C6.1"],
        "OUT_AC": ["C6.2", "J3.1"],
        "MID": ["R3.2", "R4.1", "C4.1", "U2.3"],
        "VREF": ["U2.1", "U2.4", "R5.2", "C2.2"],
    }

    design = Design(
        name="opamp-filter",
        title="1 kHz Sallen-Key low pass, single 5 V",
        rev="A",
        company="kicad_skills examples",
        notes=[
            "Second-order Sallen-Key low pass, unity gain, on one 5 V rail.",
            "R1 = R2 = 10k with C1 = 22n and C2 = 10n gives f = 1073 Hz and",
            "Q = 0.742. Butterworth wants Q = 0.707, which is 11 nF - a value",
            "one cannot buy in C0G, and C0G is what keeps the corner where it is.",
            "An X7R of the same size loses a third of its value over the rail.",
            "VREF is half the rail: R3/R4 make it and U2 buffers it. The filter's",
            "return flows into that node through C2, and a bare divider is a",
            "50k source impedance - the filter would not be this filter.",
            "C3 and C6 couple in and out, so the header sees no DC. R5 sets the",
            "input's own operating point at VREF and loads the source with 100k.",
        ],
        parts=parts,
        nets=nets,
        power_flags=["+5V", "GND"],
        board_size=(80.0, 45.0),
        tracks=[],
        vias=[],
        pour=(3.0, 3.0, 77.0, 42.0),
        notes_at=(18.0, 20.0),
        flags_at=(150.0, 35.0),
    ).snapped()

    SIG = 0.3
    POWER = 0.6
    # A SOT-23-5 puts three pads on one side at 0.95 mm, and the middle one is
    # the supply. Nothing can reach it except straight out, so the row leaves as
    # a stated fan and the router picks the nets up clear of the package - the
    # same reason the motor driver's TSSOP does, two sizes down.
    escapes: list[Track] = []
    ends: dict[str, dict[str, tuple[float, float]]] = {}
    for ref, (cx, cy, _) in (("U1", (48.0, 18.0, 0)), ("U2", (52.0, 34.0, 0))):
        west, ends[f"{ref}w"] = fan(
            design,
            ref,
            ["1", "2", "3"],
            lead=cx - 3.4,
            column=cx - 6.0,
            pitch=1.9,
            centre=cy,
            width=SIG,
        )
        east, ends[f"{ref}e"] = fan(
            design,
            ref,
            ["5", "4"],
            lead=cx + 3.4,
            column=cx + 6.0,
            pitch=2.8,
            centre=cy,
            width=SIG,
        )
        escapes += west + east
    u1w, u1e, u2w, u2e = (ends["U1w"], ends["U1e"], ends["U2w"], ends["U2e"])

    tracks = [
        *escapes,
        Track("IN", "F.Cu", SIG, ["J1.1", "C3.1"], auto=True),
        Track("IN_DC", "F.Cu", SIG, ["C3.2", "R1.1"], auto=True),
        Track("IN_DC", "F.Cu", SIG, ["C3.2", "R5.1"], auto=True),
        Track("X", "F.Cu", SIG, ["R1.2", "R2.1"], auto=True),
        Track("X", "F.Cu", SIG, ["R2.1", "C1.1"], auto=True),
        Track("FILT_IN", "F.Cu", SIG, ["R2.2", u1w["3"]], auto=True),
        Track("FILT_IN", "F.Cu", SIG, ["C2.1", u1w["3"]], auto=True),
        Track("OUT", "F.Cu", SIG, [u1w["1"], u1e["4"]], auto=True),
        Track("OUT", "F.Cu", SIG, [u1w["1"], "C1.2"], auto=True),
        Track("OUT", "F.Cu", SIG, [u1e["4"], "C6.1"], auto=True),
        Track("OUT_AC", "F.Cu", SIG, ["C6.2", "J3.1"], auto=True),
        Track("VREF", "F.Cu", SIG, [u2w["1"], u2e["4"]], auto=True),
        Track("VREF", "F.Cu", SIG, [u2w["1"], "C2.2"], auto=True),
        Track("VREF", "F.Cu", SIG, [u2w["1"], "R5.2"], auto=True),
        Track("MID", "F.Cu", SIG, ["R3.2", "R4.1"], auto=True),
        Track("MID", "F.Cu", SIG, ["R4.1", "C4.1"], auto=True),
        Track("MID", "F.Cu", SIG, ["C4.1", u2w["3"]], auto=True),
        Track("+5V", "F.Cu", POWER, ["J2.1", "C5.1"], auto=True),
        Track("+5V", "F.Cu", POWER, ["C5.1", u1w["2"]], auto=True),
        Track("+5V", "F.Cu", POWER, ["C5.1", "C7.1"], auto=True),
        Track("+5V", "F.Cu", POWER, ["C7.1", u2w["2"]], auto=True),
        Track("+5V", "F.Cu", SIG, ["C7.1", "R3.1"], auto=True),
    ]
    for pad, target in (
        ("J1.2", (12.0, 24.0)),
        ("J3.2", (68.0, 24.0)),
        ("J2.2", (66.0, 12.0)),
        ("C5.2", (53.0, 9.0)),
        ("C7.2", (61.0, 33.0)),
        ("C4.2", (44.0, 38.0)),
        ("R4.2", (37.0, 38.0)),
        (u1e["5"], (54.0, 13.0)),
        (u2e["5"], (58.0, 29.0)),
    ):
        tracks.append(Track("GND", "F.Cu", 0.5, [pad, target], auto=True, goal_layer="B.Cu"))
    return replace(design, tracks=tracks)


DESIGNS = {
    "buck-5v": buck_5v,
    "motor-driver": motor_driver,
    "pico-carrier": pico_carrier,
    "opamp-filter": opamp_filter,
}


# ---------------------------------------------------------------------------


def write_variant(design: Design, root: Path, *, check: bool = True) -> None:
    design = resolve_routes(design)
    if check:
        problems = check_board(design)
        if problems:
            raise SystemExit(
                f"{design.name}: the layout shorts {len(problems)} pair(s):\n  "
                + "\n  ".join(problems)
            )
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{design.name}.kicad_sch").write_text(emit_schematic(design), encoding="utf-8")
    (root / f"{design.name}.kicad_pro").write_text(emit_project(design), encoding="utf-8")
    emit_board(design, root / f"{design.name}.kicad_pcb")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", help="directory to write the examples into")
    parser.add_argument("--only", help="generate just this design")
    parser.add_argument(
        "--generated-on",
        default=GENERATED_ON,
        help="the date stamped into both variants (default: %(default)s)",
    )
    parser.add_argument(
        "--generated-by",
        default=GENERATED_BY,
        help="what to credit the output to (default: %(default)s)",
    )
    args = parser.parse_args(argv)

    stamp = (
        f"generated {args.generated_on} by {args.generated_by}",
        "from tools/make_examples.py in sabas0ba/kicad_skills",
    )
    out = Path(args.output)
    for name, builder in sorted(DESIGNS.items()):
        if args.only and args.only != name:
            continue
        design = replace(builder().snapped(), provenance=stamp, date=args.generated_on)
        write_variant(design, out / name / "reviewed")
        # the degraded variant is *meant* to be wrong, so it is not checked
        write_variant(degrade(design), out / name / "as-generated", check=False)
        print(f"{name}: wrote as-generated/ and reviewed/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

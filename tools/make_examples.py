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
from collections import defaultdict
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
GENERATED_ON = "2026-08-12"
GENERATED_BY = "Claude Code (claude-fable-5)"

GEOM_EPS = 1e-6
GEOM_TOL = 0.001  # two points this close on the sheet are the same point
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
    # Symbol mirror on the sheet ("x" or "y"), applied after the rotation. A
    # connector drawn on the right of the sheet needs its pins facing left, and
    # rotating it instead would reverse the pin order top to bottom.
    mirror: str = ""
    fields: dict[str, str] = field(default_factory=dict)
    # How far a wire runs off each pin before its label. The default clears a
    # two-pin symbol; a forty-pin one draws its pin numbers just outside the
    # body, and a label parked 2.54 mm out lands on top of them.
    stub: float = STUB
    # Whether a pin this design does not use gets a no-connect flag. Off by
    # default: on a two-pin part an unused pin is a mistake, and on a 48 pin one
    # it is most of them.
    no_connect: bool = False
    # What the part means, printed on the silkscreen next to it: "5V OK" on the
    # power LED, so the board explains its own indicators. Connector pins get
    # their net names automatically; this is for parts whose purpose the
    # reference alone does not state.
    silk_label: str = ""
    # Which unit of a multi-unit symbol this is. A design lists the same
    # reference once per unit, each with its own place on the sheet; the board
    # only ever sees the first of them, because there is one footprint.
    unit: int = 1

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
    # Nets that need a PWR_FLAG to satisfy ERC, each with the pin the power
    # actually comes from - the flag is wired in next to that pin, because a
    # flag parked at the sheet edge answers ERC and tells the reader nothing.
    power_flags: list[tuple[str, str]]  # (net, "REF.PIN")
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
    # Notes anchored next to the circuit block they explain, as (at, lines).
    # One block of prose in a corner reads as none: the reader cannot tell
    # which capacitor a sentence is about unless the sentence sits beside it.
    note_blocks: list[tuple[tuple[float, float], list[str]]] = field(default_factory=list)
    # Power nets drawn as wires instead of symbols. A rail that *is* the
    # circuit - a buck's output, where the feedback comes back from - reads as
    # a horizontal line with taps, not as six separate symbols that leave the
    # reader to reassemble the loop by name.
    wired_power: tuple[str, ...] = ()
    # Nets kept as labels on purpose: the miscellaneous logic whose drawn
    # wires would lattice the sheet. A name at both ends reads better than
    # eight parallel wires crossing the power section.
    label_nets: tuple[str, ...] = ()
    # Footprints whose body interior the router must not cross: tracks under
    # a digital package ride beneath the die with no plane between, so the
    # space between the pad rows is closed to everything but the pads' own
    # entries.
    route_keepout: tuple[str, ...] = ()
    # The grid `snapped` puts footprints on. A board whose placement is set by a
    # module's own 2.54 mm pad pitch cannot also sit on 0.5 mm, and pretending
    # otherwise moves the pads off the pins they have to land on.
    board_grid: float | None = BOARD_GRID
    # Whether the sheet has to be right. The degraded variant is allowed to be
    # wrong in ways that are a build error for the reviewed one - dropping two
    # symbols on the same spot is the whole point of it.
    strict: bool = True
    # Whether aligned pin pairs get a drawn wire instead of a pair of labels.
    # Off in the degraded variant: a sheet that connects only by name is what a
    # generator that never looked at its own plot leaves, and
    # `readability.label_only` exists to say so.
    draw_wires: bool = True
    # A4 fits everything so far; a part drawn in four units does not.
    paper: str = "A4"

    def part(self, ref: str) -> Part:
        return next(p for p in self.parts if p.ref == ref)

    def footprints(self) -> list[Part]:
        """One part per reference: the board has one of each, however many
        units the sheet draws it in."""
        seen: set[str] = set()
        out = []
        for part in self.parts:
            if part.ref not in seen:
                seen.add(part.ref)
                out.append(part)
        return out

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


def symbol_units(lib_id: str) -> int:
    """How many units the symbol is drawn in.

    A big part is drawn as several boxes - the iCE40 as four - and each is
    placed separately with its own pins. Emitting all of them as unit 1 makes a
    schematic KiCad reads as one unit with forty-eight pins, which is not what
    the netlist says, and the parity check disagrees about every pin that was
    supposed to be in a unit that was never placed.
    """
    units = {0}
    for sub in symbol_definition(lib_id).children("symbol"):
        name = str(sub.atom(0, ""))
        parts = name.rsplit("_", 2)
        if len(parts) == 3 and parts[1].isdigit():
            units.add(int(parts[1]))
    return max(units) or 1


def symbol_pins(lib_id: str, unit: int | None = None) -> list[PinDef]:
    out: list[PinDef] = []
    for sub in symbol_definition(lib_id).children("symbol"):
        name = str(sub.atom(0, ""))
        parts = name.rsplit("_", 2)
        this = int(parts[1]) if len(parts) == 3 and parts[1].isdigit() else 0
        # unit 0 is the shared drawing, which carries no pins worth placing twice
        if unit is not None and this not in (unit, 0):
            continue
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
    end = transform_pin(pin.x, pin.y, sx, sy, part.angle, part.mirror)
    away = math.radians(pin.angle + 180)
    out = transform_pin(
        pin.x + math.cos(away) * part.stub,
        pin.y + math.sin(away) * part.stub,
        sx,
        sy,
        part.angle,
        part.mirror,
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


def _property(
    name: str, value: str, x: float, y: float, hide: bool, justify: str = "", angle: float = 0
) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'    (property "{name}" "{escaped}" (at {x} {y} {angle:g}) {_effects(hide, justify)})'


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


# Beyond this a wire crosses the whole sheet and a name is clearer. What
# counts as "the whole sheet" depends on the sheet: an A3 drawing earns its
# size by having further to go.
MAX_WIRE_MM = {"A4": 160.0, "A3": 260.0}
WIRE_CLEAR = 1.27  # how far a drawn wire keeps from copper it does not touch


def _crosses(a0, a1, b0, b1) -> bool:
    """Strict interior crossing - the one contact two nets are allowed to have.

    KiCad joins wires that share an endpoint or meet a junction; two wires that
    simply cross stay separate nets, and every real schematic uses that.
    """

    def orient(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    d1, d2 = orient(b0, b1, a0), orient(b0, b1, a1)
    d3, d4 = orient(a0, a1, b0), orient(a0, a1, b1)
    eps = GEOM_EPS
    return ((d1 > eps and d2 < -eps) or (d1 < -eps and d2 > eps)) and (
        (d3 > eps and d4 < -eps) or (d3 < -eps and d4 > eps)
    )


def _collinear_overlap(a, b, s0, s1) -> float:
    """How far ``a-b`` runs along the same line as ``s0-s1``, zero when apart.

    Only axis-aligned segments count, which is every wire the planner draws. A
    shared endpoint is contact, not overlap - this measures the stretch beyond
    it, the difference between meeting a wire and riding it.
    """
    if abs(a[0] - b[0]) < GEOM_TOL and abs(s0[0] - s1[0]) < GEOM_TOL:
        if abs(a[0] - s0[0]) >= GEOM_TOL:
            return 0.0
        lo = max(min(a[1], b[1]), min(s0[1], s1[1]))
        hi = min(max(a[1], b[1]), max(s0[1], s1[1]))
        return max(0.0, hi - lo)
    if abs(a[1] - b[1]) < GEOM_TOL and abs(s0[1] - s1[1]) < GEOM_TOL:
        if abs(a[1] - s0[1]) >= GEOM_TOL:
            return 0.0
        lo = max(min(a[0], b[0]), min(s0[0], s1[0]))
        hi = min(max(a[0], b[0]), max(s0[0], s1[0]))
        return max(0.0, hi - lo)
    return 0.0


def _sheet_obstacles(
    design: Design,
) -> tuple[
    dict[str, tuple[str, tuple[float, float], tuple[float, float]]],
    list[tuple[float, float, float, float]],
]:
    """What a sheet wire must respect: every stub, and every box it may not enter.

    Returns the stubs by pin owner (net, pin end, stub tip) and the keep-out
    boxes - one per symbol body, one per power-symbol graphic (extended the way
    the symbol points), one per power flag, one around the notes block.
    """
    net_of: dict[tuple[str, str], str] = {}
    for net, nodes in design.nets.items():
        for entry in nodes:
            ref, _, number = entry.partition(".")
            net_of[(ref, number)] = net

    stubs: dict[str, tuple[str, tuple[float, float], tuple[float, float]]] = {}
    boxes: list[tuple[float, float, float, float]] = []
    for part in design.parts:
        ends = []
        seen: set[tuple[float, float]] = set()
        for pin in symbol_pins(part.lib_id, part.unit):
            end, out = pin_geometry(part, pin)
            ends.append(end)
            net = net_of.get((part.ref, pin.number))
            if net is None or end in seen:
                continue
            seen.add(end)
            stubs[f"{part.ref}.{pin.number}"] = (net, end, out)
        if ends:
            xs = [e[0] for e in ends]
            ys = [e[1] for e in ends]
            boxes.append((min(xs) - 1.27, min(ys) - 1.27, max(xs) + 1.27, max(ys) + 1.27))
    # The upright power symbols, their jog wires, and the PWR_FLAGs beside
    # their sources - one shared computation, so the planner avoids exactly
    # what the emitter draws.
    fixture_wires, _symbols, _flags, _junctions, fixture_boxes = power_fixtures(design)
    for tag, net, a, b in fixture_wires:
        stubs[tag] = (net, a, b)
    boxes.extend(fixture_boxes)
    if design.notes:
        widest = max(len(note) for note in design.notes)
        boxes.append(
            (
                design.notes_at[0] - 1.27,
                design.notes_at[1] - 1.27,
                design.notes_at[0] + widest * 1.1,
                design.notes_at[1] + (len(design.notes) + 1) * 5.08,
            )
        )
    for at, block in design.note_blocks:
        widest = max(len(line) for line in block)
        boxes.append((at[0] - 1.27, at[1] - 1.27, at[0] + widest * 1.1, at[1] + len(block) * 4.0))
    # The sheet frame: a wire drawn along the border prints on the border.
    width, height = {"A4": (297.0, 210.0), "A3": (420.0, 297.0)}.get(design.paper, (297.0, 210.0))
    margin = 12.0
    boxes += [
        (-margin, -margin, width + margin, margin),
        (-margin, height - margin, width + margin, height + margin),
        (-margin, -margin, margin, height + margin),
        (width - margin, -margin, width + margin, height + margin),
        # the title block, bottom right
        (width - 190.0, height - 26.0, width + margin, height + margin),
    ]
    return stubs, boxes


def plan_wires(
    design: Design,
) -> tuple[
    list[tuple[str, tuple[float, float], tuple[float, float]]], set[str], list[tuple[float, float]]
]:
    """Draw each signal net as a wire tree, with bends, and say what it costs.

    The stub-and-label sheet is a valid netlist and an unreadable drawing -
    `readability.label_only` measures exactly that. This routes instead: for
    every net that is not a power rail, the pins are joined into a tree of
    straight, L- and Z-shaped runs on the schematic grid, each leg leaving along
    its pin's own stub direction, crossing other nets only at right-angle
    transversals and keeping ``WIRE_CLEAR`` from everything it does not touch.
    A pin no clean run reaches keeps its label - a wire that dodges three parts
    to avoid a fourth reads worse than a name - and one label per net always
    survives, because the label is what names the net for the board.

    Returns the wire segments, the pins whose label the tree replaces, and the
    junction dots (three or more wire ends meeting on one point).
    """
    if not design.draw_wires:
        return [], set(), []

    stubs, boxes = _sheet_obstacles(design)
    max_wire = MAX_WIRE_MM.get(design.paper, 160.0)

    # Each pin also reserves a short runway past its tip: the corridor another
    # wire would have to leave free for this pin to be reachable at all. A
    # wire riding along someone else's runway is what walls a bus row off.
    reach: dict[str, tuple[float, float]] = {}
    for owner, (_net, end, out) in stubs.items():
        length = math.hypot(out[0] - end[0], out[1] - end[1]) or 1.0
        hx = (out[0] - end[0]) / length
        hy = (out[1] - end[1]) / length
        reach[owner] = (out[0] + 4 * GRID * hx, out[1] + 4 * GRID * hy)

    accepted: list[tuple[str, tuple[float, float], tuple[float, float]]] = []

    def valid(polyline: list[tuple[float, float]], net: str, skip: set[str]) -> bool:
        joints = {polyline[0], polyline[-1]}
        for a, b in pairwise(polyline):
            if math.dist(a, b) < GEOM_EPS:
                return False
            for box in boxes:
                if _segment_to_box(a, b, box) < GEOM_EPS:
                    return False
            for owner, (other_net, end, out) in stubs.items():
                if owner in skip:
                    continue
                # The stub and its runway are checked apart: joined, the pin
                # tip becomes an interior point, and a wire through the tip
                # would count as a clean crossing when it is in fact a tap.
                exempt = other_net == net and (end in joints or out in joints)
                # Meeting our own tree at a joint is the point of the exemption;
                # riding along the stub itself is not. A leg collinear with the
                # stub past the shared point draws over it - the overlap that
                # plots as one wire and edits as two - so exempt stubs still
                # refuse overlap. The runway stays free: an escape necessarily
                # rides its own runway outward.
                if exempt and _collinear_overlap(a, b, end, out) > GEOM_TOL:
                    return False
                for s0, s1 in ((end, out), (out, reach[owner])):
                    if _segment_distance(a, b, s0, s1) >= WIRE_CLEAR - GEOM_EPS:
                        continue
                    if exempt:
                        continue  # meeting our own tree at the joint is the point
                    if other_net != net and _crosses(a, b, s0, s1):
                        continue  # a transversal crossing is not a connection
                    return False
            for other_net, s0, s1 in accepted:
                near = _segment_distance(a, b, s0, s1) < WIRE_CLEAR - GEOM_EPS
                if not near:
                    continue
                if other_net == net and (s0 in joints or s1 in joints):
                    continue
                if other_net != net and _crosses(a, b, s0, s1):
                    continue
                return False
        return True

    def candidates(a_out, a_dir, b_out, b_dir) -> list[list[tuple[float, float]]]:
        """Rectilinear runs from a's stub tip to b's, worst case four bends.

        Each pin may first *escape* - continue a grid-multiple past its stub tip
        along its own direction, which is how a wire clears the pins beside it -
        and the escape points are joined by the two L orders and a mid-span Z.
        Two rules keep the result readable: the first leg must not double back
        over a's stub (perpendicular is fine - that is just a corner at the
        tip), and the last leg must not arrive from behind b - a wire reaching
        a pin through its own symbol is what the box check exists to refuse,
        but refusing to propose it is cheaper.
        """
        la = math.hypot(*a_dir) or 1.0
        lb = math.hypot(*b_dir) or 1.0
        a_hat = (a_dir[0] / la, a_dir[1] / la)
        b_hat = (b_dir[0] / lb, b_dir[1] / lb)
        out: list[list[tuple[float, float]]] = []

        def add(points: list[tuple[float, float]]) -> None:
            run = [points[0]]
            for point in points[1:]:
                if math.dist(point, run[-1]) > GEOM_EPS:
                    run.append(point)
            if len(run) < 2:
                return
            first = (run[1][0] - run[0][0], run[1][1] - run[0][1])
            if first[0] * a_hat[0] + first[1] * a_hat[1] < -GEOM_EPS:
                return  # doubles back over a's stub
            last = (run[-1][0] - run[-2][0], run[-1][1] - run[-2][1])
            if last[0] * b_hat[0] + last[1] * b_hat[1] > GEOM_EPS:
                return  # arrives at b from behind, through the symbol
            legs = [(q[0] - p[0], q[1] - p[1]) for p, q in pairwise(run)]
            if any(u[0] * v[0] + u[1] * v[1] < -GEOM_EPS for u, v in pairwise(legs)):
                return  # a leg that turns straight back over the one before it
            out.append(run)

        def elbows(p, q) -> list[list[tuple[float, float]]]:
            if abs(p[0] - q[0]) < GEOM_EPS or abs(p[1] - q[1]) < GEOM_EPS:
                return [[p, q]]
            return [[p, (q[0], p[1]), q], [p, (p[0], q[1]), q]]

        # Escape distances are staggered so that a whole bus leaving one pin
        # column does not fight over a single vertical; the channel offsets go
        # out far enough to clear a large symbol body sideways.
        escapes = tuple(k * GRID for k in range(11))
        offsets = [k * GRID for n in range(1, 9) for k in (-4 * n, 4 * n)]
        for ka in escapes:
            for kb in escapes:
                a_esc = (a_out[0] + ka * a_hat[0], a_out[1] + ka * a_hat[1])
                b_esc = (b_out[0] + kb * b_hat[0], b_out[1] + kb * b_hat[1])
                for middle in elbows(a_esc, b_esc):
                    add([a_out, *middle, b_out])
                # detours: a parallel channel a few grid steps aside - the only
                # way past a part that sits square between two pins that face
                # the same way (a resistor feeding the LED below it)
                for off in offsets:
                    add(
                        [
                            a_out,
                            a_esc,
                            (a_esc[0] + off, a_esc[1]),
                            (a_esc[0] + off, b_esc[1]),
                            b_esc,
                            b_out,
                        ]
                    )
                    add(
                        [
                            a_out,
                            a_esc,
                            (a_esc[0], a_esc[1] + off),
                            (b_esc[0], a_esc[1] + off),
                            b_esc,
                            b_out,
                        ]
                    )
        # Zs through the mid-span, for pin rows that face each other. Several
        # jog columns, tried centre-out: two parallel Zs cannot share one, and
        # a bus is exactly many parallel Zs.
        lanes = [0] + [s * k for k in range(1, 20) for s in (-1, 1)]
        if abs(a_hat[0]) > abs(a_hat[1]):
            mid0 = round(round((a_out[0] + b_out[0]) / 2 / GRID) * GRID, 4)
            for k in lanes:
                mid = round(mid0 + k * GRID, 4)
                add([a_out, (mid, a_out[1]), (mid, b_out[1]), b_out])
        else:
            mid0 = round(round((a_out[1] + b_out[1]) / 2 / GRID) * GRID, 4)
            for k in lanes:
                mid = round(mid0 + k * GRID, 4)
                add([a_out, (a_out[0], mid), (b_out[0], mid), b_out])
        return out

    def span(net: str) -> float:
        outs = [stubs[owner][2] for owner in design.nets[net] if owner in stubs]
        if not outs:
            return 0.0
        xs = [p[0] for p in outs]
        ys = [p[1] for p in outs]
        return (max(xs) - min(xs)) + (max(ys) - min(ys))

    dropped: set[str] = set()
    # Local nets first: a decoupling hop is one short wire wherever it goes,
    # but a cross-sheet run drawn early walls off the corridor a whole bus
    # needed. The long hauls route last and keep their labels when boxed out.
    order = sorted(
        (
            n
            for n in design.nets
            if (n not in POWER_SYMBOLS or n in design.wired_power) and n not in design.label_nets
        ),
        key=lambda n: (span(n), n),
    )
    for net in order:
        owners = [entry for entry in design.nets[net] if entry in stubs]
        if len(owners) < 2:
            continue
        # Merge whichever two fragments join most cheaply, wherever they are -
        # seeded growth dies with its seed, and a rail whose regulator pin is
        # boxed in should still get its capacitors chained to each other.
        comp = {owner: owner for owner in owners}

        def find(owner: str, comp: dict[str, str] = comp) -> str:
            while comp[owner] != owner:
                comp[owner] = comp[comp[owner]]
                owner = comp[owner]
            return owner

        # Each fragment carries exactly one label; a merge drops the loser's.
        carrier = {owner: owner for owner in owners}
        # Wires only ever accumulate, so a pair with no clean run this round
        # will not have one next round either.
        hopeless: set[tuple[str, str]] = set()
        ranked: dict[tuple[str, str], list[tuple[float, list]]] = {}
        while True:
            best = None
            for i, a in enumerate(owners):
                _, a_end, a_out = stubs[a]
                a_dir = (a_out[0] - a_end[0], a_out[1] - a_end[1])
                for b in owners[i + 1 :]:
                    if (a, b) in hopeless or find(a) == find(b):
                        continue
                    if (a, b) not in ranked:
                        _, b_end, b_out = stubs[b]
                        b_dir = (b_out[0] - b_end[0], b_out[1] - b_end[1])
                        runs = candidates(a_out, a_dir, b_out, b_dir)
                        runs += [list(reversed(c)) for c in candidates(b_out, b_dir, a_out, a_dir)]
                        manhattan = abs(b_out[0] - a_out[0]) + abs(b_out[1] - a_out[1])
                        costed = []
                        for run in runs:
                            length = sum(math.dist(p, q) for p, q in pairwise(run))
                            if length > max_wire or length > 2.0 * manhattan + 20.0:
                                continue
                            costed.append((length + 6.0 * (len(run) - 2), run))
                        costed.sort(key=lambda item: item[0])
                        ranked[(a, b)] = costed
                    found = False
                    for cost, run in ranked[(a, b)]:
                        if best is not None and cost >= best[0]:
                            found = True  # cheaper ones may still win next round
                            break
                        if valid(run, net, {a, b}):
                            best = (cost, a, b, run)
                            found = True
                            break
                    if not found:
                        hopeless.add((a, b))
            if best is None:
                break  # remaining fragments keep their labels
            _, a, b, run = best
            for p, q in pairwise(run):
                accepted.append((net, p, q))
            ra, rb = find(a), find(b)
            # The label that survives the merge goes to the connector when the
            # net reaches one: a name belongs on the generic part - the header
            # pin a harness plugs into - not in the middle of the circuit.
            keep, lose = carrier[ra], carrier[rb]
            if lose.startswith("J") and not keep.startswith("J"):
                keep, lose = lose, keep
            dropped.add(lose)
            comp[rb] = ra
            carrier[ra] = keep
    # Two runs of one net may lawfully share a stretch of line - a second
    # branch leaving the same pin rides the first before it turns. Drawn as
    # two overlapping wires that confuses KiCad 9's connectivity outright, so
    # collinear same-net segments are unioned into maximal clean runs first.
    # The escape arithmetic also leaves float dust on some coordinates, and a
    # wire 2e-14 off its pin is a wire KiCad has to be lucky to connect.
    merged: list[tuple[str, tuple[float, float], tuple[float, float]]] = []
    spans: dict[tuple[str, str, float], list[tuple[float, float]]] = defaultdict(list)
    for net, p, q in accepted:
        p = (round(p[0], 4), round(p[1], 4))
        q = (round(q[0], 4), round(q[1], 4))
        if abs(p[0] - q[0]) < GEOM_TOL:
            spans[(net, "v", p[0])].append(tuple(sorted((p[1], q[1]))))
        elif abs(p[1] - q[1]) < GEOM_TOL:
            spans[(net, "h", p[1])].append(tuple(sorted((p[0], q[0]))))
        elif math.dist(p, q) > GEOM_TOL:
            merged.append((net, p, q))
    for (net, axis, c), intervals in spans.items():
        intervals.sort()
        lo, hi = intervals[0]
        for nlo, nhi in intervals[1:]:
            if nlo <= hi + GEOM_TOL:
                hi = max(hi, nhi)
            else:
                merged.append((net, (c, lo), (c, hi)) if axis == "v" else (net, (lo, c), (hi, c)))
                lo, hi = nlo, nhi
        merged.append((net, (c, lo), (c, hi)) if axis == "v" else (net, (lo, c), (hi, c)))
    accepted = merged

    # A wire that ends on another wire's middle - or a pin tip the union just
    # swallowed - is a tee. KiCad's editor splits a wire wherever a branch
    # tees into it, and KiCad 9's connectivity *requires* files drawn that
    # way: a wire that runs through a junction connects on one side of it
    # only. So find every tee point, then split our wires the way the editor
    # would have.
    ends = {pt for _net, s0, s1 in accepted for pt in (s0, s1)}
    ends.update(out for _net, _end, out in stubs.values())
    tee_points: set[tuple[float, float]] = set()
    for _net, s0, s1 in accepted:
        for point in ends:
            if point not in (s0, s1) and _segment_to_point(s0, s1, point) < GEOM_TOL:
                tee_points.add(point)
    cut: list[tuple[str, tuple[float, float], tuple[float, float]]] = []
    for net, p, q in accepted:
        interior = [
            pt for pt in tee_points if pt not in (p, q) and _segment_to_point(p, q, pt) < GEOM_TOL
        ]
        along = sorted(
            [p, *interior, q],
            key=lambda t: (t[0] - p[0]) ** 2 + (t[1] - p[1]) ** 2,
        )
        for a, b in pairwise(along):
            if math.dist(a, b) > GEOM_TOL:
                cut.append((net, a, b))
    accepted = cut

    # One label per wire fragment survives: the label is what names the net.
    # With the wires split at every tee, a junction dot is simply any point
    # where three or more ends - wire or pin stub - now meet.
    junction_count: dict[tuple[float, float], int] = defaultdict(int)
    for _net, p, q in accepted:
        junction_count[p] += 1
        junction_count[q] += 1
    for _owner, (_net, _end, out) in stubs.items():
        junction_count[out] += 1
    junctions = sorted(point for point, count in junction_count.items() if count >= 3)
    return accepted, dropped, junctions


def schematic_shorts(design: Design) -> list[str]:
    """Every place two nets touch on the sheet.

    The stub-collision check inside :func:`emit_schematic` compares endpoints,
    which catches two pins landing on the same point and nothing else. A wire
    that *ends on another wire* joins the two nets just as surely, and on a
    sheet where every pin drags an eight millimetre stub behind it that is the
    common case rather than the exotic one. It is invisible afterwards: the
    netlist is self-consistent, the board is built from it, and the only sign is
    KiCad's schematic-parity check disagreeing about a net name.

    Crossings are not connections - KiCad joins wires that meet, not wires that
    cross - so only shared endpoints and T-junctions count here.
    """
    net_of: dict[tuple[str, str], str] = {}
    for net, nodes in design.nets.items():
        for entry in nodes:
            ref, _, number = entry.partition(".")
            net_of[(ref, number)] = net

    wires: list[tuple[str, str, tuple[float, float], tuple[float, float]]] = []
    for part in design.parts:
        seen: set[tuple[float, float]] = set()
        for pin in symbol_pins(part.lib_id, part.unit):
            net = net_of.get((part.ref, pin.number))
            end, out = pin_geometry(part, pin)
            if net is None or end in seen:
                continue
            seen.add(end)
            wires.append((net, f"{part.ref}.{pin.number}", end, out))
    for tag, net, a, b in power_fixtures(design)[0]:
        wires.append((net, tag, a, b))
    for index, (net, p0, p1) in enumerate(plan_wires(design)[0]):
        wires.append((net, f"run {index} of {net}", p0, p1))

    def touches(a0, a1, b0, b1) -> bool:
        return any(_segment_to_point(b0, b1, point) < GEOM_TOL for point in (a0, a1)) or any(
            _segment_to_point(a0, a1, point) < GEOM_TOL for point in (b0, b1)
        )

    problems = []
    for index, (net, owner, a0, a1) in enumerate(wires):
        for other, other_owner, b0, b1 in wires[index + 1 :]:
            if net == other or not touches(a0, a1, b0, b1):
                continue
            problems.append(f"{owner} ({net}) touches {other_owner} ({other})")
    return sorted(set(problems))


def _segment_to_point(a, b, point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return math.dist(point, a)
    u = max(0.0, min(1.0, ((point[0] - a[0]) * dx + (point[1] - a[1]) * dy) / length2))
    return math.dist(point, (a[0] + u * dx, a[1] + u * dy))


def emit_schematic(design: Design) -> str:
    root_uuid = stable_uuid(design.name, "sheet")
    lines = [
        f'(kicad_sch (version {SCH_VERSION}) (generator "eda-toolkit") (generator_version "9.0")',
        f'  (uuid "{root_uuid}")',
        f'  (paper "{design.paper}")',
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
    segments, replaced, junctions = plan_wires(design)
    for index, (net, p0, p1) in enumerate(segments):
        body.append(_wire(design, "run", f"{net}-{index}", p0, p1))
    for point in junctions:
        # KiCad joins wires that share an endpoint whether or not the dot is
        # drawn, but a reader does not: three ends meeting without a dot read
        # as a crossing, and a crossing reads as no connection.
        body.append(
            f"  (junction (at {point[0]} {point[1]}) (diameter 0) (color 0 0 0 0) "
            f'(uuid "{stable_uuid(design.name, "junction", point[0], point[1])}"))'
        )
    # Two stubs that happen to end on the same coordinate silently become one
    # net, and the design is quietly not the design any more. Catch it here
    # rather than in ERC, where it surfaces as a puzzle about net names.
    claimed: dict[tuple[float, float], tuple[str, str]] = {}

    # What a ratings block must not print over: every other symbol's pin box,
    # everything the power fixtures put on the sheet, and - accumulated as the
    # parts are drawn - every block already placed. Wires weigh lighter: a
    # block may cross one if that is the only clean side, and the side pick
    # prefers the side that crosses fewer.
    part_box: dict[str, tuple[float, float, float, float]] = {}
    for part in design.parts:
        pts = [pin_geometry(part, pin)[0] for pin in symbol_pins(part.lib_id, part.unit)]
        pts.append(part.sheet)
        part_box[part.ref] = (
            min(p[0] for p in pts) - 1.27,
            min(p[1] for p in pts) - 1.27,
            max(p[0] for p in pts) + 1.27,
            max(p[1] for p in pts) + 1.27,
        )
    fixtures = power_fixtures(design)
    fixture_boxes = list(fixtures[4])
    wire_boxes = [
        (min(a[0], b[0]) - 0.4, min(a[1], b[1]) - 0.4, max(a[0], b[0]) + 0.4, max(a[1], b[1]) + 0.4)
        for a, b in ([(a, b) for _n, a, b in segments] + [(a, b) for _t, _n, a, b in fixtures[0]])
    ]
    placed_blocks: list[tuple[float, float, float, float]] = fixture_boxes

    for part in design.parts:
        pins = symbol_pins(part.lib_id, part.unit)
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
                declared_nc = pin.etype == "no_connect"
                if (part.no_connect or declared_nc) and end not in drawn:
                    drawn.add(end)
                    body.append(
                        f"  (no_connect (at {end[0]} {end[1]}) "
                        f'(uuid "{stable_uuid(design.name, "nc", part.ref, pin.number)}"))'
                    )
                continue
            if end in drawn:
                if claimed[end][0] != net and design.strict:
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
                if owner[0] != net and design.strict:
                    raise SystemExit(
                        f"{design.name}: {part.ref}.{pin.number} ({net}) and {owner[1]} "
                        f"({owner[0]}) both touch {point} - move one of them"
                    )
            body.append(_wire(design, part.ref, pin.number, end, out))
            pin_owner = f"{part.ref}.{pin.number}"
            if (
                net not in POWER_SYMBOLS or net in design.wired_power
            ) and pin_owner not in replaced:
                body.append(_label(design, net, part.ref, pin.number, out, end))
        avoid = [box for ref, box in part_box.items() if ref != part.ref] + placed_blocks
        body.append(_symbol_instance(design, part, pins, avoid, wire_boxes))
        if len(avoid) > len(part_box) - 1 + len(placed_blocks):
            placed_blocks.append(avoid[-1])

    # The power hookups: jog and bus wires, upright symbols, flags - all from
    # the one computation the planner and the shorts check also read.
    fixture_wires, fixture_symbols, fixture_flags, fixture_junctions, _boxes = fixtures
    for tag, _fnet, a, b in fixture_wires:
        body.append(_wire(design, "pwr", tag, a, b))
    for power_index, (net, at) in enumerate(fixture_symbols, start=1):
        body.append(_power_symbol(design, POWER_SYMBOLS[net], net, at, power_index))
    for flag_index, (net, at) in enumerate(fixture_flags, start=1):
        body.append(_power_flag(design, net, flag_index, at))
    drawn_junctions = set(junctions)
    tree_ends = {p for _net, s0, s1 in segments for p in (s0, s1)}
    for point in fixture_junctions:
        if point not in drawn_junctions:
            drawn_junctions.add(point)
            body.append(
                f"  (junction (at {point[0]} {point[1]}) (diameter 0) (color 0 0 0 0) "
                f'(uuid "{stable_uuid(design.name, "junction", point[0], point[1])}"))'
            )
    # A flag wire is a third end at its attach point when the net's tree also
    # lands there - which needs its dot, and the planner cannot know.
    for tag, _fnet, a, _b in fixture_wires:
        if tag.endswith("#flag") and a in tree_ends and a not in drawn_junctions:
            drawn_junctions.add(a)
            body.append(
                f"  (junction (at {a[0]} {a[1]}) (diameter 0) (color 0 0 0 0) "
                f'(uuid "{stable_uuid(design.name, "junction", a[0], a[1])}"))'
            )

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
    # Anchored notes: each block sits beside the circuit it explains, so the
    # reader never has to carry a sentence across the sheet to its subject.
    for bindex, (at, block) in enumerate(design.note_blocks, start=1):
        for lindex, line in enumerate(block):
            y = at[1] + lindex * 4.0
            escaped = line.replace('"', '\\"')
            body.append(
                f'  (text "{escaped}" (at {at[0]} {round(y, 2)} 0) '
                f"{_effects(justify='left top')} "
                f'(uuid "{stable_uuid(design.name, "noteblock", bindex, lindex)}"))'
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


def _label(design: Design, net: str, ref: str, number: str, at, end=None) -> str:
    # The name reads away from the pin, whichever way the pin points: a label
    # printed back over its own stub merges with the pin number beside it.
    justify = "left bottom"
    angle = 0
    if end is not None:
        if at[0] < end[0] - GEOM_EPS:
            justify = "right bottom"
        elif abs(at[0] - end[0]) < GEOM_EPS:
            angle = 90
            justify = "left bottom" if at[1] < end[1] else "right bottom"
    return (
        f'  (label "{net}" (at {at[0]} {at[1]} {angle}) '
        f"{_effects(justify=justify)} "
        f'(uuid "{stable_uuid(design.name, "label", ref, number)}"))'
    )


def _power_geometry(
    net: str, end: tuple[float, float], out: tuple[float, float]
) -> tuple[list[tuple[tuple[float, float], tuple[float, float]]], tuple[float, float]]:
    """Where an upright power symbol and the wires reaching it go for one pin.

    Convention before convenience: a rail symbol points up, a ground symbol
    hangs down, wherever the pin happens to exit. A pin already heading the
    right way gets the symbol on its stub tip; a sideways pin gets a short
    vertical jog; a pin heading the wrong way sidesteps around its own body
    first. Returns the extra wires and the symbol origin.
    """
    want = 1.0 if net == "GND" else -1.0  # sheet y grows downward
    dx, dy = out[0] - end[0], out[1] - end[1]
    length = math.hypot(dx, dy) or 1.0
    ux, uy = dx / length, dy / length
    if abs(ux) < GEOM_EPS and uy * want > 0:
        return [], out
    if abs(uy) < GEOM_EPS:
        origin = (out[0], round(out[1] + want * 2.54, 4))
        return [(out, origin)], origin
    side = (round(out[0] + 3.81, 4), out[1])
    origin = (side[0], round(out[1] + want * 5.08, 4))
    return [(out, side), (side, origin)], origin


def _symbol_box(net: str, at: tuple[float, float]) -> tuple[float, float, float, float]:
    """The sheet area an upright power symbol and its value text occupy."""
    x, y = at
    if net == "GND":
        return (x - 2.54, y, x + 2.54, y + 6.35)
    return (x - 2.54, y - 7.62, x + 2.54, y)


def power_fixtures(design: Design):
    """Everything the power hookups add to the sheet beyond the pin stubs.

    One computation shared by the emitter, the shorts checker and the wire
    planner's obstacle map, so the three never disagree about where a jog wire
    or a PWR_FLAG actually is.

    A pin already pointing the right way gets its symbol on the stub tip. A
    sideways pin on a multi-row part - a connector's mid-row ground, most
    often - cannot simply jog: the symbol would land on the rows beneath it.
    Those taps run out past the pins' approach corridors to a shared vertical
    bus, one per net, which carries them beyond the part's extent and ends in
    a single upright symbol in clear space. That is also how a human draws a
    header with four grounds: one rail, four taps, one symbol.

    Returns (wires, symbols, flags, junctions, boxes): wires as
    (owner, net, a, b); symbols and flags as (net, at); junction dots where
    a tap tees into its bus.
    """
    net_of: dict[tuple[str, str], str] = {}
    for net, nodes in design.nets.items():
        for entry in nodes:
            ref, _, number = entry.partition(".")
            net_of[(ref, number)] = net
    flag_owner = {owner: net for net, owner in design.power_flags}

    wires: list[tuple[str, str, tuple, tuple]] = []
    symbols: list[tuple[str, tuple[float, float]]] = []
    flags: list[tuple[str, tuple[float, float]]] = []
    junctions: list[tuple[float, float]] = []
    boxes: list[tuple[float, float, float, float]] = []

    def place_flag(owner: str, net: str, at: tuple[float, float]) -> None:
        flag = (round(at[0] + 5.08, 4), at[1])
        wires.append((f"{owner}#flag", net, at, flag))
        flags.append((net, flag))
        # the box reaches one text row above the graphic: the value label sits
        # up there, clear of the rail symbol's own label one row below it
        boxes.append((flag[0] - 2.54, flag[1] - 8.89, flag[0] + 2.54, flag[1]))

    for part in design.parts:
        seen: set[tuple[float, float]] = set()
        pin_ends: list[tuple[float, float]] = []
        vertical: list[tuple[str, str, tuple, tuple]] = []
        sideways: dict[tuple[str, float], list[tuple[str, tuple, tuple]]] = defaultdict(list)
        for pin in symbol_pins(part.lib_id, part.unit):
            net = net_of.get((part.ref, pin.number))
            end, out = pin_geometry(part, pin)
            pin_ends.append(end)
            if net is None or end in seen:
                continue
            seen.add(end)
            owner = f"{part.ref}.{pin.number}"
            if net in POWER_SYMBOLS and net not in design.wired_power:
                if abs(out[1] - end[1]) > GEOM_EPS:
                    vertical.append((owner, net, end, out))
                else:
                    side = 1.0 if out[0] > end[0] else -1.0
                    sideways[(net, side)].append((owner, end, out))
            elif flag_owner.get(owner) == net:
                # a labelled net: the flag rides a short wire off the stub tip
                at = (out[0], round(out[1] - 2.54, 4))
                wires.append((f"{owner}#flag", net, out, at))
                flags.append((net, at))
                boxes.append((at[0] - 2.54, at[1] - 8.89, at[0] + 2.54, at[1]))

        for owner, net, end, out in vertical:
            jogs, origin = _power_geometry(net, end, out)
            for j, (a, b) in enumerate(jogs):
                wires.append((f"{owner}#jog{j}", net, a, b))
            symbols.append((net, origin))
            boxes.append(_symbol_box(net, origin))
            if flag_owner.get(owner) == net:
                place_flag(owner, net, origin)

        # One bus per (net, side): grounds take the nearest column, rails the
        # next, so two nets never share a vertical.
        top = min((e[1] for e in pin_ends), default=0.0)
        bottom = max((e[1] for e in pin_ends), default=0.0)
        lane_of_side: dict[float, int] = defaultdict(int)
        for (net, side), taps in sorted(sideways.items()):
            want = 1.0 if net == "GND" else -1.0
            lane = lane_of_side[side]
            lane_of_side[side] += 1
            tip_x = taps[0][2][0]
            bus_x = round(tip_x + side * (7.62 + 2.54 * lane), 4)
            rows = sorted({out[1] for _o, _e, out in taps})
            reach = bottom + 5.08 if want > 0 else top - 5.08
            origin = (bus_x, round(reach + want * 2.54 * lane, 4))
            for owner, _end, out in taps:
                wires.append((f"{owner}#tap", net, out, (bus_x, out[1])))
            # the bus, split at every tap the way the editor would draw it
            stops = sorted({*rows, origin[1]})
            for index, (a, b) in enumerate(pairwise(stops)):
                wires.append(
                    (f"{part.ref}#{net}{side:+.0f}bus{index}", net, (bus_x, a), (bus_x, b))
                )
            for row in stops[1:-1]:
                junctions.append((bus_x, row))
            symbols.append((net, origin))
            boxes.append(_symbol_box(net, origin))
            flagged = next((owner for owner, _e, _o in taps if flag_owner.get(owner) == net), None)
            if flagged is not None:
                place_flag(flagged, net, origin)
    return wires, symbols, flags, junctions, boxes


def _power_symbol(design: Design, lib_id: str, net: str, at, index: int) -> str:
    """A ground or rail symbol, upright.

    Rails point up, grounds hang down - the one orientation every reader
    assumes. The wires bend to make that true (see `_power_geometry`); the
    symbol itself never turns.
    """
    ref = f"#PWR{index:02d}"
    uid = stable_uuid(design.name, "power", index)
    root = stable_uuid(design.name, "sheet")
    label_y = round(at[1] + (3.81 if net == "GND" else -3.81), 4)
    return "\n".join(
        [
            f'  (symbol (lib_id "{lib_id}") (at {at[0]} {at[1]} 0) (unit 1)',
            "    (exclude_from_sim no) (in_bom no) (on_board yes) (dnp no)",
            f'    (uuid "{uid}")',
            _property("Reference", ref, at[0], at[1], True),
            _property("Value", net, at[0], label_y, False),
            _property("Footprint", "", at[0], at[1], True),
            _property("Datasheet", "", at[0], at[1], True),
            f'    (pin "1" (uuid "{uid}-p"))',
            f'    (instances (project "{design.name}" '
            f'(path "/{root}" (reference "{ref}") (unit 1))))',
            "  )",
        ]
    )


def _power_flag(design: Design, net: str, index: int, at: tuple[float, float]) -> str:
    """A PWR_FLAG, wired in next to the source of the rail it declares.

    Without one, ERC reports every power_in pin on an externally supplied rail
    as undriven. It used to live in a row of labelled stubs at the sheet edge;
    a flag belongs where the power actually comes onto the board, which is why
    the caller hands in the point (see `power_fixtures`).
    """
    x, y = at
    ref = f"#FLG{index:02d}"
    uid = stable_uuid(design.name, "flag", index)
    root = stable_uuid(design.name, "sheet")
    # One text row above the rail symbols' own labels: a flag stands beside a
    # rail symbol by construction, and on the same row the two names collide.
    lines = [
        f'  (symbol (lib_id "power:PWR_FLAG") (at {x} {y} 0) (unit 1)',
        "    (exclude_from_sim no) (in_bom no) (on_board yes) (dnp no)",
        f'    (uuid "{uid}")',
        _property("Reference", ref, x, y, True),
        _property("Value", "PWR_FLAG", round(x + 1.27, 4), round(y - 6.35, 4), False, "right"),
        _property("Footprint", "", x, y, True),
        _property("Datasheet", "", x, y, True),
        f'    (pin "1" (uuid "{uid}-p"))',
        f'    (instances (project "{design.name}" (path "/{root}" (reference "{ref}") (unit 1))))',
        "  )",
    ]
    return "\n".join(lines)


def _symbol_instance(
    design: Design,
    part: Part,
    pins: list[PinDef],
    avoid: list[tuple[float, float, float, float]] | None = None,
    wires: list[tuple[float, float, float, float]] | None = None,
) -> str:
    """The symbol, with its two visible fields clear of everything else.

    A fixed 6.35 mm above and below the origin is right for a two-pin part and
    lands in the middle of the pin labels of a forty-pin one. Measuring the pins
    instead puts the reference above the symbol and the value below it whatever
    size it is - which is where a reader looks for them anyway.

    ``avoid`` lists the boxes the ratings block must not print over - the other
    symbols and the power fixtures. Text on a rotated symbol is counter-rotated
    back to horizontal, the way the editor keeps fields readable when a part
    turns.
    """
    x, y = part.sheet
    points = [pin_geometry(part, pin)[0] for pin in pins] or [(x, y)]
    ends = [p[1] for p in points]
    top, bottom = min(*ends, y), max(*ends, y)
    span_x = max(p[0] for p in points) - min(p[0] for p in points)
    span_y = max(ends) - min(ends)
    uid = stable_uuid(design.name, "symbol", part.ref, part.unit)
    root = stable_uuid(design.name, "sheet")
    mirror = f" (mirror {part.mirror})" if part.mirror else ""
    text_angle = part.angle % 180  # 90/270 need the counter-turn, 0/180 do not
    # A passive's ratings belong on the page, not only in the machine-checked
    # fields: an engineer reads "100n 50V 10%" at the part, not in a table.
    # An upright small part gets value and ratings together beside the body -
    # under a capacitor sits its ground symbol, and under a labelled resistor
    # sits its label - a lying one gets the ratings under its value. Small
    # parts only: a 48-pin symbol's block would land on pins.
    visible = ("Voltage", "Tolerance", "Power", "Current")
    ratings = [n for n in visible if n in part.fields]
    upright = span_y >= span_x
    small = len(pins) <= 4 and part.unit == 1
    side_value = upright and small and bool(ratings)
    # The block goes beside the body, on whichever side prints over nothing.
    # The right side is the habit; a neighbour there sends it left.
    side, justify = 1.0, "left"
    block: tuple[float, float, float, float] | None = None
    rows_n = len(ratings) + 1 if side_value else len(ratings)
    if upright and rows_n and avoid is not None:
        # 1.45 mm per glyph is the default font's real advance; undersizing it
        # here let two blocks land one millimetre apart and read as one word
        texts = [v for n, v in part.fields.items() if n in ratings]
        if side_value:
            texts.append(part.value)
        width = max(len(t) for t in texts) * 1.45 + 0.5
        y0 = y - (rows_n - 1) * 1.27 - 1.27
        y1 = y - (rows_n - 1) * 1.27 + (rows_n - 1) * 2.54 + 1.27

        def hits(x0, x1, boxes):
            return sum(1 for b in boxes if x0 < b[2] and b[0] < x1 and y0 < b[3] and b[1] < y1)

        options = [
            ((x + 3.81, x + 3.81 + width), 1.0, "left"),
            ((x - 3.81 - width, x - 3.81), -1.0, "right"),
        ]
        clear = [o for o in options if not hits(*o[0], avoid)]
        if clear:
            # both sides clear of symbols: take the one crossing fewer wires
            (x0, x1), side, justify = min(clear, key=lambda o: hits(*o[0], wires or []))
        else:
            (x0, x1), side, justify = options[0]
        block = (x0, y0, x1, y1)

    def row_at(index: int) -> tuple[float, float]:
        return (round(x + side * 3.81, 4), round(y - (rows_n - 1) * 1.27 + index * 2.54, 4))

    if side_value:
        value_prop = _property("Value", part.value, *row_at(0), False, justify, text_angle)
    else:
        value_prop = _property(
            "Value", part.value, x, round(bottom + 2.54, 4), False, angle=text_angle
        )
    lines = [
        f'  (symbol (lib_id "{part.lib_id}") (at {x} {y} {part.angle}){mirror} (unit {part.unit})',
        "    (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)",
        f'    (uuid "{uid}")',
        _property("Reference", part.ref, x, round(top - 2.54, 4), False, angle=text_angle),
        value_prop,
        _property("Footprint", part.footprint, x, y, True),
        _property("Datasheet", part.fields.get("Datasheet", "~"), x, y, True),
    ]
    shown = 1 if side_value else 0
    for name, value in part.fields.items():
        if name == "Datasheet":
            continue
        if name in ratings and small:
            if upright:
                lines.append(_property(name, value, *row_at(shown), False, justify, text_angle))
            else:
                row = (x, round(bottom + 5.08 + shown * 2.54, 4))
                lines.append(_property(name, value, row[0], row[1], False, angle=text_angle))
                width = max(len(v) for n, v in part.fields.items() if n in ratings) * 1.45 + 0.5
                block = (x - width / 2, bottom + 3.81, x + width / 2, bottom + 5.08 + shown * 2.54)
            shown += 1
        else:
            lines.append(_property(name, value, x, y, True))
    if block is not None and avoid is not None:
        # later parts must not print their block over this one
        avoid.append(block)
    for pin in pins:
        lines.append(f'    (pin "{pin.number}" (uuid "{uid}-p{pin.number}"))')
    lines.append(
        f'    (instances (project "{design.name}" '
        f'(path "/{root}" (reference "{part.ref}") (unit {part.unit}))))'
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
    for part in design.footprints():
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
    """Whether two axis-aligned rectangles come within ``clearance``.

    The distance between them, not the overlap of the two grown by it: growing
    both and asking whether they intersect measures along the axes, and two pads
    that meet at a corner - which is every pair on the corner of a QFN - are
    further apart than that makes them look.
    """
    dx = max(0.0, one[0] - other[2], other[0] - one[2])
    dy = max(0.0, one[1] - other[3], other[1] - one[3])
    return math.hypot(dx, dy) < clearance


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
    slope: float | None = None,
    clearance: float = 0.25,
) -> tuple[list[Track], dict[str, tuple[float, float]]]:
    """Take one row of a fine-pitch package out to a pitch a router can use.

    At 0.65 mm there is nothing for a search to find: two 0.3 mm tracks and the
    clearance between them already fill the gap, and a grid coarse enough to
    finish in this decade cannot see it. So the escape is stated rather than
    searched for - every pin leaves straight and bends at 45 degrees, the
    bends staggered so no two neighbours turn abreast. Anything shallower or
    steeper than 45 reads as an accident on the plot, and the stagger is what
    buys the spacing a shared turn could only get from a shallow angle.
    ``slope`` (dx/dy of a single shared shallow turn) survives as the stated
    exception: a QFN whose four sides each spend the whole corridor escaping
    has no along-axis room to stagger, and the shallow turn is the only shape
    that fits. A caller that passes it owns the `route.odd_angle` waiver.

    The row pitch still sets the width: two straights in a 0.65 mm row hold
    nothing wider than 0.3-0.4 mm, so a power pin leaves as wide as the row
    allows and widens the moment its diagonal clears the field - which is what
    the assertion below is checking rather than trusting.

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
    placed: list[tuple[str, float, float]] = []  # (pin, pad offset, lane target)
    for index, number in enumerate(pins):
        offset = pad_position(design, f"{ref}.{number}")[across]
        target = round(centre + (index - (len(pins) - 1) / 2) * pitch, 4)
        placed.append((number, offset, target))

    if slope is not None:
        # the stated shallow-turn exception, all pins turning together
        span = min(abs(a[1] - b[1]) for a, b in pairwise(placed)) * math.cos(math.atan2(1.0, slope))
        for a, b in pairwise(placed):
            need = (widths.get(a[0], width) + widths.get(b[0], width)) / 2 + clearance
            if span < need - GEOM_EPS:
                raise SystemExit(
                    f"{design.name}: {ref} pins {a[0]} and {b[0]} leave {span:.3f} mm "
                    f"across the shared turn at slope {slope} and need {need:.3f}"
                )
        tracks: list[Track] = []
        ends: dict[str, tuple[float, float]] = {}
        for number, offset, target in placed:
            pad = f"{ref}.{number}"
            bend = round(lead + direction * abs(target - offset) * slope, 4)
            points: list[tuple[float, float] | str] = [pad, at(lead, offset)]
            if abs(target - offset) > GEOM_EPS:
                points.append(at(bend, target))
            if abs(column - bend) > GEOM_EPS:
                points.append(at(column, target))
            net = next((name for name, nodes in design.nets.items() if pad in nodes), None)
            if net is None:
                continue
            tracks.append(Track(net, "F.Cu", widths.get(number, width), points))
            ends[number] = at(column, target)
        return tracks, ends

    # Every bend is 45 degrees, staggered so no two neighbours turn abreast:
    # within each shift direction the pin farthest along that direction turns
    # first and clears the row for the one behind it. Two parallel diagonals
    # one stagger apart keep `(row + stagger) * cos45` of perpendicular space,
    # which is what lets the escape hold 45s where a shared shallow turn was
    # the only other way through a 0.65 mm row.
    plus = sorted((p for p in placed if p[2] - p[1] > GEOM_EPS), key=lambda p: -p[1])
    minus = sorted((p for p in placed if p[2] - p[1] < -GEOM_EPS), key=lambda p: p[1])
    rank = {p[0]: r for group in (plus, minus) for r, p in enumerate(group)}
    biggest = max((abs(p[2] - p[1]) for p in placed), default=0.0)
    deepest = max(rank.values(), default=0)
    room = abs(column - lead)
    stagger = 0.75
    if stagger * deepest + biggest > room:
        stagger = (room - biggest) / deepest if deepest else 0.0
        if stagger < 0.2:
            raise SystemExit(
                f"{design.name}: {ref} fan from {lead} to {column} is {room:.2f} mm deep, "
                f"not enough for a staggered 45-degree escape needing {biggest:.2f} mm of "
                "diagonal plus the stagger between neighbours"
            )
    row = min(abs(a[1] - b[1]) for a, b in pairwise(placed))
    span = (row + stagger) * math.cos(math.pi / 4)
    for a, b in pairwise(placed):
        need = (widths.get(a[0], width) + widths.get(b[0], width)) / 2 + clearance
        if span < need - GEOM_EPS:
            raise SystemExit(
                f"{design.name}: {ref} pins {a[0]} and {b[0]} are {row:.3f} mm apart, which "
                f"with a {stagger:.2f} mm stagger leaves {span:.3f} mm across the diagonals "
                f"and they need {need:.3f}"
            )

    tracks: list[Track] = []
    ends: dict[str, tuple[float, float]] = {}
    for number, offset, target in placed:
        pad = f"{ref}.{number}"
        delta = target - offset
        turn = round(lead + direction * stagger * rank.get(number, 0), 4)
        done = round(turn + direction * abs(delta), 4)
        points: list[tuple[float, float] | str] = [pad, at(lead, offset)]
        if abs(delta) > GEOM_EPS:
            if abs(turn - lead) > GEOM_EPS:
                points.append(at(turn, offset))
            points.append(at(done, target))
        if abs(column - done) > GEOM_EPS or abs(delta) <= GEOM_EPS:
            points.append(at(column, target))
        net = next((name for name, nodes in design.nets.items() if pad in nodes), None)
        if net is None:
            # An unused pin still takes its place in the row: the spacing that
            # makes the fan legal is the spacing of the whole row, and closing
            # the gap would put its neighbours where it would have been.
            continue
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
    for part in design.footprints():
        node = footprint_definition(part.footprint)
        for pad in node.children("pad"):
            number = str(pad.atom(0, ""))
            net = next(
                (name for name, nodes in design.nets.items() if f"{part.ref}.{number}" in nodes),
                "",
            )
            x0, y0, x1, y1 = pad_box(design, part, pad)
            router.add(autoroute.Obstacle(x0, y0, x1, y1, net, pad_layer(pad)))
    # The interior of a kept-out package: the strip between its pad rows,
    # closed to every net - a track that must pass goes around or under on
    # the other face beyond the body, not beneath the die.
    for ref in design.route_keepout:
        part = next(p for p in design.footprints() if p.ref == ref)
        node = footprint_definition(part.footprint)
        boxes = [pad_box(design, part, pad) for pad in node.children("pad")]
        cx = part.board[0]
        left = max((b[2] for b in boxes if (b[0] + b[2]) / 2 < cx), default=None)
        right = min((b[0] for b in boxes if (b[0] + b[2]) / 2 > cx), default=None)
        if left is None or right is None or right - left < 1.0:
            continue
        y0 = min(b[1] for b in boxes)
        y1 = max(b[3] for b in boxes)
        router.add(autoroute.Obstacle(left + 0.4, y0, right - 0.4, y1, "", None))
    for via in design.vias:
        router.add_via(via.net, via_position(design, via), via.size)
    for part in design.footprints():
        node = footprint_definition(part.footprint)
        for pad in node.children("pad"):
            if pad_layer(pad) is None:
                # a through-hole pad is a drilled hole, and hole-to-hole applies
                # to it exactly as it does to a via
                box = pad_box(design, part, pad)
                router.via_sites.append(((box[0] + box[2]) / 2, (box[1] + box[3]) / 2))
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
        done = replace(design, vias=[*design.vias, *_stitch_vias(design)])
        return _chamfer_tracks(done)
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
            print(
                f"{design.name}: ripping up for {blocked.track.net} "
                f"{blocked.track.points} (attempt {len(ripped)})",
                file=sys.stderr,
            )
            continue
        done = replace(
            design, tracks=[track for _, track in sorted(routed, key=lambda p: p[0])], vias=vias
        )
        done = replace(done, vias=[*done.vias, *_stitch_vias(done)])
        return _chamfer_tracks(done)


def _chamfer_tracks(design: Design, cut: float = 1.5) -> Design:
    """Every square corner cut to two 45s - copper bends, it does not turn.

    A right angle in a track is free to avoid and mildly costly to keep: the
    outer corner over-etches, the impedance steps, and every reviewer reads it
    as a router nobody checked. The cut is capped at half of either leg so a
    short pad entry keeps its shape, and a corner that another track tees
    into is left alone rather than cut out from under the join.
    """
    ends: dict[tuple[float, float], int] = defaultdict(int)
    resolved: list[list[tuple[float, float]]] = []
    for track in design.tracks:
        points = [resolve(design, point) for point in track.points]
        resolved.append(points)
        ends[points[0]] += 1
        ends[points[-1]] += 1
    # A cut moves copper off the square path, and what it moves toward may be
    # a via that only cleared the original corner. Foreign vias by net.
    foreign_vias = [(via.net, via_position(design, via), via.size / 2) for via in design.vias]

    tracks = []
    for track, points in zip(design.tracks, resolved, strict=True):
        if len(points) < 3:
            tracks.append(replace(track, points=list(points)))
            continue
        out: list[tuple[float, float]] = [points[0]]
        for prev, corner, nxt in zip(points, points[1:], points[2:], strict=False):
            v1 = (corner[0] - prev[0], corner[1] - prev[1])
            v2 = (nxt[0] - corner[0], nxt[1] - corner[1])
            l1, l2 = math.hypot(*v1), math.hypot(*v2)
            if l1 < GEOM_EPS or l2 < GEOM_EPS:
                continue
            # a corner already gentle - a 45 bend from the fans - stays as it
            # is; only turns sharper than ~60 degrees get the cut
            gentle = (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2) > 0.5
            if gentle or ends.get(corner, 0):
                out.append(corner)
                continue
            c = min(cut, l1 / 2, l2 / 2)
            p1 = (round(corner[0] - v1[0] / l1 * c, 3), round(corner[1] - v1[1] / l1 * c, 3))
            p2 = (round(corner[0] + v2[0] / l2 * c, 3), round(corner[1] + v2[1] / l2 * c, 3))
            blocked = any(
                net != track.net and _segment_to_point(p1, p2, at) < track.width / 2 + radius + 0.25
                for net, at, radius in foreign_vias
            )
            if blocked:
                out.append(corner)  # the square corner was the clear shape here
                continue
            out.append(p1)
            out.append(p2)
        out.append(points[-1])
        # two half-leg cuts meeting mid-segment leave a duplicate point, and a
        # duplicate point emits as a zero-length track DRC calls dangling
        deduped = [out[0]]
        for point in out[1:]:
            if math.dist(point, deduped[-1]) > GEOM_EPS:
                deduped.append(point)
        tracks.append(replace(track, points=deduped))
    return replace(design, tracks=tracks)


def _stitch_vias(design: Design) -> list[Via]:
    """Ground vias around the pour's rim, so no face's edge copper floats.

    With ground on both faces, the front pour's rim would otherwise hang on
    whatever pads it happens to touch; a ring of stitching vias ties the two
    planes together at the boundary, which is also where edge-coupled noise
    wants a short way home. Candidates every 10 mm along the rim, kept only
    where they clear every pad, every foreign track and every existing hole.
    """
    if not design.pour:
        return []
    x0, y0, x1, y1 = design.pour
    inset = 1.5
    rim: list[tuple[float, float]] = []
    step = 10.0
    left, top, right, bottom = x0 + inset, y0 + inset, x1 - inset, y1 - inset
    x = left
    while x < right + 0.01:
        rim += [(round(x, 2), round(top, 2)), (round(x, 2), round(bottom, 2))]
        x += step
    y = top + step
    while y < bottom - 0.01:
        rim += [(round(left, 2), round(y, 2)), (round(right, 2), round(y, 2))]
        y += step

    pads = []
    for part in design.footprints():
        node = footprint_definition(part.footprint)
        for pad in node.children("pad"):
            number = str(pad.atom(0, ""))
            net = next(
                (n for n, nodes in design.nets.items() if f"{part.ref}.{number}" in nodes),
                None,
            )
            pads.append((pad_box(design, part, pad), net))
    segments = []
    for track in design.tracks:
        points = [resolve(design, point) for point in track.points]
        segments.extend((track.net, a, b) for a, b in pairwise(points))
    holes = [via_position(design, via) for via in design.vias]

    kept: list[Via] = []
    for vx, vy in rim:
        radius = 0.4
        ok = True
        for (bx0, by0, bx1, by1), net in pads:
            grow = 0.4 if net == POUR_NET else radius + 0.3
            if bx0 - grow <= vx <= bx1 + grow and by0 - grow <= vy <= by1 + grow:
                ok = False
                break
        if ok:
            for net, a, b in segments:
                if net == POUR_NET:
                    continue
                if _segment_to_point(a, b, (vx, vy)) < radius + 0.45:
                    ok = False
                    break
        if ok and all(math.dist((vx, vy), hole) >= 1.2 for hole in holes):
            holes.append((vx, vy))
            kept.append(Via(POUR_NET, x=vx, y=vy))
    return kept


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
    labels = {
        name: (name if name in POWER_SYMBOLS and name not in design.wired_power else f"/{name}")
        for name in order
    }

    # Every pin the design does not use gets the name KiCad's own netlister
    # gives it: reference, unit letter when the symbol has more than one, pin
    # name and pad number.
    spares: dict[tuple[str, str], str] = {}
    for part in design.parts:
        units = symbol_units(part.lib_id)
        letter = chr(64 + part.unit) if units > 1 else ""
        for pin in symbol_pins(part.lib_id, part.unit):
            if (part.ref, pin.number) in net_of:
                continue
            if not part.no_connect and pin.etype != "no_connect":
                continue
            clean = pin.name.replace("~{", "").replace("}", "")
            spares[(part.ref, pin.number)] = (
                f"unconnected-({part.ref}{letter}-{clean}-Pad{pin.number})"
            )
    for index, name in enumerate(sorted(set(spares.values())), start=len(order) + 1):
        codes[name] = index
        labels[name] = name

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
    for name in [*order, *sorted(set(spares.values()))]:
        lines.append(f'\t(net {codes[name]} "{labels[name]}")')

    for part in design.footprints():
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
            elif number and (spare := spares.get((part.ref, number))):
                # A pad the schematic marked no-connect still has a net there -
                # KiCad invents one per pin - and a board that leaves the pad
                # bare disagrees with the netlist about every one of them.
                pad.args.append(SNode("net", [codes[spare], spare]))
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
        # Ground on both faces: the back is the reference plane, and the front
        # pour picks up what the parts leave free - stitched at the edges so
        # neither face carries a floating island to act as an antenna.
        lines.append(_zone(design, codes["GND"], "B.Cu"))
        lines.append(_zone(design, codes["GND"], "F.Cu", smd_isolated=True))

    lines.extend(_board_silk(design))

    lines.append(")")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _silk_text_item(
    design: Design, text: str, x: float, y: float, key: object, size: float = 0.8
) -> str:
    ox, oy = design.origin
    thickness = round(size * 0.15, 3)
    return (
        f'\t(gr_text "{text}" (at {round(ox + x, 4)} {round(oy + y, 4)} 0) '
        f'(layer "F.SilkS") (uuid "{stable_uuid(design.name, "silk", key)}") '
        f"(effects (font (size {size} {size}) (thickness {thickness}))))"
    )


def _courtyard_box(design: Design, part: Part) -> tuple[float, float, float, float] | None:
    """The footprint's courtyard extent on the board, or None without one."""
    node = footprint_definition(part.footprint)
    bx, by, angle = part.board
    xs: list[float] = []
    ys: list[float] = []
    for line in (*node.children("fp_line"), *node.children("fp_rect")):
        layer = line.child("layer")
        if not layer or "CrtYd" not in str(layer.atom(0, "")):
            continue
        for key in ("start", "end"):
            at = line.child(key)
            if at:
                atoms = [a for a in at.atoms() if isinstance(a, (int, float))]
                if len(atoms) >= 2:
                    rx, ry = _rotate(float(atoms[0]), float(atoms[1]), angle)
                    xs.append(bx + rx)
                    ys.append(by + ry)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _board_silk(design: Design) -> list[str]:
    """What the silkscreen says beyond the references.

    Three things a bare board has to answer without its schematic: which board
    it is (name and revision, bottom left), which signal is on which connector
    pin (the reverse-connection insurance), and what the indicators mean.
    """
    out: list[str] = []
    if not design.rev:
        # The degraded variant documents nothing about itself - that is one of
        # its findings - so the whole self-description package rides on rev.
        return out
    out.append(
        _silk_text_item(
            design,
            f"{design.name} rev {design.rev}",
            design.board_size[0] / 2,
            design.board_size[1] - 4.4,
            "boardid",
            size=1.2,
        )
    )
    if design.company:
        # the author line: a bare board also answers "whose design is this"
        out.append(
            _silk_text_item(
                design,
                design.company,
                design.board_size[0] / 2,
                design.board_size[1] - 2.0,
                "boardauthor",
                size=1.0,
            )
        )
    width, height = design.board_size
    net_of: dict[tuple[str, str], str] = {}
    for name, nodes in design.nets.items():
        for entry in nodes:
            ref, _, number = entry.partition(".")
            net_of[(ref, number)] = name
    for part in design.footprints():
        if part.ref.startswith("J"):
            node = footprint_definition(part.footprint)
            pads = [
                (str(pad.atom(0, "")), pad, pad_position_of(design, part, pad))
                for pad in node.children("pad")
            ]
            xs = [p[0] for _n, _p, p in pads]
            ys = [p[1] for _n, _p, p in pads]
            # labels go perpendicular to the pad row, on the board side, and
            # clear the whole footprint - a screw terminal's body silk would
            # swallow a pad-edge offset
            row_along_x = (max(xs) - min(xs)) >= (max(ys) - min(ys))
            clear = _courtyard_box(design, part)
            seen: set[str] = set()
            for number, pad, (px, py) in pads:
                net = net_of.get((part.ref, number))
                if not net or number in seen:
                    continue
                seen.add(number)
                bx0, by0, bx1, by1 = clear or pad_box(design, part, pad)
                if row_along_x:
                    tx = px
                    ty = (by1 + 1.2) if height / 2 - py > 0 else (by0 - 1.2)
                else:
                    tx = (bx1 + 1.6) if width / 2 - px > 0 else (bx0 - 1.6)
                    ty = py
                out.append(_silk_text_item(design, net, tx, ty, (part.ref, number)))
        if part.silk_label:
            bx, by, _angle = part.board
            dy = height / 2 - by
            out.append(
                _silk_text_item(
                    design, part.silk_label, bx, by + math.copysign(2.6, dy), (part.ref, "label")
                )
            )
    return out


ZONE_CLEARANCE = 0.4  # what the pour keeps away from copper of another net
ZONE_SLIVER = 0.35  # a strip of plane thinner than this is not worth filling
ZONE_WELD = 0.05  # how far neighbouring islands are grown into each other
# Every hole is rounded outward onto this grid. Without it a diagonal track puts
# an x edge every fraction of a millimetre, the sweep below never sees two
# neighbouring columns agree, and the plane comes out as a thousand slivers
# instead of a dozen rectangles. Rounding outward only ever adds clearance.
ZONE_GRID = 0.1


def _hole_boxes(
    design: Design, layer: str, net: str, smd_isolated: bool = False
) -> list[tuple[float, float, float, float]]:
    """Everything of another net that this layer's pour has to keep clear of.

    With ``smd_isolated`` the pour also keeps clear of its own net's surface
    pads - the fill for a ``thru_hole_only`` zone, where an SMD pad connects
    through its track and never to the plane it floats on.
    """
    keep = ZONE_CLEARANCE + ZONE_WELD

    def grown(box, half=0.0):
        return (
            math.floor((box[0] - keep - half) / ZONE_GRID) * ZONE_GRID,
            math.floor((box[1] - keep - half) / ZONE_GRID) * ZONE_GRID,
            math.ceil((box[2] + keep + half) / ZONE_GRID) * ZONE_GRID,
            math.ceil((box[3] + keep + half) / ZONE_GRID) * ZONE_GRID,
        )

    holes = []
    for part in design.footprints():
        node = footprint_definition(part.footprint)
        for pad in node.children("pad"):
            number = str(pad.atom(0, ""))
            owner = next(
                (n for n, nodes in design.nets.items() if f"{part.ref}.{number}" in nodes), None
            )
            same = owner == net and (not smd_isolated or pad_layer(pad) is None)
            if same or pad_layer(pad) not in (None, layer):
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
    design: Design, layer: str, net: str, smd_isolated: bool = False
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
        for hole in _hole_boxes(design, layer, net, smd_isolated)
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
    for part in design.footprints():
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


def _zone(design: Design, code: int, layer: str = "B.Cu", smd_isolated: bool = False) -> str:
    """The ground pour, and the fill that goes with it.

    The fill is computed here rather than left for KiCad because the committed
    board has to be complete on its own: an unfilled zone means DRC reports
    every ground pad unconnected, and the fabrication output ships without a
    plane. KiCad's own filler is not available - it needs a display, and the
    container has none - so this is the same subtraction done by hand.

    ``smd_isolated`` makes the zone connect through-hole only: on the component
    face, a thermal spoke to a fine-pitch pad is hostage to whatever copper
    crowds it - DRC calls the survivor a starved thermal - and every surface
    pad already has the track it was routed with. The plane ties on at the
    through-holes and the stitching vias instead.
    """
    ox, oy = design.origin
    x0, y0, x1, y1 = design.pour
    outline = [(ox + x0, oy + y0), (ox + x1, oy + y0), (ox + x1, oy + y1), (ox + x0, oy + y1)]
    pts = " ".join(f"(xy {round(x, 4)} {round(y, 4)})" for x, y in outline)
    connect = "\t\t(connect_pads thru_hole_only" if smd_isolated else "\t\t(connect_pads"
    lines = [
        "\t(zone",
        f"\t\t(net {code})",
        f'\t\t(net_name "{POUR_NET}")',
        f'\t\t(layer "{layer}")',
        f'\t\t(uuid "{stable_uuid(design.name, "zone", layer)}")',
        "\t\t(hatch edge 0.5)",
        connect,
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
    for left, top, right, bottom in _fill_rectangles(design, layer, POUR_NET, smd_isolated):
        corners = [(left, top), (right, top), (right, bottom), (left, bottom)]
        island = " ".join(f"(xy {round(ox + x, 4)} {round(oy + y, 4)})" for x, y in corners)
        lines.append(f'\t\t(filled_polygon (layer "{layer}") (pts {island}))')
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
    thin = min([0.25, *(track.width for track in design.tracks)])
    parts = []
    for index, part in enumerate(design.parts):
        sx, sy = part.sheet
        bx, by, angle = part.board
        # Every fourth part is turned to a nonsense angle - except a fine-pitch
        # one, whose escape is stated rather than searched for and which turns
        # into a board no router can finish. `as-generated` is meant to be a bad
        # board, and a board is only bad if it exists.
        pads = len(footprint_definition(part.footprint).children("pad"))
        turned = 37.0 if index % 4 == 0 and pads <= 8 else angle
        parts.append(
            replace(
                part,
                sheet=(round(sx + OFF_GRID[0], 4), round(sy + OFF_GRID[1], 4)),
                board=(round(bx + 0.23, 3), round(by - 0.17, 3), turned),
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
        strict=False,
        draw_wires=False,
        notes=[],
        note_blocks=[],
        parts=parts,
        power_flags=[],
        # One width for everything, which is what a generator that never asked
        # what a net carries produces. Never wider than the design's own
        # narrowest, because a board whose escape was drawn for 0.2 mm has no
        # room for 0.25 mm and the degraded variant is supposed to be bad, not
        # unroutable.
        tracks=[replace(t, width=thin) for t in design.tracks],
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
    parts = [
        Part(
            "J1",
            "Connector:Screw_Terminal_01x02",
            "12V IN",
            "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2_1x02_P5.00mm_Horizontal",
            sheet=(38.1, 63.5),
            mirror="y",
            board=(10.0, 12.0, 270.0),
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
            sheet=(63.5, 69.85),
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
            sheet=(78.74, 69.85),
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
            sheet=(134.62, 74.93),
            angle=270.0,
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
            sheet=(153.67, 68.58),
            angle=90.0,
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
            sheet=(166.37, 74.93),
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
            sheet=(179.07, 74.93),
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
            sheet=(215.9, 93.98),
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
            sheet=(215.9, 107.95),
            board=(107.0, 46.0, 180.0),
            angle=90.0,
            silk_label="5V OK",
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
            sheet=(200.66, 68.58),
            board=(116.0, 36.0, 90.0),
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
        Track(
            "+5V", "F.Cu", W, [(96.3, 30.0), (107.0, 30.0), (107.0, 42.0), (110.0, 42.0), "J2.1"]
        ),
        Track("+5V", "F.Cu", SIG, ["C3.1", (96.3, 42.0), (99.088, 42.0), "R1.1"]),
        Track("LED_A", "F.Cu", SIG, ["R1.2", "D2.2"]),
        # Ground: a stub from each pad to a via of its own, straight into the
        # pour. Only the two through-hole terminals, outside the pour, run far.
        Track("GND", "F.Cu", W, ["J1.2", (10.0, 50.0), (16.0, 50.0)]),
        Track("GND", "F.Cu", W, ["J2.2", (121.0, 36.0), (121.0, 50.0), (110.0, 50.0)]),
        # The explicit return: input ground to output ground at the same width
        # as the forward path, so the 2 A loop does not depend on the pour
        # alone. It rides the bottom edge, under the LED branch, crossing
        # nothing.
        Track("GND", "F.Cu", W, [(16.0, 50.0), (110.0, 50.0)]),
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
        # The tab is the die's thermal path and the switch loop's return: a
        # ring of vias just off the pad ties it straight into both pours.
        # Off the pad, not on it - via-in-pad drinks the solder at reflow.
        Via("GND", x=69.5, y=7.5),
        Via("GND", x=69.5, y=12.0),
        Via("GND", x=69.5, y=16.5),
        Via("GND", x=60.5, y=5.2),
        Via("GND", x=65.5, y=5.2),
        Via("GND", x=60.5, y=18.8),
        Via("GND", x=65.5, y=18.8),
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
        notes=[],
        note_blocks=[
            (
                (30.48, 87.63),
                [
                    "Input: C1 220u/35V bulk, C2 100n/50V bypass -",
                    "both above 1.5x the 12 V they sit on.",
                ],
            ),
            (
                (95.25, 45.72),
                [
                    "LM2596S-5 is the fixed 5 V part:",
                    "FB ties straight to the output, no divider.",
                ],
            ),
            (
                (127.0, 91.44),
                [
                    "D1 SS34 (3 A / 40 V) catches the inductor current.",
                    "L1 33 uH, 3 A saturation: ripple 0.6 A pk-pk at 2 A out.",
                    "C3 220u/16V bulk, C4 100n/25V bypass on the 5 V rail.",
                ],
            ),
            (
                (207.01, 121.92),
                ["5V OK: 3 mA through R1."],
            ),
            (
                (30.48, 100.33),
                ["Power copper is 1.0 mm, good for 2.7 A at a 10 C rise (IPC-2221)."],
            ),
        ],
        parts=parts,
        nets=nets,
        power_flags=[("+12V", "J1.1"), ("GND", "J1.2"), ("+5V", "L1.2")],
        board_size=(126.0, 56.0),
        tracks=tracks,
        vias=vias,
        pour=(2.0, 2.0, 124.0, 54.0),
        wired_power=("+12V", "+5V"),
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
            mirror="y",
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
            sheet=(55.88, 86.36),
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
            sheet=(68.58, 86.36),
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
            sheet=(96.52, 72.39),
            angle=90.0,
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
            sheet=(149.86, 60.96),
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
            sheet=(160.02, 60.96),
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
            sheet=(81.28, 107.95),
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
            sheet=(81.28, 121.92),
            board=(80.0, 6.0, 180.0),
            silk_label="VM OK",
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
            sheet=(172.72, 82.55),
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
            sheet=(172.72, 95.25),
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
        notes=[],
        note_blocks=[
            (
                (17.78, 102.87),
                [
                    "C1 100 uF / 25 V bulk on a rail that can reach 10.8 V -",
                    "1.5x headroom, and reserve for the motor current steps.",
                ],
            ),
            (
                (93.98, 115.57),
                ["VM present: 1.5 mA through R2."],
            ),
            (
                (60.96, 55.88),
                [
                    "C3 10 nF: the charge-pump flying capacitor",
                    "between VM and VCP - the datasheet's value.",
                ],
            ),
            (
                (147.32, 33.02),
                [
                    "C4 1 uF bypasses VINT, the internal 3.3 V",
                    "regulator; R1 10k pulls up open-drain nFAULT.",
                ],
            ),
            (
                (162.56, 106.68),
                [
                    "AISEN/BISEN grounded: no current sense,",
                    "the part's internal limit only.",
                    "A lands on J2 reversed, B on J3 straight -",
                    "the package brings them out that way.",
                ],
            ),
            (
                (17.78, 60.96),
                ["Logic header in track-arrival order:", "grounds at both ends."],
            ),
        ],
        parts=parts,
        nets=nets,
        power_flags=[("VM", "J1.1"), ("GND", "J1.2"), ("VINT", "C4.1")],
        board_size=(100.0, 50.0),
        tracks=[],
        vias=[],
        pour=(3.0, 3.0, 97.0, 47.0),
        label_nets=("nSLEEP", "AIN1", "AIN2", "BIN1", "BIN2", "nFAULT"),
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
        widths={"2": 0.4, "4": 0.4, "5": 0.4, "7": 0.4},
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
            mirror="y",
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
            sheet=(90.17, 39.37),
            mirror="y",
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
            sheet=(127.0, 45.72),
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
            sheet=(180.34, 60.96),
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
            sheet=(193.04, 60.96),
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
            sheet=(193.04, 76.2),
            board=(64.0, 20.0, 180.0),
            silk_label="3V3 OK",
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

    nets["+5V"] = ["J1.1", "D1.2"]
    nets["VSYS"] += ["D1.1", "C1.1"]
    nets["+3V3"] += ["C2.1", "R1.1"]
    nets["GND"] += ["J1.2", "C1.2", "C2.2", "D3.1"]
    nets["LED_P"] = ["R1.2", "D3.2"]

    design = Design(
        name="pico-carrier",
        title="Raspberry Pi Pico carrier, 5 V in",
        rev="A",
        company="kicad_skills examples",
        notes=[],
        note_blocks=[
            (
                (71.12, 17.78),
                [
                    "5 V in feeds VSYS through D1 - the datasheet's own arrangement:",
                    "it keeps USB and the external supply from fighting when both are",
                    "plugged in. C1 22 uF / 16 V is the bulk that goes with it.",
                ],
            ),
            (
                (198.12, 64.77),
                [
                    "C2 bypasses the module's own 3.3 V;",
                    "D3 says that rail is up.",
                ],
            ),
            (
                (20.32, 146.05),
                [
                    "Every module pin goes 1:1 to the header beside it, as names:",
                    "the pair of labels at each pin is the mapping. J4 counts down",
                    "against the module - the Pico numbers its right side from the",
                    "bottom, a header from the top.",
                ],
            ),
            (
                (165.1, 146.05),
                [
                    "AGND goes to the header on its own: the module",
                    "already joins it to GND inside, and joining it",
                    "again here is two power outputs wired together.",
                ],
            ),
            (
                (165.1, 160.02),
                [
                    "The back plane necessarily opens under the module's two",
                    "pin rows: forty through-holes at 2.54 mm leave no copper",
                    "between them. It is continuous around and between the rows.",
                ],
            ),
        ],
        parts=parts,
        nets=nets,
        power_flags=[("+5V", "J1.1"), ("VSYS", "D1.1"), ("ADC_VREF", "U1.35")],
        board_size=(82.0, 68.0),
        tracks=[],
        vias=[],
        pour=(3.0, 3.0, 79.0, 65.0),
        # High enough that the last line stays inside the sheet frame.
        notes_at=(20.0, 146.0),
        # The module's 2.54 mm pad pitch decides where everything goes; snapping
        # to 0.5 mm would move the headers off the pins they exist to reach.
        board_grid=None,
        label_nets=(
            *(f"GP{n}" for n in range(29)),
            "RUN",
            "3V3_EN",
            "VBUS",
            "ADC_VREF",
            "AGND",
        ),
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
        Track("+5V", "F.Cu", POWER, ["J1.1", "D1.2"], auto=True),
        Track("VSYS", "F.Cu", POWER, ["D1.1", "J4.2"], auto=True),
        Track("VSYS", "F.Cu", POWER, ["C1.1", "D1.1"], auto=True),
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
            angle=90.0,
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
            (
                70.0,
                128.27,
            ),
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
            (181.61, 74.93),
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
            angle=90.0,
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
            angle=180.0,
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
            (30.0, 8.0, 0.0),
            MPN="1729128",
            Manufacturer="Phoenix Contact",
            Datasheet="https://www.phoenixcontact.com/product/1729128",
        ),
        _passive(
            "R3",
            "Device:R",
            "100k",
            "Resistor_SMD:R_0805_2012Metric",
            (74.93, 152.4),
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
            (74.93, 175.26),
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
            (104.14, 163.83),
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
            sheet=(149.86, 160.02),
            board=(52.0, 34.0, 0.0),
            stub=6.35,
            fields={
                "MPN": "MCP6001RT-I/OT",
                "Manufacturer": "Microchip",
                "Datasheet": "https://ww1.microchip.com/downloads/en/DeviceDoc/21733j.pdf",
            },
        ),
        _passive(
            "R7",
            "Device:R",
            "100k",
            "Resistor_SMD:R_0805_2012Metric",
            (36.0, 120.0),
            (12.0, 26.0, 90.0),
            Tolerance="1%",
            Power="0.125W",
            MPN="RC0805FR-07100KL",
            Manufacturer="Yageo",
            Datasheet=YAGEO,
        ),
        _passive(
            "R6",
            "Device:R",
            "100k",
            "Resistor_SMD:R_0805_2012Metric",
            (224.79, 134.62),
            (70.0, 27.0, 90.0),
            Tolerance="1%",
            Power="0.125W",
            MPN="RC0805FR-07100KL",
            Manufacturer="Yageo",
            Datasheet=YAGEO,
        ),
        _passive(
            "C7",
            "Device:C",
            "100n",
            "Capacitor_SMD:C_0805_2012Metric",
            (181.61, 152.4),
            (58.0, 30.0, 0.0),
            Voltage="25V",
            Tolerance="10%",
            MPN="CL21B104KBCNNNC",
            Manufacturer="Samsung",
            Datasheet=SAMSUNG.format("CL21B104KBCNNNC"),
        ),
        Part(
            "TP1",
            "Connector:TestPoint",
            "TP",
            "TestPoint:TestPoint_Pad_D1.5mm",
            sheet=(144.78, 87.63),
            board=(40.0, 14.0, 0.0),
            no_connect=False,
        ),
        Part(
            "TP2",
            "Connector:TestPoint",
            "TP",
            "TestPoint:TestPoint_Pad_D1.5mm",
            sheet=(186.69, 87.63),
            board=(56.0, 24.0, 0.0),
            no_connect=False,
        ),
        Part(
            "TP3",
            "Connector:TestPoint",
            "TP",
            "TestPoint:TestPoint_Pad_D1.5mm",
            sheet=(168.91, 168.91),
            board=(46.0, 38.0, 0.0),
            no_connect=False,
        ),
    ]

    nets = {
        "+5V": ["J2.1", "R3.1", "C5.1", "C7.1", "U1.2", "U2.2"],
        "GND": [
            "J2.2",
            "J1.2",
            "J3.2",
            "R4.2",
            "R6.2",
            "R7.2",
            "C4.2",
            "C5.2",
            "C7.2",
            "U1.5",
            "U2.5",
        ],
        "IN": ["J1.1", "C3.1", "R7.1"],
        "IN_DC": ["C3.2", "R5.1", "R1.1"],
        "X": ["R1.2", "R2.1", "C1.1"],
        "FILT_IN": ["R2.2", "C2.1", "U1.3", "TP1.1"],
        "OUT": ["U1.1", "U1.4", "C1.2", "C6.1", "TP2.1"],
        "OUT_AC": ["C6.2", "J3.1", "R6.1"],
        "MID": ["R3.2", "R4.1", "C4.1", "U2.3"],
        "VREF": ["U2.1", "U2.4", "R5.2", "C2.2", "TP3.1"],
    }

    design = Design(
        name="opamp-filter",
        title="1 kHz Sallen-Key low pass, single 5 V",
        rev="A",
        company="kicad_skills examples",
        notes=[],
        note_blocks=[
            (
                (78.74, 41.91),
                [
                    "R1 = R2 = 10k with C1 22n / C2 10n: f = 1073 Hz, Q = 0.742.",
                    "Butterworth wants Q = 0.707 = 11 nF - not a C0G value, and",
                    "C0G is what keeps the corner where it is; an X7R of this",
                    "size loses a third of its value over the rail.",
                ],
            ),
            (
                (95.25, 186.69),
                [
                    "VREF is half the rail: R3/R4 make it, U2 buffers it. The",
                    "filter's return flows into that node through C2; a bare",
                    "divider is a 50k source - the filter would not be this filter.",
                ],
            ),
            (
                (17.78, 137.16),
                [
                    "C3/C6 couple in and out: the header sees no DC.",
                    "R5 sets the input's operating point at VREF;",
                    "R7/R6 bleed the coupling caps so nothing pops.",
                ],
            ),
            (
                (127.0, 60.96),
                [
                    "TP1-TP3: filter input, output and VREF -",
                    "where the simulation meets the board.",
                ],
            ),
        ],
        parts=parts,
        nets=nets,
        power_flags=[("+5V", "J2.1"), ("GND", "J2.2")],
        board_size=(80.0, 45.0),
        tracks=[],
        vias=[
            # mid-board ties between the faces: the signal row slices the
            # front pour, and these give its pieces a short way to the plane
            Via("GND", x=21.0, y=22.5),
            Via("GND", x=33.0, y=22.5),
            Via("GND", x=45.0, y=25.5),
            Via("GND", x=66.0, y=22.5),
        ],
        pour=(3.0, 3.0, 77.0, 42.0),
        notes_at=(18.0, 20.0),
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
        Track("IN", "F.Cu", SIG, ["J1.1", "R7.1"], auto=True),
        Track("IN_DC", "F.Cu", SIG, ["C3.2", "R1.1"], auto=True),
        Track("IN_DC", "F.Cu", SIG, ["C3.2", "R5.1"], auto=True),
        Track("X", "F.Cu", SIG, ["R1.2", "R2.1"], auto=True),
        Track("X", "F.Cu", SIG, ["R2.1", "C1.1"], auto=True),
        Track("FILT_IN", "F.Cu", SIG, ["R2.2", u1w["3"]], auto=True),
        Track("FILT_IN", "F.Cu", SIG, ["TP1.1", "R2.2"], auto=True),
        Track("OUT", "F.Cu", SIG, ["TP2.1", "C6.1"], auto=True),
        Track("VREF", "F.Cu", SIG, ["TP3.1", "C2.2"], auto=True),
        Track("FILT_IN", "F.Cu", SIG, ["C2.1", u1w["3"]], auto=True),
        Track("OUT", "F.Cu", SIG, [u1w["1"], u1e["4"]], auto=True),
        Track("OUT", "F.Cu", SIG, [u1w["1"], "C1.2"], auto=True),
        Track("OUT", "F.Cu", SIG, [u1e["4"], "C6.1"], auto=True),
        Track("OUT_AC", "F.Cu", SIG, ["C6.2", "J3.1"], auto=True),
        Track("OUT_AC", "F.Cu", SIG, ["J3.1", "R6.1"], auto=True),
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
        ("R6.2", (70.0, 32.0)),
        ("R7.2", (12.0, 30.0)),
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


ICE40 = "FPGA_Lattice:ICE40UP5K-SG48ITR"
TI = "https://www.ti.com/lit/ds/symlink/pcm5102a.pdf"


def fpga_audio() -> Design:
    """An iCE40UP5K driving a PCM5102A over I2S, on two layers.

    This one is here to be difficult, and the difficulty is worth stating
    plainly: a 0.5 mm pitch QFN with pads on four sides is not a two layer
    board. Real iCE40 designs are four layer, with the escape dropping straight
    into an inner layer through via-in-pad or a dogbone per pin. This generator
    knows two layers, so the escape has to be a fan out on the top - twelve pins
    a side walked from 0.5 mm to 0.8 mm, at 0.2 mm track and 0.2 mm clearance,
    which is a fine-line process and says so in the fabrication notes.

    What that costs is visible in the plot: a 7 mm chip needs a 25 mm square of
    board around it before anything else can be placed, and the parts that talk
    to it are pushed to the edges. That is the honest answer to "can this be
    done on two layers", and it is worth having as an example precisely because
    the answer is "yes, and you would not want to".

    The rest is a normal small digital board. The FPGA boots from U4 over its
    own SPI port, runs from a 12 MHz oscillator, and clocks I2S out to U2. Two
    rails: 3.3 V in for the I/O banks and the codec, and 1.2 V from U3 for the
    core. VCCPLL gets its own RC from the core rail rather than a direct
    connection, which is what the datasheet asks for and what keeps the PLL out
    of the core's supply noise.
    """
    parts = [
        *(
            Part(
                "U1",
                ICE40,
                "iCE40UP5K",
                "Package_DFN_QFN:QFN-48-1EP_7x7mm_P0.5mm_EP3.5x3.5mm",
                sheet=where,
                board=(40.0, 40.0, 0.0),
                stub=6.35,
                no_connect=True,
                unit=unit,
                fields={
                    "MPN": "ICE40UP5K-SG48ITR",
                    "Manufacturer": "Lattice Semiconductor",
                    "Datasheet": "https://www.latticesemi.com/view_document?document_id=51968",
                },
            )
            # Bank 0 faces the codec, bank 1 faces the flash, bank 2 is here for
            # its VCCIO pin alone, and the supplies are a box of their own.
            for unit, where in enumerate(
                [(200.0, 110.0), (200.0, 215.0), (60.0, 110.0), (110.0, 45.0)], start=1
            )
        ),
        Part(
            "U2",
            "Audio:PCM5102A",
            "PCM5102A",
            "Package_SO:TSSOP-20_4.4x6.5mm_P0.65mm",
            sheet=(330.0, 110.0),
            board=(72.0, 40.0, 180.0),
            stub=6.35,
            fields={
                "MPN": "PCM5102APWR",
                "Manufacturer": "Texas Instruments",
                "Datasheet": TI,
            },
        ),
        Part(
            "U3",
            "Regulator_Linear:AP2112K-1.2",
            "AP2112K-1.2",
            "Package_TO_SOT_SMD:SOT-23-5",
            sheet=(60.0, 45.0),
            board=(14.0, 24.0, 0.0),
            fields={
                "Voltage": "1.2V",
                "Current": "600mA",
                "MPN": "AP2112K-1.2TRG1",
                "Manufacturer": "Diodes Incorporated",
                "Datasheet": "https://www.diodes.com/assets/Datasheets/AP2112.pdf",
            },
        ),
        Part(
            "U4",
            "Memory_Flash:W25Q32JVSS",
            "W25Q32JV",
            "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
            sheet=(200.0, 270.0),
            board=(40.0, 72.0, 0.0),
            fields={
                "MPN": "W25Q32JVSSIQ",
                "Manufacturer": "Winbond",
                "Datasheet": "https://www.winbond.com/resource-files/w25q32jv%20revi%2005182022%20plus.pdf",
            },
        ),
        Part(
            "X1",
            "Oscillator:ASE-xxxMHz",
            "12MHz",
            "Oscillator:Oscillator_SMD_Abracon_ASE-4Pin_3.2x2.5mm",
            sheet=(60.0, 180.0),
            board=(30.0, 14.0, 0.0),
            fields={
                "Tolerance": "50ppm",
                "MPN": "ASE-12.000MHZ-L-C-T",
                "Manufacturer": "Abracon",
                "Datasheet": "https://abracon.com/Oscillators/ASE.pdf",
            },
        ),
        Part(
            "J1",
            "Connector:Screw_Terminal_01x02",
            "3V3 IN",
            "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2_1x02_P5.00mm_Horizontal",
            sheet=(35.0, 30.0),
            board=(8.0, 8.0, 270.0),
            fields={
                "MPN": "1729128",
                "Manufacturer": "Phoenix Contact",
                "Datasheet": "https://www.phoenixcontact.com/product/1729128",
            },
        ),
        Part(
            "J2",
            "Connector:Conn_01x03_Pin",
            "AUDIO OUT",
            "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
            sheet=(395.0, 110.0),
            board=(89.0, 38.0, 0.0),
            fields={
                "MPN": "61300311121",
                "Manufacturer": "Wurth Elektronik",
                "Datasheet": "https://www.we-online.com/components/products/datasheet/61300311121.pdf",
            },
        ),
        Part(
            "J3",
            "Connector:Conn_01x06_Pin",
            "SPI PROG",
            "Connector_PinHeader_2.54mm:PinHeader_1x06_P2.54mm_Vertical",
            # Clear of the title block, which owns the bottom right corner.
            sheet=(370.0, 245.0),
            board=(74.0, 66.0, 0.0),
            mirror="y",
            fields={
                "MPN": "61300611121",
                "Manufacturer": "Wurth Elektronik",
                "Datasheet": "https://www.we-online.com/components/products/datasheet/61300611121.pdf",
            },
        ),
    ]

    def cap(ref, value, sheet, board, voltage, mpn, angle=0.0):
        return Part(
            ref,
            "Device:C",
            value,
            "Capacitor_SMD:C_0603_1608Metric",
            sheet=sheet,
            board=board,
            angle=angle,
            fields={
                "Voltage": voltage,
                "Tolerance": "10%",
                "MPN": mpn,
                "Manufacturer": "Samsung",
                "Datasheet": f"https://product.samsungsem.com/mlcc/{mpn}.do",
            },
        )

    def res(ref, value, sheet, board, mpn, angle=0.0):
        return Part(
            ref,
            "Device:R",
            value,
            "Resistor_SMD:R_0603_1608Metric",
            sheet=sheet,
            board=board,
            angle=angle,
            fields={
                "Tolerance": "1%",
                "Power": "0.1W",
                "MPN": mpn,
                "Manufacturer": "Yageo",
                "Datasheet": YAGEO,
            },
        )

    parts += [
        cap("C1", "10u", (95.0, 45.0), (18.0, 16.0, 0.0), "16V", "CL10A106MQ8NNNC"),
        cap("C2", "100n", (140.0, 45.0), (22.0, 20.0, 0.0), "25V", "CL10B104KB8NNNC"),
        cap("C3", "10u", (60.0, 70.0), (22.0, 30.0, 0.0), "16V", "CL10A106MQ8NNNC"),
        cap("C4", "100n", (85.0, 70.0), (25.0, 38.5, 90.0), "25V", "CL10B104KB8NNNC"),
        cap("C5", "100n", (165.0, 70.0), (56.0, 43.0, 90.0), "25V", "CL10B104KB8NNNC"),
        cap("C17", "10u", (165.0, 45.0), (59.0, 43.0, 90.0), "16V", "CL10A106MQ8NNNC"),
        res("R3", "100R", (190.0, 45.0), (63.0, 50.0, 90.0), "RC0603FR-07100RL"),
        res("R4", "10k", (330.0, 240.0), (56.0, 74.0, 0.0), "RC0603FR-0710KL"),
        cap("C6", "100n", (228.6, 95.25), (57.0, 46.0, 0.0), "25V", "CL10B104KB8NNNC"),
        cap("C7", "100n", (228.6, 120.65), (61.0, 46.0, 0.0), "25V", "CL10B104KB8NNNC"),
        cap("C8", "100n", (255.0, 270.0), (46.0, 68.0, 0.0), "25V", "CL10B104KB8NNNC"),
        cap("C9", "100n", (95.0, 180.0), (36.0, 14.0, 0.0), "25V", "CL10B104KB8NNNC"),
        cap("C10", "100n", (228.6, 146.05), (60.0, 30.0, 0.0), "25V", "CL10B104KB8NNNC"),
        cap("C11", "100n", (312.42, 70.0), (85.0, 35.0, 0.0), "25V", "CL10B104KB8NNNC"),
        cap("C16", "100n", (347.98, 70.0), (84.0, 49.0, 0.0), "25V", "CL10B104KB8NNNC"),
        cap("C12", "2u2", (300.0, 180.0), (60.0, 50.0, 0.0), "16V", "CL10A225KO8NNNC"),
        cap("C13", "2u2", (330.0, 180.0), (84.0, 41.0, 90.0), "16V", "CL10A225KO8NNNC"),
        cap("C14", "2u2", (360.0, 180.0), (84.0, 45.5, 90.0), "16V", "CL10A225KO8NNNC"),
        res("R1", "10k", (110.0, 150.0), (28.5, 22.0, 0.0), "RC0603FR-0710KL"),
        res("R2", "10k", (140.0, 150.0), (34.0, 22.0, 0.0), "RC0603FR-0710KL"),
        cap("C15", "100n", (140.0, 70.0), (57.0, 50.0, 180.0), "25V", "CL10B104KB8NNNC"),
    ]

    nets = {
        "+3V3": [
            "J1.1",
            "C1.1",
            "C2.1",
            "U3.1",
            "U3.3",
            "U1.1",
            "U1.22",
            "U1.33",
            "U1.24",
            "C6.1",
            "C7.1",
            "R1.1",
            "R2.1",
            "U4.8",
            "C8.1",
            "X1.4",
            "C9.1",
            "U2.20",
            "U2.8",
            "U2.1",
            "C10.1",
            "C11.1",
            "R4.1",
            "C16.1",
        ],
        "+1V2": ["U3.5", "C3.1", "C4.1", "C15.1", "U1.5", "U1.30", "R3.1"],
        # the PLL supply is filtered from the core rail, not tied to it
        "VCCPLL": ["R3.2", "C5.1", "C17.1", "U1.29"],
        "GND": [
            "J1.2",
            "C1.2",
            "C2.2",
            "U3.2",
            "C3.2",
            "C4.2",
            "C5.2",
            "C6.2",
            "C7.2",
            "U1.49",
            "U4.4",
            "C8.2",
            "X1.2",
            "C9.2",
            "U2.19",
            "U2.9",
            "U2.3",
            "C10.2",
            "C15.2",
            "C17.2",
            "C11.2",
            "C16.2",
            "C12.2",
            "C14.2",
            "J2.2",
            "J3.6",
        ],
        # R4 holds the flash deselected while the FPGA is in reset and its
        # pins are still floating - without it the boot bus is a lottery
        "SPI_SS": ["U1.16", "U4.1", "J3.1", "R4.2"],
        "SPI_SCK": ["U1.15", "U4.6", "J3.2"],
        "SPI_SI": ["U1.17", "U4.5", "J3.3"],
        "SPI_SO": ["U1.14", "U4.2", "J3.4"],
        "CRESET": ["U1.8", "R1.2", "J3.5"],
        "CDONE": ["U1.7", "R2.2"],
        "CLK12": ["X1.3", "U1.37"],
        # On the east side, in the order the codec wants them: a bus that
        # leaves the package already in the right order does not cross itself.
        "I2S_SCK": ["U1.36", "U2.12"],
        "I2S_BCK": ["U1.35", "U2.13"],
        "I2S_DIN": ["U1.34", "U2.14"],
        "I2S_LRCK": ["U1.32", "U2.15"],
        "OUTL": ["U2.6", "J2.3"],
        "OUTR": ["U2.7", "J2.1"],
        "LDOO": ["U2.18", "C12.1"],
        # the flying capacitor sits between CAPP and CAPM; the reservoir from
        # VNEG to ground - the inverter cannot run with either elsewhere
        "CAPP": ["U2.2", "C13.1"],
        "CAPM": ["U2.4", "C13.2"],
        "VNEG": ["U2.5", "C14.1"],
        # The codec's mode pins are strapped rather than driven: 16-bit I2S,
        # no de-emphasis, normal filter, un-muted.
        "FMT": ["U2.16"],
        "DEMP": ["U2.10"],
        "FLT": ["U2.11"],
        "XSMT": ["U2.17"],
        "WP": ["U4.3"],
        "HOLD": ["U4.7"],
        "OSC_EN": ["X1.1"],
    }
    # The strapped pins go to the rail their state asks for rather than to a net
    # of their own - a pin held low is held low by copper, not by a name.
    for pin, rail in (
        ("U2.16", "GND"),
        ("U2.10", "GND"),
        ("U2.11", "GND"),
        ("U2.17", "+3V3"),
        ("U4.3", "+3V3"),
        ("U4.7", "+3V3"),
        ("X1.1", "+3V3"),
    ):
        nets[rail].append(pin)
    for name in ("FMT", "DEMP", "FLT", "XSMT", "WP", "HOLD", "OSC_EN"):
        del nets[name]

    design = Design(
        name="fpga-audio",
        title="iCE40UP5K to PCM5102A, I2S out",
        rev="A",
        company="kicad_skills examples",
        notes=[],
        note_blocks=[
            (
                (17.78, 208.28),
                [
                    "A 0.5 mm pitch QFN with pads on four sides is not a two layer",
                    "board. A real iCE40 design drops each pin into an inner layer;",
                    "this one has no inner layer, so all 48 escape on the top at",
                    "0.2 mm track and 0.2 mm clearance - a fine-line process, and",
                    "the reason a 7 mm chip needs 25 mm of board around it.",
                ],
            ),
            (
                (17.78, 87.63),
                [
                    "C1 10u + C2 100n: the 3.3 V input, at U3.",
                    "C3/C4: the 1.2 V core rail it makes. C15 sits",
                    "on U1's VCC pins; VCCPLL is filtered from the",
                    "core rail through R3, C17 and C5 at the pin -",
                    "core switching noise stays out of the PLL.",
                ],
            ),
            (
                (215.9, 168.91),
                [
                    "C6/C7, C10: one 100n per FPGA I/O-bank",
                    "supply pin, beside the bank they feed.",
                ],
            ),
            (
                (302.26, 148.59),
                [
                    "U2's mode pins are strapped, not driven: 16-bit I2S,",
                    "no de-emphasis, normal filter, un-muted. C11/C16",
                    "bypass its supplies; C12-C14 are the charge pump",
                    "and LDO reservoirs the datasheet asks for.",
                ],
            ),
            (
                (256.54, 273.05),
                [
                    "U1 boots from U4 over its own SPI port; J3 is that bus",
                    "plus CRESET, so the flash can be written in circuit.",
                    "R4 holds the select up while the FPGA configures;",
                    "R1/R2 hold CRESET and CDONE up, both open drain.",
                ],
            ),
        ],
        parts=parts,
        nets=nets,
        # GND has no power-output pin on it either: every ground here is a
        # power *input*, and without a flag ERC says so.
        power_flags=[("+3V3", "J1.1"), ("GND", "J1.2")],
        board_size=(94.0, 84.0),
        label_nets=("I2S_SCK", "I2S_BCK", "I2S_DIN", "I2S_LRCK"),
        route_keepout=(),
        tracks=[],
        vias=[],
        pour=(3.0, 3.0, 91.0, 81.0),
        # Four units of one symbol and twenty-odd parts do not fit on A4.
        paper="A3",
    ).snapped()

    # Everything that lands on the QFN's escape lands at 0.2 mm: the escape
    # walks the row out to 0.8 mm, and 0.8 mm is not enough for a 0.3 mm track
    # to squeeze between two of its neighbours. Only the input, which never goes
    # near the chip, is wider.
    FINE, SIG, POWER = 0.2, 0.2, 0.4
    cx, cy = 40.0, 40.0
    sides = {
        # Each row runs the way the pads do, not the way the numbers do: a QFN
        # counts anticlockwise, so its east and north rows are bottom-to-top and
        # right-to-left. Handing them over the other way round makes every
        # escape on that side cross every other one, and the fan is legal
        # nowhere.
        "west": ([str(n) for n in range(1, 13)], "x", 35.55, 27.0),
        "south": ([str(n) for n in range(13, 25)], "y", 44.45, 53.0),
        "east": ([str(n) for n in range(36, 24, -1)], "x", 44.45, 53.0),
        "north": ([str(n) for n in range(48, 36, -1)], "y", 35.55, 27.0),
    }
    escapes: list[Track] = []
    pad_of: dict[str, tuple[float, float]] = {}

    def end(spec: str):
        return pad_of.get(spec, spec)

    def escape(ref, pins, **kw):
        tracks, ends = fan(design, ref, pins, **kw)
        escapes.extend(tracks)
        pad_of.update({f"{ref}.{number}": point for number, point in ends.items()})

    for pins, axis, lead, column in sides.values():
        escape(
            "U1",
            pins,
            lead=lead,
            column=column,
            pitch=1.0,
            centre=cy if axis == "x" else cx,
            axis=axis,
            width=FINE,
            slope=3.0,
            clearance=0.2,
        )
    # The codec, the regulator and the flash each have a supply pin in the
    # middle of a row, which is the one place a search cannot reach.
    # U2 is turned round so that its I2S pins face the FPGA and its outputs
    # face the connector; that also swaps which row is which side.
    escape(
        "U2",
        [str(n) for n in range(11, 21)],
        lead=67.6,
        column=64.0,
        pitch=1.0,
        centre=40.0,
        width=SIG,
    )
    escape(
        "U2",
        [str(n) for n in range(10, 0, -1)],
        lead=76.4,
        column=80.0,
        pitch=1.0,
        centre=40.0,
        width=SIG,
    )
    escape("U3", ["1", "2", "3"], lead=11.4, column=9.0, pitch=1.9, centre=24.0, width=SIG)
    escape("U3", ["5", "4"], lead=16.6, column=19.0, pitch=2.8, centre=24.0, width=SIG)
    escape("U4", ["1", "2", "3", "4"], lead=36.1, column=33.5, pitch=2.0, centre=72.0, width=SIG)
    escape("U4", ["8", "7", "6", "5"], lead=43.9, column=46.5, pitch=2.0, centre=72.0, width=SIG)

    # The exposed pad is the ground, and it is stitched rather than routed.
    vias = [Via("GND", x=cx + dx, y=cy + dy) for dx in (-1.0, 0.0, 1.0) for dy in (-1.0, 0.0, 1.0)]

    # A decoupling capacitor's ground via sits against its own pad, on the far
    # side from the supply pad. The loop the capacitor exists to close runs
    # pad, via, plane; a via at the end of a routed track puts that track's
    # inductance inside the loop, which is what `layout.decoupling_via` measures.
    anchored = []
    for cref in ("C1", "C2", "C3", "C9", "C10", "C11", "C15"):
        supply = pad_position(design, f"{cref}.1")
        ground = pad_position(design, f"{cref}.2")
        length = math.hypot(ground[0] - supply[0], ground[1] - supply[1]) or 1.0
        ux = (ground[0] - supply[0]) / length
        uy = (ground[1] - supply[1]) / length
        offset = (round(1.2 * ux, 4), round(1.2 * uy, 4))
        vias.append(Via("GND", pad=f"{cref}.2", offset=offset))
        anchored.append(
            Track(
                "GND",
                "F.Cu",
                0.4,
                [
                    f"{cref}.2",
                    (round(ground[0] + offset[0], 4), round(ground[1] + offset[1], 4)),
                ],
            )
        )

    # Every endpoint goes through `end`, which returns the far end of a pin's
    # escape when it has one and the pad itself when it does not.
    tracks = [*escapes, *anchored]
    routes = [
        ("+3V3", POWER, [("J1.1", "C1.1"), ("C1.1", "U3.1"), ("C1.1", "C2.1"), ("C2.1", "U3.3")]),
        (
            "+3V3",
            SIG,
            [
                ("C2.1", "R1.1"),
                ("R1.1", "R2.1"),
                ("R2.1", "U1.1"),
                ("U1.22", "C6.1"),
                ("C6.1", "U1.33"),
                ("U1.33", "C7.1"),
                ("C7.1", "U1.24"),
                ("C7.1", "C10.1"),
                ("C10.1", "U2.20"),
                ("C10.1", "C11.1"),
                ("C11.1", "U2.8"),
                ("C11.1", "C16.1"),
                ("C16.1", "U2.1"),
                ("C10.1", "U2.17"),
                ("R2.1", "C8.1"),
                # ...and this is what joins the input side to the bank supplies.
                # Without it +3V3 is two islands that the schematic calls one net.
                ("C8.1", "U1.22"),
                ("C8.1", "U4.8"),
                ("C8.1", "U4.3"),
                ("U4.3", "U4.7"),
                ("C8.1", "C9.1"),
                ("C9.1", "X1.4"),
                ("C9.1", "X1.1"),
            ],
        ),
        (
            "+1V2",
            SIG,
            [
                ("U3.5", "C3.1"),
                ("C3.1", "C4.1"),
                ("C4.1", "U1.5"),
                ("C4.1", "C15.1"),
                ("C15.1", "U1.30"),
                ("C15.1", "R3.1"),
            ],
        ),
        ("VCCPLL", SIG, [("R3.2", "C17.1"), ("C17.1", "C5.1"), ("C5.1", "U1.29")]),
        ("SPI_SS", SIG, [("U1.16", "U4.1"), ("U4.1", "J3.1"), ("J3.1", "R4.2")]),
        ("+3V3", SIG, [("C8.1", "R4.1")]),
        ("SPI_SCK", SIG, [("U1.15", "U4.6"), ("U4.6", "J3.2")]),
        ("SPI_SI", SIG, [("U1.17", "U4.5"), ("U4.5", "J3.3")]),
        ("SPI_SO", SIG, [("U1.14", "U4.2"), ("U4.2", "J3.4")]),
        ("CRESET", SIG, [("U1.8", "R1.2"), ("R1.2", "J3.5")]),
        ("CDONE", SIG, [("U1.7", "R2.2")]),
        ("CLK12", SIG, [("X1.3", "U1.37")]),
        ("I2S_SCK", SIG, [("U1.36", "U2.12")]),
        ("I2S_BCK", SIG, [("U1.35", "U2.13")]),
        ("I2S_DIN", SIG, [("U1.34", "U2.14")]),
        ("I2S_LRCK", SIG, [("U1.32", "U2.15")]),
        ("OUTL", SIG, [("U2.6", "J2.3")]),
        ("OUTR", SIG, [("U2.7", "J2.1")]),
        ("LDOO", SIG, [("U2.18", "C12.1")]),
        ("CAPP", SIG, [("U2.2", "C13.1")]),
        ("CAPM", SIG, [("U2.4", "C13.2")]),
        ("VNEG", SIG, [("U2.5", "C14.1")]),
    ]
    for net, width, pairs in routes:
        for a, b in pairs:
            tracks.append(Track(net, "F.Cu", width, [end(a), end(b)], auto=True))

    for pad, target in (
        ("J1.2", (12.0, 12.0)),
        ("U3.2", (16.0, 27.0)),
        ("C4.2", (22.5, 36.0)),
        ("C5.2", (56.0, 40.8)),
        ("C17.2", (59.0, 40.8)),
        ("C6.2", (59.0, 43.5)),
        ("C7.2", (64.0, 46.0)),
        ("C8.2", (46.0, 71.0)),
        ("X1.2", (30.0, 10.0)),
        ("U4.4", (37.0, 75.0)),
        # The codec's grounds - two real ones and three mode pins strapped low -
        # drop through beside their own escapes rather than walking west into a
        # corridor that four other nets are already using.
        ("U2.19", (62.5, 43.5)),
        ("U2.11", (62.5, 35.5)),
        ("U2.16", (62.5, 40.5)),
        ("U2.10", (82.0, 33.5)),
        ("U2.9", (82.0, 37.5)),
        ("U2.3", (82.0, 42.5)),
        ("C16.2", (84.0, 52.0)),
        ("C12.2", (60.0, 53.0)),
        ("C14.2", (87.0, 47.0)),
        ("J2.2", (89.5, 45.5)),
        ("J3.6", (78.0, 70.0)),
    ):
        tracks.append(Track("GND", "F.Cu", 0.4, [end(pad), target], auto=True, goal_layer="B.Cu"))
    return replace(design, tracks=tracks, vias=vias)


DESIGNS = {
    "buck-5v": buck_5v,
    "motor-driver": motor_driver,
    "pico-carrier": pico_carrier,
    "opamp-filter": opamp_filter,
    "fpga-audio": fpga_audio,
}


# ---------------------------------------------------------------------------


def write_variant(design: Design, root: Path, *, check: bool = True) -> None:
    shorts = schematic_shorts(design) if check else []
    if shorts:
        raise SystemExit(
            f"{design.name}: the sheet shorts {len(shorts)} pair(s):\n  " + "\n  ".join(shorts)
        )
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
        # Routed once, then degraded: the copper stays where the router put it
        # and the parts move out from under it, which is what a generator that
        # never looked at its own output leaves behind - and is also the
        # difference between a minute and half an hour on the fine-pitch board.
        design = resolve_routes(
            replace(builder().snapped(), provenance=stamp, date=args.generated_on)
        )
        write_variant(design, out / name / "reviewed")
        # the degraded variant is *meant* to be wrong, so it is not checked
        write_variant(degrade(design), out / name / "as-generated", check=False)
        print(f"{name}: wrote as-generated/ and reviewed/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

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
import hashlib
import inspect
import json
import math
import re
import sys
import uuid
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from itertools import combinations, pairwise
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import autoroute

from eda_toolkit.kicad import pcb_review
from eda_toolkit.kicad import s_expression as sexp
from eda_toolkit.kicad.pcb_review import _shortest_clear
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
GENERATED_ON = "2026-09-05"
GENERATED_BY = "Claude Code"

GEOM_EPS = 1e-6
GEOM_TOL = 0.001  # two points this close on the sheet are the same point
VIA_SIZE = 0.8  # what the router drops when it has to change layer
# How near two drilled holes may be, centre to centre. KiCad's own constraint
# on these boards is 0.2495 mm between the barrels, which for the 0.4 mm drills
# here is 0.65 mm between centres; the round number above it is what the
# generator places to.
HOLE_TO_HOLE_MM = 0.7
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
    # Reviewed exceptions to automatic connector legend placement, in board
    # coordinates: pin number -> (x, y, justification). Does not move copper.
    pin_legend_at: dict[str, tuple[float, float, str]] = field(default_factory=dict)
    # Whether this connector gets the automatic per-pin net legend. The legend
    # is reverse-connection insurance and worth its ink on a header a builder
    # wires by hand. On a connector whose pinout the standard fixes - a USB
    # receptacle, an SD socket - it names nothing the assembler can act on and
    # costs a dozen strings of silk beside the board edge, so a design may
    # decline it. Declining is per part, so the headers on the same board keep
    # theirs - and it means "the standard names the pins", not "this connector
    # goes unmarked": `silk_label` then has to say what the connector is. See
    # `__post_init__`.
    pin_legend: bool = True
    # Whether to print the symbol's value on the sheet. A fiducial's value is
    # the word "Fiducial" and a screw hole's is its thread: nothing a reader
    # needs, and one more string to collide with a wire. The libraries leave
    # both visible, so this is the design's call rather than theirs.
    show_value: bool = True
    # Whether the designator prints on the board's silkscreen. A fiducial's
    # does not: it names a target the assembly machine finds optically, nobody
    # reads it on a bare board, and on a small board it competes for the same
    # edge strip the board's own name needs.
    show_reference: bool = True
    # Which unit of a multi-unit symbol this is. A design lists the same
    # reference once per unit, each with its own place on the sheet; the board
    # only ever sees the first of them, because there is one footprint.
    unit: int = 1

    def __post_init__(self) -> None:
        # Declining the per-pin legend says the standard names the pins. It
        # does not say the connector goes unmarked: a sixteen-pad receptacle
        # with nothing beside it is harder to read than one with a pinout, not
        # easier, and `silk.unlabeled_connector` reports exactly that - a
        # finding the ai-generated policy promotes to an error, so a design
        # that declined the legend and printed nothing would fail its own gate.
        # So the flag requires the label that takes the legend's place.
        if not self.pin_legend and not self.silk_label:
            raise ValueError(
                f"{self.ref}: pin_legend=False needs a silk_label saying what the "
                "connector is - a standard fixes its pinout, not its identity"
            )

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
    # Some back-layer runs are a deliberate part of the floorplan rather than
    # leftovers from the maze router.  Keep those runs on the requested layer
    # when the final clean-up pass looks for short hops it can surface.
    keep_layer: bool = False


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


# What a screw actually takes up where it meets the board, and what a
# connector takes up above it. The review rules read the same numbers from
# their own THRESHOLDS; these are the generator's copy, and the examples are
# what keeps the two honest.
# How far a designator stays from the board edge: the second is the floor a
# fab needs, the first is the room a placement is asked for before it settles
# for the floor.
# KiCad's stroke font is proportional: 0.94 to 1.07 of the text size per
# character, measured on written boards. The placement takes the top of that
# range, and the line box is 1.55 of the size tall.
SILK_CHAR_ADVANCE = 1.07
SILK_LINE_HEIGHT = 1.55
# The gap the fab wants between ink and a mask opening or other ink.
SILK_CLEARANCE = 0.2

SILK_EDGE_ROOM = 1.0
SILK_EDGE_MARGIN = 0.5

# The generator's own silk graphics: the frame round a relocated pin legend and
# the leader that ties it back to its pad. 0.12 is what KiCad's own libraries
# draw their outlines at.
SILK_LINE_WIDTH = 0.12
# The air between a framed label's text and its frame.
SILK_FRAME_ROOM = 0.3
# How finely a leader is sampled when asking whether it crosses anything, and
# how long it has to be drawn before it reads as a pointer rather than a tick.
SILK_LEADER_STEP = 0.3
SILK_LEADER_MIN = 1.5
# How many of a connector's legends have to sit at one offset from their own
# pins before the row is a column a person reads by position rather than by
# proximity. Two names are two names; three in a line are a pinout, and taking
# one of them out of the line to point at its pin makes the pinout worse.
LEGEND_COLUMN_MIN = 3

# When a surface pad is a heat sink rather than a land: the same two numbers
# the review rules use to tell a thermal pad from a chip part.
RELIEF_PAD_AREA_MM2 = 4.0
RELIEF_PAD_SIDE_MM = 2.0

FASTENER_HEAD = 7.0
FASTENER_GAP = 0.5
CONNECTOR_ACCESS = 2.0


@dataclass
class Mounting:
    """What the board asks for in the way of screw holes.

    ``grounded`` is the decision the enclosure makes, not the board: a hole
    whose pad carries ground bonds the chassis to the circuit's reference,
    which is what you want when the enclosure is metal and part of the shield,
    and a ground loop you did not ask for when it is not. Plain holes by
    default, and a design that wants the bond says so.
    """

    count: int = 4
    grounded: bool = False
    # M3 clearance: a 3.2 mm drill, and the steel that arrives with the screw.
    # ``keep`` is what the *fastener* occupies, not what the footprint draws: a
    # pan head on a DIN 125 washer is 7 mm across, so nothing else - part body,
    # track, board edge - may come within half of that plus a little air.
    drill: float = 3.2
    keep: float = FASTENER_HEAD / 2 + FASTENER_GAP


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
    # The router uses the two outer signal layers. Fine-pitch designs may add
    # two uninterrupted inner planes without changing that routing model.
    copper_layers: int = 2
    power_plane: str | None = None
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
    # Footprints whose whole courtyard is closed to copper of any net but their
    # own. `route_keepout` fences the strip *between* two rows of pads, which a
    # single-row part does not have: a 1xN header's body is one column of pads
    # and the board either side of it, and a rail crossing it is exactly the
    # `route.under_package` a connector reports - under the shell, where nobody
    # can probe it and the housing has to come off to see it. The part's own
    # escapes still leave, which is why this is not a plain rectangle in
    # `keepouts`.
    body_keepout: tuple[str, ...] = ()
    # Nets the rest of the board is routed around, beyond the ones the widths
    # already say. A track wider than the board's thinnest carries current and
    # has first pick by that alone; a clock, a bus that must arrive together,
    # a pair that has to stay a pair, carry nothing extra and have to be named.
    # See `_route_rank`: a net in this class is routed first and is never moved
    # behind a plain one to make room, so the plain one goes round instead.
    priority_nets: tuple[str, ...] = ()
    # Rectangles of board, in board coordinates, closed to the router on both
    # faces. A part at the edge of a board leaves a strip behind it that is
    # routable and never the right answer: a search that finds it comes at the
    # part's pads from the side nothing arrives from, and crosses the plane to
    # do it. Saying the strip is not for routing is how a layout states which
    # side a connector is approached from.
    keepouts: tuple[tuple[float, float, float, float], ...] = ()
    # Holes for the screws that hold the board in whatever it goes into. The
    # value is what the design asks for: how many, and whether their pads carry
    # ground. `mount_holes` materialises them into parts near the corners,
    # because where they can go is a question about the finished layout rather
    # than something worth typing four coordinates for.
    mounting: Mounting | None = None
    # Where the first hole's symbol goes on the sheet; the rest follow it in a
    # row. They carry no net unless the holes are grounded, so anywhere the
    # drawing has room will do.
    mounting_sheet: tuple[float, float] | None = None
    # How many fiducials the board carries for the assembly machine to align
    # to. Three is the usual answer on an SMD board; nought says the board is
    # hand-built and the machine is not coming.
    fiducials: int = 0
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


def hides_value(part: Part) -> bool:
    """Whether the library draws this symbol's value hidden.

    A fiducial's value is the word "Fiducial" and a mounting hole's is its
    thread: nothing a reader of the sheet needs, which is why the libraries
    hide both. Printing them anyway is one more string to collide with a wire,
    and that is what it did.
    """
    node = symbol_definition(part.lib_id)
    for prop in node.children("property"):
        if str(prop.atom(0, "")) != "Value":
            continue
        if prop.child("hide") is not None:
            return True
        effects = prop.child("effects")
        return effects is not None and effects.child("hide") is not None
    return False


def in_bom(footprint: str) -> bool:
    """Whether this part is a line on the bill of materials.

    Asked of the *footprint*, because that is the side KiCad's parity check
    reads and the side the fab agrees with: a test point, a mounting hole and
    a fiducial are all real copper with nothing to buy, and their library
    footprints say `exclude_from_bom`. The symbol libraries are less
    consistent - `Connector:TestPoint` claims to be a BOM line - and following
    them is how three test points came to disagree with their own footprints
    on every board that has them.
    """
    node = footprint_definition(footprint)
    attr = node.child("attr")
    return attr is None or "exclude_from_bom" not in [str(a) for a in attr.atoms()]


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


def symbol_body(lib_id: str, unit: int | None = None) -> list[tuple[float, float]]:
    """The corners of the shape the library draws, in library coordinates.

    Pins are a poor stand-in for it. An LED's two pins span 2.54 mm and its
    emission arrows reach 4.6 mm off to one side; a value cleared of the pins
    still prints through the part, which is exactly what the plot showed.
    """
    corners: list[tuple[float, float]] = []
    for sub in symbol_definition(lib_id).children("symbol"):
        name = str(sub.atom(0, ""))
        parts = name.rsplit("_", 2)
        this = int(parts[1]) if len(parts) == 3 and parts[1].isdigit() else 0
        if unit is not None and this not in (unit, 0):
            continue
        for poly in sub.children("polyline"):
            pts = poly.child("pts")
            for xy in pts.children("xy") if pts else []:
                atoms = xy.atoms()
                if len(atoms) >= 2:
                    corners.append((float(atoms[0]), float(atoms[1])))
        for rect in sub.children("rectangle"):
            start, end = rect.child("start"), rect.child("end")
            if start is None or end is None:
                continue
            sx, sy = (float(a) for a in start.atoms()[:2])
            ex, ey = (float(a) for a in end.atoms()[:2])
            corners.extend([(sx, sy), (ex, sy), (ex, ey), (sx, ey)])
        for circle in sub.children("circle"):
            centre = circle.child("center")
            if centre is None:
                continue
            cx, cy = (float(a) for a in centre.atoms()[:2])
            r = float(circle.value("radius", default=0.0) or 0.0)
            corners.extend([(cx - r, cy - r), (cx + r, cy + r)])
        for arc in sub.children("arc"):
            for tag in ("start", "mid", "end"):
                node = arc.child(tag)
                if node is not None:
                    atoms = node.atoms()
                    corners.append((float(atoms[0]), float(atoms[1])))
    return corners


def body_box(part: Part) -> tuple[float, float, float, float] | None:
    """Where on the sheet the part is actually drawn, pins and graphics both."""
    pts = [pin_geometry(part, pin)[0] for pin in symbol_pins(part.lib_id, part.unit)]
    sx, sy = part.sheet
    pts += [
        transform_pin(cx, cy, sx, sy, part.angle, part.mirror)
        for cx, cy in symbol_body(part.lib_id, part.unit)
    ]
    if not pts:
        return None
    return (
        min(p[0] for p in pts),
        min(p[1] for p in pts),
        max(p[0] for p in pts),
        max(p[1] for p in pts),
    )


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
    child_indent = f"{indent}  "
    body = [f'{child_indent}({name} "{value}")' for name, value in fields if value]
    body += [
        f'{child_indent}(comment {number} "{text}")'
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
        else:
            # A symbol with no pins - a screw hole, a fiducial - draws no stub,
            # so a planner that builds its obstacles from stubs cannot see it
            # and runs its wires straight through the drawing. It occupies
            # board the same as anything else.
            boxes.append(
                (
                    part.sheet[0] - 3.81,
                    part.sheet[1] - 6.35,
                    part.sheet[0] + 3.81,
                    part.sheet[1] + 10.16,
                )
            )
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


def _plan_wires(
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


WIRE_CACHE = Path(__file__).resolve().parent.parent / ".cache" / "wires"
_wire_memo: dict[str, tuple] = {}


def _wire_digest(design: Design) -> str:
    """A key over everything the wire planner reads.

    Same argument as `_routing_digest`, and the same shape: on the FPGA sheet
    the planner spends six minutes, it is asked twice per variant, and most of
    what gets edited here does not move a pin.
    """
    lines = [
        design.name,
        repr((design.paper, design.draw_wires, GRID, WIRE_CLEAR)),
        repr(design.label_nets),
        repr(design.wired_power),
        repr(design.power_flags),
        repr([block[0] for block in design.note_blocks]),
        repr(design.notes),
    ]
    for part in design.parts:
        lines.append(
            f"P {part.ref}|{part.lib_id}|{part.unit}|{part.sheet}|{part.angle}|{part.mirror}"
        )
    for net, nodes in sorted(design.nets.items()):
        lines.append(f"N {net}={','.join(sorted(nodes))}")
    lines.append(inspect.getsource(_plan_wires))
    lines.append(inspect.getsource(power_fixtures))
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:32]


def plan_wires(design: Design):
    """The wire tree for this sheet, computed once and kept.

    The planner is the most expensive thing in this generator after the
    router, it is asked for the same answer by the shorts check and by the
    emitter, and the answer only changes when a pin moves. So it is memoised
    in the process and cached on disk, keyed by everything it reads - see
    `_wire_digest`.
    """
    if not design.draw_wires:
        return [], set(), []
    digest = _wire_digest(design)
    if digest in _wire_memo:
        return _wire_memo[digest]
    path = WIRE_CACHE / f"{design.name}.{digest}.json"
    try:
        blob = json.loads(path.read_text())
        found = (
            [(net, tuple(a), tuple(b)) for net, a, b in blob["segments"]],
            set(blob["dropped"]),
            [tuple(point) for point in blob["junctions"]],
        )
    except (OSError, ValueError, KeyError):
        found = _plan_wires(design)
        try:
            WIRE_CACHE.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {
                        "segments": [[net, list(a), list(b)] for net, a, b in found[0]],
                        "dropped": sorted(found[1]),
                        "junctions": [list(point) for point in found[2]],
                    }
                )
            )
        except OSError:
            pass
    _wire_memo[digest] = found
    return found


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

    # Bucketed on a coarse grid: only wires that share a cell can touch, and
    # asking every pair on a sheet with a thousand of them costs minutes.
    CELL = 12.7
    cells: dict[tuple[int, int], list[int]] = defaultdict(list)
    for index, (_net, _owner, a0, a1) in enumerate(wires):
        for cx in range(int(min(a0[0], a1[0]) // CELL), int(max(a0[0], a1[0]) // CELL) + 1):
            for cy in range(int(min(a0[1], a1[1]) // CELL), int(max(a0[1], a1[1]) // CELL) + 1):
                cells[(cx, cy)].append(index)

    problems = []
    for index, (net, owner, a0, a1) in enumerate(wires):
        nearby: set[int] = set()
        for cx in range(int(min(a0[0], a1[0]) // CELL) - 1, int(max(a0[0], a1[0]) // CELL) + 2):
            for cy in range(int(min(a0[1], a1[1]) // CELL) - 1, int(max(a0[1], a1[1]) // CELL) + 2):
                nearby.update(cells.get((cx, cy), ()))
        for other_index in sorted(nearby):
            if other_index <= index:
                continue
            other, other_owner, b0, b1 = wires[other_index]
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
        for a, b in (
            [(a, b) for _n, a, b in segments]
            + [(a, b) for _t, _n, a, b in fixtures[0]]
            # The stub each pin brings out is as much a net as a planned run,
            # and it is the one nearest the fields - a designator sitting above
            # a part sits on the stub leaving its top pin. Collected before any
            # symbol is written so every symbol sees every stub, not only the
            # ones belonging to parts placed before it.
            + [
                pin_geometry(part, pin)
                for part in design.parts
                for pin in symbol_pins(part.lib_id, part.unit)
                if net_of.get((part.ref, pin.number)) is not None
            ]
        )
    ]
    placed_blocks: list[tuple[float, float, float, float]] = fixture_boxes
    # Every string already committed to the page: the net names beside the
    # pins, and the rail and flag names on the power fixtures. A symbol's own
    # fields are added as it is written, so each one measures against
    # everything placed before it as well as against these.
    body_boxes = _body_boxes(design)
    label_at, label_boxes = _plan_labels(design, net_of, replaced)
    text_boxes: list[tuple[float, float, float, float]] = list(label_boxes)
    wire_index, text_index = _BoxIndex(wire_boxes), _BoxIndex(text_boxes)

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
                placement = label_at.get(out) or _label_options(out, end)[0]
                body.append(_label(design, net, part.ref, pin.number, out, placement))
        avoid = [box for ref, box in part_box.items() if ref != part.ref] + placed_blocks
        body.append(_symbol_instance(design, part, pins, avoid, wire_index, text_index))
        if len(avoid) > len(part_box) - 1 + len(placed_blocks):
            placed_blocks.append(avoid[-1])

    # The power hookups: jog and bus wires, upright symbols, flags - all from
    # the one computation the planner and the shorts check also read.
    fixture_wires, fixture_symbols, fixture_flags, fixture_junctions, _boxes = fixtures
    for tag, _fnet, a, b in fixture_wires:
        body.append(_wire(design, "pwr", tag, a, b))
    for power_index, (net, at) in enumerate(fixture_symbols, start=1):
        body.append(
            _power_symbol(
                design,
                POWER_SYMBOLS[net],
                net,
                at,
                power_index,
                wire_boxes,
                body_boxes,
                text_index,
            )
        )
    for flag_index, (net, at) in enumerate(fixture_flags, start=1):
        body.append(_power_flag(design, net, flag_index, at, wire_boxes, body_boxes, text_index))
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

    # What a note must not print over. The title block is on the list because
    # it is printed text like any other, and a sentence that runs into it is
    # the one collision a reader sees before they see the circuit.
    page_w, page_h = {"A4": (297.0, 210.0), "A3": (420.0, 297.0)}.get(design.paper, (297.0, 210.0))
    # The title block is 120 mm wide and 44 mm tall on the sheets KiCad draws
    # with this many comment rows; the numbers are rounded outwards, because a
    # note that stops one millimetre short of it still reads.
    note_avoid = [
        body_boxes,
        text_index,
        wire_index,
        [(page_w - 125.0, page_h - 46.0, page_w + 12.0, page_h + 12.0)],
    ]

    if design.notes:
        # Below the circuit, not beside it. Started at the top of the sheet the
        # notes ran straight through the input section - which no rule catches,
        # because nothing about it changes the netlist. It is only visible by
        # looking at the plot, which is why the plot is in the documentation.
        first = (design.notes_at[0], design.notes_at[1] + 5.08)
        dx, dy = _place_note(design.notes, first, 5.08, note_avoid)
        text_index.add(_note_box(design.notes, first[0] + dx, first[1] + dy, 5.08))
        for index, note in enumerate(design.notes, start=1):
            y = design.notes_at[1] + index * 5.08 + dy
            escaped = note.replace('"', '\\"')
            body.append(
                f'  (text "{escaped}" (at {round(design.notes_at[0] + dx, 2)} {round(y, 2)} 0) '
                f"{_effects(justify='left top')} "
                f'(uuid "{stable_uuid(design.name, "note", index)}"))'
            )
    # Anchored notes: each block sits beside the circuit it explains, so the
    # reader never has to carry a sentence across the sheet to its subject.
    for bindex, (at, block) in enumerate(design.note_blocks, start=1):
        dx, dy = _place_note(block, at, 4.0, note_avoid)
        text_index.add(_note_box(block, at[0] + dx, at[1] + dy, 4.0))
        for lindex, line in enumerate(block):
            y = at[1] + lindex * 4.0 + dy
            escaped = line.replace('"', '\\"')
            body.append(
                f'  (text "{escaped}" (at {round(at[0] + dx, 2)} {round(y, 2)} 0) '
                f"{_effects(justify='left top')} "
                f'(uuid "{stable_uuid(design.name, "noteblock", bindex, lindex)}"))'
            )

    lines += body
    lines += ["  (sheet_instances", '    (path "/" (page "1"))', "  )", ")"]
    return "\n".join(lines) + "\n"


def _note_box(lines: list[str], x: float, y: float, pitch: float):
    """The block a run of note lines covers, as `sch_review._text_extent` sees it.

    Notes are anchored `left top`, one `(text ...)` item per line, so the
    block is as wide as its longest line and as tall as one row per line.
    """
    widest = max((len(line) for line in lines), default=0)
    return (x, y, x + widest * 1.1, y + (len(lines) - 1) * pitch + 2.54)


# How far a block of notes may be slid to find clear paper, ordered nearest
# first so a note with room keeps the spot it was given. Two centimetres is
# about the limit of "beside the circuit it explains", which is the whole
# reason it is where it is; the ordering means the reach is only used when the
# near paper is full.
_NOTE_SHIFTS = sorted(
    {
        (round(dx, 4), round(dy, 4))
        for dx in (0.0, *(sign * n * 2.54 for n in range(1, 9) for sign in (1, -1)))
        for dy in (0.0, *(sign * n * 2.54 for n in range(1, 9) for sign in (1, -1)))
    },
    key=lambda d: (abs(d[0]) + abs(d[1]), abs(d[0]), d[1] < 0, d[0] < 0),
)


def _place_note(lines: list[str], at, pitch: float, obstacles) -> tuple[float, float]:
    """Slide a block of notes to the nearest paper nothing else is printed on.

    Of the three kinds of string on a sheet a note is the one with room to
    move: a field is anchored to its part and a label to its wire, but a
    sentence explaining the circuit only has to be near it. So the notes go
    last and they are the ones that give way.
    """
    scored = []
    for rank, (dx, dy) in enumerate(_NOTE_SHIFTS):
        box = _note_box(lines, at[0] + dx, at[1] + dy, pitch)
        scored.append((sum(_hits(group, box) for group in obstacles), rank, dx, dy))
    scored.sort(key=lambda item: item[:2])
    return scored[0][2], scored[0][3]


def _wire(design: Design, ref: str, number: str, a, b) -> str:
    return (
        f"  (wire (pts (xy {a[0]} {a[1]}) (xy {b[0]} {b[1]})) "
        f"(stroke (width 0) (type default)) "
        f'(uuid "{stable_uuid(design.name, "wire", ref, number)}"))'
    )


def _turned_box(box, x: float, y: float, angle: float):
    """The box a string covers once the sheet has turned it 90 degrees.

    The same arithmetic as `sch_review._turned`, for the same reason as
    `_text_box`: text is being placed so that rule finds nothing, so both
    sides have to be looking at the same rectangle.
    """
    if round(abs(angle) % 180) != 90:
        return box
    dx0, dy0, dx1, dy1 = box[0] - x, box[1] - y, box[2] - x, box[3] - y
    return (x + dy0, y - dx1, x + dy1, y - dx0)


def _label_box(text: str, at, angle: float, justify: str):
    return _turned_box(_text_box(text, at[0], at[1], justify), at[0], at[1], angle)


def _label_options(at, end) -> list[tuple[float, str]]:
    """Where a net name at a stub tip may read, best first.

    Away from the pin is the first choice: a name printed back over its own
    stub runs into the pin number beside it. But away is only a preference.
    A five-character name on a 2.54 mm stub is twice as long as the stub, so
    "away" regularly means straight into whatever part is next along the row,
    and a name drawn through a diode costs more than a name drawn beside its
    own pin. The other three quarters are listed so the picker can take one.
    """
    if abs(at[0] - end[0]) < GEOM_EPS:
        away = "left bottom" if at[1] < end[1] else "right bottom"
        back = "right bottom" if away == "left bottom" else "left bottom"
        return [(90, away), (0, "left bottom"), (0, "right bottom"), (90, back)]
    away = "left bottom" if at[0] > end[0] else "right bottom"
    back = "right bottom" if away == "left bottom" else "left bottom"
    return [(0, away), (90, "left bottom"), (90, "right bottom"), (0, back)]


def _body_boxes(design: Design) -> list[tuple[float, float, float, float]]:
    """Every symbol as `rule_text_over_text` sees it: the shape KiCad draws.

    The review rule reads the same outline out of the schematic's own
    `lib_symbols`, so both sides are looking at the same rectangle - which is
    the only way placing text to satisfy the rule can be honest.
    """
    boxes = []
    for part in design.parts:
        box = body_box(part)
        if box is not None:
            boxes.append(box)
    return boxes


def _plan_labels(design: Design, net_of, replaced):
    """Pick a reading direction for every net name before any of them is drawn.

    The anchor is not negotiable - a label joins the net by sitting on the
    wire, so moving it moves the connection - but the direction it reads in
    is. So each name is offered the four quarters of its anchor and takes the
    first that prints over nothing, measured against the symbol bodies the
    review rule measures and against the names already placed.

    Returns the placement per stub tip and the boxes they ended up covering,
    so the symbol fields placed afterwards can miss them too.
    """
    bodies = _body_boxes(design)

    placement: dict[tuple[float, float], tuple[float, str]] = {}
    boxes: list[tuple[float, float, float, float]] = []
    placed = _BoxIndex()
    for part in design.parts:
        drawn: set[tuple[float, float]] = set()
        for pin in symbol_pins(part.lib_id, part.unit):
            net = net_of.get((part.ref, pin.number))
            if net is None or (net in POWER_SYMBOLS and net not in design.wired_power):
                continue
            if f"{part.ref}.{pin.number}" in replaced:
                continue
            end, out = pin_geometry(part, pin)
            if out in placement or end in drawn:
                continue
            drawn.add(end)
            scored = []
            for rank, (angle, justify) in enumerate(_label_options(out, end)):
                box = _label_box(net, out, angle, justify)
                scored.append((placed.hits(box) + _hits(bodies, box), rank, angle, justify, box))
            scored.sort(key=lambda item: item[:2])
            _cost, _rank, angle, justify, box = scored[0]
            placement[out] = (angle, justify)
            boxes.append(box)
            placed.add(box)
    return placement, boxes


def _label(design: Design, net: str, ref: str, number: str, at, placement) -> str:
    angle, justify = placement
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


def _power_symbol(
    design: Design,
    lib_id: str,
    net: str,
    at,
    index: int,
    wires: list[tuple[float, float, float, float]] | None = None,
    avoid: list[tuple[float, float, float, float]] | None = None,
    texts: _BoxIndex | None = None,
) -> str:
    """A ground or rail symbol, upright.

    Rails point up, grounds hang down - the one orientation every reader
    assumes. The wires bend to make that true (see `_power_geometry`); the
    symbol itself never turns.

    Its name does move. Four grounds hanging off one row of pins put four
    "GND"s on one line a pin pitch apart, which prints as GNGNGNGND; the name
    is offered the row below and either side of the stem before it settles.
    """
    ref = f"#PWR{index:02d}"
    uid = stable_uuid(design.name, "power", index)
    root = stable_uuid(design.name, "sheet")
    down = 1.0 if net == "GND" else -1.0
    label_x, label_y, label_just = _pick_field(
        net,
        [
            (at[0], round(at[1] + down * 3.81, 4), ""),
            (round(at[0] + 2.54, 4), round(at[1] + down * 3.81, 4), "left"),
            (round(at[0] - 2.54, 4), round(at[1] + down * 3.81, 4), "right"),
            (at[0], round(at[1] + down * 5.72, 4), ""),
            (round(at[0] + 2.54, 4), round(at[1] + down * 5.72, 4), "left"),
            (round(at[0] - 2.54, 4), round(at[1] + down * 5.72, 4), "right"),
            (at[0], round(at[1] + down * 7.62, 4), ""),
        ],
        wires,
        avoid,
        texts,
    )
    if texts is not None:
        texts.add(_text_box(net, label_x, label_y, label_just))
    return "\n".join(
        [
            f'  (symbol (lib_id "{lib_id}") (at {at[0]} {at[1]} 0) (unit 1)',
            "    (exclude_from_sim no) (in_bom no) (on_board yes) (dnp no)",
            f'    (uuid "{uid}")',
            _property("Reference", ref, at[0], at[1], True),
            _property("Value", net, label_x, label_y, False, label_just),
            _property("Footprint", "", at[0], at[1], True),
            _property("Datasheet", "", at[0], at[1], True),
            f'    (pin "1" (uuid "{uid}-p"))',
            f'    (instances (project "{design.name}" '
            f'(path "/{root}" (reference "{ref}") (unit 1))))',
            "  )",
        ]
    )


def _text_box(text: str, x: float, y: float, justify: str) -> tuple[float, float, float, float]:
    """The box a field covers, measured the way the review rule measures it.

    Deliberately the same arithmetic as `sch_review._field_extent`: the
    generator is placing text so that rule finds nothing, so it has to be
    looking at the same rectangle the rule will look at, not a near miss.
    """
    width = len(text) * 1.4
    height = 1.9
    if "right" in justify:
        x0, x1 = x - width, x
    elif "left" in justify:
        x0, x1 = x, x + width
    else:
        x0, x1 = x - width / 2, x + width / 2
    return (x0, y - height / 2, x1, y + height / 2)


class _BoxIndex:
    """Boxes on a coarse grid, so "how many of these does this one touch" is a
    question about the neighbourhood rather than about the whole sheet.

    Placing one field asks it a hundred times, a sheet holds two hundred
    fields, and a dense one holds a thousand wires to miss. Done the obvious
    way that is six minutes of arithmetic per sheet, which was most of what
    regenerating the FPGA example cost.
    """

    CELL = 12.7

    def __init__(self, boxes=()) -> None:
        self.boxes: list[tuple[float, float, float, float]] = []
        self.cells: dict[tuple[int, int], list[int]] = defaultdict(list)
        for box in boxes:
            self.add(box)

    def add(self, box: tuple[float, float, float, float]) -> None:
        index = len(self.boxes)
        self.boxes.append(box)
        for cell in self._cells(box):
            self.cells[cell].append(index)

    def _cells(self, box):
        for cx in range(int(box[0] // self.CELL), int(box[2] // self.CELL) + 1):
            for cy in range(int(box[1] // self.CELL), int(box[3] // self.CELL) + 1):
                yield (cx, cy)

    def hits(self, box) -> int:
        seen: set[int] = set()
        for cell in self._cells(box):
            seen.update(self.cells.get(cell, ()))
        found = 0
        for index in seen:
            b = self.boxes[index]
            if box[0] < b[2] and b[0] < box[2] and box[1] < b[3] and b[1] < box[3]:
                found += 1
        return found


def _hits(boxes, box) -> int:
    if isinstance(boxes, _BoxIndex):
        return boxes.hits(box)
    return sum(
        1
        for b in boxes or []
        if box[0] < b[2] and b[0] < box[2] and box[1] < b[3] and b[1] < box[3]
    )


def _pick_field(
    text: str,
    candidates: list[tuple[float, float, str]],
    wires: list[tuple[float, float, float, float]] | None,
    avoid: list[tuple[float, float, float, float]] | None = None,
    texts: list[tuple[float, float, float, float]] | _BoxIndex | None = None,
    extra: list[tuple[float, float, float, float] | None] | None = None,
) -> tuple[float, float, str]:
    """The first of `candidates` that prints over nothing, else the least bad.

    A designator or a value has no one right spot - above the part, beside it,
    a row further out are all readable. What is not readable is any of them
    printed through something else, and the somethings rank: text on text is
    two strings nobody can read, text on a net is one string nobody can read,
    and text a little close to a symbol body is still text. So the caller
    lists the spots it would accept in order of preference and this takes the
    first that is clear, breaking ties in that order.
    """
    # `extra` is text this symbol has already committed to but not yet handed
    # back - its own block, its own designator - which is not in the sheet's
    # index yet and still has to be missed.
    also = [box for box in (extra or []) if box]
    scored = []
    for cx, cy, justify in candidates:
        box = _text_box(text, cx, cy, justify)
        scored.append(
            (
                _hits(texts, box) + _hits(also, box),
                _hits(wires, box),
                _hits(avoid, box),
                (cx, cy, justify),
            )
        )
    scored.sort(key=lambda item: item[:3])
    return scored[0][3]


def _power_flag(
    design: Design,
    net: str,
    index: int,
    at: tuple[float, float],
    wires: list[tuple[float, float, float, float]] | None = None,
    avoid: list[tuple[float, float, float, float]] | None = None,
    texts: _BoxIndex | None = None,
) -> str:
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
    # Which side that row runs off to depends on what the flag is standing in;
    # the tap wire reaching it is often on one side only.
    # A ladder outwards rather than a short list: on the carrier the flag
    # stands beside a forty-pin module whose own pin legend fills the strip
    # next to it, and the nearest clear air is three text rows away. Ordered
    # by how far it strays from the graphic, so a flag with room beside it
    # still gets the row it always had.
    candidates = [
        (
            round(x + dx, 4),
            round(y - dy, 4),
            # The name reads *away* from the flag: to the left of it, right
            # justified. Written the other way round the two sides produce
            # almost the same box, so half the ladder was a duplicate of the
            # other half and the flag had half the room it looked like it had.
            "right" if dx < 0 else "left",
        )
        for dy in (6.35, 7.62, 10.16, 12.7, 15.24, 17.78, 20.32)
        for dx in (1.27, -1.27, 3.81, -3.81, 7.62, -7.62, 11.43, -11.43)
    ]
    vx, vy, vjust = _pick_field("PWR_FLAG", candidates, wires, avoid, texts)
    if texts is not None:
        texts.add(_text_box("PWR_FLAG", vx, vy, vjust))
    lines = [
        f'  (symbol (lib_id "power:PWR_FLAG") (at {x} {y} 0) (unit 1)',
        "    (exclude_from_sim no) (in_bom no) (on_board yes) (dnp no)",
        f'    (uuid "{uid}")',
        _property("Reference", ref, x, y, True),
        _property("Value", "PWR_FLAG", vx, vy, False, vjust),
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
    texts: list[tuple[float, float, float, float]] | None = None,
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
    # KiCad adds the symbol's rotation to the field's own angle, and where the
    # sum is half a turn it keeps the glyphs upright by swapping the
    # justification rather than printing them upside down. So a string the
    # placer decided should read to the *right* of its anchor has to be
    # written `justify right` on a part standing at 90 degrees. Without this
    # every rotated part's fields printed on the opposite side from the one
    # they were measured on - which is how an LED ended up with its ratings
    # drawn through its own arrows.
    flip = round((part.angle + text_angle) % 360) in (180, 270)

    def written(justify: str) -> str:
        if not flip:
            return justify
        return " ".join({"left": "right", "right": "left"}.get(w, w) for w in justify.split())

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
    nudge = 0.0
    block_offset = 3.81
    flat_dx, flat_dy = 0.0, 5.08
    block: tuple[float, float, float, float] | None = None
    rows_n = len(ratings) + 1 if side_value else len(ratings)
    # The symbol's own ground, from the shape the library actually draws. The
    # pin column is not it: a diode's emission arrows reach 4.6 mm past its
    # pins, and a block measured against the pins alone prints on them.
    drawn = body_box(part) or (
        min(p[0] for p in points),
        top,
        max(p[0] for p in points),
        bottom,
    )
    own = (drawn[0] - 0.5, min(drawn[1], top) - 1.0, drawn[2] + 0.5, max(drawn[3], bottom) + 1.0)
    # From here on "above the part" and "below the part" mean above and below
    # what KiCad draws. A regulator's rectangle stands 2.5 mm proud of its top
    # pin row, so a designator placed one row above the *pins* is a designator
    # printed on the box.
    top, bottom = min(top, drawn[1]), max(bottom, drawn[3])
    if upright and rows_n and avoid is not None:
        # 1.45 mm per glyph is the default font's real advance; undersizing it
        # here let two blocks land one millimetre apart and read as one word
        strings = [v for n, v in part.fields.items() if n in ratings]
        if side_value:
            strings.append(part.value)
        width = max(len(t) for t in strings) * 1.45 + 0.5
        y0 = y - (rows_n - 1) * 1.27 - 1.27
        y1 = y - (rows_n - 1) * 1.27 + (rows_n - 1) * 2.54 + 1.27

        def hits(x0, x1, shift, boxes):
            return _hits(boxes, (x0, y0 + shift, x1, y1 + shift))

        # Which side, how far out, and how far up or down: the block has to
        # miss the neighbouring symbols *and* every net, and on a dense sheet
        # only one of the twelve does. Nearest and to the right is the habit,
        # so the list is ordered that way and the first clear one wins.
        # The last two are the block clear of the symbol altogether, below and
        # above it - where a lying part already puts its ratings. A capacitor
        # boxed in by a wire either side has nowhere beside it to go, and the
        # answer is not to give up and print on one of them.
        shifts = (
            0.0,
            1.27,
            -1.27,
            2.54,
            -2.54,
            3.81,
            -3.81,
            5.08,
            -5.08,
            6.35,
            -6.35,
            round(bottom + 3.81 - y0, 4),
            round(top - 3.81 - y1, 4),
        )
        options = [
            (distance * direction, shift, 1.0 if direction > 0 else -1.0)
            for distance in (3.81, 5.08, 6.35, 7.62, 8.89)
            for direction in (1, -1)
            for shift in shifts
        ]
        scored = []
        for offset, shift, direction in options:
            span = (
                (x + offset, x + offset + width)
                if direction > 0
                else (x + offset - width, x + offset)
            )
            scored.append(
                (
                    hits(*span, shift, texts or []),
                    hits(*span, shift, wires or []),
                    hits(*span, shift, [*avoid, own]),
                    offset,
                    shift,
                    direction,
                    span,
                )
            )
        # Other people's text first, then nets, then symbol bodies. A block
        # printed a little close to a neighbour is still readable and the
        # margin rules will say so; a block with a net drawn through it is not
        # readable at all; and a block on another block is two of them lost.
        # The ordering above breaks ties by habit, so a sheet with room still
        # puts the block where it always was.
        scored.sort(key=lambda item: item[:3])
        _t, _w, _a, block_offset, nudge, direction, (x0, x1) = scored[0]
        side = direction
        justify = "left" if direction > 0 else "right"
        block = (x0, y0 + nudge, x1, y1 + nudge)
    elif rows_n and avoid is not None:
        # A lying part stacks its ratings under its value, centred - and used
        # to do so without looking. Under an oscillator sits its own ground
        # symbol, so "5.08 mm below the body" put "50ppm" straight through the
        # word GND. The question is the same one the upright branch asks
        # sideways: which row, and how far left or right, prints over nothing.
        width = max(len(v) for n, v in part.fields.items() if n in ratings) * 1.45 + 0.5
        options = [
            (dx, dy)
            for dy in (5.08, 7.62, 10.16, 12.7, 15.24)
            for dx in (0.0, 2.54, -2.54, 5.08, -5.08, 7.62, -7.62, 10.16, -10.16)
        ]
        scored = []
        for rank, (dx, dy) in enumerate(options):
            box = (
                x + dx - width / 2,
                bottom + dy - 1.27,
                x + dx + width / 2,
                bottom + dy + (rows_n - 1) * 2.54 + 1.27,
            )
            scored.append(
                (_hits(texts or [], box), _hits(wires or [], box), _hits(avoid, box), rank, dx, dy)
            )
        scored.sort(key=lambda item: item[:4])
        _t, _w, _a, _rank, flat_dx, flat_dy = scored[0]
        block = (
            x + flat_dx - width / 2,
            bottom + flat_dy - 1.27,
            x + flat_dx + width / 2,
            bottom + flat_dy + (rows_n - 1) * 2.54 + 1.27,
        )

    def row_at(index: int) -> tuple[float, float]:
        return (
            round(x + block_offset, 4),
            round(y - (rows_n - 1) * 1.27 + index * 2.54 + nudge, 4),
        )

    # The designator sits above the part, which is where the wire leaving its
    # top pin runs - so it prints on the net unless it steps aside. Off to the
    # side of the stub, on the same side the ratings took, keeps it clear of
    # both the wire and the symbol.
    if upright and small:
        near, far = ("left", "right") if side > 0 else ("right", "left")
        ref_options = [
            (round(x + side * 1.27, 4), round(top - 1.27, 4), near),
            (round(x - side * 1.27, 4), round(top - 1.27, 4), far),
            (round(x - side * 1.27, 4), round(top - 2.54, 4), far),
            (x, round(top - 2.54, 4), ""),
            (round(x + side * 1.27, 4), round(top - 2.54, 4), near),
            (x, round(top - 3.81, 4), ""),
        ]
        if block is not None:
            # A polarised capacitor's pins sit closer to its body than a plain
            # one's, so "one row above the top pin" can land inside the ratings
            # block. Above the block is above the block whatever the symbol is.
            above = round(block[1] - 1.27, 4)
            ref_options += [
                (x, above, ""),
                (round(x + side * 1.27, 4), above, near),
                (round(x - side * 1.27, 4), above, far),
            ]
        # And then outwards. A capacitor with a wire either side of its stub
        # has none of the six spots above it free, and a designator printed on
        # a net is worse than one two millimetres further out than habit.
        ref_options += [
            (round(x + sign * reach, 4), round(top - row, 4), "left" if sign > 0 else "right")
            for row in (1.27, 2.54, 3.81, 5.08)
            for reach in (2.54, 3.81, 5.08, 6.35, 8.89)
            for sign in (int(side), -int(side))
        ]
    else:
        # A wide part has no stub column to step out of, so the designator
        # stays centred above it and only steps up a row if that lands on a
        # net running across the top of the symbol.
        # A wide part's own pin stubs run up its centre column and out to
        # either side of it, so a designator that only steps 1.27 mm aside
        # steps onto the next stub. The list reaches out past the pin field.
        ref_options = [
            (x, round(top - 2.54, 4), ""),
            *(
                (round(x + sign * reach, 4), round(top - row, 4), "left" if sign > 0 else "right")
                for row in (2.54, 3.81)
                for reach in (1.27, 3.81, 6.35, 8.89)
                for sign in (1, -1)
            ),
            (x, round(top - 3.81, 4), ""),
        ]
    # The block is placed first and the designator gets out of *its* way: a
    # rating block has three strings that have to stay together and a
    # designator has one that can go anywhere legible.
    rx, ry, ref_justify = _pick_field(part.ref, ref_options, wires, avoid, texts, [block, own])
    ref_at: tuple[float, float] = (rx, ry)

    if side_value:
        value_prop = _property(
            "Value",
            part.value,
            *row_at(0),
            hides_value(part) or not part.show_value,
            written(justify),
            text_angle,
        )
    else:
        # Same argument as the designator: below the part is where the wire
        # leaving its bottom pin runs.
        # A multi-pin part usually brings a pin out of the middle of its
        # bottom edge - a ground pin, most often - and a centred value lands
        # on that pin's own stub. Left-justified from clear of the centre
        # column, the string starts where no stub is.
        step = 1.27 if small else 2.54
        vx, vy, vjust = _pick_field(
            part.value,
            [
                (
                    round(x + sign * reach, 4),
                    round(bottom + step + row, 4),
                    "left" if sign > 0 else "right",
                )
                for row in (0.0, 1.27, 2.54, 3.81, 5.08)
                for reach in (step, step + 2.54, step + 5.08, step + 7.62, step + 10.16)
                for sign in (1, -1)
            ],
            wires,
            avoid,
            texts,
            [block, own, _text_box(part.ref, rx, ry, ref_justify)],
        )
        value_prop = _property(
            "Value",
            part.value,
            vx,
            vy,
            hides_value(part) or not part.show_value,
            written(vjust),
            text_angle,
        )
    lines = [
        f'  (symbol (lib_id "{part.lib_id}") (at {x} {y} {part.angle}){mirror} (unit {part.unit})',
        f"    (exclude_from_sim no) (in_bom {'yes' if in_bom(part.footprint) else 'no'}) "
        "(on_board yes) (dnp no)",
        f'    (uuid "{uid}")',
        _property("Reference", part.ref, *ref_at, False, written(ref_justify), text_angle),
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
                lines.append(
                    _property(name, value, *row_at(shown), False, written(justify), text_angle)
                )
            else:
                row = (
                    round(x + flat_dx, 4),
                    round(bottom + flat_dy + shown * 2.54, 4),
                )
                lines.append(_property(name, value, row[0], row[1], False, angle=text_angle))
            shown += 1
        else:
            lines.append(_property(name, value, x, y, True))
    if block is not None and avoid is not None:
        # later parts must not print their block over this one
        avoid.append(block)
    if texts is not None:
        record = texts.add if isinstance(texts, _BoxIndex) else texts.append
        # Every string this symbol just put on the page, so the next symbol
        # measures against them. Two ratings blocks a millimetre apart read as
        # one word, and "220u" over "100n" reads as neither.
        if block is not None:
            record(block)
        record(_text_box(part.ref, *ref_at, ref_justify))
        if not side_value:
            record(_text_box(part.value, vx, vy, vjust))
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


def board_layers(count: int, power_plane: str | None = None) -> str:
    """KiCad's layer table for a two- or four-layer example board."""
    if count == 2:
        return BOARD_LAYERS
    if count != 4:
        raise ValueError(f"examples support two or four copper layers, got {count}")
    power_name = f' "{power_plane}"' if power_plane else ""
    return BOARD_LAYERS.replace(
        '\t\t(2 "B.Cu" signal)',
        f'\t\t(2 "In1.Cu" power "GND")\n\t\t(4 "In2.Cu" power{power_name})\n\t\t(6 "B.Cu" signal)',
    )


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


def _move_reference_off_pads(
    design: Design,
    part: Part,
    node: SNode,
    all_pads: list[tuple[float, float, float, float]] | None = None,
    printed: list[tuple[float, float, float, float]] | None = None,
    bodies: list[tuple[float, float, float, float]] | None = None,
) -> None:
    """Put the designator somewhere it can still be read after assembly.

    A library footprint puts its reference where that part's own outline
    leaves room, which for a module with pads down both sides and along the
    bottom is the middle of a pad. Silk over a pad is not a designator: the
    mask opens there, the ink is scraped off in fabrication, and what is left
    is a pad that will not wet.

    ``bodies`` are the other parts' courtyards, and they weigh as much as a
    pad: a designator under a neighbour's shell prints fine on the bare board
    and is gone the moment that neighbour is fitted. The fuse on the motor
    driver had its name a millimetre inside the bulk capacitor's outline. The
    part's own body weighs the same, and for the same reason.

    Measured on the board rather than in the footprint's own frame, because
    the two disagree the moment the part is turned: the anchor rotates with
    the footprint and the string does not, so a label that sat clear above an
    upright part reaches sideways into the pads of a lying one.
    """
    prop = next((p for p in node.children("property") if str(p.atom(0, "")) == "Reference"), None)
    at = prop.child("at") if prop else None
    if at is None:
        return
    atoms = [a for a in at.atoms() if isinstance(a, (int, float))]
    if len(atoms) < 2:
        return
    bx, by, angle = part.board
    pads = [pad_box(design, part, pad) for pad in node.children("pad")]
    if not pads:
        # A part with no pads still has a designator to place, and a mounting
        # hole is the case that matters: it lives in a corner, its library
        # points the reference outward, and outward is off the board. Its
        # courtyard is the extent to step clear of.
        court = _courtyard_box(design, part)
        pads = [court] if court else [(bx - 1.0, by - 1.0, bx + 1.0, by + 1.0)]
    obstacles = list(all_pads if all_pads is not None else pads) + list(printed or [])
    # the extent KiCad will actually print, rounded up (see `_text_extent`)
    half_x, half_y = _text_extent(part.ref, 1.0)
    # What the designator has to step clear of is the part itself. Its own body
    # hides more of its name than any neighbour does: an electrolytic capacitor
    # and an inductor each span their own two pads, and a module spans fifty
    # millimetres of them, so the clear gap a library leaves between the pads is
    # under the part and the name printed there is readable exactly until the
    # board is assembled. The courtyard is the fallback for a footprint that
    # draws no fabrication outline.
    own = _body_box(design, part) or _courtyard_box(design, part)
    if own is None:
        own = (
            min(box[0] for box in pads),
            min(box[1] for box in pads),
            max(box[2] for box in pads),
            max(box[3] for box in pads),
        )
    heavy = [*(bodies or []), own]
    # How far out to look, measured from the part's own outline rather than
    # from one of its pads: a name has to clear the whole part. Offsets are
    # built in board coordinates - a designator prints horizontally whatever
    # the footprint's rotation, so the string reaches along the board's x
    # whichever way the part is turned - then rotated back into the footprint's
    # frame, which is what `at` states. Near beats far; ties go to the earlier
    # candidate.
    # Measured per direction, not as one radius: a footprint is anchored where
    # its library chose to anchor it, which for a screw terminal is pin 1 and
    # not the middle of its shell. One radius big enough to clear the far side
    # puts the name three millimetres past the near side, close enough to the
    # next part to read as its.
    up = by - own[1] + half_y
    down = own[3] - by + half_y
    left = bx - own[0] + half_x
    right = own[2] - bx + half_x
    gaps = (0.4, 1.0, 1.8, 2.8)
    offsets = [
        *(
            spot
            for gap in gaps
            for spot in (
                (0.0, -(up + gap)),
                (0.0, down + gap),
                (-(left + gap), 0.0),
                (right + gap, 0.0),
            )
        ),
        # and the corners, for the part hemmed in on all four sides
        *(
            (sx * ((right if sx > 0 else left) + gap), sy * ((down if sy > 0 else up) + gap))
            for gap in gaps
            for sx in (-1.0, 1.0)
            for sy in (-1.0, 1.0)
        ),
    ]
    candidates = (
        (float(atoms[0]), float(atoms[1])),
        *(_rotate(dx, dy, -angle) for dx, dy in offsets),
    )

    # Scored rather than first-fit, and every candidate is scored, so a part
    # with no clear position gets the least bad one instead of keeping the
    # library's - which for a part at the edge of the board points outward, and
    # outward is off the board. `_silk_intrusion` is what grades them: ink past
    # the outline is never printed at all (the panel is routed at the line and
    # the designator leaves with the offcut), ink on a pad is a pad that will
    # not wet, and ink on a courtyard is merely close. Ties go to the earlier
    # candidate, which is how "near beats far" survives the change.
    def cost(spot: tuple[float, float]) -> float:
        rx, ry = _rotate(spot[0], spot[1], angle)
        box = (bx + rx - half_x, by + ry - half_y, bx + rx + half_x, by + ry + half_y)
        return _silk_intrusion(design, box, obstacles, heavy)

    _rank, (cx, cy) = min(enumerate(candidates), key=lambda item: (cost(item[1]), item[0]))
    rx, ry = _rotate(cx, cy, angle)
    at.args = [cx, cy, *atoms[2:]]
    if printed is not None:
        printed.append((bx + rx - half_x, by + ry - half_y, bx + rx + half_x, by + ry + half_y))
    return


def _hide_property(node: SNode, name: str) -> None:
    """Keep a footprint property in the file but off the silkscreen.

    KiCad's parity check compares the symbol's fields with the footprint's, so
    the property has to stay; only its `hide` flag changes.
    """
    for prop in node.children("property"):
        if str(prop.atom(0, "")) != name:
            continue
        for child in list(prop.children("hide")):
            prop.args.remove(child)
        prop.args.append(SNode("hide", [Bare("yes")]))
        return


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


def anchor_site(
    design: Design,
    pad_ref: str,
    away: tuple[float, float],
    tracks: Sequence[Track],
    vias: Sequence[Via],
) -> tuple[float, float] | None:
    """Where a via may stand beside ``pad_ref``, or None if nowhere may.

    The loop a decoupling capacitor exists to close runs pad, via, plane, so
    the via wants to be as near its ground pad as it can get - and *not* in it
    (`via.in_pad`): a hole in a land is a hole solder wicks down. That leaves a
    ring of legal positions, and which of them is free depends on what else the
    board has already placed. Picking the nearest free one beats naming a fixed
    offset, which is how an earlier version of this walled off the corridor its
    neighbour's supply needed - the via was legal, and it was in the way.

    Nearest first, then fanning either side of ``away`` - the direction facing
    off the part, which is where a hand would try first.
    """
    boxes = [
        pad_box(design, part, pad)
        for part in design.footprints()
        for pad in footprint_definition(part.footprint).children("pad")
    ]
    holes = [via_position(design, via) for via in vias]
    laid = []
    for track in tracks:
        points = [resolve(design, point) for point in track.points]
        laid.extend((track.net, track.width, a, b) for a, b in pairwise(points))
    px, py = pad_position(design, pad_ref)
    net = next((n for n, nodes in design.nets.items() if pad_ref in nodes), None)
    radius = VIA_SIZE / 2
    for step in range(2, 9):  # 0.5 mm out to 2.0, in quarter millimetres
        for turn in (0, 45, -45, 90, -90, 135, -135, 180):
            angle = math.radians(turn)
            dx = away[0] * math.cos(angle) - away[1] * math.sin(angle)
            dy = away[0] * math.sin(angle) + away[1] * math.cos(angle)
            vx, vy = round(px + dx * step * 0.25, 4), round(py + dy * step * 0.25, 4)
            box = (vx - radius, vy - radius, vx + radius, vy + radius)
            if any(
                bx0 - radius - ZONE_CLEARANCE <= vx <= bx1 + radius + ZONE_CLEARANCE
                and by0 - radius - ZONE_CLEARANCE <= vy <= by1 + radius + ZONE_CLEARANCE
                for bx0, by0, bx1, by1 in boxes
            ):
                continue
            if any(
                other != net and _segment_to_box(a, b, box) < width / 2 + ZONE_CLEARANCE
                for other, width, a, b in laid
            ):
                continue
            if any(math.dist((vx, vy), hole) < 1.2 for hole in holes):
                continue
            return (vx, vy)
    return None


def _spoke_width(design: Design, part: Part, pad: SNode, net: str, heavy: bool = False) -> float:
    """How wide each of a relieved pad's four spokes has to be.

    A thermal relief is a deliberate bottleneck, and on a signal pad that is
    all it has to be. On a power pad it is also the conductor: every ampere
    the track brings in leaves through the spokes, so a relief sized for
    solderability alone puts a 0.5 mm neck in a 3 A return path - a hot spot
    where the copper is thinnest and nobody drew it.

    KiCad always draws four spokes and offers no way to ask for more, so the
    answer is width. Each spoke is sized at half the widest track that reaches
    the pad, which puts twice the track's own copper across the four of them,
    and never below the default: a relief that is wider than the track feeding
    it has stopped being a relief.

    ``heavy`` is the pad with no track at all - a regulator's tab, whose whole
    return current leaves through the spokes and the plane. Nothing about the
    copper says how much that is, and the part it belongs to is the reason the
    board exists, so it takes the widest spoke the relief still survives.
    """
    if heavy:
        return THERMAL_SPOKE_MAX
    box = pad_box(design, part, pad)
    widest = 0.0
    for track in design.tracks:
        if track.net != net:
            continue
        points = [resolve(design, point) for point in track.points]
        if any(box[0] <= x <= box[2] and box[1] <= y <= box[3] for x, y in points):
            widest = max(widest, track.width)
    return round(min(max(THERMAL_SPOKE, widest / 2), THERMAL_SPOKE_MAX), 3)


def _wants_relief(design: Design, part: Part, pad: SNode) -> bool:
    """Whether a pad is heavy enough that the plane must not drink its heat.

    A chip capacitor's land reflows with the whole board in an oven that is
    heating the plane anyway, and a solid tie is the better electrical answer.
    A regulator's tab is not that: a hundred square millimetres tied straight
    into the pour reaches solder temperature after the part's own leads do, and
    the part lifts on the leads that got there first.

    Unless the copper is the heat path on purpose, which is what a via array in
    the pad says. A QFN's exposed pad with nine vias under it is cooling the
    die through them, and relieving it would be undoing the design.
    """
    if str(pad.atom(1, "")) != "smd" or pad.child("drill") is not None:
        return False
    if pad.child("zone_connect") is not None:
        return False  # the footprint or the design has already decided
    size = [a for a in pad.child("size").atoms() if isinstance(a, (int, float))]
    width = float(size[0])
    height = float(size[1] if len(size) > 1 else size[0])
    if width * height < RELIEF_PAD_AREA_MM2 or min(width, height) < RELIEF_PAD_SIDE_MM:
        return False
    box = pad_box(design, part, pad)
    return not any(
        box[0] <= point[0] <= box[2] and box[1] <= point[1] <= box[3]
        for point in (via_position(design, via) for via in design.vias)
    )


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
    # A fan of one has no neighbour to crowd: the spacing check below has
    # nothing to compare and the loop it guards does not run.
    row = min((abs(a[1] - b[1]) for a, b in pairwise(placed)), default=0.0)
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


# The ratio at which a routed track stops being a route and starts being a
# tour. Deliberately `pcb_review.THRESHOLDS["wander_ratio"]`: the generator is
# laying copper so that rule finds nothing, so the number it aims at has to be
# the number the rule will measure it against.
WANDER_LIMIT = 2.0
# ...and its floor, for the same reason: below five millimetres of excess the
# knee that takes a track round a pad is a detour and is also correct.
WANDER_FLOOR_MM = 5.0
# How many times the loop will re-order for quality. Each attempt re-routes
# the whole board, which on the fine-pitch one is the better part of an hour,
# and the tracks it has promoted stay at the front - so a handful of passes
# buys most of what an afternoon of them would.
WANDER_ATTEMPTS = 6
# How many times one track may be promoted to the front of the order before
# the loop calls its lane impossible rather than merely taken.
RIPUP_TRIES = 4


def _bus_of(net: str) -> str | None:
    """The bundle a net travels with, read from its name.

    I2S_LRCK, I2S_DIN, I2S_BCK and I2S_SCK are one bus; so are the four SPI
    lines. The convention is the name's first word, and it is the same
    convention a person uses when they route the four as parallel lanes in
    one corridor. Power rails are not buses - their names share prefixes for
    a different reason - and a net with no underscore travels alone.
    """
    base = net.lstrip("/")
    if "_" not in base:
        return None
    head = base.split("_", 1)[0]
    if not head or head[0] in "+-" or head[0].isdigit() or head.upper() == "GND":
        return None
    return head


def _package_boxes(design: Design) -> list[tuple[float, float, float, float]]:
    """Every part as a rectangle, for measuring what a route had to go round.

    A run from one side of a package to the other cannot take the straight
    line, because the straight line is through the package: a SOT-23-5's
    feedback wrap is three millimetres of separation and eighteen of copper,
    and it is right. `route.wander` prices it against going *round*, so the
    generator has to price it the same way or it spends its afternoon chasing
    wraps that are correct.
    """
    boxes = []
    for part in design.footprints():
        node = footprint_definition(part.footprint)
        pads = [pad_box(design, part, pad) for pad in node.children("pad")]
        if len(pads) < 2:
            continue
        boxes.append(
            (
                min(b[0] for b in pads),
                min(b[1] for b in pads),
                max(b[2] for b in pads),
                max(b[3] for b in pads),
            )
        )
    return boxes


def _tee_component(
    laid: list[tuple[str, list[tuple[float, float]], int]],
    via_points: list[tuple[float, float]],
    pads: list[tuple[tuple[float, float, float, float], str | None]],
    seed: tuple[float, float],
) -> list[tuple[str, list[tuple[float, float]], int]]:
    """The net's already-laid copper that ``seed`` is electrically joined to.

    This is what makes a tee safe to offer the router: a link that finishes on
    its own net's copper is only finished if that copper already reaches the
    pad the link named - ending on an island of the same net that nothing
    joins yet leaves the pad unconnected, and DRC is the only thing that would
    notice. Runs join where one's end lies on another, through a via of the
    net, and through the net's own pads; the component is grown outward from
    the pad ``seed`` sits on.
    """
    TOL = 0.05

    def on_run(point, entry) -> bool:
        _layer, points, _index = entry
        return any(_segment_distance(point, point, s0, s1) < TOL for s0, s1 in pairwise(points))

    def inside(point, box) -> bool:
        return box[0] - TOL <= point[0] <= box[2] + TOL and box[1] - TOL <= point[1] <= box[3] + TOL

    pad_hits: list[set[int]] = []
    via_hits: list[set[int]] = []
    for layer, points, _index in laid:
        pad_hits.append(
            {
                number
                for number, (box, pad_side) in enumerate(pads)
                if pad_side in (None, layer) and any(inside(point, box) for point in points)
            }
        )
        via_hits.append(
            {
                number
                for number, point in enumerate(via_points)
                if on_run(point, (layer, points, _index))
            }
        )

    def linked(i: int, j: int) -> bool:
        if pad_hits[i] & pad_hits[j] or via_hits[i] & via_hits[j]:
            return True
        if laid[i][0] != laid[j][0]:
            return False
        pi, pj = laid[i][1], laid[j][1]
        return (
            on_run(pi[0], laid[j])
            or on_run(pi[-1], laid[j])
            or on_run(pj[0], laid[i])
            or on_run(pj[-1], laid[i])
        )

    seed_pads = {number for number, (box, _side) in enumerate(pads) if inside(seed, box)}
    member = [
        bool(seed_pads & pad_hits[index]) or on_run(seed, entry) for index, entry in enumerate(laid)
    ]
    grew = True
    while grew:
        grew = False
        for i in range(len(laid)):
            if member[i]:
                continue
            if any(member[j] and linked(i, j) for j in range(len(laid))):
                member[i] = True
                grew = True
    return [entry for index, entry in enumerate(laid) if member[index]]


def _absorb_tee(
    routed: list[tuple[int, Track]],
    component: list[tuple[str, list[tuple[float, float]], int]],
    layer: str,
    landing: tuple[float, float],
) -> None:
    """Make a tee landing a stated corner of the run it landed on.

    Every reshaping pass pins the points other copper ends on - but only if
    the point is a *vertex* of the polyline. A branch that ends mid-segment is
    connected copper the trunk does not know about, and the first pass to
    straighten or chamfer that stretch moves the trunk out from under the
    join. Inserting the landing as a corner makes it a join like any other:
    `_straighten` and `_doglegged` cut there, `_chamfer_tracks` leaves it.
    """
    for entry_layer, points, _index in component:
        if entry_layer != layer:
            continue
        if any(math.dist(landing, point) < 1e-3 for point in points):
            return  # an existing corner or end - already a join
    for entry_layer, points, rindex in component:
        if entry_layer != layer:
            continue
        for cut in range(len(points) - 1):
            if _segment_distance(landing, landing, points[cut], points[cut + 1]) < 1e-3:
                points.insert(cut + 1, landing)
                place_index, track = routed[rindex]
                stated = list(track.points)
                stated.insert(cut + 1, landing)
                routed[rindex] = (place_index, replace(track, points=stated))
                return


def _route_all(
    design: Design, order: list[Track]
) -> tuple[list[tuple[int, Track]], list[Via], list[tuple[float, Track]]]:
    """Route every ``auto`` track in ``order``, or say which one had no room.

    The third return is what each track cost in the end: the ratio of the
    copper it took to the straight line between its own two ends, for every
    track that came out longer than `WANDER_LIMIT` times it. A net routed
    last takes what the ones before it left, and what they leave is sometimes
    a tour of the board - which is a routing order problem, not a floorplan
    one, and the caller can do something about it.
    """
    router = autoroute.Router(*design.board_size)
    # The same pads again, keyed by net, for the connectivity question the
    # tee asks: which copper already reaches the pad this link names.
    net_pads: dict[str, list[tuple[tuple[float, float, float, float], str | None]]] = defaultdict(
        list
    )
    for part in design.footprints():
        node = footprint_definition(part.footprint)
        for pad in node.children("pad"):
            number = str(pad.atom(0, ""))
            net = next(
                (name for name, nodes in design.nets.items() if f"{part.ref}.{number}" in nodes),
                "",
            )
            x0, y0, x1, y1 = pad_box(design, part, pad)
            router.add(autoroute.Obstacle(x0, y0, x1, y1, net, pad_layer(pad), pad=True))
            if net:
                net_pads[net].append(((x0, y0, x1, y1), pad_layer(pad)))
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
    # The courtyard of a part nothing else may cross, open to the nets the part
    # itself is on so its own escapes still leave.
    for ref in design.body_keepout:
        part = next(p for p in design.footprints() if p.ref == ref)
        box = _courtyard_box(design, part)
        if box is None:
            continue
        own = frozenset(
            name
            for name, nodes in design.nets.items()
            if any(node.split(".")[0] == ref for node in nodes)
        )
        router.add(autoroute.Obstacle(*box, "", None, open_to=own))
    for x0, y0, x1, y1 in design.keepouts:
        router.add(autoroute.Obstacle(x0, y0, x1, y1, "", None))
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
    # Every run laid so far, by net, with where it sits in ``routed`` - the
    # copper a later link of the same net may tee into (see `_tee_component`).
    laid_runs: dict[str, list[tuple[str, list[tuple[float, float]], int]]] = defaultdict(list)
    for rindex, (_position, stated) in enumerate(routed):
        laid_runs[stated.net].append(
            (stated.layer, [resolve(design, point) for point in stated.points], rindex)
        )
    tours: list[tuple[float, Track]] = []
    packages = _package_boxes(design)
    # Where each bus's members already run, so the next member can travel
    # beside them (see Router.route's ``follow``).
    bus_paths: dict[str, list[list[tuple[float, float]]]] = {}
    vias = list(design.vias)
    for index, track in enumerate(order):
        a, b = (resolve(design, point) for point in track.points)
        start_hole = through_hole(design, track.points[0])
        goal_hole = through_hole(design, track.points[-1])
        # What this link may tee into: the net's own copper, already joined
        # to one of the link's ends. Joined to the goal, the search may
        # finish on it; joined to the start, the link is routed the other
        # way round so it still may - that is the common case, a trunk
        # arriving at a pad and the next link leaving the same pad. Without
        # this every junction the net has sits on a pad, because a pad is
        # the only place a link is allowed to finish.
        # ...but only toward a *pad*: a link that names a bare coordinate is
        # aimed at a stated junction - the end of a trunk someone drew - and
        # landing anywhere short of it strands the trunk's tail as a stub.
        # A pad is a terminal in its own right, so copper between the
        # landing and the pad still ends somewhere real.
        entries = laid_runs.get(track.net, ())
        net_vias = [via_position(design, v) for v in vias if v.net == track.net]
        component = (
            _tee_component(entries, net_vias, net_pads.get(track.net, []), b)
            if entries and isinstance(track.points[-1], str)
            else []
        )
        if (
            not component
            and entries
            and track.goal_layer is None
            and isinstance(track.points[0], str)
        ):
            component = _tee_component(entries, net_vias, net_pads.get(track.net, []), a)
            if component:
                a, b = b, a
                start_hole, goal_hole = goal_hole, start_hole
        path = router.route(
            track.net,
            a,
            b,
            track.width,
            start_layer=None if start_hole else track.layer,
            goal_layer=track.goal_layer or (None if goal_hole else track.layer),
            crowd=[
                resolve(design, point)
                for later in order[index + 1 :]
                if later.net != track.net
                for point in later.points
            ],
            # The back layer of these boards is a ground plane, and a signal
            # laid across it saws the plane in two under its own return
            # current - which is what `route.return_path` measures. So a
            # millimetre spent on the plane side is priced at thirty on the
            # front, and the router crosses only where the front side would
            # cost it more than that.
            #
            # It was forty for a while, which is not a preference but a
            # prohibition: at forty, one millimetre of crossing buys a forty
            # millimetre tour, and the boards grew them - a supply run at 4.4x
            # the straight line, all of it on the front, which is what
            # `route.wander` was written to catch. Below thirty the trade
            # reverses: the short back-layer hops the router then takes cut the
            # plane under the same net's own front copper, and
            # `route.return_path` picks that up instead. Thirty is where
            # neither fires. GND is not charged: its own copper is the plane.
            #
            # Nor is a link the design has put on the back itself - declared
            # on B.Cu, asked to finish there, and kept there. That is the
            # floorplan's decision, made where the front is full: the motor
            # driver's logic drops leave a via column on the package's east
            # side for a header ten millimetres south, and at thirty the
            # search preferred a seventy-five millimetre tour of the front
            # over seventeen on the back, round both motor terminals and
            # across every bridge output on the way.
            back_cost=None
            if track.net == POUR_NET or (track.keep_layer and track.layer != "F.Cu")
            else 30.0,
            follow=bus_paths.get(_bus_of(track.net) or ""),
            tee=[(layer, points) for layer, points, _index in component] or None,
            # ...and the rest of the net's copper - laid but not joined to
            # the goal - may be crossed but not ridden (see Router.route).
            avoid=[
                (layer, points)
                for layer, points, index in entries
                if index not in {i for _l, _p, i in component}
            ]
            or None,
        )
        if path is None:
            raise Blocked(track)
        if bus := _bus_of(track.net):
            for _layer, bus_points in path.runs:
                bus_paths.setdefault(bus, []).append(list(bus_points))
        for layer, points in path.runs:
            for start, end in pairwise(points):
                router.add_track(track.net, start, end, track.width, layer)
            routed.append(
                (place[id(track)], replace(track, points=list(points), layer=layer, auto=False))
            )
            # A separate copy, index-aligned with the track's own points:
            # `_absorb_tee` inserts a landing into both at the same position.
            laid_runs[track.net].append((layer, list(points), len(routed) - 1))
        # A route that finished on the trunk instead of the pad: make the
        # landing a corner the trunk states, or the clean-up passes will
        # move the trunk out from under the join.
        if component and path.runs:
            landing_layer, landing_points = path.runs[-1]
            landing = landing_points[-1]
            if math.dist(landing, b) > GEOM_EPS:
                _absorb_tee(routed, component, landing_layer, landing)
        for point in path.vias:
            router.add_via(track.net, point, VIA_SIZE)
            vias.append(Via(track.net, x=point[0], y=point[1], size=VIA_SIZE))
        laid = sum(
            math.dist(start, end) for _layer, points in path.runs for start, end in pairwise(points)
        )
        # `_shortest_clear` is `route.wander`'s own baseline, imported rather
        # than copied: the generator is laying copper so that rule finds
        # nothing, and two implementations of the same measurement drift.
        direct = _shortest_clear(a, b, packages)
        if direct > 0.5 and laid > WANDER_LIMIT * direct and laid - direct > WANDER_FLOOR_MM:
            tours.append((laid / direct, track))
    return routed, vias, tours


ROUTE_CACHE = Path(__file__).resolve().parent.parent / ".cache" / "routes"
ROUTE_CACHE_VERSION = 2


def _track_signature(design: Design, track: Track) -> str:
    """What a track asks the router for, as text.

    Written from the *unresolved* points, so a track keeps its identity when
    the part it lands on moves - which is what makes the rip-up order worth
    carrying from one run to the next.
    """
    return f"{track.net}|{track.layer}|{track.width}|{track.goal_layer}|{track.points}"


def _routing_digest(design: Design) -> str:
    """A key over everything the router reads, and nothing else.

    Most edits to this generator - where a designator prints, how a legend
    picks its side, what the fill counts as one island - do not move a single
    track, and re-routing an 84 mm board with a 48-pin QFN on it takes the
    better part of an hour. So the answer is kept, keyed by the question: the
    board outline, the parts and their pads, every track and via the design
    states, and the source of the router itself, so that changing how it
    routes invalidates every answer it gave.
    """
    lines = [
        str(ROUTE_CACHE_VERSION),
        design.name,
        repr(design.board_size),
        repr(design.keepouts),
        repr(design.route_keepout),
        repr(design.body_keepout),
        repr(design.priority_nets),
        repr((VIA_SIZE, POUR_NET)),
    ]
    for part in sorted(design.footprints(), key=lambda p: p.ref):
        lines.append(f"P {part.ref}|{part.footprint}|{part.board}")
        # Unrouted and unconnected pads are obstacles too. Endpoint positions
        # alone do not capture a library pad's size, drill or layer changes.
        lines.append(sexp.dumps(footprint_definition(part.footprint)))
    for name, nodes in sorted(design.nets.items()):
        lines.append(f"N {name}={','.join(sorted(nodes))}")
    for track in design.tracks:
        points = [tuple(round(v, 4) for v in resolve(design, point)) for point in track.points]
        lines.append(
            f"T {_track_signature(design, track)}|{track.auto}|{track.keep_layer}|{points}"
        )
    for via in design.vias:
        lines.append(f"V {via.net}|{via_position(design, via)}|{via.size}|{via.drill}")
    lines.append(Path(autoroute.__file__).read_text())
    lines.append(inspect.getsource(_route_all))
    # The order the links are offered in is as much the question as the
    # search: the classes and how a failure or a tour moves a link live here.
    lines.append(inspect.getsource(resolve_routes))
    lines.append(inspect.getsource(_route_rank))
    lines.append(inspect.getsource(_promoted))
    lines.append(inspect.getsource(_tee_component))
    lines.append(inspect.getsource(_absorb_tee))
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()[:32]


def _cache_read(name: str, digest: str) -> tuple[list[Track], list[Via]] | None:
    path = ROUTE_CACHE / f"{name}.{digest}.json"
    try:
        blob = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(blob, dict) or blob.get("version") != ROUTE_CACHE_VERSION:
        return None
    tracks = [
        Track(
            net=t["net"],
            layer=t["layer"],
            width=t["width"],
            points=[tuple(point) for point in t["points"]],
            goal_layer=t.get("goal_layer"),
            keep_layer=t["keep_layer"],
        )
        for t in blob["tracks"]
    ]
    vias = [
        Via(net=v["net"], x=v["x"], y=v["y"], drill=v["drill"], size=v["size"])
        for v in blob["vias"]
    ]
    return tracks, vias


def _cache_write(design: Design, digest: str, done: Design, order: list[Track]) -> None:
    try:
        ROUTE_CACHE.mkdir(parents=True, exist_ok=True)
        (ROUTE_CACHE / f"{design.name}.{digest}.json").write_text(
            json.dumps(
                {
                    "version": ROUTE_CACHE_VERSION,
                    "tracks": [
                        {
                            "net": t.net,
                            "layer": t.layer,
                            "width": t.width,
                            "points": [list(resolve(done, p)) for p in t.points],
                            "goal_layer": t.goal_layer,
                            "keep_layer": t.keep_layer,
                        }
                        for t in done.tracks
                    ],
                    "vias": [
                        {
                            "net": v.net,
                            "x": via_position(done, v)[0],
                            "y": via_position(done, v)[1],
                            "drill": v.drill,
                            "size": v.size,
                        }
                        for v in done.vias
                    ],
                }
            )
        )
        # The order is worth keeping on its own: it is what the rip-up loop
        # spent its afternoon learning, and it stays useful after an edit that
        # changes the digest.
        (ROUTE_CACHE / f"{design.name}.order.json").write_text(
            json.dumps([_track_signature(design, t) for t in order])
        )
    except OSError:
        pass


def _save_order(design: Design, order: list[Track]) -> None:
    """Write the routing order down as soon as a rip-up changes it.

    The order is what the rip-up loop spends its afternoon learning, and on
    the fine-pitch board an afternoon is the literal cost: eleven rip-ups, a
    routing pass between each. Saving only on success means a machine that
    goes away in the middle throws all of it out.

    Only the rip-ups. A rip-up is knowledge - that net has to go early or it
    has no room - and it holds whatever else changes. A promotion for
    tidiness is a guess, and writing those down poisons the file for the next
    run: the guess that made a net unroutable comes back as the starting
    order.
    """
    try:
        ROUTE_CACHE.mkdir(parents=True, exist_ok=True)
        (ROUTE_CACHE / f"{design.name}.order.json").write_text(
            json.dumps([_track_signature(design, t) for t in order])
        )
    except OSError:
        pass


def _learned_order(design: Design, order: list[Track]) -> list[Track]:
    """Start from the order that worked last time, where it still applies."""
    try:
        known = json.loads((ROUTE_CACHE / f"{design.name}.order.json").read_text())
    except (OSError, ValueError):
        return order
    rank = {signature: index for index, signature in enumerate(known)}
    return sorted(order, key=lambda t: rank.get(_track_signature(design, t), len(rank)))


def _route_rank(design: Design, track: Track, thinnest: float) -> int:
    """Which class a link is routed in: 0 goes first, 1 goes round it.

    The board is routed around the nets that have something to lose - the
    ones carrying current, a clock, a bus, a pair - and the rest go wherever
    is left. Width says most of it: a link wider than the thinnest on the
    board is wide because of what it carries, and the widths here are stated
    on purpose. What width cannot say, `Design.priority_nets` does.

    The motor driver is why the classes exist. Its four 0.4 mm bridge outputs
    run a clear corridor west to the terminals; one 0.3 mm logic input with
    both ends on the east side found its straight lane taken and toured the
    whole west end of the board instead, and the chase for tidiness put it
    *first* - so the outputs then hopped under it, two vias apiece, ten
    barrels in a column. A logic input that has to tour tours; the outputs
    it tours across are not the ones that pay for it.
    """
    if track.net in design.priority_nets or track.width > thinnest + GEOM_EPS:
        return 0
    return 1


def _promoted(order: list[Track], track: Track, rank: Callable[[Track], int]) -> list[Track]:
    """`order` with `track` moved to the front of its own class.

    The front of its class, not of the board: a plain link that failed or
    toured is given first pick among the plain links, and still routes after
    every link with a claim on the board. Where the classes meet is the only
    place the two orders touch.
    """
    rest = [t for t in order if t is not track]
    at = next((i for i, t in enumerate(rest) if rank(t) >= rank(track)), len(rest))
    rest.insert(at, track)
    return rest


def resolve_routes(
    design: Design, use_cache: bool = True, *, require_cache: bool = False
) -> Design:
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

    A net that finds *bad* room gets the same treatment. Having somewhere to
    go is not the same as having somewhere sensible to go: the op-amp's
    feedback wrap has to get from one side of a SOT-23-5 to the other, and
    routed last it took a fifty-six millimetre tour of the board to cover
    thirteen millimetres, because everything nearer was already spoken for.
    That is the shape `route.wander` reports and it is an ordering problem, so
    the loop that fixes ordering fixes it: the worst tour goes to the front
    and the set is routed again. A track that still tours from first pick has
    nowhere better to be, and is left alone rather than chased.

    The result is cached against everything the router read (see
    `_routing_digest`), because most of what gets edited around here does not
    move copper and the FPGA board takes the better part of an hour to route.
    """
    design = _straighten(design)
    auto = [track for track in design.tracks if track.auto]
    if not auto:
        return _pipeline(design)
    # Two classes, and shortest first within each. The nets with something
    # to lose - current, a clock, a bus - are routed while the board is empty
    # and nothing routed later may push them aside (see `_route_rank`); the
    # rest go round them. A thirteen millimetre connection has few ways to be
    # made and a forty millimetre one has many, so within a class the short
    # ones are the ones that have to choose while there is still room - and a
    # short net forced into a long path is exactly the ratio `route.wander`
    # measures. (The learned order below overrides this where it applies;
    # this is what a fresh clone, which has no learned order, starts from.)
    thinnest = min(track.width for track in auto)
    # Plain links the board turned out to have no lane for behind the nets
    # with first pick. Feasibility is the hard constraint and the classes are
    # not: such a link is lifted ahead of them, once, and the log says so -
    # that is a floorplan with no room for it, which is the placement's
    # problem to fix and not something to hide by declaring the net special.
    lifted: set[int] = set()

    def rank(track: Track) -> int:
        return 0 if id(track) in lifted else _route_rank(design, track, thinnest)

    order = sorted(
        auto,
        key=lambda t: (rank(t), math.dist(*(resolve(design, point) for point in t.points))),
    )
    digest = _routing_digest(design)
    if use_cache:
        cached = _cache_read(design.name, digest)
        if cached:
            print(f"{design.name}: routing unchanged, reusing {digest[:8]}", file=sys.stderr)
            done = replace(design, tracks=cached[0], vias=cached[1])
            return _pipeline(done)
        if require_cache:
            raise SystemExit(f"{design.name}: required route cache is missing for {digest}")
        # What was learned holds within a class; it does not put a plain
        # link ahead of one with a claim. A stable sort keeps the rest.
        order = sorted(_learned_order(design, order), key=rank)
    ripped: list[Track] = []
    relaid: list[Track] = []
    # An order that routed everything, and whether tours may still be chased.
    # Feasibility is the hard constraint and tidiness is not: promoting a
    # wandering net to the front can take the only lane some other net had, and
    # when it does the answer is to go back to the order that worked rather
    # than to declare the floorplan impossible.
    safe_order: list[Track] | None = None
    chase = True
    while True:
        try:
            routed, vias, tours = _route_all(design, order)
        except Blocked as blocked:
            # A track that blocks again has not necessarily been given the
            # board: the rip-up before it put someone else at the front, so
            # this one is second or fifth by the time it runs. Promoting it
            # once more is cheap and often enough - on the fine-pitch board
            # every net that failed routed on its own, which is congestion
            # and an order to be found, not a floorplan with no lane. What
            # is not cheap is doing this forever, so it is counted.
            if ripped.count(blocked.track) >= RIPUP_TRIES:
                if rank(blocked.track) > 0:
                    lifted.add(id(blocked.track))
                    ripped = [t for t in ripped if t is not blocked.track]
                    order = _promoted(order, blocked.track, rank)
                    _save_order(design, order)
                    print(
                        f"{design.name}: {blocked.track.net} {blocked.track.points} has no "
                        "lane behind the nets with first pick - lifting it ahead of them; "
                        "the floorplan leaves it no other room",
                        file=sys.stderr,
                    )
                    continue
                if safe_order is not None:
                    print(
                        f"{design.name}: re-ordering for tidiness left {blocked.track.net} "
                        f"{blocked.track.points} with no lane - going back to the order that "
                        "routed and keeping the tours",
                        file=sys.stderr,
                    )
                    order, ripped, chase = list(safe_order), [], False
                    continue
                raise SystemExit(
                    f"{design.name}: no route for {blocked.track.net} between "
                    f"{blocked.track.points} even with first pick of the board - "
                    "the floorplan has no lane for it"
                ) from None
            ripped.append(blocked.track)
            order = _promoted(order, blocked.track, rank)
            _save_order(design, order)
            print(
                f"{design.name}: ripping up for {blocked.track.net} "
                f"{blocked.track.points} (attempt {len(ripped)})",
                file=sys.stderr,
            )
            continue
        # The worst one *this loop has not already tried*. A wrap that still
        # tours from first pick has nowhere better to be, and taking `max` over
        # everything would let it stand in front of the ones that do.
        safe_order = list(order)
        worst = max(
            ((r, t) for r, t in tours if t not in relaid and t not in ripped),
            key=lambda pair: pair[0],
            default=None,
        )
        if chase and worst is not None and len(relaid) < WANDER_ATTEMPTS:
            ratio, track = worst
            relaid.append(track)
            order = _promoted(order, track, rank)
            print(
                f"{design.name}: {track.net} {track.points} came out {ratio:.1f}x "
                f"the straight line - routing it first (attempt {len(relaid)})",
                file=sys.stderr,
            )
            continue
        done = replace(
            design, tracks=[track for _, track in sorted(routed, key=lambda p: p[0])], vias=vias
        )
        # A cold run ignores both cached routes and the learned order, but
        # records its fresh answer so the next run can verify the warm path.
        _cache_write(design, digest, done, order)
        return _pipeline(done)


def _on_45_grid(a, b) -> bool:
    """Whether the line from a to b runs along an axis or a 45.

    A straightened route is only an improvement if it lands on the grid the
    rest of the board is drawn on. Between two pads at whatever coordinates
    their packages put them, the direct line usually does not: on the FPGA
    board it produced a nine millimetre run at 169.7 degrees, which is the
    "slip of the mouse" angle `route.odd_angle` exists to report. A crooked
    route that is on the grid beats a straight one that is not.
    """
    dx, dy = abs(b[0] - a[0]), abs(b[1] - a[1])
    return dx < GEOM_EPS or dy < GEOM_EPS or abs(dx - dy) < GEOM_EPS


def _unlooped(design: Design) -> Design:
    """Cut the redundant loops out of a net's copper.

    Routing one link of a net at a time treats the net's own copper as free
    space, so a later link happily crosses an earlier one - same potential,
    DRC silent - and the net ends up carrying a closed loop with an X drawn
    on the plot. `route.self_crossing` measures it; a person never draws it.

    The cut is a graph question. Split every same-net, same-layer crossing at
    its intersection so the X becomes a node, build the net's node graph
    (vias stitch the layers), and while any cycle remains, remove the longest
    chain of the cycle that runs junction-to-junction: connectivity is kept
    by definition - a cycle has two ways round - and the amputated X is left
    as an ordinary corner. The pour net keeps its loops; a plane is a mesh on
    purpose. Vias stranded on removed copper go with it.
    """
    keep_nets = {POUR_NET}
    segs: list[dict] = []
    passthrough: list[Track] = []
    for track in design.tracks:
        if track.net in keep_nets or track.auto:
            passthrough.append(track)
            continue
        points = [resolve(design, point) for point in track.points]
        for a, b in pairwise(points):
            if math.dist(a, b) < GEOM_EPS:
                continue
            segs.append(
                {
                    "net": track.net,
                    "layer": track.layer,
                    "width": track.width,
                    "a": a,
                    "b": b,
                    "source": track,
                }
            )

    def _cross_point(p1, p2, p3, p4):
        d1 = (p2[0] - p1[0], p2[1] - p1[1])
        d2 = (p4[0] - p3[0], p4[1] - p3[1])
        denom = d1[0] * d2[1] - d1[1] * d2[0]
        if abs(denom) < 1e-12:
            return None
        t = ((p3[0] - p1[0]) * d2[1] - (p3[1] - p1[1]) * d2[0]) / denom
        u = ((p3[0] - p1[0]) * d1[1] - (p3[1] - p1[1]) * d1[0]) / denom
        if 1e-6 < t < 1 - 1e-6 and 1e-6 < u < 1 - 1e-6:
            return (round(p1[0] + t * d1[0], 4), round(p1[1] + t * d1[1], 4))
        return None

    # Split at every same-net, same-layer crossing so the X is a node.
    changed = True
    while changed:
        changed = False
        for i in range(len(segs)):
            for j in range(i + 1, len(segs)):
                one, other = segs[i], segs[j]
                if one["net"] != other["net"] or one["layer"] != other["layer"]:
                    continue
                point = _cross_point(one["a"], one["b"], other["a"], other["b"])
                if point is None:
                    continue
                segs[i : i + 1] = [
                    {**one, "b": point},
                    {**one, "a": point},
                ]
                # j moved by one because i split into two
                k = j + 1
                other = segs[k]
                segs[k : k + 1] = [
                    {**other, "b": point},
                    {**other, "a": point},
                ]
                changed = True
                break
            if changed:
                break

    def key(point):
        return (round(point[0], 3), round(point[1], 3))

    pad_nodes: dict[str, set] = defaultdict(set)
    pad_geometry: dict[str, list] = defaultdict(list)
    for part in design.footprints():
        node = footprint_definition(part.footprint)
        for pad in node.children("pad"):
            number = str(pad.atom(0, ""))
            owner = next(
                (n for n, nodes in design.nets.items() if f"{part.ref}.{number}" in nodes), None
            )
            if owner:
                centre = pad_position_of(design, part, pad)
                pad_nodes[owner].add(key(centre))
                pad_geometry[owner].append((centre, pad_box(design, part, pad), pad_layer(pad)))

    # A segment that passes *over* a pad of its own net feeds that pad by the
    # overlap - KiCad's connectivity is geometric, this graph is endpoint
    # topology, and the difference disconnected two pads the first time the
    # cutter ran: the only chain feeding R1 crossed the pad mid-run, the graph
    # had no node there, and the chain looked redundant. Split the segment at
    # the pad and the feed becomes an anchor the cut has to respect.
    #
    # Over it on the pad's own layer. A run on the back passing under a
    # front-side land touches nothing, and splitting it there manufactures a
    # node the front-side copper ending on that land then shares - which is
    # a cycle that never existed. The cutter closed one on the op-amp board:
    # a stub from a pad up to a via and the back-side run coming down from
    # that via read as a loop, the stub and the via went, and the run was
    # left starting at the pad on the wrong layer with no way up to it.
    for net, geometry in pad_geometry.items():
        index = 0
        while index < len(segs):
            seg = segs[index]
            if seg["net"] != net:
                index += 1
                continue
            a, b = seg["a"], seg["b"]
            length = math.dist(a, b)
            split_at = None
            for centre, box, layer in geometry:
                if length < GEOM_EPS:
                    break
                if layer is not None and layer != seg["layer"]:
                    continue
                t = ((centre[0] - a[0]) * (b[0] - a[0]) + (centre[1] - a[1]) * (b[1] - a[1])) / (
                    length * length
                )
                if not 0.01 < t < 0.99:
                    continue
                point = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
                if not (
                    box[0] - 0.05 <= point[0] <= box[2] + 0.05
                    and box[1] - 0.05 <= point[1] <= box[3] + 0.05
                ):
                    continue
                if key(point) in (key(a), key(b)):
                    continue
                split_at = (round(point[0], 4), round(point[1], 4))
                break
            if split_at is None:
                index += 1
                continue
            segs[index : index + 1] = [{**seg, "b": split_at}, {**seg, "a": split_at}]
            pad_nodes[net].add(key(split_at))

    def in_pad(net: str, node) -> bool:
        """Whether a node sits inside a pad of its own net.

        The pad's copper connects everything inside its outline, so a node in
        there is fed whatever the track graph says - and the escape fan draws
        a deliberate micro-hook inside off-grid pads to guarantee the overlap.
        The first version of this cutter read one of those hooks as a
        dangling loop and amputated a pad's only feed with it.
        """
        for _centre, box, _layer in pad_geometry.get(net, ()):
            if box[0] - 0.05 <= node[0] <= box[2] + 0.05 and (
                box[1] - 0.05 <= node[1] <= box[3] + 0.05
            ):
                return True
        return False

    removed: set[int] = set()
    immune: set[int] = set()
    nets = sorted({seg["net"] for seg in segs})
    for net in nets:
        while True:
            indices = [i for i, seg in enumerate(segs) if seg["net"] == net and i not in removed]
            # Immune edges go into the spanning tree first, so the cycle the
            # search reports is always closed by a removable edge - or by
            # nothing, and the net is done.
            indices.sort(key=lambda i: (i not in immune, i))
            graph: dict[tuple, list[tuple]] = defaultdict(list)
            for i in indices:
                a, b = key(segs[i]["a"]), key(segs[i]["b"])
                if a == b:
                    removed.add(i)
                    continue
                graph[a].append((b, i))
                graph[b].append((a, i))
            # a spanning forest; any edge it does not use closes a cycle
            parent: dict[tuple, tuple] = {}

            def find(x, parent=parent):
                while parent.setdefault(x, x) != x:
                    parent[x] = parent[parent[x]]
                    x = parent[x]
                return x

            extra = None
            used: set[int] = set()
            for i in indices:
                if i in removed:
                    continue
                a, b = key(segs[i]["a"]), key(segs[i]["b"])
                ra, rb = find(a), find(b)
                if ra == rb:
                    if i in immune:
                        # an immune closure - a pad hook - is allowed to stand
                        continue
                    extra = i
                    break
                parent[ra] = rb
                used.add(i)
            if extra is None:
                break
            # the cycle: tree path between the extra edge's ends, plus the edge
            start, goal = key(segs[extra]["a"]), key(segs[extra]["b"])
            tree: dict[tuple, list[tuple]] = defaultdict(list)
            for i in used:
                a, b = key(segs[i]["a"]), key(segs[i]["b"])
                tree[a].append((b, i))
                tree[b].append((a, i))
            back: dict[tuple, tuple] = {start: (None, None)}
            queue = [start]
            while queue:
                here = queue.pop(0)
                if here == goal:
                    break
                for nxt, i in tree[here]:
                    if nxt not in back:
                        back[nxt] = (here, i)
                        queue.append(nxt)
            # the cycle as a closed walk: nodes[k] -edge[k]-> nodes[k+1],
            # with the extra edge closing goal back to start
            path_nodes = [goal]
            path_edges = []
            node = goal
            while back.get(node, (None, None))[0] is not None:
                prev, i = back[node]
                path_edges.append(i)
                path_nodes.append(prev)
                node = prev
            cyc_nodes = list(reversed(path_nodes))  # start ... goal
            cyc_edges = [*reversed(path_edges), extra]  # start->...->goal->start
            count = len(cyc_edges)
            if all(in_pad(net, n) for n in cyc_nodes):
                # a loop wholly inside one pad is the escape fan's entry
                # hook, not redundant copper: it is what overlaps the pad
                immune.update(cyc_edges)
                continue
            anchors = [
                k
                for k, n in enumerate(cyc_nodes)
                if n in pad_nodes.get(net, ()) or in_pad(net, n) or len(graph[n]) >= 3
            ]
            if len(anchors) < 2:
                # a loop hanging off at most one point of the tree feeds
                # nothing: all of it is redundant
                removed.update(cyc_edges)
                continue
            # chains: the runs of cycle edges between consecutive anchors,
            # walking the closed cycle once around from the first anchor
            chains = []
            for which, at in enumerate(anchors):
                nxt = anchors[(which + 1) % len(anchors)]
                span = (nxt - at) % count or count
                chains.append([cyc_edges[(at + step) % count] for step in range(span)])

            def chain_len(chain, segs=segs):
                return sum(math.dist(segs[i]["a"], segs[i]["b"]) for i in chain)

            removed.update(max(chains, key=chain_len))

    kept_segs = [seg for i, seg in enumerate(segs) if i not in removed]

    # A via lives if copper still *touches* it, which is not the same question
    # as whether a track ends exactly on it. `_snap_to_45` runs before this and
    # moves ends by a fraction of a millimetre; keying the two together exactly
    # threw away vias whose track had shifted a quarter of a millimetre, and
    # took the layer change with them - two unconnected items on the FPGA
    # board, and a stub at each end of the hole where the via had been.
    def touched(via: Via) -> bool:
        centre = via_position(design, via)
        reach = via.size / 2 + GEOM_EPS
        return any(
            seg["net"] == via.net and _segment_distance(centre, centre, seg["a"], seg["b"]) <= reach
            for seg in kept_segs
        )

    vias = [via for via in design.vias if via.net in keep_nets or touched(via)]
    tracks = passthrough + [
        replace(seg["source"], points=[seg["a"], seg["b"]]) for seg in kept_segs
    ]
    return replace(design, tracks=tracks, vias=vias)


def _copper_oracle(design: Design):
    """The geometry every reshaping pass has to respect, built once.

    ``others`` is every track as resolved points; ``clear(track, own, a, b)``
    says whether the segment a-b may be drawn for that track; ``pinned(net, point)``
    says whether copper may move away from a point - track ends, pad centres,
    and anywhere inside a pad of the net's own, because `write_variant`
    resolves the routes a second time and by then a pad can be an interior
    corner of a merged polyline, touching off-centre. Reshaping through it
    takes the copper off the pad. One oracle, shared by `_straighten` and
    `_doglegged`, so the two passes cannot disagree about what is in the way.
    """
    others = [
        (track.net, track.layer, track.width, [resolve(design, p) for p in track.points])
        for track in design.tracks
    ]
    pads: list[tuple[str | None, tuple[float, float, float, float]]] = []
    pad_points: set[tuple[float, float]] = set()
    for part in design.footprints():
        node = footprint_definition(part.footprint)
        for pad in node.children("pad"):
            number = str(pad.atom(0, ""))
            owner = next(
                (n for n, nodes in design.nets.items() if f"{part.ref}.{number}" in nodes), None
            )
            pads.append((owner, pad_box(design, part, pad)))
            centre = pad_position_of(design, part, pad)
            pad_points.add((round(centre[0], 3), round(centre[1], 3)))

    def clear(track: Track, skip_index: int, a, b) -> bool:
        # 0.26, not the 0.2 the DRC asks: a redrawn segment that lands at
        # exactly the limit fails the check by the width of a rounding
        # error, and one router cell (0.25) of real margin costs nothing.
        slack = 0.26
        half = track.width / 2
        for index, (net, layer, width, points) in enumerate(others):
            if index == skip_index or layer != track.layer:
                continue
            if net == track.net:
                # The net's own copper may be touched - that is a junction -
                # but not crossed: a redraw that slices through its own
                # net's other branch is `route.self_crossing`, the X the
                # loop-cutter just spent its time removing.
                for s0, s1 in pairwise(points):
                    if pcb_review._properly_crosses(a, b, s0, s1):
                        return False
                continue
            for s0, s1 in pairwise(points):
                if _segment_distance(a, b, s0, s1) < half + width / 2 + slack:
                    return False
        for owner, box in pads:
            if owner == track.net:
                continue
            if _segment_to_box(a, b, box) < half + slack:
                return False
        for via in design.vias:
            if via.net == track.net:
                continue
            if _segment_to_box(a, b, _via_box(design, via)) < half + slack:
                return False
        return True

    joins = {
        (round(point[0], 3), round(point[1], 3))
        for _net, _layer, _width, points in others
        for point in (points[0], points[-1])
    }
    joins |= pad_points

    def pinned(net: str, point: tuple[float, float]) -> bool:
        """Whether copper may not move away from this point.

        Track ends and pad centres, and also any point *inside* a pad of the
        net's own: a routed run can touch its pad off-centre - the overlap is
        the connection - and a reshaping pass that only pins the centre pulls
        the copper off the pad. That is one DRC unconnected item and nothing
        visible in the shape, which is exactly how it slipped through.
        """
        if (round(point[0], 3), round(point[1], 3)) in joins:
            return True
        for owner, box in pads:
            if owner != net:
                continue
            if box[0] - 0.05 <= point[0] <= box[2] + 0.05 and (
                box[1] - 0.05 <= point[1] <= box[3] + 0.05
            ):
                return True
        return False

    def update(index: int, resolved_points) -> None:
        """Put a redrawn track's new geometry into the oracle immediately.

        The oracle is a snapshot, and a pass that redraws two tracks against
        the snapshot lets each move into the corridor the other just left -
        on the FPGA board I2S_DIN and I2S_LRCK both doglegged into the same
        lane and ended 0.03 mm apart. Every accepted redraw goes straight
        back into ``others`` so the next decision sees the copper as it is,
        not as it was.
        """
        net, layer, width, _stale = others[index]
        others[index] = (net, layer, width, resolved_points)

    return others, clear, pinned, update


def _straighten(design: Design) -> Design:
    """Take the corners out of a stated route that no longer needs them.

    A hand-placed track's waypoints are written when the parts are somewhere,
    and the parts move. What is left is copper going up, along and back down
    to join two pads that ended up in a straight line with nothing between
    them - which is what a reviewer sees first and has no explanation for.

    Only where the straight line is genuinely clear, and only where the stated
    route is a fifth longer than it: a route shaped on purpose - a feedback
    tap kept away from a switch node, a loop closed deliberately - is shaped
    because something is in the way, and stays.
    """
    others, clear, pinned, update = _copper_oracle(design)

    # A waypoint another track ends on is a join, not a corner: straightening
    # through it leaves the other one in mid air, which is `route.stub` and a
    # net that is no longer connected. So the polyline is cut at its joins and
    # each free stretch between two of them is straightened on its own - a
    # tee in the middle of a run no longer holds the whole run crooked.
    #
    # A pad is a join too, and the reason is worth stating: `write_variant`
    # resolves the routes a second time, so this sees the *routed* design as
    # well as the stated one, and by then the run arriving at a pad and the
    # one leaving it have been merged into a single polyline with the pad as
    # an interior corner. Straightening through that corner takes the copper
    # off the pad and the net is quietly no longer connected - which is not
    # visible in the shape, only in DRC.
    tracks = []
    for own_index, (track, (_net, _layer, _width, points)) in enumerate(
        zip(design.tracks, others, strict=True)
    ):
        if track.auto or len(track.points) < 3:
            tracks.append(track)
            continue
        cuts = [0]
        cuts += [index for index in range(1, len(points) - 1) if pinned(track.net, points[index])]
        cuts.append(len(points) - 1)
        kept = [track.points[0]]
        for first, last in pairwise(cuts):
            if last - first < 2:
                kept.extend(track.points[first + 1 : last + 1])
                continue
            stretch = points[first : last + 1]
            length = sum(math.dist(a, b) for a, b in pairwise(stretch))
            direct = math.dist(stretch[0], stretch[-1])
            if (
                direct > GEOM_EPS
                and length > direct * 1.2
                and _on_45_grid(stretch[0], stretch[-1])
                and clear(track, own_index, stretch[0], stretch[-1])
            ):
                kept.append(track.points[last])
            else:
                kept.extend(track.points[first + 1 : last + 1])
        if len(kept) < len(track.points):
            update(own_index, [resolve(design, point) for point in kept])
            tracks.append(replace(track, points=kept))
        else:
            tracks.append(track)
    return replace(design, tracks=tracks)


def _doglegged(design: Design) -> Design:
    """Redraw a wiggly stretch as the shape a human draws: one 45-degree jog.

    The router thinks in grid cells and its paths keep the cell size as their
    rhythm - the op-amp board's median segment was 0.75 mm against 1.8-3.5 mm
    on KiCad's hand-routed demo boards, and that stutter is most of what makes
    a plot read as autorouted. A person covers the same distance with two
    strokes: the long straight along the dominant direction and one diagonal
    for the offset (interf_u is drawn almost entirely from that shape).

    So every free stretch between joins is offered the two dogleg orderings -
    diagonal first, or straight first - and takes one if it is clear of every
    other net and not longer than what it replaces. Endpoints stay put, so
    connectivity cannot change; pads and track ends are joins, for the same
    reason `_straighten` pins them.
    """
    others, clear, pinned, update = _copper_oracle(design)

    tracks = []
    for own_index, (track, (_net, _layer, _width, points)) in enumerate(
        zip(design.tracks, others, strict=True)
    ):
        if track.auto or len(track.points) < 4:
            tracks.append(track)
            continue
        cuts = [0]
        cuts += [index for index in range(1, len(points) - 1) if pinned(track.net, points[index])]
        cuts.append(len(points) - 1)

        def gentle(u, v) -> bool:
            """Whether continuing from direction u into direction v turns no
            sharper than a right angle. Sharper is a hairpin: the dogleg's own
            corners are 45s by construction, but the seam where it meets the
            copper it did not redraw can turn back on itself, and the chamfer
            pass then carves that seam into the 45-degree acute corner
            `route.acute_angle` reports."""
            lu, lv = math.hypot(*u), math.hypot(*v)
            if lu < GEOM_EPS or lv < GEOM_EPS:
                return True
            return (u[0] * v[0] + u[1] * v[1]) / (lu * lv) >= -1e-6

        def redraw(track, points, lo, hi, own_index=own_index):
            """The stretch as dogleg strokes: whole if clear, else by halves.

            A dense board rarely clears the full stretch in one stroke - some
            other net crosses the middle of it - but the halves either side
            of the crossing usually do clear, and a person would draw exactly
            those. A split is taken only when *both* halves redraw: half a
            dogleg meeting half a staircase is a seam at whatever angle the
            two happened to arrive, and the seams read worse than the stairs.
            """
            a, b = points[lo], points[hi]
            dx, dy = b[0] - a[0], b[1] - a[1]
            if hi - lo >= 3 and not (
                abs(dx) < GEOM_EPS or abs(dy) < GEOM_EPS or abs(abs(dx) - abs(dy)) < GEOM_EPS
            ):
                run = abs(abs(dx) - abs(dy))
                diag = min(abs(dx), abs(dy))
                sx, sy = math.copysign(1, dx), math.copysign(1, dy)
                if abs(dx) > abs(dy):
                    elbows = [(a[0] + sx * run, a[1]), (a[0] + sx * diag, a[1] + sy * diag)]
                else:
                    elbows = [(a[0], a[1] + sy * run), (a[0] + sx * diag, a[1] + sy * diag)]
                length = sum(math.dist(points[i], points[i + 1]) for i in range(lo, hi))
                if run + diag * math.sqrt(2) <= length + GEOM_EPS:
                    for elbow in elbows:
                        elbow = (round(elbow[0], 4), round(elbow[1], 4))
                        into = (
                            gentle(
                                (a[0] - points[lo - 1][0], a[1] - points[lo - 1][1]),
                                (elbow[0] - a[0], elbow[1] - a[1]),
                            )
                            if lo > 0
                            else True
                        )
                        out = (
                            gentle(
                                (b[0] - elbow[0], b[1] - elbow[1]),
                                (points[hi + 1][0] - b[0], points[hi + 1][1] - b[1]),
                            )
                            if hi < len(points) - 1
                            else True
                        )
                        if (
                            into
                            and out
                            and clear(track, own_index, a, elbow)
                            and clear(track, own_index, elbow, b)
                        ):
                            return [elbow]
            if hi - lo >= 6:
                mid = (lo + hi) // 2
                left = redraw(track, points, lo, mid)
                right = redraw(track, points, mid, hi)
                if left is not None and right is not None:
                    seam = points[mid]
                    if gentle(
                        (seam[0] - left[-1][0], seam[1] - left[-1][1]),
                        (right[0][0] - seam[0], right[0][1] - seam[1]),
                    ):
                        return [*left, track.points[mid], *right]
            return None

        kept = [track.points[0]]
        changed = False
        for first, last in pairwise(cuts):
            drawn = redraw(track, points, first, last) if last - first >= 3 else None
            if drawn is None:
                kept.extend(track.points[first + 1 : last + 1])
            else:
                kept.extend(drawn)
                kept.append(track.points[last])
                changed = True
        if changed:
            update(own_index, [resolve(design, point) for point in kept])
            tracks.append(replace(track, points=kept))
        else:
            tracks.append(track)
    return replace(design, tracks=tracks)


def _signed_turn(u: tuple[float, float], v: tuple[float, float]) -> float:
    """The direction change from u to v, in signed degrees."""
    return math.degrees(math.atan2(u[0] * v[1] - u[1] * v[0], u[0] * v[0] + u[1] * v[1]))


def _spread_hairpins(design: Design) -> Design:
    """Give a doubling-back corner pair room to read as a wrap.

    A pin whose escape leaves one way and whose net goes the other has to
    turn back - on the motor board, 135 degrees. The router folds the whole
    reversal into half a millimetre: a 90 and a 45 with a tenth of a
    millimetre between them, each corner blameless on its own, the pair a
    hairpin the eye reads at arm's length - and the reviewer circled both.
    A person makes the same turn with the same two corners a stride apart,
    and it reads as a deliberate wrap. So any two same-direction corners
    summing past 100 degrees within 1.2 mm get the middle leg extended to
    the stride and the exit re-drawn as a dogleg back onto the old path -
    where the board leaves room; where it does not, `route.hairpin` says so.
    """
    others, clear, pinned, update = _copper_oracle(design)
    STRIDE = 1.2

    tracks = []
    for own_index, (track, (_net, _layer, _width, points)) in enumerate(
        zip(design.tracks, others, strict=True)
    ):
        if len(points) < 4:
            tracks.append(track)
            continue
        pts = [tuple(point) for point in points]
        changed = False
        index = 1
        while index < len(pts) - 2:
            a, b, c, d = pts[index - 1], pts[index], pts[index + 1], pts[index + 2]
            u = (b[0] - a[0], b[1] - a[1])
            m = (c[0] - b[0], c[1] - b[1])
            v = (d[0] - c[0], d[1] - c[1])
            middle = math.hypot(*m)
            if min(math.hypot(*u), middle, math.hypot(*v)) < GEOM_EPS or middle >= STRIDE:
                index += 1
                continue
            turn_in, turn_out = _signed_turn(u, m), _signed_turn(m, v)
            if turn_in * turn_out <= 0 or abs(turn_in) + abs(turn_out) < 100.0:
                index += 1
                continue
            if pinned(track.net, b) or pinned(track.net, c):
                index += 1
                continue
            # First choice: no fold at all. Retract along the incoming leg
            # - all the way to its head when the board allows - stand the
            # turn up square, and take the remaining 45s - twelve o'clock,
            # half-past one, three, as the reviewer drew it - rejoining the
            # outgoing straight where the turn meets its line. The fold's
            # own corners disappear instead of being spread.
            rejoin = index + 2
            while rejoin + 1 < len(pts) and not pinned(track.net, pts[rejoin]):
                onward = (
                    pts[rejoin + 1][0] - pts[rejoin][0],
                    pts[rejoin + 1][1] - pts[rejoin][1],
                )
                if abs(_signed_turn(v, onward)) > 1e-6:
                    break
                rejoin += 1
            q = pts[rejoin]
            lu = math.hypot(*u)
            lv = math.hypot(*v)
            h_in = (u[0] / lu, u[1] / lu)
            h_out = (v[0] / lv, v[1] / lv)
            total = _signed_turn(h_in, h_out)
            if abs(abs(total) - 180.0) < 1e-6:
                total = math.copysign(180.0, turn_in)
            steps = round(abs(total) / 45.0)

            def _rot(vec, degrees_):
                r = math.radians(degrees_)
                cr, sr = math.cos(r), math.sin(r)
                return (vec[0] * cr - vec[1] * sr, vec[0] * sr + vec[1] * cr)

            def _cross(p1, p2):
                return p1[0] * p2[1] - p1[1] * p2[0]

            arced = False
            if 3 <= steps <= 4:
                sigma = 45.0 if total > 0 else -45.0
                # Stand the turn up square, then take the 45s: twelve
                # o'clock, half-past one, three. The first 45 - half-past
                # ten - made the turn read as an S; the reviewer wants the
                # turn to stand up straight where the line leaves the part,
                # so the intermediate headings start at 90 degrees.
                mids = [_rot(h_in, sigma * i) for i in range(2, steps)]
                a0 = a
                # Where to land: the outgoing straight - and, when the turn
                # keeps rolling the same way at its far end, the straight
                # after it too. An exit that only runs a couple of
                # millimetres before its next 45 leaves no room to rejoin
                # it without a stub out the old heading; carrying the turn
                # through that 45 and landing on the longer line beyond is
                # what lets the turn start right at the head of the leg.
                frames = [(rejoin, q, h_out, mids)]
                if steps < 4 and not pinned(track.net, q) and rejoin + 1 < len(pts):
                    w = (pts[rejoin + 1][0] - q[0], pts[rejoin + 1][1] - q[1])
                    lw = math.hypot(*w)
                    if lw > GEOM_EPS:
                        h_next = (w[0] / lw, w[1] / lw)
                        if abs(_signed_turn(h_out, h_next) - sigma) < 1e-6:
                            rejoin2 = rejoin + 1
                            while rejoin2 + 1 < len(pts) and not pinned(track.net, pts[rejoin2]):
                                onward = (
                                    pts[rejoin2 + 1][0] - pts[rejoin2][0],
                                    pts[rejoin2 + 1][1] - pts[rejoin2][1],
                                )
                                if abs(_signed_turn(w, onward)) > 1e-6:
                                    break
                                rejoin2 += 1
                            frames.append(
                                (rejoin2, pts[rejoin2], h_next, [*mids, _rot(h_in, sigma * steps)])
                            )
                # No stub out the old heading if it can be helped: turning
                # square right at the head of the incoming straight is the
                # reviewer's ideal - the 45 the escape already carries makes
                # that corner - so lead 0 goes first, and a short stub is
                # kept only where the square turn does not fit.
                leads = [0.0]
                lead = 0.3
                while lead <= lu - 0.3 + 1e-9:
                    leads.append(lead)
                    lead += 0.25
                for lead in leads:
                    if arced:
                        break
                    for rj, qq, ho, mm in frames:
                        sum_mid = (sum(p[0] for p in mm), sum(p[1] for p in mm))
                        per_t = _cross(sum_mid, ho)
                        if abs(per_t) <= 1e-9:
                            continue
                        base = _cross((a0[0] - qq[0], a0[1] - qq[1]), ho)
                        t = -(base + _cross(h_in, ho) * lead) / per_t
                        if t < 0.35:
                            continue
                        if lead < GEOM_EPS and index > 1:
                            prev = pts[index - 2]
                            hp = (a0[0] - prev[0], a0[1] - prev[1])
                            if abs(_signed_turn(hp, mm[0])) > 90.0 + 1e-6:
                                continue
                        point = (a0[0] + h_in[0] * lead, a0[1] + h_in[1] * lead)
                        chain = (
                            [] if lead < GEOM_EPS else [(round(point[0], 4), round(point[1], 4))]
                        )
                        for mid in mm:
                            point = (point[0] + mid[0] * t, point[1] + mid[1] * t)
                            chain.append((round(point[0], 4), round(point[1], 4)))
                        forward = (qq[0] - point[0]) * ho[0] + (qq[1] - point[1]) * ho[1]
                        if forward > GEOM_EPS and all(
                            clear(track, own_index, p1, p2) for p1, p2 in pairwise([a0, *chain, qq])
                        ):
                            pts[index:rj] = chain
                            changed = True
                            arced = True
                            break
            if arced:
                index += 1
                continue
            direction = (m[0] / middle, m[1] / middle)
            spread = (
                round(b[0] + direction[0] * STRIDE, 4),
                round(b[1] + direction[1] * STRIDE, 4),
            )
            # Rejoin at the far end of the outgoing *straight*, not at the
            # first vertex: the exit often carries a redundant collinear
            # point half a millimetre out, and a dogleg aimed at that folds
            # straight back on itself.
            rejoin = index + 2
            while rejoin + 1 < len(pts) and not pinned(track.net, pts[rejoin]):
                onward = (
                    pts[rejoin + 1][0] - pts[rejoin][0],
                    pts[rejoin + 1][1] - pts[rejoin][1],
                )
                if abs(_signed_turn(v, onward)) > 1e-6:
                    break
                rejoin += 1
            d = pts[rejoin]
            delta = (d[0] - spread[0], d[1] - spread[1])
            adx, ady = abs(delta[0]), abs(delta[1])
            run = abs(adx - ady)
            diag = min(adx, ady)
            sx = math.copysign(1.0, delta[0]) if adx > GEOM_EPS else 0.0
            sy = math.copysign(1.0, delta[1]) if ady > GEOM_EPS else 0.0
            if adx >= ady:
                straight_first = (round(spread[0] + sx * run, 4), spread[1])
            else:
                straight_first = (spread[0], round(spread[1] + sy * run, 4))
            diag_first = (round(spread[0] + sx * diag, 4), round(spread[1] + sy * diag, 4))
            # Diagonal first: the reviewer drew the shape - out of the fold's
            # base at twelve o'clock, quarter-to-two, then three - and the
            # diagonal-first order is what produces it. Straight-first put a
            # stub of straight between the turn and the diagonal: one more
            # corner than the turn needs.
            elbows = [diag_first, straight_first]
            # ...checking every new corner, the rejoin at d against the leg
            # that follows it included: easing one hairpin must not fold
            # another at the seam. Both dogleg orders are offered, the way
            # `_doglegged` offers them - a crowded pocket often admits one.
            after = [pts[rejoin + 1]] if rejoin + 1 < len(pts) else []
            for elbow in elbows:
                middle_points = [spread]
                if math.dist(spread, elbow) > GEOM_EPS and math.dist(elbow, d) > GEOM_EPS:
                    middle_points.append(elbow)
                legs = [b, *middle_points, d]
                corners_ok = all(
                    abs(
                        _signed_turn(
                            (q[0] - p[0], q[1] - p[1]),
                            (r[0] - q[0], r[1] - q[1]),
                        )
                    )
                    <= 90.0 + 1e-6
                    for p, q, r in zip(legs, legs[1:], [*legs[2:], *after], strict=False)
                )
                if corners_ok and all(clear(track, own_index, p, q) for p, q in pairwise(legs)):
                    pts[index + 1 : rejoin] = middle_points
                    changed = True
                    break
            index += 1
        if changed:
            update(own_index, pts)
            tracks.append(replace(track, points=pts))
        else:
            tracks.append(track)
    return replace(design, tracks=tracks)


def _teardrops(design: Design) -> Design:
    """Fillet the joint where a track meets a land it is much narrower than.

    A track entering a pad is a step change in width, and a step change is
    where the copper tears: the drill wanders a little, the annulus is a
    little thin, the connector gets levered on and off, and the crack starts
    at the corner where the two meet. A teardrop is the fix every fab asks
    for - copper that widens into the land instead of butting against it -
    and it costs nothing but a few segments.

    Drawn as tracks rather than as KiCad's own teardrop zones: the shapes are
    the same copper either way, and a track is something every version of the
    file format and every gerber writer already understands. Each one is
    checked against its neighbours before it is kept, so a fillet never eats
    the clearance a fine-pitch escape needs - which is also why the pads that
    get one are the roomy ones, headers and through-holes and discretes,
    rather than the 0.5 mm rows where there is nothing to widen into.
    """
    _others, clear, _pinned, _update = _copper_oracle(design)
    # Keyed by position, not by pad name: by the time this runs the router has
    # replaced every named endpoint with the coordinate it resolved to, and a
    # pass that asks for names finds none and quietly does nothing.
    lands: dict[tuple[float, float], tuple[tuple[float, float], float, bool]] = {}
    for part in design.footprints():
        node = footprint_definition(part.footprint)
        for pad in node.children("pad"):
            box = pad_box(design, part, pad)
            centre = pad_position_of(design, part, pad)
            lands[(round(centre[0], 3), round(centre[1], 3))] = (
                centre,
                min(box[2] - box[0], box[3] - box[1]),
                pad.child("drill") is not None,
            )

    grown: list[Track] = []
    kept: list[Track] = []
    for index, track in enumerate(design.tracks):
        points = [resolve(design, point) for point in track.points]
        trimmed = list(points)
        for at, inward in ((0, 1), (-1, -1)):
            end = trimmed[at]
            land = lands.get((round(end[0], 3), round(end[1], 3)))
            if land is None or len(trimmed) < 2:
                continue
            centre, short, drilled = land
            widest = min(short * 0.9, track.width * 3)
            if widest < track.width * 1.6:
                continue  # nothing to widen into: the land is the track's size
            run = trimmed[1] if inward == 1 else trimmed[-2]
            length = math.dist(centre, run)
            reach = min(TEARDROP_MAX_MM if drilled else TEARDROP_MAX_MM * 0.8, length * 0.6, short)
            if reach < 0.3:
                continue
            heading = ((run[0] - centre[0]) / length, (run[1] - centre[1]) / length)
            steps = 3
            fillet: list[Track] = []
            for step in range(steps):
                a = (
                    round(centre[0] + heading[0] * reach * step / steps, 4),
                    round(centre[1] + heading[1] * reach * step / steps, 4),
                )
                b = (
                    round(centre[0] + heading[0] * reach * (step + 1) / steps, 4),
                    round(centre[1] + heading[1] * reach * (step + 1) / steps, 4),
                )
                width = round(widest + (track.width - widest) * (step + 0.5) / steps, 3)
                probe = replace(track, width=width, points=[a, b], auto=False)
                if not clear(probe, index, a, b):
                    fillet = []
                    break
                fillet.append(probe)
            if not fillet:
                continue
            # The taper *replaces* the first stretch of the run rather than
            # lying on top of it. Copper drawn twice is copper drawn twice
            # whatever it is for - `route.acute_angle` reports the nought
            # degree pair, and it is right to.
            trimmed[at] = fillet[-1].points[-1]
            grown.extend(fillet)
        if trimmed != points:
            kept.append(replace(track, points=trimmed))
        else:
            kept.append(track)
    if not grown:
        return design
    return replace(design, tracks=[*kept, *grown])


def _pipeline(design: Design) -> Design:
    """Everything that happens to the copper after the search has found it.

    One list, called from all three exits of `resolve_routes` - the board with
    nothing to route, the cache hit and the fresh route - because three copies
    of a nine-deep nest is how a pass comes to run on two of them.
    """
    design = _snap_to_45(design)
    design = _untraced(design)
    design = _unlooped(design)
    design = _join_runs(design)
    design = _unfold_tracks(design)
    design = _doglegged(design)
    design = _spread_hairpins(design)
    design = _chamfer_tracks(design)
    design = _unspiked(design)
    design = _joined(design)
    design = _landed(design)
    # Landing a joint on its pad moves a track end, and a moved end is a new
    # corner: the de-spike runs again over what that left behind. The fillets
    # go on after all of it, because a taper is built against the end of a run
    # and an end that moves afterwards leaves the taper pointing at nothing.
    design = _unspiked(design)
    design = _welded(design)
    design = _surfaced(design)
    design = _uncrowded(design)
    design = _teardrops(design)
    return _stitched(design)


def _joined(design: Design) -> Design:
    """Put a via back wherever a net changes layer without one.

    The router spends a via at every layer change and hands back where it put
    it. Then half a dozen passes reshape the copper - snapping to 45s,
    chamfering corners, spreading folds - and every one of them moves track
    ends without moving the holes underneath them. A joint that slides a
    millimetre away from its via is two dangling track ends and one
    unconnected net, which is exactly what the FPGA board came back with.

    So the joints are re-derived from the copper that actually got written:
    anywhere copper of one net ends on two layers at the same point and no
    via of that net reaches it, one goes back in. Cheaper than teaching six
    passes to carry the vias with them, and it cannot drift, because it reads
    the finished geometry rather than remembering the routed one.
    """
    ends: dict[tuple[str, tuple[float, float]], set[str]] = defaultdict(set)
    for track in design.tracks:
        points = [resolve(design, point) for point in track.points]
        for end in (points[0], points[-1]):
            ends[(track.net, (round(end[0], 3), round(end[1], 3)))].add(track.layer)
    holes = [(via.net, via_position(design, via), via.size) for via in design.vias]
    added: list[Via] = []
    for (net, point), layers in sorted(ends.items()):
        if len(layers) < 2:
            continue
        if any(
            hole_net == net and math.dist(point, centre) <= size / 2 + GEOM_EPS
            for hole_net, centre, size in holes
        ):
            continue
        added.append(Via(net, x=point[0], y=point[1], size=VIA_SIZE))
        holes.append((net, point, VIA_SIZE))
    if not added:
        return design
    print(
        f"{design.name}: {len(added)} layer change(s) had lost their via to the "
        "reshaping passes, put back",
        file=sys.stderr,
    )
    return replace(design, vias=[*design.vias, *added])


def _drilled_pads(design: Design) -> list[tuple[str | None, tuple[float, float], float]]:
    """Every plated hole on the board, as net, centre and drill radius."""
    out: list[tuple[str | None, tuple[float, float], float]] = []
    for part in design.footprints():
        node = footprint_definition(part.footprint)
        for pad in node.children("pad"):
            drill = pad.child("drill")
            if drill is None:
                continue
            sizes = [a for a in drill.atoms() if isinstance(a, (int, float))]
            if not sizes:
                continue
            number = str(pad.atom(0, ""))
            net = next(
                (n for n, nodes in design.nets.items() if f"{part.ref}.{number}" in nodes), None
            )
            out.append((net, pad_position_of(design, part, pad), float(sizes[0]) / 2))
    return out


def _landed(design: Design) -> Design:
    """A layer change beside a through-hole pad belongs on the pad.

    A plated hole joins the two faces by itself - that is what plating a hole
    is - so a via a millimetre from one of its own net's pads buys nothing and
    costs the thing a fab measures: hole to hole. Two drills a millimetre
    apart leave 0.15 mm of laminate between them at a 0.25 mm rule, and the
    board comes back with a note or does not come back at all.

    So the joint moves onto the pad: every track end at the via slides to the
    pad centre, the via goes, and the copper is joined by the hole that was
    already there. Only a pad of the via's own net will do, and only one near
    enough that the slide cannot reach past what the run already cleared.
    """
    if not design.vias:
        return design
    pads = [entry for entry in _drilled_pads(design) if entry[0]]
    if not pads:
        return design
    moves: dict[tuple[float, float], tuple[float, float]] = {}
    kept: list[Via] = []
    for via in design.vias:
        centre = via_position(design, via)
        landing = next(
            (
                position
                for net, position, radius in pads
                if net == via.net
                # near enough to be the same joint, and close enough that the
                # drills would foul: beyond this the via is a via
                and GEOM_EPS < math.dist(centre, position) <= radius + via.size / 2 + 0.6
            ),
            None,
        )
        if landing is None:
            kept.append(via)
            continue
        moves[(round(centre[0], 3), round(centre[1], 3))] = landing
    if not moves:
        return design
    tracks = []
    for track in design.tracks:
        points = [resolve(design, point) for point in track.points]
        moved = [moves.get((round(x, 3), round(y, 3)), (x, y)) for x, y in points]
        deduped = [moved[0]]
        for point in moved[1:]:
            if math.dist(point, deduped[-1]) > GEOM_EPS:
                deduped.append(point)
        if len(deduped) < 2:
            continue
        tracks.append(replace(track, points=deduped))
    print(
        f"{design.name}: {len(moves)} layer change(s) sat beside a plated hole of "
        "their own net, landed on it",
        file=sys.stderr,
    )
    return replace(design, tracks=tracks, vias=kept)


# How long a back-layer hop may be and still be worth trying to lift. Beyond
# this the front had a reason to refuse it, and the search is wasted.
SURFACE_MAX_MM = 14.0


# Two points nearer than this are one point: the reshaping passes round to
# three decimals, and a chamfer of a chamfer can leave a segment eighteen
# microns long - too short to be copper, long enough to be a corner.
WELD_MM = 0.06


def _welded(design: Design) -> Design:
    """The acute corner `_unspiked` cannot see, because it is not a bend.

    Where a run of one width meets a run of another - a stitched ground tap
    arriving at a routed spine - the corner belongs to neither track, so a pass
    that walks one track's points at a time never meets it. The rule that reads
    the finished board does: it sees copper, not the objects the copper was
    written from, and a reversal there is the same acid trap as any other.

    So the joint is the thing that moves. The two far ends stay where they are
    and the point between them becomes the elbow that joins them without
    reversing, one track keeping each leg and each keeping its own width.

    `_pinned_points` is deliberately not what holds it still. That set pins
    every track end, because moving one of a pair leaves the other in mid air -
    which is the very case this pass exists to handle, and it handles it by
    moving both. What may not move is a pad or a via: copper there is reaching
    something, and the something does not move.
    """

    def key(point: tuple[float, float]) -> tuple[float, float]:
        return (round(point[0], 3), round(point[1], 3))

    anchored = {key(via_position(design, via)) for via in design.vias}
    for part in design.footprints():
        node = footprint_definition(part.footprint)
        for pad in node.children("pad"):
            anchored.add(key(pad_position_of(design, part, pad)))

    # Debris first. A corner cut from a corner can leave a pair of segments a
    # few hundredths of a millimetre long, and the angle between two of those
    # is whatever the rounding made it - 88 degrees, on a run that is visibly
    # straight. Only a point with debris on *both* sides comes out, so a real
    # short leg into a pad keeps its shape.
    tracks = []
    for track in design.tracks:
        points = [resolve(design, point) for point in track.points]
        kept = list(points)
        index = 1
        while index < len(kept) - 1:
            if (
                key(kept[index]) not in anchored
                and math.dist(kept[index - 1], kept[index]) < WELD_MM
                and math.dist(kept[index], kept[index + 1]) < WELD_MM
            ):
                del kept[index]
                index = max(1, index - 1)
                continue
            index += 1
        tracks.append(replace(track, points=kept) if len(kept) != len(points) else track)

    foreign_vias, foreign_pads, around = _route_obstacles(design)
    ends: dict[tuple[str, str, tuple[float, float]], list[tuple[int, int]]] = defaultdict(list)
    for index, track in enumerate(tracks):
        points = [resolve(design, point) for point in track.points]
        for at in (0, len(points) - 1):
            ends[(track.net, track.layer, key(points[at]))].append((index, at))
    moved = 0
    for (_net, _layer, joint), owners in ends.items():
        if len(owners) != 2 or joint in anchored:
            continue
        legs = []
        for index, at in owners:
            points = [resolve(design, point) for point in tracks[index].points]
            if len(points) >= 2:
                legs.append((index, at, points[1] if at == 0 else points[-2]))
        if len(legs) != 2:
            continue
        first, second = legs[0][2], legs[1][2]
        v1 = (joint[0] - first[0], joint[1] - first[1])
        v2 = (second[0] - joint[0], second[1] - joint[1])
        l1, l2 = math.hypot(*v1), math.hypot(*v2)
        if l1 < GEOM_EPS or l2 < GEOM_EPS:
            continue
        if (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2) > -1e-9:
            continue
        neighbours = around(joint)
        for knee in _elbow(first, second):
            if math.dist(knee, first) <= GEOM_EPS or math.dist(knee, second) <= GEOM_EPS:
                continue
            if not all(
                _clear_of(tracks[index], a, b, foreign_vias, foreign_pads, neighbours)
                for index, _at, _far in legs
                for a, b in ((first, knee), (knee, second))
            ):
                continue
            for index, at, _far in legs:
                points = [resolve(design, point) for point in tracks[index].points]
                points[at if at == 0 else -1] = knee
                tracks[index] = replace(tracks[index], points=points)
            moved += 1
            break
    if not moved:
        return design
    print(
        f"{design.name}: {moved} joint(s) between runs of different widths reversed, "
        "moved to the elbow between their neighbours",
        file=sys.stderr,
    )
    return replace(design, tracks=tracks)


def _surfaced(design: Design) -> Design:
    """Lift a short back-layer hop to the front, where the front is empty.

    The back of a two-layer board is a ground plane, and every signal laid
    across it saws the plane in two under its own return current: the return
    goes round the cut, the loop grows by the detour, and `route.return_path`
    measures the result. The router already prices a millimetre on the plane
    side at thirty on the front, but it pays that price *while routing*, before
    the rest of the board exists - so a hop it bought at the time may have
    clear front-side air beside it by the end.

    So each short back-layer run is offered the front once more, against the
    finished board. Where it fits, the copper moves up and the plane heals; and
    a layer change nothing needs any more takes its via with it. Where it does
    not, nothing happens: the run stays where the router put it.
    """
    if not design.pour:
        return design
    foreign_vias, foreign_pads, around = _route_obstacles(design)
    tracks = list(design.tracks)

    def key(point: tuple[float, float]) -> tuple[float, float]:
        return (round(point[0], 3), round(point[1], 3))

    # What the run meets at each end, so a hop lifted between two 0.4 mm runs
    # does not arrive as 0.2 mm and leave two width steps where the via used
    # to be. A width change at a layer change is a change nobody reads; the
    # same change mid-run on one face is `route.width_step`.
    neighbours: dict[tuple[float, float], list[tuple[int, float]]] = defaultdict(list)
    for other_index, other in enumerate(tracks):
        points = [resolve(design, point) for point in other.points]
        for end in (points[0], points[-1]):
            neighbours[key(end)].append((other_index, other.width))

    lifted = 0
    for index, track in enumerate(tracks):
        if track.layer != "B.Cu" or track.net == POUR_NET or track.keep_layer:
            continue
        points = [resolve(design, point) for point in track.points]
        if sum(math.dist(a, b) for a, b in pairwise(points)) > SURFACE_MAX_MM:
            continue
        abutting = {
            round(width, 3)
            for end in (points[0], points[-1])
            for other_index, width in neighbours[key(end)]
            if other_index != index
        }
        # A hop between two runs of the *same* width can be lifted and widened
        # to match them. A hop between runs of two different widths cannot: the
        # step has to happen somewhere, and the tidiest somewhere is the layer
        # change it already had - a width change at a via is a change nobody
        # reads, and the same change mid-run on one face is `route.width_step`.
        if len(abutting) > 1:
            continue
        for width in [next(iter(abutting))] if abutting else [track.width]:
            front = replace(track, layer="F.Cu", width=width)
            if all(
                _clear_of(front, a, b, foreign_vias, foreign_pads, around(a))
                for a, b in pairwise(points)
            ):
                tracks[index] = front
                lifted += 1
                break
    if not lifted:
        return design
    # A via joins two faces, and a face with nothing left under it needs no
    # join. Under it, not only at a run's end: a tap dropped into the middle of
    # a stated back-layer spine has no run ending at it and is still the only
    # way that tap reaches the spine.
    non_front: dict[str, list[tuple[tuple[float, float], tuple[float, float]]]] = defaultdict(list)
    for track in tracks:
        if track.layer == "F.Cu":
            continue
        points = [resolve(design, point) for point in track.points]
        non_front[track.net].extend(pairwise(points))
    vias = [
        via
        for via in design.vias
        if via.net in {POUR_NET, design.power_plane}
        or any(
            _segment_to_point(a, b, via_position(design, via)) <= via.size / 2 + GEOM_EPS
            for a, b in non_front.get(via.net, ())
        )
    ]
    print(
        f"{design.name}: {lifted} back-layer hop(s) had clear air on the front by "
        f"the end, lifted; {len(design.vias) - len(vias)} via(s) went with them",
        file=sys.stderr,
    )
    return replace(design, tracks=tracks, vias=vias)


def _uncrowded(design: Design) -> Design:
    """Merge same-net vias drilled closer than a fabricator will place them.

    Two barrels 0.5 mm apart is `drc.hole_to_hole`, and the FPGA board's +3V3
    came back with a pair: the search spends a via at each end of a hop, and two
    hops that turn round within half a millimetre of each other each get one.
    The reshaping passes then slide the copper without ever bringing the holes
    back together.

    They are the same net and their copper already overlaps, so the pair is
    electrically one hole drilled twice. This replaces it with one via at the
    centre of everything the two were serving - the track ends whose copper each
    of them covers - and only if that one still covers all of it. A via anchored
    to a pad is left alone: it was placed beside that pad on purpose.
    """
    ends: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for track in design.tracks:
        points = [resolve(design, point) for point in track.points]
        for end in (points[0], points[-1]):
            ends[track.net].append((round(end[0], 3), round(end[1], 3)))

    def serving(via: Via) -> list[tuple[float, float]]:
        centre = via_position(design, via)
        return [
            end
            for end in ends.get(via.net, ())
            if math.dist(end, centre) <= via.size / 2 + GEOM_EPS
        ]

    vias = list(design.vias)
    merged = 0
    while True:
        for left, right in combinations(range(len(vias)), 2):
            one, two = vias[left], vias[right]
            if one.net != two.net or one.pad or two.pad:
                continue
            if math.dist(via_position(design, one), via_position(design, two)) >= HOLE_TO_HOLE_MM:
                continue
            wanted = [*serving(one), *serving(two)] or [
                via_position(design, one),
                via_position(design, two),
            ]
            middle = (
                round(sum(p[0] for p in wanted) / len(wanted), 3),
                round(sum(p[1] for p in wanted) / len(wanted), 3),
            )
            size = min(one.size, two.size)
            if any(math.dist(p, middle) > size / 2 + GEOM_EPS for p in wanted):
                continue
            vias[left] = replace(
                one, x=middle[0], y=middle[1], size=size, drill=min(one.drill, two.drill)
            )
            del vias[right]
            merged += 1
            break
        else:
            break
    if not merged:
        return design
    print(
        f"{design.name}: {merged} pair(s) of same-net vias were drilled too close "
        "to each other, merged",
        file=sys.stderr,
    )
    return replace(design, vias=vias)


def _stitched(design: Design) -> Design:
    """Add the stitching vias, once the copper has stopped moving.

    Stitching used to happen before the clean-up passes, and the clean-up
    passes move copper: a snapped segment or a chamfered corner would slide
    into a via that had cleared the square path it replaced. A via has to be
    placed against the geometry that will actually be written, so it goes last.
    """
    return replace(design, vias=[*design.vias, *_stitch_vias(design)])


def _via_box(design: Design, via: Via) -> tuple[float, float, float, float]:
    """A via as the square `check_board` measures it, not as a circle."""
    vx, vy = via_position(design, via)
    half = via.size / 2
    return (vx - half, vy - half, vx + half, vy + half)


def _snap_to_45(design: Design) -> Design:
    """Rewrite any segment that runs at an angle nobody chose.

    The router works on a grid, so its own moves are axis-aligned or 45s. The
    pads are not on that grid - a footprint puts them where the package says -
    so the one segment joining a routed path to a pad lands at whatever angle
    the arithmetic produced. That is where a board's twenty-degree bends come
    from, and it is why they cluster at pin entries on the plot rather than
    anywhere a person would have drawn them.

    Each offending segment becomes two that are on the grid: a straight leg
    along its dominant axis, then a 45 into the pad. The pair stays inside the
    original segment's bounding box, so it cannot reach anything the straight
    line did not already pass.
    """
    # The straight line already cleared everything, so a knee that does not is
    # simply not taken: an angle nobody chose beats a board that will not build.
    others: list[tuple[str, str, float, tuple, tuple]] = []
    for track in design.tracks:
        points = [resolve(design, point) for point in track.points]
        others.extend((track.net, track.layer, track.width, a, b) for a, b in pairwise(points))
    pads: list[tuple[str | None, str | None, tuple]] = []
    for part in design.footprints():
        node = footprint_definition(part.footprint)
        for pad in node.children("pad"):
            number = str(pad.atom(0, ""))
            owner = next(
                (n for n, nodes in design.nets.items() if f"{part.ref}.{number}" in nodes), None
            )
            pads.append((owner, pad_layer(pad), pad_box(design, part, pad)))
    # A via is on every layer, so a knee taken on either face has to clear it.
    # The straight segment this replaces did clear it - that is exactly why it
    # was routed there - and the knee moves copper, so it has to ask again.
    # Squared off, the way `check_board` measures a via: a round via inscribed
    # in the box clears where the box does not, and the check that decides
    # whether the board builds is the one to agree with.
    vias = [(via.net, _via_box(design, via)) for via in design.vias]

    def clear(track: Track, a, b) -> bool:
        half = track.width / 2
        for net, layer, width, s0, s1 in others:
            if net == track.net or layer != track.layer:
                continue
            if _segment_distance(a, b, s0, s1) < half + width / 2 + 0.2:
                return False
        for owner, layer, box in pads:
            if owner == track.net or layer not in (None, track.layer):
                continue
            if _segment_to_box(a, b, box) < half + 0.2:
                return False
        for net, box in vias:
            if net == track.net:
                continue
            if _segment_to_box(a, b, box) < half + 0.2:
                return False
        return True

    tracks = []
    for track in design.tracks:
        points = [resolve(design, point) for point in track.points]
        out: list[tuple[float, float]] = [points[0]]
        for a, b in pairwise(points):
            dx, dy = b[0] - a[0], b[1] - a[1]
            adx, ady = abs(dx), abs(dy)
            straight = adx < GEOM_TOL or ady < GEOM_TOL or abs(adx - ady) < GEOM_TOL
            if not straight:
                if adx > ady:
                    knee = (round(b[0] - math.copysign(ady, dx), 4), a[1])
                else:
                    knee = (a[0], round(b[1] - math.copysign(adx, dy), 4))
                # The knee is taken even when a leg comes out shorter than the
                # track is wide, which leaves a nub `route.acute_angle` reports.
                # Refusing it is worse: the segment then keeps the angle it had,
                # and a twentieth of a millimetre of nub beats a nine
                # millimetre run at 169 degrees. Measured both ways - declining
                # short knees put 157 off-grid segments back across the five
                # boards.
                if (
                    math.dist(out[-1], knee) > GEOM_TOL
                    and math.dist(knee, b) > GEOM_TOL
                    and clear(track, out[-1], knee)
                    and clear(track, knee, b)
                ):
                    out.append(knee)
            out.append(b)
        tracks.append(replace(track, points=out))
    return replace(design, tracks=tracks)


def _join_runs(design: Design) -> Design:
    """Join tracks that are one run written as two.

    A route arrives as several ``Track`` objects - an escape, then what the
    router found, then a hand-placed leg - and where two of them meet nothing
    marks the join as a join. The corner there is a corner like any other, but
    the chamfer only sees inside a single polyline, so those were the square
    corners left on the plots. Merging first makes them interior, and the
    chamfer does the rest.

    Only a point where exactly two ends meet is a join: three is a tee, and a
    tee is a connection somebody meant.
    """

    def key(point: tuple[float, float]) -> tuple[float, float]:
        return (round(point[0], 3), round(point[1], 3))

    ends: dict[tuple[float, float], int] = defaultdict(int)
    resolved = []
    for track in design.tracks:
        points = [resolve(design, point) for point in track.points]
        resolved.append(points)
        ends[key(points[0])] += 1
        ends[key(points[-1])] += 1

    runs = [[track, list(points)] for track, points in zip(design.tracks, resolved, strict=True)]
    merged = True
    while merged:
        merged = False
        for i, (ta, pa) in enumerate(runs):
            if ta is None:
                continue
            for j in range(i + 1, len(runs)):
                tb, pb = runs[j]
                if tb is None:
                    continue
                if (ta.net, ta.layer, ta.width) != (tb.net, tb.layer, tb.width):
                    continue
                for a_end, b_end in ((-1, 0), (-1, -1), (0, 0), (0, -1)):
                    if math.dist(pa[a_end], pb[b_end]) > GEOM_TOL:
                        continue
                    if ends[key(pa[a_end])] != 2:
                        continue
                    left = pa if a_end == -1 else pa[::-1]
                    right = pb if b_end == 0 else pb[::-1]
                    runs[i][0] = replace(ta, keep_layer=ta.keep_layer or tb.keep_layer)
                    runs[i][1] = left + right[1:]
                    runs[j][0] = None
                    merged = True
                    break
                if merged:
                    break
            if merged:
                break
    return replace(
        design,
        tracks=[replace(t, points=p) for t, p in runs if t is not None],
    )


def _corner_ok(p: tuple[float, float], a: tuple[float, float], c: tuple[float, float]) -> bool:
    """Whether p-a-c is a bend a board is allowed to have: 90 degrees or wider."""
    v1 = (a[0] - p[0], a[1] - p[1])
    v2 = (c[0] - a[0], c[1] - a[1])
    l1, l2 = math.hypot(*v1), math.hypot(*v2)
    if l1 < GEOM_EPS or l2 < GEOM_EPS:
        return True
    return (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2) >= -GEOM_EPS


def _pinned_points(design: Design) -> set[tuple[float, float]]:
    """Every point on the copper that no clean-up pass may move.

    Three kinds. The end of a track, because another track is joined to it and
    moving one leaves the other in mid air - `route.stub`. A via, because the
    hole cannot follow a chamfer that cuts its corner away. And a pad, because
    once `_join_runs` has merged the run arriving at a pad with the one leaving
    it, the pad is an interior corner of one polyline like any other, and a
    chamfer will happily cut it off the pad it was there to reach. Each is one
    unconnected net per cut, and it is not visible in the shape.
    """
    pinned: set[tuple[float, float]] = set()
    for track in design.tracks:
        points = [resolve(design, point) for point in track.points]
        for point in (points[0], points[-1]):
            pinned.add((round(point[0], 3), round(point[1], 3)))
    for via in design.vias:
        point = via_position(design, via)
        pinned.add((round(point[0], 3), round(point[1], 3)))
    for part in design.footprints():
        node = footprint_definition(part.footprint)
        for pad in node.children("pad"):
            point = pad_position_of(design, part, pad)
            pinned.add((round(point[0], 3), round(point[1], 3)))
    return pinned


def _unfold_tracks(design: Design) -> Design:
    """Drop the copper a run lays down and then walks back along.

    Joining two routes at a shared end can leave the polyline going out past
    the join and straight back to it - a 0 degree corner, which is what
    ``route.acute_angle`` reports and what reads on the plot as a spur nobody
    can explain. The overshoot carries no current, so it comes out.

    Only an exact reversal, and only where the walk back stops short of where
    it set out from: a run that retraces *past* its own start is a routing
    mistake, not a stray point, and deleting a point would move copper the
    board still needs. A corner another track ends on stays too - it is a
    join, however it looks.
    """

    def key(point: tuple[float, float]) -> tuple[float, float]:
        return (round(point[0], 3), round(point[1], 3))

    pinned = _pinned_points(design)
    resolved = [[resolve(design, point) for point in track.points] for track in design.tracks]

    tracks = []
    for track, points in zip(design.tracks, resolved, strict=True):
        out = list(points)
        changed = True
        while changed and len(out) >= 3:
            changed = False
            for index in range(1, len(out) - 1):
                a, b, c = out[index - 1], out[index], out[index + 1]
                v1 = (b[0] - a[0], b[1] - a[1])
                v2 = (c[0] - b[0], c[1] - b[1])
                l1, l2 = math.hypot(*v1), math.hypot(*v2)
                if l1 < GEOM_EPS or l2 < GEOM_EPS:
                    continue
                if (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2) > -0.999:
                    continue
                if key(b) in pinned:
                    continue
                if l2 >= l1 and index > 1 and not _corner_ok(out[index - 2], a, c):
                    # Walking back past where it set out from moves the corner
                    # before this one. Usually that corner is the other half of
                    # the same fold and both go; where it is not, the run keeps
                    # its ugly spur rather than trade it for a worse bend.
                    continue
                del out[index]
                changed = True
                break
        tracks.append(replace(track, points=out))
    return replace(design, tracks=tracks)


def _untraced(design: Design) -> Design:
    """Drop the copper a second run lays down on top of the first.

    Two runs of one net that meet at a point and leave it along the *same*
    line are one run drawn twice. The shorter is inside the longer, carries
    no current the longer is not already carrying, and on the plot reads as a
    track that stops in mid air. `route.acute_angle` reports it as a corner of
    nought degrees, which is exactly what it is: on the FPGA board one of them
    was nine millimetres of track laid back along itself.

    `_unfold_tracks` catches this inside a single polyline. It cannot catch it
    between two, because there is no corner in either one to remove - the fold
    is in the pair. So this runs *before* `_join_runs`, while each routed run
    is still its own track and the duplicate is still two tracks sharing an
    end. Afterwards the pair has become one polyline with the shared point in
    its middle, and that point is usually a pad - which the clean-up passes
    pin, precisely so they cannot take copper off it.
    """
    points = [[resolve(design, point) for point in track.points] for track in design.tracks]

    def key(point):
        return (round(point[0], 3), round(point[1], 3))

    # A shared point that is a pad has to stay reached: the copper may double
    # back over itself there, but something must still touch the pad.
    pad_points = set()
    for part in design.footprints():
        node = footprint_definition(part.footprint)
        for pad in node.children("pad"):
            pad_points.add(key(pad_position_of(design, part, pad)))

    ends: dict[tuple, list[tuple[int, int]]] = defaultdict(list)
    for index, chain in enumerate(points):
        if len(chain) < 2:
            continue
        ends[(key(chain[0]), design.tracks[index].layer, design.tracks[index].net)].append(
            (index, 0)
        )
        ends[(key(chain[-1]), design.tracks[index].layer, design.tracks[index].net)].append(
            (index, -1)
        )

    drop: dict[int, set[int]] = defaultdict(set)
    move: dict[tuple[int, int], tuple[float, float]] = {}
    for (at, _layer, _net), owners in ends.items():
        if len(owners) != 2:
            continue
        (one, one_end), (two, two_end) = owners
        if one == two:
            continue
        here = (at[0], at[1])
        legs = []
        for index, end in ((one, one_end), (two, two_end)):
            chain = points[index]
            far = chain[1] if end == 0 else chain[-2]
            legs.append((math.dist(here, far), index, end, far))
        (len_a, _ia, _ea, far_a), (len_b, _ib, _eb, far_b) = legs
        if len_a < GEOM_EPS or len_b < GEOM_EPS:
            continue
        v1 = ((far_a[0] - here[0]) / len_a, (far_a[1] - here[1]) / len_a)
        v2 = ((far_b[0] - here[0]) / len_b, (far_b[1] - here[1]) / len_b)
        dot = v1[0] * v2[0] + v1[1] * v2[1]
        if dot >= 0.999:
            # Both leave along the same ray, so the shorter is inside the
            # longer: it carries nothing the longer does not, and its far end
            # still sits on copper once it is gone. The longer one comes back
            # to that far end too - past it the pair was only travelling out
            # and straight back. Dropping the short leg alone leaves the long
            # one hanging over the gap, which is `route.stub`.
            (_short, short_i, short_e, short_far), (_long, long_i, long_e, _far) = sorted(legs)
            drop[short_i].add(0 if short_e == 0 else len(points[short_i]) - 1)
            if (round(here[0], 3), round(here[1], 3)) not in pad_points:
                move[(long_i, 0 if long_e == 0 else len(points[long_i]) - 1)] = short_far
            continue
        if dot <= 0.0:
            continue
        # An acute meeting whose short leg is thinner than the track is wide:
        # the run overshot the point where the two legs actually cross and
        # came back. The overshoot is inside the other leg's own copper, so
        # pulling the meeting point back to the crossing costs nothing and
        # turns a nought-point-one millimetre spur into a corner.
        (short_len, short_i, short_e, behind), (_long_len, long_i, long_e, long_far) = sorted(legs)
        width = max(design.tracks[short_i].width, design.tracks[long_i].width)
        if short_len >= width:
            continue
        chain = points[short_i]
        if any(
            math.dist(other, here) < width + 0.2
            for at_index, other_chain in enumerate(points)
            if at_index not in (short_i, long_i)
            for other in (other_chain[0], other_chain[-1])
            if other_chain
        ):
            continue
        along = (long_far[0] - here[0], long_far[1] - here[1])
        span = math.hypot(*along)
        if span < GEOM_EPS:
            continue
        unit = (along[0] / span, along[1] / span)
        offset = (behind[0] - here[0]) * unit[0] + (behind[1] - here[1]) * unit[1]
        if not 0.0 < offset < span:
            continue
        crossing = (
            round(here[0] + unit[0] * offset, 4),
            round(here[1] + unit[1] * offset, 4),
        )
        move[(short_i, 0 if short_e == 0 else len(chain) - 1)] = crossing
        move[(long_i, 0 if long_e == 0 else len(points[long_i]) - 1)] = crossing

    if not drop and not move:
        return design
    tracks = []
    for index, track in enumerate(design.tracks):
        if index not in drop and not any(key[0] == index for key in move):
            tracks.append(track)
            continue
        kept = [
            move.get((index, at), point)
            for at, point in enumerate(track.points)
            if at not in drop[index]
        ]
        trimmed = [
            point
            for at, point in enumerate(kept)
            if at == 0
            or math.dist(resolve(design, point), resolve(design, kept[at - 1])) > GEOM_EPS
        ]
        if len(trimmed) >= 2:
            tracks.append(replace(track, points=trimmed))
    return replace(design, tracks=tracks)


def _route_obstacles(design: Design):
    """What a reshaping pass has to miss: other nets' vias, pads and copper.

    Returned as three things rather than one, because that is how the passes
    ask: two lists to scan and a lookup that answers "what is near this point"
    without walking every segment on the board.
    """
    foreign_vias = [(via.net, _via_box(design, via)) for via in design.vias]
    foreign_pads: list[tuple[str | None, tuple[float, float, float, float]]] = []
    for part in design.footprints():
        node = footprint_definition(part.footprint)
        for pad in node.children("pad"):
            number = str(pad.atom(0, ""))
            owner = next(
                (n for n, nodes in design.nets.items() if f"{part.ref}.{number}" in nodes), None
            )
            foreign_pads.append((owner, pad_box(design, part, pad)))
    CELL = 4.0
    near: dict[tuple[int, int], list[tuple]] = defaultdict(list)
    for other in design.tracks:
        points = [resolve(design, point) for point in other.points]
        for a, b in pairwise(points):
            entry = (other.net, other.layer, other.width, a, b)
            for cx in range(int(min(a[0], b[0]) // CELL), int(max(a[0], b[0]) // CELL) + 1):
                for cy in range(int(min(a[1], b[1]) // CELL), int(max(a[1], b[1]) // CELL) + 1):
                    near[(cx, cy)].append(entry)

    def around(point: tuple[float, float]) -> list[tuple]:
        cx, cy = int(point[0] // CELL), int(point[1] // CELL)
        found: list[tuple] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                found += near.get((cx + dx, cy + dy), ())
        return found

    return foreign_vias, foreign_pads, around


def _elbow(
    start: tuple[float, float], end: tuple[float, float]
) -> list[tuple[float, float, float, float]]:
    """The two ways to get from one point to another in 45 degree geometry.

    Diagonal first or straight first, and nothing else: both are the shortest
    path the grid admits, both leave `start` heading at `end`, and neither
    steps back to a heading the line has already left - which is the whole
    complaint about the jog this replaces.
    """
    dx, dy = end[0] - start[0], end[1] - start[1]
    sx = math.copysign(1.0, dx) if dx else 0.0
    sy = math.copysign(1.0, dy) if dy else 0.0
    run = min(abs(dx), abs(dy))
    diagonal_first = (round(start[0] + sx * run, 3), round(start[1] + sy * run, 3))
    if abs(dx) >= abs(dy):
        straight_first = (round(end[0] - sx * run, 3), round(start[1], 3))
    else:
        straight_first = (round(start[0], 3), round(end[1] - sy * run, 3))
    return [diagonal_first, straight_first]


def _clear_of(
    track: Track,
    a: tuple[float, float],
    b: tuple[float, float],
    foreign_vias: list[tuple[str | None, tuple[float, float, float, float]]],
    foreign_pads: list[tuple[str | None, tuple[float, float, float, float]]],
    neighbours: list[tuple],
) -> bool:
    """Whether a proposed piece of copper misses everything of another net."""
    half = track.width / 2
    return not (
        any(
            net != track.net and _segment_to_box(a, b, box) < half + 0.25
            for net, box in foreign_vias
        )
        or any(
            net != track.net
            and layer == track.layer
            and _segment_distance(a, b, s0, s1) < half + width / 2 + 0.25
            for net, layer, width, s0, s1 in neighbours
        )
        or any(
            owner != track.net and _segment_to_box(a, b, box) < half + 0.25
            for owner, box in foreign_pads
        )
    )


def _unspiked(design: Design) -> Design:
    """Corners tighter than a right angle, redrawn without the step back.

    A router that has to land on a 45 sometimes buys the angle with a quarter
    of a millimetre in the wrong direction first: out of the via, up, and then
    down and to the left. Every leg is legal and the pair is a spike - an acid
    trap on the board and, to a reader, a line that changed its mind.

    The two points either side of the spike are kept and the point between them
    is replaced by the elbow that joins them without reversing: the same L a
    hand would draw, one diagonal and one straight, in whichever order fits.
    Neither is ever longer than what it replaces, so nothing is spent on this
    but the check that the new copper still clears everything the old did.
    """
    foreign_vias, foreign_pads, around = _route_obstacles(design)
    pinned = _pinned_points(design)
    tracks = []
    for track in design.tracks:
        points = [resolve(design, point) for point in track.points]
        if len(points) < 3:
            tracks.append(track)
            continue
        out = list(points)
        changed = False
        index = 1
        while index < len(out) - 1:
            prev, corner, nxt = out[index - 1], out[index], out[index + 1]
            v1 = (corner[0] - prev[0], corner[1] - prev[1])
            v2 = (nxt[0] - corner[0], nxt[1] - corner[1])
            l1, l2 = math.hypot(*v1), math.hypot(*v2)
            if (
                l1 < GEOM_EPS
                or l2 < GEOM_EPS
                or (round(corner[0], 3), round(corner[1], 3)) in pinned
                # the interior angle: a dot product below zero is the reversing
                # half of the circle, which is every turn sharper than a right
                # angle
                or (v1[0] * v2[0] + v1[1] * v2[1]) / (l1 * l2) > -1e-9
            ):
                index += 1
                continue
            neighbours = around(corner)
            for knee in _elbow(prev, nxt):
                legs = [
                    point
                    for point in (knee,)
                    if math.dist(point, prev) > GEOM_EPS and math.dist(point, nxt) > GEOM_EPS
                ]
                if all(
                    _clear_of(track, a, b, foreign_vias, foreign_pads, neighbours)
                    for a, b in pairwise([prev, *legs, nxt])
                ):
                    out[index : index + 1] = legs
                    changed = True
                    break
            index += 1
        if not changed:
            tracks.append(track)
            continue
        deduped = [out[0]]
        for point in out[1:]:
            if math.dist(point, deduped[-1]) > GEOM_EPS:
                deduped.append(point)
        tracks.append(replace(track, points=deduped))
    return replace(design, tracks=tracks)


def _chamfer_tracks(design: Design, cut: float = 1.5) -> Design:
    """Every square corner cut to two 45s - copper bends, it does not turn.

    A right angle in a track is free to avoid and mildly costly to keep: the
    outer corner over-etches, the impedance steps, and every reviewer reads it
    as a router nobody checked. The cut is capped at half of either leg so a
    short pad entry keeps its shape, and a corner that another track tees
    into is left alone rather than cut out from under the join.
    """

    def key(point: tuple[float, float]) -> tuple[float, float]:
        return (round(point[0], 3), round(point[1], 3))

    pinned = _pinned_points(design)
    resolved = [[resolve(design, point) for point in track.points] for track in design.tracks]
    # A cut moves copper off the square path, and what it moves toward may be
    # a via, a pad, or another net's track that only cleared the original
    # corner. All three have to be asked, and the answer for all three is the
    # same: leave the corner square rather than build a short.
    foreign_vias = [(via.net, _via_box(design, via)) for via in design.vias]
    # And the net's own vias: a via the router dropped a fraction off a corner
    # sits under the leg, not on the corner point, so nothing pins it - and a
    # cut that shortens that leg past the via leaves the via joined to one
    # face. The FPGA board's 3.3 V rail lost a via that way.
    own_vias: dict[str, list[tuple[tuple[float, float], float]]] = defaultdict(list)
    for via in design.vias:
        own_vias[via.net].append((via_position(design, via), via.size))
    # Bucketed by a coarse grid: a board this size carries thousands of
    # segments and a corner only cares about the ones beside it, so asking all
    # of them turns a minute into an hour.
    CELL = 4.0
    near_tracks: dict[tuple[int, int], list[tuple]] = defaultdict(list)
    for other, points in zip(design.tracks, resolved, strict=True):
        for a, b in pairwise(points):
            entry = (other.net, other.layer, other.width, a, b)
            for cx in range(int(min(a[0], b[0]) // CELL), int(max(a[0], b[0]) // CELL) + 1):
                for cy in range(int(min(a[1], b[1]) // CELL), int(max(a[1], b[1]) // CELL) + 1):
                    near_tracks[(cx, cy)].append(entry)

    def around(point: tuple[float, float]) -> list[tuple]:
        cx, cy = int(point[0] // CELL), int(point[1] // CELL)
        found: list[tuple] = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                found += near_tracks.get((cx + dx, cy + dy), ())
        return found

    foreign_pads: list[tuple[str | None, tuple[float, float, float, float]]] = []
    for part in design.footprints():
        node = footprint_definition(part.footprint)
        for pad in node.children("pad"):
            number = str(pad.atom(0, ""))
            owner = next(
                (n for n, nodes in design.nets.items() if f"{part.ref}.{number}" in nodes), None
            )
            foreign_pads.append((owner, pad_box(design, part, pad)))

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
            if gentle or key(corner) in pinned:
                out.append(corner)
                continue
            # A big cut reads best; a small one still beats a square corner.
            # Try the full one, then halves of it, before giving the corner up.
            taken = None
            for factor in (1.0, 0.5, 0.25, 0.12):
                c = min(cut * factor, l1 / 2, l2 / 2)
                if c < 0.15:
                    break
                p1 = (
                    round(corner[0] - v1[0] / l1 * c, 3),
                    round(corner[1] - v1[1] / l1 * c, 3),
                )
                p2 = (
                    round(corner[0] + v2[0] / l2 * c, 3),
                    round(corner[1] + v2[1] / l2 * c, 3),
                )
                blocked = (
                    any(
                        math.dist(corner, centre) <= c + size / 2 + GEOM_EPS
                        for centre, size in own_vias[track.net]
                    )
                    or any(
                        net != track.net and _segment_to_box(p1, p2, box) < track.width / 2 + 0.25
                        for net, box in foreign_vias
                    )
                    or any(
                        net != track.net
                        and layer == track.layer
                        and _segment_distance(p1, p2, s0, s1) < track.width / 2 + width / 2 + 0.25
                        for net, layer, width, s0, s1 in around(corner)
                    )
                    or any(
                        owner != track.net and _segment_to_box(p1, p2, box) < track.width / 2 + 0.25
                        for owner, box in foreign_pads
                    )
                )
                if not blocked:
                    taken = (p1, p2)
                    break
            if taken is None:
                out.append(corner)  # nothing fitted; a square corner it stays
                continue
            out.append(taken[0])
            out.append(taken[1])
        out.append(points[-1])
        # two half-leg cuts meeting mid-segment leave a duplicate point, and a
        # duplicate point emits as a zero-length track DRC calls dangling
        deduped = [out[0]]
        for point in out[1:]:
            if math.dist(point, deduped[-1]) > GEOM_EPS:
                deduped.append(point)
        tracks.append(replace(track, points=deduped))
    # Two tracks that draw the same copper are one track drawn twice: the plot
    # cannot tell, but every endpoint count can, and a doubled end reads to the
    # angle rule as a run folding back on itself.
    seen: set[tuple] = set()
    unique = []
    for track in tracks:
        key = (
            track.net,
            track.layer,
            round(track.width, 4),
            tuple((round(x, 3), round(y, 3)) for x, y in track.points),
        )
        reverse = (key[0], key[1], key[2], tuple(reversed(key[3])))
        if key in seen or reverse in seen:
            continue
        seen.add(key)
        unique.append(track)
    return replace(design, tracks=unique)


# What the gap-filling pass below aims for, measured round the *ring* - which
# is not what the rule measures. `emc.stitching_pitch` walks the board
# outline, and the ring sits inset from it, so a stretch that is under
# eighteen millimetres here is longer out there: the shortfall is the inset
# again at each corner the stretch crosses. Fourteen leaves room for that
# without laying a via the rule was never going to ask for.
RIM_MAX_GAP_MM = 14.0

# How many times the fill pass may halve what is left. Each round can only put
# one via in a gap, so getting a via onto both sides of a blocked corner needs
# a second, and a third round is where it stops finding anything new.
RIM_FILL_ROUNDS = 4


def _stitch_vias(design: Design) -> list[Via]:
    """Ground vias over the pour, so no face's copper floats.

    Two jobs, one mechanism. Around the rim, the front pour would otherwise
    hang on whatever pads it happens to touch, and the boundary is where
    edge-coupled noise wants a short way home. Across the middle it matters
    more: the clearance channels shred the front pour into pieces there, and a
    piece that touches no ground pad of its own is not poured copper at all -
    the filler drops it as an orphan, which is how a plane becomes the blank
    a reviewer sees. A via every few millimetres gives each piece something to
    hold onto, and it is also the return path the plane was poured for.

    Candidates on a grid, kept only where they clear every pad, every foreign
    track and every existing hole.
    """
    if not design.pour:
        return []
    x0, y0, x1, y1 = design.pour
    inset = 1.5
    rim: list[tuple[float, float]] = []
    step = 10.0
    left, top, right, bottom = x0 + inset, y0 + inset, x1 - inset, y1 - inset
    # The ring carries the direction its edge runs in, because a candidate
    # that lands on a pad is not a candidate to give up on: it is one to slide
    # along the rim until it finds room. Dropping it instead is what left the
    # boards with 20-40 mm gaps against `emc.stitching_pitch`'s 18 - the
    # congested stretches, which are the ones the rule is about, were exactly
    # the stretches where every candidate collided with something.
    # (x, y, along, inward): the direction the edge runs in, and the direction
    # that goes deeper into the band rather than out over the board edge.
    ring: list[tuple[float, float, tuple[float, float], tuple[float, float]]] = []

    def _stations(start: float, end: float) -> list[float]:
        """Both corners, and evenly spaced stations no more than a step apart.

        Stepping from one corner and stopping when the next is overshot leaves
        the last stretch short of it: a 55 mm edge on a 10 mm step ends five
        millimetres early, and the rule measures round the corner, so the run
        from there to the first station on the next side is two spans. That is
        the 20 mm gap that survived on the buck board once the sliding was in.
        """
        spans = max(1, math.ceil((end - start) / step - 0.01))
        return [start + (end - start) * i / spans for i in range(spans + 1)]

    for x in _stations(left, right):
        ring.append((round(x, 2), round(top, 2), (1.0, 0.0), (0.0, 1.0)))
        ring.append((round(x, 2), round(bottom, 2), (1.0, 0.0), (0.0, -1.0)))
    for y in _stations(top, bottom)[1:-1]:  # the corners came from the sides
        ring.append((round(left, 2), round(y, 2), (0.0, 1.0), (1.0, 0.0)))
        ring.append((round(right, 2), round(y, 2), (0.0, 1.0), (-1.0, 0.0)))
    # The interior: purpose first, mesh second. A hand-stitched board puts
    # its vias where the plane needs them - beside every place a signal
    # crosses on the back layer, because that is where the plane is cut and
    # the return current needs a way over the cut - and only then scatters a
    # coarse mesh so no orphaned piece of pour is left floating. The 6 mm
    # carpet this used to lay reads as a printed pattern from across the
    # room; KiCad's own demo boards run 0.3-16 vias per decimetre of track
    # and none of them is a uniform grid.
    for track in design.tracks:
        if track.net == POUR_NET or track.layer != "B.Cu":
            continue
        points = [resolve(design, point) for point in track.points]
        for end in (points[0], points[-1]):
            for dx, dy in ((2.0, 0.0), (-2.0, 0.0), (0.0, 2.0), (0.0, -2.0)):
                rim.append((round(end[0] + dx, 2), round(end[1] + dy, 2)))
    inner = 12.0
    y = top + inner / 2
    while y < bottom - 0.01:
        x = left + inner / 2
        while x < right + 0.01:
            rim.append((round(x, 2), round(y, 2)))
            x += inner
        y += inner

    pads = []
    for part in design.footprints():
        node = footprint_definition(part.footprint)
        for pad in node.children("pad"):
            number = str(pad.atom(0, ""))
            net = next(
                (n for n, nodes in design.nets.items() if f"{part.ref}.{number}" in nodes),
                None,
            )
            # A pad may carry its own clearance, and the ones that do mean it:
            # a fiducial asks for 0.6 mm so the camera sees copper against bare
            # laminate. Taking the flat default instead is how a slid via
            # landed half a millimetre from FID1 and failed DRC.
            pads.append((pad_box(design, part, pad), net, float(pad.value("clearance", 0) or 0)))
    segments = []
    for track in design.tracks:
        points = [resolve(design, point) for point in track.points]
        segments.extend((track.net, track.width, a, b) for a, b in pairwise(points))
    holes = [(via_position(design, via), via.size) for via in design.vias]

    def clears(vx: float, vy: float) -> bool:
        radius = 0.4
        # Inside the pour, by the via's own size plus the edge rule's
        # clearance: a candidate offset from a track end can land past the
        # pour and onto the outline itself, where it is both an edge
        # violation and an orphan the fill never reaches.
        if not (x0 + 0.7 <= vx <= x1 - 0.7 and y0 + 0.7 <= vy <= y1 - 0.7):
            return False
        for (bx0, by0, bx1, by1), _net, stated in pads:
            # Same distance whoever owns the pad. A ground via touching a
            # ground land is still a hole in a land, and solder wicks down it
            # exactly as it does on a signal pad; the plane gains nothing from
            # the two being one piece of copper here rather than a stub away.
            grow = radius + max(0.3, stated)
            if bx0 - grow <= vx <= bx1 + grow and by0 - grow <= vy <= by1 + grow:
                return False
        for net, width, a, b in segments:
            if net == POUR_NET:
                continue
            # The via is square to `check_board`, so it is square here
            # too: measured as a circle it clears by its radius and the
            # corner is 0.17 mm nearer, which is a short nobody drew.
            pad = (vx - radius, vy - radius, vx + radius, vy + radius)
            if _segment_to_box(a, b, pad) < width / 2 + 0.45:
                return False
        # Two rules, and a via has to satisfy both, because they measure
        # different things in different metrics.
        #
        # The copper is square to `check_board`, so it is square here as well,
        # and it is measured against each existing via's own size: a stitching
        # via 1.25 mm from a 0.8 mm routing via clears any centre-to-centre
        # rule written for two 0.8 mm holes and still shorts, because what has
        # to clear is the gap between their edges along whichever axis is
        # tighter, and on the diagonal that is not the distance between their
        # centres.
        #
        # The drills are round, and hole-to-hole is the fabricator's rule
        # rather than the artwork's: two barrels that clear each other's copper
        # on the diagonal can still be nearer than a drill bit may be placed to
        # its neighbour. Replacing this radial check with the square one above
        # is what put `drc.hole_to_hole` on the FPGA board.
        return all(
            max(abs(vx - hx), abs(vy - hy)) >= (size + 2 * radius) / 2 + 0.25
            and math.dist((vx, vy), (hx, hy)) >= 1.2
            for (hx, hy), size in holes
        )

    kept: list[Via] = []

    # Along the rim first, then one row deeper into the band the rule measures.
    # Half a step either way is as far as a via can move and still be the one
    # that belongs at this station rather than its neighbour's.
    def _offsets(reach: float) -> list[float]:
        out = [0.0]
        step_out = 0.5
        while step_out <= reach + 0.01:
            out += [step_out, -step_out]
            step_out += 0.5
        return out

    def _room_on_the_rim(vx, vy, along, inward, reach: float | None = None):
        """The nearest spot on this stretch of rim that a via actually fits."""
        for depth in (0.0, 1.5):
            for offset in _offsets(step / 2 if reach is None else reach):
                px = round(vx + along[0] * offset + inward[0] * depth, 2)
                py = round(vy + along[1] * offset + inward[1] * depth, 2)
                if clears(px, py):
                    return (px, py)
        return None

    seated: list[tuple[float, float]] = []
    for vx, vy, along, inward in ring:
        placed = _room_on_the_rim(vx, vy, along, inward)
        if placed is not None:
            holes.append((placed, VIA_SIZE))
            seated.append(placed)
            kept.append(Via(POUR_NET, x=placed[0], y=placed[1]))

    # Then the gaps that are still gaps. A station whose slide ran out leaves
    # its neighbour a stretch of nineteen millimetres against a limit of
    # eighteen, and the answer to that is another via in the middle of it, not
    # a wider slide: a via that walks further than half a step is standing at
    # its neighbour's station. Measured the way `emc.stitching_pitch` measures
    # it, round the rim rather than across the corner.
    width, height = right - left, bottom - top
    perimeter = 2 * (width + height)

    def _arc_of(x: float, y: float) -> float:
        """Where a point sits round the inset rectangle, from its top-left."""
        edges = (abs(y - top), abs(right - x), abs(bottom - y), abs(x - left))
        nearest = edges.index(min(edges))
        if nearest == 0:
            return min(max(x - left, 0.0), width)
        if nearest == 1:
            return width + min(max(y - top, 0.0), height)
        if nearest == 2:
            return width + height + min(max(right - x, 0.0), width)
        return 2 * width + height + min(max(bottom - y, 0.0), height)

    def _station_at(arc: float):
        """The rim point that far round, with its along and inward directions."""
        arc %= perimeter
        if arc <= width:
            return (left + arc, top, (1.0, 0.0), (0.0, 1.0))
        arc -= width
        if arc <= height:
            return (right, top + arc, (0.0, 1.0), (-1.0, 0.0))
        arc -= height
        if arc <= width:
            return (right - arc, bottom, (1.0, 0.0), (0.0, -1.0))
        return (left, bottom - (arc - width), (0.0, 1.0), (1.0, 0.0))

    # A fill station may walk further than a ring station: half a step was the
    # limit because past it a via stands at its neighbour's station, and a fill
    # station has no neighbour - it is placed precisely where the rim is empty,
    # so half the gap is its reach. That is what gets round a mounting hole:
    # the corner itself belongs to the screw, and the nearest copper a via may
    # sit on is eight millimetres along the edge from it, one on each side.
    # Which takes more than one round, since each round can only halve a gap.
    arcs = sorted(_arc_of(*point) for point in seated)
    for _round in range(RIM_FILL_ROUNDS):
        if not arcs:
            break
        added = []
        for start, end in pairwise([*arcs, arcs[0] + perimeter]):
            if end - start <= RIM_MAX_GAP_MM:
                continue
            middle = (start + end) / 2
            placed = _room_on_the_rim(*_station_at(middle), reach=(end - start) / 2)
            if placed is not None:
                holes.append((placed, VIA_SIZE))
                kept.append(Via(POUR_NET, x=placed[0], y=placed[1]))
                added.append(_arc_of(*placed))
        if not added:
            break
        arcs = sorted([*arcs, *added])

    for vx, vy in rim:
        if clears(vx, vy):
            holes.append(((vx, vy), VIA_SIZE))
            kept.append(Via(POUR_NET, x=vx, y=vy))

    # Then the guarantee the mesh cannot give: every piece of the front pour
    # holds a via of its own. The clearance channels shred the front face
    # into strips - the band above a connector row, the shell along an edge -
    # and a strip whose only tie is somewhere far away reads as fenced-off
    # copper even when it is not; the reviewer circled three of them on two
    # boards. The pieces are taken *before* the orphan drop, so a strip
    # nothing else reaches gets a via here instead of staying a blank.
    pieces = _fill_rectangles(design, "F.Cu", POUR_NET, smd_isolated=True, connected=False)
    parent = list(range(len(pieces)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def overlap(a, b) -> bool:
        return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]

    for i in range(len(pieces)):
        for j in range(i + 1, len(pieces)):
            if overlap(pieces[i], pieces[j]):
                parent[find(i)] = find(j)
    groups: dict[int, list[int]] = defaultdict(list)
    for i in range(len(pieces)):
        groups[find(i)].append(i)
    ground = [via_position(design, v) for v in design.vias if v.net == POUR_NET]
    ground += [(v.x, v.y) for v in kept]
    for members in groups.values():
        area = sum((pieces[i][2] - pieces[i][0]) * (pieces[i][3] - pieces[i][1]) for i in members)
        if area < 8.0:
            continue  # too small to earn a drill; the orphan drop takes it
        if any(
            pieces[i][0] <= gx <= pieces[i][2] and pieces[i][1] <= gy <= pieces[i][3]
            for i in members
            for gx, gy in ground
        ):
            continue
        biggest = sorted(
            members,
            key=lambda i: min(pieces[i][2] - pieces[i][0], pieces[i][3] - pieces[i][1]),
            reverse=True,
        )
        placed = False
        for i in biggest[:8]:
            x0, y0, x1, y1 = pieces[i]
            if min(x1 - x0, y1 - y0) < 1.1:
                continue
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            for vx, vy in (
                (cx, cy),
                (cx, (y0 + 3 * y1) / 4),
                (cx, (3 * y0 + y1) / 4),
                ((x0 + 3 * x1) / 4, cy),
                ((3 * x0 + x1) / 4, cy),
            ):
                if not (x0 + 0.55 <= vx <= x1 - 0.55 and y0 + 0.55 <= vy <= y1 - 0.55):
                    continue
                vx, vy = round(vx, 2), round(vy, 2)
                if clears(vx, vy):
                    holes.append(((vx, vy), VIA_SIZE))
                    kept.append(Via(POUR_NET, x=vx, y=vy))
                    ground.append((vx, vy))
                    placed = True
                    break
            if placed:
                break
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


def _canonicalize_board_uuids(path: Path) -> None:
    """Undo the random UUIDs that pcbnew assigns while saving a board.

    The emitter gives every object a stable UUID, but loading and saving for
    zone fill replaces UUIDs inside library footprints. That makes two cold
    generations differ even when their copper is identical. Re-key every UUID
    by structural order after pcbnew is finished, and rewrite group membership
    references with the same map. The order is KiCad's saved order, so the
    result changes when the board changes and is identical when it does not.
    """
    root = sexp.load(path)
    mapping: dict[str, str] = {}

    def collect(node: SNode) -> None:
        if node.name == "uuid":
            for atom in node.atoms():
                old = str(atom)
                if old not in mapping:
                    mapping[old] = stable_uuid(path.stem, "canonical-pcb", len(mapping))
        for child in node.children():
            collect(child)

    def rewrite(node: SNode) -> None:
        if node.name in ("uuid", "members"):
            node.args = [mapping.get(str(atom), atom) for atom in node.args]
        for child in node.children():
            rewrite(child)

    collect(root)
    rewrite(root)
    path.write_text(sexp.dumps(root) + "\n", encoding="utf-8")


def board_net_name(design: Design, name: str) -> str:
    """The exact net name KiCad derives from a power symbol or root-sheet label."""
    return name if name in POWER_SYMBOLS and name not in design.wired_power else f"/{name}"


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
    labels = {name: board_net_name(design, name) for name in order}

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
        board_layers(design.copper_layers, design.power_plane),
        "\t(setup",
        "\t\t(pad_to_mask_clearance 0)",
        "\t\t(allow_soldermask_bridges_in_footprints no)",
        "\t)",
        '\t(net 0 "")',
    ]
    for name in [*order, *sorted(set(spares.values()))]:
        lines.append(f'\t(net {codes[name]} "{labels[name]}")')

    # Every pad on the board, so a designator that steps off its own part's
    # pads does not step onto its neighbour's - which is the same defect one
    # part along. `printed` collects the designators already placed, so they
    # do not stack either.
    all_pads = [
        pad_box(design, other, pad)
        for other in design.footprints()
        for pad in footprint_definition(other.footprint).children("pad")
    ] + [box for other in design.footprints() for box in _footprint_silk(design, other)]
    # The connector legends are placed before any designator, and the
    # designators then have to miss them: a legend names one pin of one
    # connector and has to sit against it, while a designator can go anywhere
    # legible. Computed here and computed again when the silk is emitted -
    # the placement reads nothing that changes in between, so the two agree.
    legend_boxes: list[tuple[float, float, float, float]] = []
    _board_silk(design, legend_boxes=legend_boxes)
    printed: list[tuple[float, float, float, float]] = list(legend_boxes)
    # Where every part's body will be, so no designator is put under a
    # neighbour's: readable on the bare board, hidden on the assembled one.
    extents = {part.ref: _part_extent(design, part) for part in design.footprints()}
    for part in design.footprints():
        node = footprint_definition(part.footprint)
        bx, by, angle = part.board
        node.args.insert(
            1, SNode("at", [round(ox + bx, 4), round(oy + by, 4)] + ([angle] if angle else []))
        )
        _reuuid(node, design.name, "fp", part.ref)
        node.args.insert(2, _uuid_node(stable_uuid(design.name, "fp", part.ref)))
        _place_footprint_zones(node, ox + bx, oy + by, angle)
        _set_property(node, "Reference", part.ref)
        if part.show_reference:
            bodies = [box for ref, box in extents.items() if ref != part.ref]
            _move_reference_off_pads(design, part, node, all_pads, printed, bodies)
        else:
            _hide_property(node, "Reference")
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
                heavy = name == POUR_NET and _wants_relief(design, part, pad)
                if heavy:
                    # 1 is KiCad's "thermal relief" for this pad alone. The
                    # zone stays as it is: a chip land still floods solid,
                    # which is the better electrical answer and reflows fine.
                    pad.args.append(SNode("zone_connect", [1]))
                spoke = _spoke_width(design, part, pad, name, heavy=heavy)
                if spoke > THERMAL_SPOKE and pad.child("thermal_bridge_width") is None:
                    # Every relieved pad, drilled or not: the zone's own spoke
                    # width is a floor, and a power pad wants more than the
                    # floor (see `_spoke_width`).
                    pad.args.append(SNode("thermal_bridge_width", [spoke]))
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
        if design.copper_layers == 4:
            # The uninterrupted reference plane is why this stack-up exists;
            # the outer pours still spread heat and collect local ground.
            lines.append(_zone(design, codes["GND"], "In1.Cu", net_name="GND"))
            if design.power_plane:
                lines.append(
                    _zone(
                        design,
                        codes[design.power_plane],
                        "In2.Cu",
                        net_name=design.power_plane,
                    )
                )

    lines.extend(_board_silk(design))

    lines.append(")")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _silk_text_item(
    design: Design,
    text: str,
    x: float,
    y: float,
    key: object,
    size: float = 0.8,
    justify: str = "",
    angle: float = 0.0,
) -> str:
    ox, oy = design.origin
    thickness = round(size * 0.15, 3)
    where = f" (justify {justify})" if justify else ""
    return (
        f'\t(gr_text "{text}" (at {round(ox + x, 4)} {round(oy + y, 4)} {round(angle, 1):g}) '
        f'(layer "F.SilkS") (uuid "{stable_uuid(design.name, "silk", key)}") '
        f"(effects (font (size {size} {size}) (thickness {thickness})){where}))"
    )


def _silk_line_item(
    design: Design, a: tuple[float, float], b: tuple[float, float], key: object
) -> str:
    ox, oy = design.origin
    return (
        f"\t(gr_line (start {round(ox + a[0], 4)} {round(oy + a[1], 4)}) "
        f"(end {round(ox + b[0], 4)} {round(oy + b[1], 4)}) "
        f"(stroke (width {SILK_LINE_WIDTH}) (type default)) "
        f'(layer "F.SilkS") (uuid "{stable_uuid(design.name, "silk", key)}"))'
    )


def _box_gap(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> float:
    """How far apart two rectangles are, zero where they touch or overlap."""
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return math.hypot(dx, dy)


def _inked(
    box: tuple[float, float, float, float], room: float
) -> tuple[float, float, float, float]:
    """A drawn line's box, given the width it is drawn at and its clearance.

    A line's bounding box has no area in one direction, and `_silk_intrusion`
    counts area: a leader drawn straight across a part's outline cost nothing
    at all, so the search happily did it and KiCad reported `silk_overlap`.
    """
    return (box[0] - room, box[1] - room, box[2] + room, box[3] + room)


def _names_its_pin(
    box: tuple[float, float, float, float],
    net: str,
    pads: list[tuple[tuple[float, float, float, float], str]],
) -> bool:
    """Whether the pad nearest a legend carries the net the legend names.

    This is the whole of what a pin legend has to achieve: a reader takes a
    name to belong to the pad beside it, so a name with somebody else's pad
    closer names that pad, however carefully it was placed against its own.

    The test is the net and not the pad, because that is what a reader gets
    right or wrong. `VIN` printed between a terminal's pin and the fuse pad
    that pin feeds names both of them, and both are VIN; the same string
    beside the fuse's *other* pad names the rail on the far side of the fuse,
    which is a different net and a different thing.

    Measured rectangle to rectangle rather than centre to centre, because a
    long name anchored at its pin reaches past two other parts and its middle
    is nowhere near either end.
    """
    nearest = min(_box_gap(box, pad) for pad, _net in pads)
    return any(name == net for pad, name in pads if _box_gap(box, pad) <= nearest + GEOM_EPS)


def _leg_points(
    start: tuple[float, float], end: tuple[float, float]
) -> list[list[tuple[float, float]]]:
    """The ways from one point to another in horizontal, vertical and 45° legs.

    Two of them: turn first and run straight in, or run straight out and turn
    at the end. Both are drawing conventions for a leader; which one is clear
    of the parts is what decides between them.
    """
    dx, dy = end[0] - start[0], end[1] - start[1]
    run = min(abs(dx), abs(dy))
    kx = math.copysign(run, dx)
    ky = math.copysign(run, dy)
    corners = ((start[0] + kx, start[1] + ky), (end[0] - kx, end[1] - ky))
    paths = []
    for corner in corners:
        points = [start, corner, end]
        trimmed = [p for i, p in enumerate(points) if i == 0 or math.dist(p, points[i - 1]) > 0.01]
        if len(trimmed) > 1:
            paths.append(trimmed)
    return paths


def _face(
    box: tuple[float, float, float, float], towards: tuple[float, float], room: float
) -> tuple[tuple[float, float], tuple[float, float]]:
    """The point just off the side of a rectangle that faces somewhere else,
    and the direction that side looks in."""
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    dx, dy = towards[0] - cx, towards[1] - cy
    if abs(dx) >= abs(dy):
        sign = 1.0 if dx > 0 else -1.0
        return ((box[2] + room if dx > 0 else box[0] - room, cy), (sign, 0.0))
    sign = 1.0 if dy > 0 else -1.0
    return ((cx, box[3] + room if dy > 0 else box[1] - room), (0.0, sign))


def _emerges(
    start: tuple[float, float],
    heading: tuple[float, float],
    hidden_by: list[tuple[float, float, float, float]],
    over: list[tuple[float, float, float, float]],
    bounds: tuple[float, float, float, float],
    limit: float = 12.0,
) -> tuple[float, float] | None:
    """Where a line leaving a pad stops being hidden by its own part.

    A screw terminal's pads are under its shell and its silkscreen outline is
    drawn round the lot, so a leader drawn from the pad itself is ink nobody
    can see and `silk_overlap` besides. It is drawn from the point it comes out
    at instead, which is on the pad's own side of the part: a line leaving the
    top edge of a terminal came from a pin at the top of it.

    Which is the whole value of it, and why a run that would cross ``over`` -
    any other pad - is no run at all, and None is returned for it. The motor
    driver's terminal had both its legends pointing at the same spot on the
    bottom edge of the shell: the upper pin's leader had gone down *through*
    the lower pin to get there, and under the shell nobody could see that it
    started higher up.
    """
    room = SILK_LINE_WIDTH / 2 + SILK_CLEARANCE
    point = start
    for _ in range(int(limit / 0.1)):
        box = (point[0] - room, point[1] - room, point[0] + room, point[1] + room)
        if any(_box_gap(box, pad) <= 0.0 for pad in over):
            return None
        if not (
            bounds[0] <= box[0]
            and bounds[1] <= box[1]
            and box[2] <= bounds[2]
            and box[3] <= bounds[3]
        ):
            # off the edge of the board, where nothing is printed at all
            return None
        if all(_box_gap(box, other) > 0.0 for other in hidden_by):
            return point
        point = (point[0] + heading[0] * 0.1, point[1] + heading[1] * 0.1)
    return None


def _runs_over(points: list[tuple[float, float]], box: tuple[float, float, float, float]) -> bool:
    """Whether a drawn polyline runs over a rectangle anywhere along it."""
    room = SILK_LINE_WIDTH / 2 + SILK_CLEARANCE
    for a, b in pairwise(points):
        steps = max(1, math.ceil(math.dist(a, b) / SILK_LEADER_STEP))
        for step in range(steps + 1):
            t = step / steps
            x = a[0] + (b[0] - a[0]) * t
            y = a[1] + (b[1] - a[1]) * t
            if _box_gap(box, (x - room, y - room, x + room, y + room)) <= 0.0:
                return True
    return False


def _path_intrusion(
    design: Design,
    points: list[tuple[float, float]],
    taken: list[tuple[float, float, float, float]],
    heavy: list[tuple[float, float, float, float]] | None = None,
) -> float:
    """What a drawn polyline takes from everything else, sampled along it."""
    room = SILK_LINE_WIDTH / 2 + SILK_CLEARANCE
    total = 0.0
    for a, b in pairwise(points):
        steps = max(1, math.ceil(math.dist(a, b) / SILK_LEADER_STEP))
        for step in range(steps + 1):
            t = step / steps
            x = a[0] + (b[0] - a[0]) * t
            y = a[1] + (b[1] - a[1]) * t
            total += _silk_intrusion(design, (x - room, y - room, x + room, y + room), taken, heavy)
    return total


def _footprint_silk(design: Design, part: Part) -> list[tuple[float, float, float, float]]:
    """The boxes a footprint's own silkscreen graphics print in.

    A designator is placed off the pads and off the other designators, and
    every library footprint also draws itself: the two ticks beside a chip
    resistor, the outline round a terminal block, the pin-1 wedge. Ink on ink
    is `silk_overlap` and, on the assembled board, an unreadable label - so the
    body counts as taken, the same as a pad does.

    One box per segment rather than one round the whole part: a designator that
    fits between an outline and its pin-1 mark should be allowed to sit there.
    """
    node = footprint_definition(part.footprint)
    bx, by, angle = part.board
    boxes: list[tuple[float, float, float, float]] = []
    for shape in (
        *node.children("fp_line"),
        *node.children("fp_rect"),
        *node.children("fp_circle"),
        *node.children("fp_arc"),
        *node.children("fp_poly"),
    ):
        layer = shape.child("layer")
        if not layer or "SilkS" not in str(layer.atom(0, "")):
            continue
        points: list[tuple[float, float]] = []
        if shape.name == "fp_circle":
            centre, edge = shape.child("center"), shape.child("end")
            ca = [a for a in (centre.atoms() if centre else []) if isinstance(a, (int, float))]
            ea = [a for a in (edge.atoms() if edge else []) if isinstance(a, (int, float))]
            if len(ca) < 2 or len(ea) < 2:
                continue
            radius = math.dist((float(ca[0]), float(ca[1])), (float(ea[0]), float(ea[1])))
            rx, ry = _rotate(float(ca[0]), float(ca[1]), angle)
            boxes.append((bx + rx - radius, by + ry - radius, bx + rx + radius, by + ry + radius))
            continue
        if shape.name == "fp_poly":
            pts = shape.child("pts")
            for xy in pts.children("xy") if pts else ():
                atoms = [a for a in xy.atoms() if isinstance(a, (int, float))]
                if len(atoms) >= 2:
                    points.append(_rotate(float(atoms[0]), float(atoms[1]), angle))
        else:
            for key in ("start", "mid", "end"):
                at = shape.child(key)
                atoms = [a for a in (at.atoms() if at else []) if isinstance(a, (int, float))]
                if len(atoms) >= 2:
                    points.append(_rotate(float(atoms[0]), float(atoms[1]), angle))
        if not points:
            continue
        # Half the stroke, so a line is a line and not a mathematical one.
        pad = 0.1
        boxes.append(
            (
                bx + min(p[0] for p in points) - pad,
                by + min(p[1] for p in points) - pad,
                bx + max(p[0] for p in points) + pad,
                by + max(p[1] for p in points) + pad,
            )
        )
    return boxes


def _graphic_extent(
    design: Design, part: Part, token: str
) -> tuple[float, float, float, float] | None:
    """How far a footprint's graphics on one layer family reach on the board.

    Circles count. A mounting hole and a fiducial both draw their courtyard as
    one `fp_circle`, and a reader that only knows about lines and rectangles
    decides they take up no room at all - which is how three fiducials came to
    be placed on top of three screw holes.
    """
    node = footprint_definition(part.footprint)
    bx, by, angle = part.board
    xs: list[float] = []
    ys: list[float] = []
    for shape in (
        *node.children("fp_line"),
        *node.children("fp_rect"),
        *node.children("fp_circle"),
        *node.children("fp_arc"),
    ):
        layer = shape.child("layer")
        if not layer or token not in str(layer.atom(0, "")):
            continue
        if shape.name == "fp_circle":
            centre = shape.child("center")
            edge = shape.child("end")
            if centre is None or edge is None:
                continue
            ca = [a for a in centre.atoms() if isinstance(a, (int, float))]
            ea = [a for a in edge.atoms() if isinstance(a, (int, float))]
            if len(ca) < 2 or len(ea) < 2:
                continue
            radius = math.dist((float(ca[0]), float(ca[1])), (float(ea[0]), float(ea[1])))
            rx, ry = _rotate(float(ca[0]), float(ca[1]), angle)
            xs.extend((bx + rx - radius, bx + rx + radius))
            ys.extend((by + ry - radius, by + ry + radius))
            continue
        for key in ("start", "mid", "end"):
            at = shape.child(key)
            if at:
                atoms = [a for a in at.atoms() if isinstance(a, (int, float))]
                if len(atoms) >= 2:
                    rx, ry = _rotate(float(atoms[0]), float(atoms[1]), angle)
                    xs.append(bx + rx)
                    ys.append(by + ry)
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _courtyard_box(design: Design, part: Part) -> tuple[float, float, float, float] | None:
    """The board a part claims: its outline plus the room to place it in."""
    return _graphic_extent(design, part, "CrtYd")


def _body_box(design: Design, part: Part) -> tuple[float, float, float, float] | None:
    """What the fitted part hides, as the library's fabrication outline.

    The courtyard is the room a part needs and is deliberately bigger than the
    part; the fabrication outline is the part. The difference is where a
    designator belongs - beside the body, inside the courtyard - and telling
    them apart is what stops a name being printed where its own part covers it.
    An electrolytic capacitor, an inductor and a module all span their own
    pads, so the gap the library leaves between the pads is under the part.
    """
    return _graphic_extent(design, part, "Fab")


def _place_footprint_zones(node: SNode, bx: float, by: float, angle: float) -> None:
    """Move a footprint's own zones to where the footprint was placed.

    Everything else inside a footprint - pads, graphics, text - is stored
    relative to it and KiCad places it for you. A `zone` is not: KiCad stores
    a footprint zone in *board* coordinates, so a library entry drawn at the
    origin stays at the origin however the part is placed.

    The Raspberry Pi Pico module carries two pad keep-outs, and for four
    rounds they sat at (0, -6) - off the board, keeping nothing out. DRC is
    silent (an empty region violates nothing) and the only visible sign was
    the plot: "fit to page" fits the bounding *box*, so every view of that
    board came out at half scale in one corner. `layout.zone_outside_outline`
    reports it now; this is what stops it happening.
    """
    for zone in node.children("zone"):
        for polygon in zone.walk("polygon"):
            pts = polygon.child("pts")
            for xy in pts.children("xy") if pts else []:
                atoms = [a for a in xy.atoms() if isinstance(a, (int, float))]
                if len(atoms) < 2:
                    continue
                rx, ry = _rotate(float(atoms[0]), float(atoms[1]), angle)
                xy.args = [round(bx + rx, 6), round(by + ry, 6)]


def _text_extent(text: str, size: float, thickness: float = 0.0) -> tuple[float, float]:
    """Half the width and half the height KiCad will print a string in.

    Measured against `EDA_ITEM.GetBoundingBox()` on a written board rather than
    guessed: the stroke font advances between 0.94 and 1.07 of the size per
    character depending on which characters they are, and the line box is 1.55
    of the size tall. Taking the top of the first range is what makes this an
    upper bound - the placement has to be sure a string *fits*, so its estimate
    rounds up, where the review rule's `_silk_bbox` rounds down for the mirror
    reason and only reports overlap it is sure of.

    `SILK_CLEARANCE` is on top, because ink that merely touches a pad is still
    `silk_over_copper`: the fab wants a gap, not a tie.
    """
    thickness = thickness or size * 0.15
    half_x = (len(text) * size * SILK_CHAR_ADVANCE + thickness) / 2 + SILK_CLEARANCE
    half_y = (size * SILK_LINE_HEIGHT + thickness) / 2 + SILK_CLEARANCE
    return (half_x, half_y)


def _silk_box(
    text: str, x: float, y: float, size: float, justify: str = "", angle: float = 0.0
) -> tuple[float, float, float, float]:
    """What a silk string covers on the board, for keeping it off things.

    Turned a quarter (``angle`` 90) the string runs up the board: KiCad reads
    it bottom to top, so left-justified text starts at the anchor and extends
    upward, right-justified text ends there and extends downward.
    """
    half_x, half_y = _text_extent(text, size)
    if justify == "left":
        along = (-SILK_CLEARANCE, 2 * half_x - SILK_CLEARANCE)
    elif justify == "right":
        along = (-2 * half_x + SILK_CLEARANCE, SILK_CLEARANCE)
    else:
        along = (-half_x, half_x)
    if abs(angle) % 180 == 90:
        return (x - half_y, y - along[1], x + half_y, y - along[0])
    return (x + along[0], y - half_y, x + along[1], y + half_y)


def _part_extent(design: Design, part: Part) -> tuple[float, float, float, float]:
    """How much board a part takes up: its courtyard, or its pads if it has
    none. Some module footprints draw no courtyard at all, and taking that to
    mean "takes up no room" is how a legend gets printed across its pads."""
    box = _courtyard_box(design, part)
    if box:
        return box
    node = footprint_definition(part.footprint)
    boxes = [pad_box(design, part, pad) for pad in node.children("pad")]
    if not boxes:
        return (0.0, 0.0, 0.0, 0.0)
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _silk_intrusion(
    design: Design,
    box: tuple[float, float, float, float],
    taken: list[tuple[float, float, float, float]],
    heavy: list[tuple[float, float, float, float]] | None = None,
) -> float:
    """How much of other people's board a silk box takes, in mm^2.

    Zero means it is clear. Counting the area rather than the collisions is
    what separates "half a legend across a module's pads" from "a tenth of a
    millimetre into a chip capacitor's courtyard": both are one collision, and
    only one of them matters.

    ``heavy`` is the copper. A courtyard grazed is a legend a little close to
    a part and `silk.over_pad` says nothing about it; a *pad* covered is a pad
    that will not wet. Fifty times the area is what keeps the second from ever
    winning a tie against the first.
    """
    width, height = design.board_size
    outside = (
        max(0.0, SILK_EDGE_ROOM - box[0])
        + max(0.0, SILK_EDGE_ROOM - box[1])
        + max(0.0, box[2] - (width - SILK_EDGE_ROOM))
        + max(0.0, box[3] - (height - SILK_EDGE_ROOM))
    )

    def area(boxes):
        return sum(
            max(0.0, min(box[2], c[2]) - max(box[0], c[0]))
            * max(0.0, min(box[3], c[3]) - max(box[1], c[1]))
            for c in boxes
        )

    # off the board is never the better option, and neither is a pad
    return area(taken) + area(heavy or []) * 50.0 + outside * 100.0


def _framed_legend(
    design: Design,
    text: str,
    pad: tuple[float, float, float, float],
    key: object,
    pads: list[tuple[float, float, float, float]],
    taken: list[tuple[float, float, float, float]],
    heavy: list[tuple[float, float, float, float]],
    hidden_by: list[tuple[float, float, float, float]] | None = None,
    home: tuple[float, float, float, float] | None = None,
    size: float = 0.8,
    boxed: bool = True,
) -> tuple[list[str], list[tuple[float, float, float, float]], float] | None:
    """A pin legend for a pin that has no clear room beside it.

    A two-pin screw terminal at the edge of a board has nowhere to be labelled:
    outboard is where the wire goes in, and inboard is the fuse and the clamp
    that every supply input carries. Pushing the name out past them is what
    made the buck converter's terminal unreadable - `VIN` printed two
    millimetres from the fuse's pad and eleven from the pin it named.

    So the name stops trying to sit against its pin and says which pin it means
    instead: it goes where there is room, in a frame that marks it as a label
    rather than a part's name, and a leader in horizontal, vertical and 45°
    legs runs from the frame to the pad. Positions are tried outward from the
    pad, and the first one whose frame *and* leader are both clear wins, so the
    leader stays short. Returns None where the board has no room at all, and
    the caller keeps the placement it had.

    ``hidden_by`` is what its own connector covers the pad with: its body, the
    outline it draws round it, and the room it claims. The leader is drawn from
    where it comes out of all that rather than from the pad, because ink under
    a shell is ink nobody reads and `silk_overlap` besides - and the side it
    comes out at is what says which pin it came from. Past that point it is
    charged for everything, its own connector included, so a label on the far
    side of a terminal is reached round it and not across it.
    """
    px, py = (pad[0] + pad[2]) / 2, (pad[1] + pad[3]) / 2
    half_x, half_y = _text_extent(text, size)
    frame_x = half_x + (SILK_FRAME_ROOM if boxed else 0.0)
    frame_y = half_y + (SILK_FRAME_ROOM if boxed else 0.0)
    radii = (4.0, 5.5, 7.0, 8.5, 10.0, 12.0, 14.0, 17.0, 20.0)
    spots = [
        (
            px + radius * math.cos(math.radians(step * 30.0)),
            py + radius * math.sin(math.radians(step * 30.0)),
        )
        for radius in radii
        for step in range(12)
    ]
    # Nothing beyond the furthest spot can be reached by frame or leader, and
    # the scoring walks these lists once per sampled point: on the FPGA board
    # that is six hundred boxes a point, of which a couple of dozen are in
    # range at all.
    span = radii[-1] + frame_x + frame_y + 1.0
    near = (px - span, py - span, px + span, py + span)
    taken = [box for box in taken if _box_gap(near, box) <= 0.0]
    heavy = [box for box in heavy if _box_gap(near, box) <= 0.0]
    # Grown by the clearance the fab wants: a leader that comes out half a
    # clearance from a pad is "silkscreen clipped by solder mask" just as one
    # across it is, and coming out beside the fuse is not worth a direction.
    over = [_inked(box, SILK_CLEARANCE) for box in pads if box != pad]
    width, height = design.board_size
    bounds = (SILK_EDGE_ROOM, SILK_EDGE_ROOM, width - SILK_EDGE_ROOM, height - SILK_EDGE_ROOM)
    # Every way out of the pad, worked out once: the leader may leave by any
    # side of it, not only the side its label happens to be on. Coming out of
    # the top of a terminal and going round is how one gets past the part
    # standing between it and the only clear strip.
    ways = [
        (start, heading)
        for point, heading in (
            ((px, pad[1] - SILK_CLEARANCE), (0.0, -1.0)),
            ((px, pad[3] + SILK_CLEARANCE), (0.0, 1.0)),
            ((pad[0] - SILK_CLEARANCE, py), (-1.0, 0.0)),
            ((pad[2] + SILK_CLEARANCE, py), (1.0, 0.0)),
        )
        # It comes out clear of everything, not only of its own part: a leader
        # whose first millimetre is under the neighbouring fuse's outline never
        # had a chance of being read as coming from this pin.
        if (start := _emerges(point, heading, [*(hidden_by or []), *heavy], over, bounds))
        is not None
    ]
    if not ways:
        return None
    # The label first, on its own: a name printed across a designator cannot be
    # read at all, where a leader crossing one is untidy and still points where
    # it points. So the spots are scored for the label, the least-intruding are
    # kept, and only those are asked what a leader to them would cost - which
    # is also what makes trying seventy leaders per spot affordable.
    scored = []
    for index, (x, y) in enumerate(spots):
        frame = (x - frame_x, y - frame_y, x + frame_x, y + frame_y)
        room = (
            frame[0] - SILK_CLEARANCE,
            frame[1] - SILK_CLEARANCE,
            frame[2] + SILK_CLEARANCE,
            frame[3] + SILK_CLEARANCE,
        )
        scored.append((_silk_intrusion(design, room, taken, heavy), index, x, y, frame))
    floor = min(entry[0] for entry in scored)
    best: (
        tuple[
            tuple[float, float, int],
            float,
            float,
            tuple[float, float, float, float],
            list[tuple[float, float]],
        ]
        | None
    ) = None
    for label_cost, index, x, y, frame in scored:
        if label_cost > floor + GEOM_EPS:
            continue
        routes = [
            [start, *legs] if stub else legs
            for start, heading in ways
            for stub in (0.0, 1.2, 2.4, 3.6)
            for end in (
                (x, frame[1] - SILK_CLEARANCE),
                (x, frame[3] + SILK_CLEARANCE),
                (frame[0] - SILK_CLEARANCE, y),
                (frame[2] + SILK_CLEARANCE, y),
            )
            for legs in _leg_points(
                (start[0] + heading[0] * stub, start[1] + heading[1] * stub), end
            )
        ]
        # Its own label counts too, shrunk so that arriving at a face does not
        # read as crossing it: offered four faces to come in by, a leader
        # happily took the far one and drew itself straight through the name.
        inside = (frame[0] + 0.5, frame[1] + 0.5, frame[2] - 0.5, frame[3] - 0.5)
        # A leader leaves its connector once. One that comes back over it to
        # reach a label on the far side is a line across the part, and a reader
        # following it has to guess where it went under: the Pico carrier's
        # terminal had one out of the top, round, and back down across the
        # shell to a label below.
        routes = [
            legs
            for legs in routes
            if all(
                bounds[0] <= point[0] <= bounds[2] and bounds[1] <= point[1] <= bounds[3]
                for point in legs
            )
            and (home is None or not _runs_over(legs, home))
        ]
        if not routes:
            continue
        cost, path = min(
            ((_path_intrusion(design, legs, taken, [*heavy, inside]), legs) for legs in routes),
            key=lambda item: item[0],
        )
        # Then what the leader takes, then a leader long enough to read as a
        # pointer rather than a tick, then the nearest spot to the pin.
        drawn = sum(math.dist(a, b) for a, b in pairwise(path))
        rank = (cost, max(0.0, SILK_LEADER_MIN - drawn), index)
        if best is None or rank < best[0]:
            best = (rank, x, y, frame, path)
        if best[0][0] <= GEOM_EPS and best[0][1] <= 0.0:
            break
    if best is None:
        return None
    _rank, x, y, frame, path = best
    corners = [
        (frame[0], frame[1]),
        (frame[2], frame[1]),
        (frame[2], frame[3]),
        (frame[0], frame[3]),
    ]
    items = [
        _silk_text_item(design, text, x, y, key, size=size),
        *(
            _silk_line_item(design, a, b, (key, "frame", index))
            for index, (a, b) in enumerate(zip(corners, corners[1:] + corners[:1], strict=False))
            if boxed
        ),
        *(
            _silk_line_item(design, a, b, (key, "leader", index))
            for index, (a, b) in enumerate(pairwise(path))
        ),
    ]
    ink_room = SILK_LINE_WIDTH / 2 + SILK_CLEARANCE
    boxes = [
        frame,
        *(
            _inked((min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])), ink_room)
            for a, b in pairwise(path)
        ),
    ]
    return items, boxes, floor + best[0][0]


def _board_silk(
    design: Design,
    legend_boxes: list[tuple[float, float, float, float]] | None = None,
) -> list[str]:
    """What the silkscreen says beyond the references.

    Three things a bare board has to answer without its schematic: which board
    it is (name and revision, bottom left), which signal is on which connector
    pin (the reverse-connection insurance), and what the indicators mean.

    ``printed`` is where the designators ended up. They are placed first, in
    the footprint emitter, and this is the only way what comes after can miss
    them - which is the difference between "3V3 OK" beside its LED and "3V3
    OK" printed through the word D3.
    """
    out: list[str] = []
    if not design.rev:
        # The degraded variant documents nothing about itself - that is one of
        # its findings - so the whole self-description package rides on rev.
        return out
    width, height = design.board_size
    courtyards = {part.ref: _part_extent(design, part) for part in design.footprints()}
    taken = list(courtyards.values())
    # Every string this function has put on the board so far, so the next one
    # measures against it as well as against the parts.
    placed: list[tuple[float, float, float, float]] = []
    # Legends that need a frame and a leader, held over until every legend that
    # does fit beside its pin has been placed.
    relocate: list[
        tuple[Part, str, str, tuple[float, float, float, float], tuple[float, float, str, float]]
    ] = []
    net_of: dict[tuple[str, str], str] = {}
    for name, nodes in design.nets.items():
        for entry in nodes:
            ref, _, number = entry.partition(".")
            net_of[(ref, number)] = name
    landed = [
        (pad_box(design, part, pad), net_of.get((part.ref, str(pad.atom(0, ""))), ""))
        for part in design.footprints()
        for pad in footprint_definition(part.footprint).children("pad")
    ]
    all_pads = [box for box, _net in landed]
    # What a frame and its leader have to keep off besides the pads: the drills,
    # which are copper the mask opens over just as a pad is, and every line the
    # footprints draw round themselves. Ink on either is a KiCad finding -
    # `silk_over_copper` and `silk_overlap` - and neither is in `all_pads`.
    via_room = VIA_SIZE / 2 + SILK_CLEARANCE
    ink_room = SILK_LINE_WIDTH / 2 + SILK_CLEARANCE
    obstacles = [
        *(
            (vx - via_room, vy - via_room, vx + via_room, vy + via_room)
            for vx, vy in (via_position(design, via) for via in design.vias)
        ),
        *(
            _inked(box, ink_room)
            for part in design.footprints()
            for box in _footprint_silk(design, part)
        ),
    ]
    for part in design.footprints():
        if part.ref.startswith("J") and part.pin_legend:
            node = footprint_definition(part.footprint)
            pads = [
                (str(pad.atom(0, "")), pad, pad_position_of(design, part, pad))
                for pad in node.children("pad")
            ]
            xs = [p[0] for _n, _p, p in pads]
            ys = [p[1] for _n, _p, p in pads]
            own_pads = [pad_box(design, part, pad) for _n, pad, _p in pads]
            pad_of = {number: pad_box(design, part, pad) for number, pad, _p in pads}
            # labels go perpendicular to the pad row, on the board side, and
            # clear the whole footprint - a screw terminal's body silk would
            # swallow a pad-edge offset
            row_along_x = (max(xs) - min(xs)) >= (max(ys) - min(ys))
            bx0, by0, bx1, by1 = courtyards[part.ref]
            # One pin, one name. A connector that brings a pin out twice
            # (a shield, a mounting tab) names it once.
            seen: set[str] = set()
            entries: list[tuple[str, float, float, str]] = []
            for number, _pad, (px, py) in pads:
                net = net_of.get((part.ref, number))
                if not net or number in seen:
                    continue
                seen.add(number)
                entries.append((number, px, py, net))
            if not entries:
                continue
            along = sorted(px if row_along_x else py for _n, px, py, _net in entries)
            pitch = min((b - a for a, b in pairwise(along) if b - a > GEOM_EPS), default=2.54)
            # The whole row is laid out at once, and every legend of it gets
            # the same side and the same distance from the row: a column of
            # names that line up is a pinout a person can read down, and one
            # that staggers to dodge its neighbours is not. Across the row a
            # name has to fit within the pitch, so on a 2.54 mm header the
            # names turn a quarter and stand up from their pins; along a
            # vertical row they lie flat beside it, one per pin, and a name
            # that does not fit the pitch there is a name that has to turn.
            # Each legend is anchored on its own pin and never slides along
            # the row: the pin it names is the nearest one by construction.
            widest = max(2 * _text_extent(net, 0.8)[0] for _n, _px, _py, net in entries)
            # Flat names beside a vertical row stack by their height, and
            # 1.24 mm fits any pitch a connector has.
            angle = 90.0 if row_along_x and widest > pitch - GEOM_EPS else 0.0
            # Either side of the pad row will do; which one is a question
            # of what is already there. A connector at an edge has an empty
            # strip on the outboard side and the rest of the board on the
            # other, so the measurement picks outboard on its own - and on a
            # carrier, where the module is inboard, it has to, because
            # inboard is a pad and silk over a pad is a pad that will not
            # wet. Then further out, for the row with a chip part standing
            # in the strip beside it.
            if row_along_x:
                # above the row the names hang up from the anchor (left-
                # justified when turned), below it down from the anchor
                above = [
                    (lambda px, py, g=gap, y=by0: (px, y - g), "left" if angle else "")
                    for gap in (1.2, 2.6, 4.0, 5.4, 6.8)
                ]
                below = [
                    (lambda px, py, g=gap, y=by1: (px, y + g), "right" if angle else "")
                    for gap in (1.2, 2.6, 4.0, 5.4, 6.8)
                ]
                outboard_first = height / 2 - (by0 + by1) / 2 > 0
                layouts = above + below if outboard_first else below + above
            else:
                left = [
                    (lambda px, py, g=gap, x=bx0: (x - g, py), "right")
                    for gap in (1.6, 3.0, 4.4, 5.8, 7.2)
                ]
                right = [
                    (lambda px, py, g=gap, x=bx1: (x + g, py), "left")
                    for gap in (1.6, 3.0, 4.4, 5.8, 7.2)
                ]
                outboard_first = width / 2 - (bx0 + bx1) / 2 < 0
                layouts = right + left if outboard_first else left + right
            # Deliberately *not* the designators. A legend names one pin of
            # one connector and has to sit against it; a designator can go
            # anywhere legible. So the legend is placed first and the
            # designator gets out of its way - the same order the schematic
            # side uses for a label and a field. The other parts' bodies
            # weigh as much as a pad: a name under a neighbour's shell is
            # readable on the bare board and gone on the assembled one. This
            # connector's own pads count too - "against" is not "on".
            bodies = [box for ref, box in courtyards.items() if ref != part.ref]

            def cost(
                layout, entries=entries, bodies=bodies, angle=angle, own_pads=own_pads
            ) -> float:
                anchor, justify = layout
                return sum(
                    _silk_intrusion(
                        design,
                        _silk_box(net, *anchor(px, py), 0.8, justify, angle),
                        [*own_pads, *placed],
                        [*all_pads, *bodies],
                    )
                    for _n, px, py, net in entries
                )

            anchor, justify = min(layouts, key=cost)
            # A row that cannot be lined up clean - a chip part standing in
            # the strip at one pin's height, with the board's edge on the
            # other side - falls back to naming each pin on its own: the
            # same sides and distances, and a slide along the row that
            # stops while the pin it names is still the nearest one. The
            # Pico carrier's supply terminal has the fuse in its strip, and
            # its 5 V legend steps a quarter pitch up the row to clear the
            # fuse's pad rather than print across it.
            aligned = cost((anchor, justify)) <= GEOM_EPS
            row: list[tuple[str, float, float, str, float, float, str, float]] = []
            for number, px, py, net in entries:
                tx, ty = anchor(px, py)
                text_justify, text_angle = justify, angle
                if not aligned:
                    here = px if row_along_x else py
                    elsewhere = [q for q in along if abs(q - here) > GEOM_EPS]
                    slides = [
                        s
                        for s in (0.0, -1.27, 1.27, -2.54, 2.54)
                        if not elsewhere
                        or abs(s) < min(abs(q - (here + s)) for q in elsewhere) - GEOM_EPS
                    ]
                    spots = [
                        (
                            (lambda px, py, a=a, s=s: (a(px, py)[0] + s, a(px, py)[1]))
                            if row_along_x
                            else (lambda px, py, a=a, s=s: (a(px, py)[0], a(px, py)[1] + s)),
                            j,
                        )
                        for a, j in layouts
                        for s in slides
                    ]
                    spot_anchor, text_justify = min(
                        spots,
                        key=lambda spot, n=number, px=px, py=py, net=net: cost(
                            spot, entries=[(n, px, py, net)]
                        ),
                    )
                    tx, ty = spot_anchor(px, py)
                if number in part.pin_legend_at:
                    tx, ty, text_justify = part.pin_legend_at[number]
                    text_angle = 0.0
                row.append((number, px, py, net, tx, ty, text_justify, text_angle))
            # Which of them came out as a column, asked of where they ended up
            # rather than of what was attempted: one pin of a twenty-pin row
            # with a fiducial in its strip sends the whole row to per-pin
            # placement, and nineteen of them still land in one line.
            offsets = [
                (round(tx - px, 1), round(ty - py, 1)) for _n, px, py, _net, tx, ty, *_ in row
            ]
            column = {offset for offset in offsets if offsets.count(offset) >= LEGEND_COLUMN_MIN}
            for index, (number, _px, _py, net, tx, ty, text_justify, text_angle) in enumerate(row):
                box = _silk_box(net, tx, ty, 0.8, text_justify, text_angle)
                # Last resort, and the only one that always works: a name with
                # somebody else's pad nearer to it than its own names that pad,
                # so it stops trying to sit against the pin and points at it
                # instead. The supply terminals are where this happens - a
                # fuse and a clamp stand between the terminal and the rest of
                # the board, and the board's edge is on its other side.
                #
                # A name that is one of a column is exempt: the column is read
                # by position, the third name down belongs to the third pin,
                # and taking one out of the line to point at its pin costs more
                # than it buys. Four of the Pico carrier's forty header names
                # have a bypass capacitor's pad marginally nearer than their
                # own pin, and all forty read fine.
                #
                # Held over to a second pass rather than done here: a leader
                # has to miss every legend on the board, and the connectors
                # after this one have not been laid out yet. The Pico carrier
                # drew one across `GP28` and `AGND` that way.
                mine = pad_of[number]
                if offsets[index] not in column and not _names_its_pin(box, net, landed):
                    relocate.append((part, number, net, mine, (tx, ty, text_justify, text_angle)))
                    continue
                placed.append(box)
                out.append(
                    _silk_text_item(
                        design,
                        net,
                        tx,
                        ty,
                        (part.ref, number),
                        justify=text_justify,
                        angle=text_angle,
                    )
                )
    for part in design.footprints():
        if part.silk_label:
            bx, by, _angle = part.board
            dy = height / 2 - by
            # Inboard of the part first - a label at the board edge is a label
            # half off it - then further out, then either side. The designator
            # is already on the board by now and is the thing this collides
            # with: both want the clear millimetre next to a two-pad part.
            reach = math.copysign(1.0, dy)
            spots = [
                (bx, by + reach * 2.6, ""),
                (bx, by + reach * 4.0, ""),
                (bx, by - reach * 2.6, ""),
                (bx, by - reach * 4.0, ""),
                (bx + 4.0, by, "left"),
                (bx - 4.0, by, "right"),
                (bx, by + reach * 5.4, ""),
                # then round the part, for the indicator whose usual strips
                # have been taken - a relocated legend's frame is 4 mm of board
                *(
                    (
                        bx + radius * math.cos(math.radians(step * 45.0)),
                        by + radius * math.sin(math.radians(step * 45.0)),
                        "",
                    )
                    for radius in (5.0, 6.5, 8.0)
                    for step in range(8)
                ),
            ]
            # Other parts' bodies weigh what their pads do: "3V3 OK" beside an
            # LED is there to be read on the finished board, and a relocated
            # legend's frame can have taken the strip it used to use.
            tx, ty, justify = min(
                spots,
                key=lambda spot: (
                    _silk_intrusion(
                        design,
                        _silk_box(part.silk_label, spot[0], spot[1], 0.8, spot[2]),
                        placed,
                        [
                            *(_inked(box, SILK_CLEARANCE) for box in all_pads),
                            *obstacles,
                            *taken,
                        ],
                    ),
                    math.dist((spot[0], spot[1]), (bx, by)),
                ),
            )
            placed.append(_silk_box(part.silk_label, tx, ty, 0.8, justify))
            out.append(
                _silk_text_item(
                    design, part.silk_label, tx, ty, (part.ref, "label"), justify=justify
                )
            )
    # The names with no room beside their pin, now that every name that did
    # have room is down. Ink on ink weighs what ink on a pad does here: the
    # frame and its leader have a whole board to choose from, and `silk_overlap`
    # is a real finding where a courtyard grazed is not.
    for part, number, net, mine, plain in relocate:
        attempts = [
            _framed_legend(
                design,
                net,
                mine,
                (part.ref, number),
                all_pads,
                [],
                [
                    # a pad grown by the clearance the fab wants: ink that
                    # merely grazes a mask opening is still "silkscreen clipped
                    # by solder mask", and the graze cost so little that the
                    # search took it
                    *(_inked(box, SILK_CLEARANCE) for box in all_pads),
                    *obstacles,
                    *courtyards.values(),
                    *placed,
                ],
                hidden_by=[
                    *_footprint_silk(design, part),
                    courtyards[part.ref],
                    *([body] if (body := _body_box(design, part)) else []),
                ],
                home=courtyards[part.ref],
                boxed=boxed,
            )
            # A frame says "this is a label, not a part's name", and is worth
            # four millimetres of board where there are four to spare. Where
            # there are not - the Pico carrier's terminal has the board's edge
            # on two sides of it - the name goes bare and the leader still says
            # which pin it means, which was the point.
            for boxed in (True, False)
        ]
        clean = [attempt for attempt in attempts if attempt is not None]
        framed = min(clean, key=lambda attempt: attempt[2]) if clean else None
        if framed is None:
            # nowhere on the board to put it; keep the placement it had
            tx, ty, text_justify, text_angle = plain
            box = _silk_box(net, tx, ty, 0.8, text_justify, text_angle)
            items, boxes = (
                [
                    _silk_text_item(
                        design,
                        net,
                        tx,
                        ty,
                        (part.ref, number),
                        justify=text_justify,
                        angle=text_angle,
                    )
                ],
                [box],
            )
        else:
            items, boxes, _cost = framed
        out.extend(items)
        placed.extend(boxes)
    # The board's own name goes last, because it is the string with the
    # most freedom about where it goes and so the one that moves. Placed
    # first it had no idea the legends were coming and printed itself
    # through `GP22`.
    # Bottom centre is where a board says its own name, and on a board with
    # room that is where it stays. A carrier whose module runs the length of
    # it has no bottom centre to write in, so the next-best strips are offered
    # in turn and the first clear one wins - silk over a pad is not a legend,
    # it is a pad you cannot solder.
    board_id = f"{design.name} rev {design.rev}"
    lines = [(board_id, 1.2, "boardid")]
    if design.company:
        # the author line: a bare board also answers "whose design is this"
        lines.append((design.company, 1.0, "boardauthor"))
    stack = (
        max(_text_extent(text, size)[0] * 2 for text, size, _key in lines),
        2.4 * len(lines),
    )

    def strip(y: float) -> list[tuple[float, float]]:
        """Positions along one horizontal band, working out from its middle."""
        offsets = [0.0]
        for step in range(1, int(width / 4) + 1):
            offsets.extend((-step * 2.0, step * 2.0))
        return [
            (width / 2 + offset, y)
            for offset in offsets
            if stack[0] / 2 + SILK_EDGE_ROOM
            <= width / 2 + offset
            <= width - stack[0] / 2 - SILK_EDGE_ROOM
        ]

    # Bottom centre is where a board says its own name, and on a board with
    # room that is where it stays. A carrier whose module runs the length of it
    # has no bottom centre to write in, so the band is scanned outward from
    # there, then the top band, then the middle - and the whole list is scored,
    # so a crowded board gets the least bad place rather than the first.
    spots = strip(height - 4.4) + strip(4.4) + strip(height / 2)

    def stack_box(spot: tuple[float, float]) -> tuple[float, float, float, float]:
        return (
            spot[0] - stack[0] / 2,
            spot[1] - 1.0,
            spot[0] + stack[0] / 2,
            spot[1] - 1.0 + stack[1],
        )

    # The designators are already placed by the time this runs for real, and
    # the board's own name is the string with the most freedom about where it
    # goes - so it is the one that moves. Scored rather than first-clear, so a
    # crowded board still gets the least bad strip instead of the first one in
    # the list.
    # The parts' bodies weigh as much as their pads: a board name under a
    # fitted part is a board with no name.
    at = min(
        spots,
        key=lambda spot: (
            _silk_intrusion(design, stack_box(spot), placed, [*all_pads, *taken]),
            spots.index(spot),
        ),
    )
    for index, (text, size, key) in enumerate(lines):
        out.append(_silk_text_item(design, text, at[0], at[1] + index * 2.4, key, size=size))
        # counted with the rest of the ink from here on: a relocated legend's
        # frame has the run of the board and the board's name does not
        placed.append(_silk_box(text, at[0], at[1] + index * 2.4, size))
    if legend_boxes is not None:
        # Everything this function put on the board, so the designators - placed
        # after it and free to go anywhere legible - can miss all of it. It has
        # to be everything: the two passes agree only because neither reads
        # anything the other writes.
        legend_boxes[:] = placed
    return out


ZONE_CLEARANCE = 0.25  # what the pour keeps away from copper of another net
# 0.25, not the 0.4 it used to be: a 2.54 mm header column carries pads about
# 1.5 mm tall, and at 0.4 of clearance a side the web between neighbours came
# to 0.34 mm - under ZONE_SLIVER, so the sweep dropped every one and the
# column became a full-height slot through the plane. The reviewer circled
# three of those. At 0.25 the web is 0.44 mm and the plane flows between the
# pins, which is what every hand-laid carrier board shows; the boards' DRC
# clearance is 0.2, so the pour still clears everything it must.
ZONE_SLIVER = 0.35  # a strip of plane thinner than this is not worth filling
# What the zone states in the file, for KiCad's own filler to honour. The
# minimum width is what stops the plane squeezing into a gap it cannot be
# etched into reliably - a web narrower than this is a sliver the fab may or
# may not deliver - and the thermal numbers are the relief a pad gets where the
# plane meets it: a gap all round and four spokes across it, so a soldering
# iron can bring the joint up to temperature instead of the whole plane.
ZONE_MIN_WIDTH = 0.25
# How far a teardrop reaches out of the land it fillets. Long enough to be a
# fillet rather than a blob, short enough that it is still the pad's business
# and not the net's.
TEARDROP_MAX_MM = 1.0
THERMAL_GAP = 0.4
THERMAL_SPOKE = 0.5
# How wide a spoke may grow before the relief stops relieving. Four of these
# is 4 mm of copper into the pad, which is more than any track on these boards.
THERMAL_SPOKE_MAX = 1.0
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
    design: Design, layer: str, net: str, smd_isolated: bool = False, connected: bool = True
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
    if connected:
        return _connected_islands(design, islands, layer, net)
    # ``connected=False`` is for the stitcher, which wants to see the pieces
    # *before* the orphan drop - an orphan it can reach with a via is not an
    # orphan any more, it is the strip the reviewer wanted filled.
    return islands


def _connected_islands(design, islands, layer: str, net: str):
    """Drop the pieces of plane that do not reach the rest of the plane.

    A track laid across the pour can fence a corner of it off. KiCad's own
    filler calls that an island and removes it; leaving it in the file instead
    is one `unconnected_items` error per orphan, because a zone that is two
    separate shapes is two separate pieces of copper.

    Touching *some* ground copper is not enough to keep a piece. Two pieces
    that each hold a bypass cap's ground pad and nothing else are still two
    pieces, and KiCad reports the pair. What joins pieces on a two layer board
    is the other face: anything with a via, or a through-hole pad, is on the
    same copper as everything else with one. So the plane on the far side is a
    node in this graph, every via and through-hole pad is an edge to it, and a
    piece survives only if it can be walked from there.
    """
    plane = len(islands)  # the other face, which every via reaches
    parent = list(range(len(islands) + 1))

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

    def touching(anchor: tuple[float, float, float, float]) -> list[int]:
        return [
            i
            for i, box in enumerate(islands)
            if box[0] <= anchor[2]
            and anchor[0] <= box[2]
            and box[1] <= anchor[3]
            and anchor[1] <= box[3]
        ]

    # Each of these is a piece of this net's copper. Whatever it touches, it
    # joins: two islands under one pad are one island as far as current is
    # concerned. `crosses` says whether it also reaches the other face.
    links: list[tuple[tuple[float, float, float, float], bool]] = []
    for via in design.vias:
        if via.net == net:
            vx, vy = via_position(design, via)
            links.append(((vx, vy, vx, vy), True))
    for part in design.footprints():
        node = footprint_definition(part.footprint)
        for pad in node.children("pad"):
            number = str(pad.atom(0, ""))
            if f"{part.ref}.{number}" not in design.nets.get(net, ()):
                continue
            on = pad_layer(pad)
            if on in (None, layer):
                links.append((pad_box(design, part, pad), on is None))
    for track in design.tracks:
        if track.net != net or track.layer != layer:
            continue
        points = [resolve(design, p) for p in track.points]
        # the whole run is one piece of copper, so every island any of its
        # corners lands on is joined to every other
        first: int | None = None
        for point in points:
            for index in touching((*point, *point)):
                if first is None:
                    first = index
                union(index, first)

    for anchor, crosses in links:
        found = touching(anchor)
        if crosses:
            found.append(plane)
        for index in found[1:]:
            union(index, found[0])

    return [box for i, box in enumerate(islands) if find(i) == find(plane)]


def _zone(
    design: Design,
    code: int,
    layer: str = "B.Cu",
    smd_isolated: bool = False,
    net_name: str = POUR_NET,
) -> str:
    """A plane pour, declared but not filled.

    The fill used to be computed here, by sweeping the outline and subtracting
    everything of another net, because KiCad's own filler was believed to need
    a display the container has not got. It does not: `pcbnew`'s `ZONE_FILLER`
    runs headless in both images, and `_kicad_fill` below hands every board to
    it after this file is written.

    That is worth more than tidiness. The sweep was *this file's* idea of the
    clearance rule, and a reviewer found the two disagreeing - copper reaching
    within 0.075 mm of a pad it should have kept 0.25 from, on a board whose
    DRC was green, because `pcb drc --refill-zones` throws the committed fill
    away and checks KiCad's. What is drawn and what is checked are the same
    polygon now, and the clearance, the minimum width and the thermal spokes
    all come from the board's own rules.

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
    zone_uuid = (
        stable_uuid(design.name, "zone", layer)
        if net_name == POUR_NET
        else stable_uuid(design.name, "zone", layer, net_name)
    )
    lines = [
        "\t(zone",
        f"\t\t(net {code})",
        f'\t\t(net_name "{board_net_name(design, net_name)}")',
        f'\t\t(layer "{layer}")',
        f'\t\t(uuid "{zone_uuid}")',
        "\t\t(hatch edge 0.5)",
        connect,
        f"\t\t\t(clearance {ZONE_CLEARANCE})",
        "\t\t)",
        f"\t\t(min_thickness {ZONE_MIN_WIDTH})",
        "\t\t(filled_areas_thickness no)",
        "\t\t(fill yes",
        f"\t\t\t(thermal_gap {THERMAL_GAP})",
        f"\t\t\t(thermal_bridge_width {THERMAL_SPOKE})",
        "\t\t)",
        f"\t\t(polygon (pts {pts}))",
        "\t)",
    ]
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
no ESR stated on the regulator's output capacitor -> spec.missing_esr
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

# The parts every board's power input carries, and where their datasheets are.
# The TVS link is the one KiCad's own Diode library states for the SMAJ series.
LITTELFUSE_466 = "https://www.littelfuse.com/products/fuses/surface-mount-fuses/thin-film-fuses/466"
LITTELFUSE_SMAJ = (
    "https://www.littelfuse.com/media?resourcetype=datasheets"
    "&itemid=75e32973-b177-4ee3-a0ff-cedaf1abdb93&filename=smaj-datasheet"
)


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
            # Far enough left of the fuse that the two pin stubs between them
            # do not overlap: each pin runs 2.54 mm before its wire.
            sheet=(30.48, 63.5),
            mirror="y",
            board=(6.0, 20.0, 270.0),
            fields={
                "MPN": "1729128",
                "Manufacturer": "Phoenix Contact",
                "Datasheet": "https://www.phoenixcontact.com/product/1729128",
            },
        ),
        # The input is protected before anything else sees it: a fuse in
        # series, then a TVS across the fused rail. Reversed leads forward-bias
        # the TVS and the fuse opens; a transient above the regulator's rating
        # is clamped. Neither exists on a bench, both exist in a box.
        #
        # On the board they take the corridor between the terminal and the
        # regulator's tab via ring - the one strip the rebuilt floorplan left
        # empty, and the strip the input rail crosses anyway.
        Part(
            "F1",
            "Device:Fuse",
            "3A",
            "Fuse:Fuse_1206_3216Metric",
            sheet=(46.99, 63.5),
            angle=90.0,
            board=(15.0, 20.0, 0.0),
            fields={
                "Current": "3A",
                "MPN": "0466003.NR",
                "Manufacturer": "Littelfuse",
                "Datasheet": LITTELFUSE_466,
            },
        ),
        Part(
            "D3",
            "Device:D_Zener",
            "SMAJ18A",
            "Diode_SMD:D_SMA",
            sheet=(60.96, 69.85),
            angle=270.0,
            # Cathode up to the fused rail, anode down to its own via: the
            # clamp's return is the shortest one on the board.
            board=(15.0, 27.0, 270.0),
            fields={
                "Voltage": "18V",
                "Power": "400W",
                "MPN": "SMAJ18A",
                "Manufacturer": "Littelfuse",
                "Datasheet": LITTELFUSE_SMAJ,
            },
        ),
        Part(
            "C1",
            "Device:C_Polarized",
            "220u",
            "Capacitor_SMD:CP_Elec_8x10.5",
            sheet=(73.66, 69.85),
            board=(30.0, 29.0, 270.0),
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
            board=(36.0, 22.0, 0.0),
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
            board=(27.0, 15.0, 180.0),
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
            board=(42.0, 22.0, 270.0),
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
            board=(53.5, 16.5, 0.0),
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
            board=(64.0, 14.0, 90.0),
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
            board=(72.5, 16.5, 0.0),
            fields={
                "Voltage": "16V",
                "Tolerance": "20%",
                # The output capacitor's ESR is a design value here, not an
                # accident of the part picked: the LM2596's loop is designed
                # around it (SNVS124G section 9.1.3 gives it both an upper and
                # a lower limit), so a substitute has to meet it as surely as
                # the voltage rating.
                "ESR": "150mR max @100kHz",
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
            board=(74.0, 28.0, 0.0),
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
            board=(80.0, 28.0, 180.0),
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
            board=(85.0, 16.5, 90.0),
            # KiCad 9 catches the automatic +5V legend on C3's body silk.
            # Put it below the output terminal, clear of both outlines.
            pin_legend_at={"1": (80.0, 21.8, "right")},
            fields={
                "MPN": "1729128",
                "Manufacturer": "Phoenix Contact",
                "Datasheet": "https://www.phoenixcontact.com/product/1729128",
            },
        ),
    ]

    nets = {
        "VIN": ["J1.1", "F1.1"],
        "+12V": ["F1.2", "D3.1", "C1.1", "C2.1", "U1.1"],
        "GND": [
            "J1.2",
            "D3.2",
            "C1.2",
            "C2.2",
            "U1.3",
            "U1.5",
            "D1.2",
            "C3.2",
            "C4.2",
            "J2.2",
            "D2.1",
        ],
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
        # The input connector may be remote; the energy-storage parts may not.
        # C1, C2 and VIN form one compact branch at the regulator pin - now
        # with the fuse in the way of it and the clamp hanging off it.
        Track("VIN", "F.Cu", W, ["J1.1", "F1.1"]),
        Track("+12V", "F.Cu", W, ["F1.2", (16.4, 23.0), "D3.1"]),
        Track("+12V", "F.Cu", W, ["F1.2", (18.0, 20.0), (18.0, 25.3), "C1.1"]),
        Track("+12V", "F.Cu", W, ["C1.1", (30.0, 24.0), "C2.1"]),
        Track("+12V", "F.Cu", W, ["C2.1", (35.05, 20.8), (34.65, 20.4), "U1.1"]),
        # Turning the TO-263 puts its pin field toward D1 and L1. The hot switch
        # loop is now a few millimetres, not a trip across half the board.
        Track("SW", "F.Cu", W, ["U1.2", (42.0, 16.7)]),
        Track("SW", "F.Cu", W, [(42.0, 16.7), "L1.1"]),
        Track("SW", "F.Cu", W, [(42.0, 16.7), "D1.1"]),
        # FB senses at the output capacitor, so it ends *on* that pad rather
        # than at a coordinate the output rail happens to pass through: a
        # junction that exists only because two numbers agree is one corner
        # away from being a dangling end.
        Track(
            "+5V",
            "F.Cu",
            SIG,
            ["U1.4", (37.35, 13.3), (42.35, 8.3), (65.8, 8.3), (70.3, 12.8), "C3.1"],
        ),
        # Output rail is one short row: switch node, inductor, capacitors, load.
        Track("+5V", "F.Cu", W, ["L1.2", "C3.1"]),
        Track("+5V", "F.Cu", W, [(64.0, 16.5), "C4.1"]),
        Track("+5V", "F.Cu", W, ["C3.1", (70.3, 13.0), (81.5, 13.0), "J2.1"]),
        Track("+5V", "F.Cu", SIG, ["C3.1", (70.3, 24.0), (73.0875, 26.7875), "R1.1"]),
        Track("LED_A", "F.Cu", SIG, ["R1.2", "D2.2"]),
        # Ground: a stub from each pad to a via of its own, straight into the
        # pour. Only the two through-hole terminals, outside the pour, run far.
        Track("GND", "F.Cu", W, ["J1.2", (6.0, 30.0), (10.0, 34.0)]),
        Track("GND", "F.Cu", W, ["J2.2", (88.0, 14.5), (88.0, 29.0), (83.0, 34.0)]),
        # The explicit return: input ground to output ground at the same width
        # as the forward path, so the 2 A loop does not depend on the pour
        # alone. It rides the bottom edge, under the LED branch, crossing
        # nothing.
        Track("GND", "F.Cu", W, [(10.0, 34.0), (83.0, 34.0)]),
        Track("GND", "F.Cu", W, ["U1.3", (39.5, 15.0)]),
        Track("GND", "F.Cu", W, ["U1.5", (37.5, 11.6)]),
        Track("GND", "F.Cu", W, [(25.5, 15.0), (25.5, 21.8)]),  # the TO-263 tab
        Track("GND", "F.Cu", W, ["D3.2", (15.0, 31.0)]),
        Track("GND", "F.Cu", W, ["C1.2", (30.0, 34.0)]),
        Track("GND", "F.Cu", W, ["C2.2", (38.5, 22.0)]),
        Track("GND", "F.Cu", W, ["D1.2", (42.0, 26.5)]),
        Track("GND", "F.Cu", W, ["C4.2", (64.0, 11.5)]),
        Track("GND", "F.Cu", W, ["C3.2", (77.7, 18.5)]),
        Track("GND", "F.Cu", SIG, ["D2.1", (80.9375, 31.0)]),
    ]
    vias = [
        # The tab is the die's thermal path and the switch loop's return: a
        # ring of vias just off the pad ties it straight into both pours.
        # Off the pad, not on it - via-in-pad drinks the solder at reflow.
        Via("GND", x=19.5, y=19.5),
        Via("GND", x=19.5, y=15.0),
        Via("GND", x=19.5, y=10.5),
        Via("GND", x=28.5, y=21.8),
        Via("GND", x=23.5, y=21.8),
        Via("GND", x=28.5, y=8.2),
        Via("GND", x=23.5, y=8.2),
        Via("GND", x=10.0, y=34.0),
        Via("GND", x=83.0, y=34.0),
        Via("GND", x=39.5, y=15.0),
        Via("GND", x=37.5, y=11.6),
        Via("GND", x=25.5, y=21.8),
        Via("GND", x=15.0, y=31.0),
        Via("GND", x=30.0, y=34.0),
        Via("GND", x=38.5, y=22.0),
        Via("GND", x=42.0, y=26.5),
        Via("GND", x=64.0, y=11.5),
        Via("GND", x=77.7, y=18.5),
        Via("GND", x=80.9375, y=31.0),
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
                    "Input: F1 3 A opens on a fault or reversed leads; D3 clamps",
                    "above 18 V and is the diode the reversed supply flows through.",
                    "C1 220u/35V bulk, C2 100n/50V bypass - both above 1.5x the 12 V.",
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
                    "C3's ESR is part of the design: 0.6 A x 150 mR = 90 mV",
                    "ripple, and the LM2596 loop needs ESR in the output",
                    "capacitor - an all-ceramic substitute rings.",
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
        power_flags=[("+12V", "F1.2"), ("GND", "J1.2"), ("+5V", "L1.2")],
        board_size=(92.0, 38.0),
        tracks=tracks,
        vias=vias,
        pour=(1.2, 1.2, 90.8, 36.8),
        mounting=Mounting(),
        fiducials=3,
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

    *The blocks.* The motor terminals face the left edge, power enters at the
    top-right edge and logic enters at the bottom. The driver sits between
    them. Logic drops to the back near its pins so the three local capacitors
    can sit beside the supply pins, without a twelve-millimetre escape fan.
    """
    parts = [
        Part(
            "J1",
            "Connector:Screw_Terminal_01x02",
            "VM 2.7-10.8V",
            "TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-2_1x02_P5.00mm_Horizontal",
            # 33, not 30: mirrored, the connector prints its long value to the
            # left, and at 30 that string reached the sheet frame's ruler strip.
            # Not further right either: the fuse and its pin stubs want the
            # room between the terminal and the bulk capacitor.
            sheet=(33.02, 80.01),
            board=(62.0, 7.0, 270.0),
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
            sheet=(66.04, 86.36),
            # 46 and turned round, not 52: the fuse and the clamp want the
            # column between this and the terminal, and the supply pad has to
            # be the one facing them so the rail does not cross the bulk
            # capacitor's own ground to get in. Not further left than 46
            # either - the board writes its own name in the strip this
            # capacitor's courtyard bounds, and it needs the width.
            board=(46.0, 8.0, 180.0),
            fields={
                "Voltage": "25V",
                "Tolerance": "20%",
                "MPN": "EEE-FK1E101P",
                "Manufacturer": "Panasonic",
                "Datasheet": "https://industrial.panasonic.com/cdbs/www-data/pdf/RDF0000/ABA0000C1053.pdf",
            },
        ),
        # Fuse and TVS between the terminal and the rail. VM may reach 10.8 V,
        # so the TVS stands off 12 V; reversed leads flow through it as a
        # diode and open the fuse. Both stand in the column between the
        # terminal's courtyard and the bulk capacitor's, which is the strip
        # the input rail crosses anyway.
        Part(
            "F1",
            "Device:Fuse",
            "3A",
            "Fuse:Fuse_1206_3216Metric",
            sheet=(49.53, 80.01),
            angle=90.0,
            board=(53.7, 8.0, 180.0),
            fields={
                "Current": "3A",
                "MPN": "0466003.NR",
                "Manufacturer": "Littelfuse",
                "Datasheet": LITTELFUSE_466,
            },
        ),
        Part(
            "D3",
            "Device:D_Zener",
            "SMAJ12A",
            "Diode_SMD:D_SMA",
            sheet=(57.15, 91.44),
            angle=270.0,
            # Cathode up to the fused rail, anode down to its own via: below
            # the fuse, in the same column, with the whole strip to the right
            # of the capacitor free.
            board=(53.7, 14.0, 270.0),
            fields={
                "Voltage": "12V",
                "Power": "400W",
                "MPN": "SMAJ12A",
                "Manufacturer": "Littelfuse",
                "Datasheet": LITTELFUSE_SMAJ,
            },
        ),
        Part(
            "C2",
            "Device:C",
            "10u",
            "Capacitor_SMD:C_0805_2012Metric",
            sheet=(78.74, 86.36),
            board=(43.0, 26.0, 0.0),
            fields={
                "Voltage": "25V",
                "Tolerance": "10%",
                "MPN": "CL21A106KAYNNNE",
                "Manufacturer": "Samsung",
                "Datasheet": "https://weblib.samsungsem.com/mlcc/mlcc-ec-data-sheet.do?partNumber=CL21A106KAYNNN",
            },
        ),
        Part(
            "C3",
            "Device:C",
            "10n",
            "Capacitor_SMD:C_0805_2012Metric",
            sheet=(96.52, 72.39),
            angle=90.0,
            board=(43.0, 28.5, 180.0),
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
            board=(36.5, 25.5, 0.0),
            fields={
                "MPN": "DRV8833PWR",
                "Manufacturer": "Texas Instruments",
                "Datasheet": "https://www.ti.com/lit/ds/symlink/drv8833.pdf",
            },
        ),
        Part(
            "C4",
            "Device:C",
            "2u2",
            "Capacitor_SMD:C_0805_2012Metric",
            sheet=(149.86, 60.96),
            board=(43.0, 23.5, 0.0),
            fields={
                "Voltage": "25V",
                "Tolerance": "10%",
                "MPN": "CL21A225KAFNNNE",
                "Manufacturer": "Samsung",
                "Datasheet": "https://weblib.samsungsem.com/mlcc/mlcc-ec-data-sheet.do?partNumber=CL21A225KAFNNN",
            },
        ),
        Part(
            "R2",
            "Device:R",
            "4k7",
            "Resistor_SMD:R_0805_2012Metric",
            sheet=(96.52, 107.95),
            # Out of the supply row: the fuse and the clamp took it, and the
            # strip below the terminal was the board's largest free area.
            board=(58.0, 20.0, 0.0),
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
            sheet=(96.52, 121.92),
            board=(62.0, 20.0, 180.0),
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
            board=(10.0, 22.5, 90.0),
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
            board=(10.0, 33.5, 90.0),
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
            board=(42.0, 40.0, 270.0),
            fields={
                "MPN": "61300811121",
                "Manufacturer": "Wurth Elektronik",
                "Datasheet": "https://www.we-online.com/components/products/datasheet/61300811121.pdf",
            },
        ),
    ]

    nets = {
        "VIN": ["J1.1", "F1.1"],
        "VM": ["F1.2", "D3.1", "C1.1", "C2.1", "C3.1", "U1.12", "R2.1"],
        # J4 is in the order the tracks arrive, so that nothing has to cross to
        # reach it: ground at both ends, then the two signals that come round
        # the outside of the package and the four that come straight out of it.
        # The four leave the package's east side from two columns of vias, at
        # x = 40.2-40.75 and at x = 38.2, and drop south on the back: the east
        # column lands first, the west column after it, and within a column
        # the upper via lands nearer. Any other order has two drops crossing,
        # and the one that loses has to go round the whole west end of the
        # board - which is what AIN1 did when it was on pin 3.
        "GND": [
            "J1.2",
            "D3.2",
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
        "VINT": ["U1.14", "C4.1"],
        "nFAULT": ["U1.8", "J4.7"],
        "nSLEEP": ["U1.1", "J4.2"],
        "AIN2": ["U1.15", "J4.3"],
        "BIN1": ["U1.9", "J4.4"],
        "AIN1": ["U1.16", "J4.5"],
        "BIN2": ["U1.10", "J4.6"],
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
        title="Dual H-bridge, DRV8833PW, 2 x 0.5 A RMS",
        rev="A",
        company="kicad_skills examples",
        notes=[],
        note_blocks=[
            (
                (17.78, 118.11),
                [
                    "PW package: 0.5 A RMS per bridge at VM=5 V, 25 C.",
                    "Not the 1.5 A thermally enhanced PWP/RTY versions.",
                    "0.4 mm outputs; verify temperature and stall current",
                    "for the actual motor. OCP is not a 0.5 A limiter.",
                ],
            ),
            (
                (17.78, 106.68),
                [
                    "C1 100 uF / 25 V bulk on a rail that can reach 10.8 V -",
                    "C2 10 uF / 25 V ceramic is the local VM bypass.",
                ],
            ),
            (
                (109.22, 115.57),
                ["VM indicator: about 1.5 mA at VM=9 V."],
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
                    "C4 2.2 uF bypasses VINT; no external load.",
                    "nFAULT needs a host-side 10k pull-up to 3.3 V.",
                ],
            ),
            (
                (162.56, 106.68),
                [
                    "AISEN/BISEN grounded: PWM current regulation",
                    "disabled; OCP is fault protection, not regulation.",
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
        power_flags=[("VM", "F1.2"), ("GND", "J1.2"), ("VINT", "C4.1")],
        board_size=(68.0, 46.0),
        tracks=[],
        vias=[],
        pour=(1.2, 1.2, 66.8, 44.8),
        # A motor's chassis is metal and the board bolts to it: the holes carry
        # ground so the enclosure is at the circuit's reference rather than
        # floating beside it.
        mounting=Mounting(grounded=True),
        fiducials=3,
        label_nets=("nSLEEP", "AIN1", "AIN2", "BIN1", "BIN2", "nFAULT"),
    )

    # Snap before the fan-out is worked out: it measures from where the pads
    # actually are, so the placement has to be final first.
    design = design.snapped()

    # -- the escape from the package ---------------------------------------
    # The PW package is rated at 0.5 A RMS per bridge under TI's stated
    # conditions. Outputs use the full 0.4 mm the escape geometry admits.
    SIG, POWER = 0.3, 0.4
    # The output side spreads to 2.0 mm because four 0.8 mm tracks start there
    # and two of them are 0.8 mm apart from a ground via.
    left, west = fan(
        design,
        "U1",
        ["1", "2", "3", "4", "5", "6", "7", "8"],
        lead=30.0,
        column=24.5,
        pitch=1.4,
        centre=25.5,
        width=SIG,
        # the four bridge outputs leave at the width they keep: no step
        widths={"2": POWER, "4": POWER, "5": POWER, "7": POWER, "3": POWER, "6": POWER},
    )
    left = [track for track in left if track.net not in {"nSLEEP", "GND"}]
    # The east side does not fan: the three supply pins reach their
    # capacitors on short front-side runs, GND drops to the back pour through
    # its own via, and the four logic inputs each get a via of their own at
    # the land's edge and go south on the back from there.
    # The east lands begin at x=38.625. A 0.58 mm via at x=38.2 leaves
    # 0.135 mm copper gap to its own land, so it does not require via-in-pad.
    east = {"16": (38.2, 23.225), "15": (40.75, 23.625), "10": (38.2, 27.125), "9": (40.2, 28.3)}
    right = [
        Track("AIN1", "F.Cu", SIG, ["U1.16", east["16"]]),
        Track("AIN2", "F.Cu", SIG, ["U1.15", (40.5, 23.875), east["15"]]),
        Track("BIN2", "F.Cu", SIG, ["U1.10", east["10"]]),
        Track("BIN1", "F.Cu", SIG, ["U1.9", (39.675, 27.775), east["9"]]),
    ]
    stops = {
        "3": (35.0, 24.525),
        "6": (35.0, 26.475),
        "13": (38.2, 25.175),
    }
    tracks = [
        *left,
        *right,
        *(Track("GND", "F.Cu", POWER, [f"U1.{number}", point]) for number, point in stops.items()),
    ]
    local_ground = (45.15, 26.0)
    vint_ground = (45.15, 23.5)
    vias = [
        *(Via("GND", x=point[0], y=point[1], size=0.58, drill=0.3) for point in stops.values()),
        Via("GND", x=local_ground[0], y=local_ground[1]),
        Via("GND", x=vint_ground[0], y=vint_ground[1]),
    ]

    # -- the supply, placed by hand ----------------------------------------
    # On two layers the supply has no plane to disappear into, so it is a
    # stated front-side spine: down the free column right of the capacitors,
    # then two short arms west into the bypass pair. The spine passes between
    # the two ground vias at x = 45.15, which is why they sit 2.5 mm apart -
    # the gap between their barrels is 1.7 mm and the arm needs 0.9 of it.
    #
    # The back of the board stays what it was on four layers: ground, and the
    # short logic lanes. Putting the rail there instead would have cut the
    # only reference plane the signals have, and the cut would have run the
    # length of the board.
    SPINE_X = 48.7
    vm_bypass, pump_supply = (42.05, 24.75), (43.95, 28.5)
    tracks += [
        Track("VM", "F.Cu", POWER, ["U1.12", (41.875, 25.825), "C2.1"]),
        Track("VM", "F.Cu", POWER, [vm_bypass, "C2.1"]),
        # terminal, fuse, clamp, bulk: one row, left to right as it flows
        Track("VIN", "F.Cu", POWER, ["J1.1", "F1.1"], auto=True),
        Track("VM", "F.Cu", POWER, ["F1.2", (52.3, 10.0), "D3.1"]),
        Track("GND", "F.Cu", POWER, ["D3.2", (53.7, 19.0)]),
        Track("VM", "F.Cu", POWER, ["F1.2", "C1.1"], auto=True),
        # the spine, and its two arms
        Track("VM", "F.Cu", POWER, ["C1.1", (SPINE_X, 8.0), (SPINE_X, 28.5)]),
        Track("VM", "F.Cu", POWER, [(SPINE_X, 24.75), vm_bypass]),
        Track("VM", "F.Cu", POWER, [(SPINE_X, 28.5), pump_supply]),
        Track("VM", "F.Cu", POWER, ["F1.2", "R2.1"], auto=True),
        Track("VCP", "F.Cu", POWER, ["U1.11", (40.6, 26.475), (42.05, 27.925), "C3.2"]),
        Track("VINT", "F.Cu", POWER, ["U1.14", (41.025, 24.525), "C4.1"]),
        Track("LED_A", "F.Cu", SIG, ["R2.2", "D2.2"], auto=True),
    ]
    vias += [Via("GND", x=53.7, y=19.0)]
    # The four logic inputs are boxed in by the supply fan on the front. A
    # short, ordered row of drops is clearer than four tours around that fan.
    for net, pin, header in (
        ("AIN2", "15", "J4.3"),
        ("BIN1", "9", "J4.4"),
        ("AIN1", "16", "J4.5"),
        ("BIN2", "10", "J4.6"),
    ):
        site = east[pin]
        vias.append(Via(net, x=site[0], y=site[1], size=0.58, drill=0.3))
        if net == "AIN2":
            # One declared back-side lane avoids the router's expensive-front
            # preference turning this connection into a 42 mm tour. It turns
            # at 35.72 so the last leg to pin 3 is a 45.
            tracks.append(
                Track(
                    net, "B.Cu", SIG, [site, (41.2, 24.075), (41.2, 35.72), header], keep_layer=True
                )
            )
            continue
        tracks.append(
            Track(
                net,
                "B.Cu",
                SIG,
                [site, header],
                auto=True,
                goal_layer="B.Cu",
                keep_layer=True,
            )
        )

    # -- ground ------------------------------------------------------------
    # Routed before the signals, because a ground pad that has to walk to find a
    # via has already lost the loop it was there to close. Each one asks for the
    # back of the board a couple of millimetres away and the router spends the
    # via; the plane is under all of it.
    tracks += [
        Track("GND", "F.Cu", POWER, ["C2.2", local_ground]),
        Track("GND", "F.Cu", POWER, ["C4.2", vint_ground]),
        Track("GND", "F.Cu", POWER, ["C1.2", (40.0, 12.0)], auto=True, goal_layer="B.Cu"),
        Track("GND", "F.Cu", POWER, ["J1.2", (60.0, 15.0)], auto=True, goal_layer="B.Cu"),
        Track("GND", "F.Cu", POWER, ["J4.1", (44.0, 42.0)], auto=True, goal_layer="B.Cu"),
        Track("GND", "F.Cu", POWER, ["J4.8", (22.0, 42.0)], auto=True, goal_layer="B.Cu"),
        Track("GND", "F.Cu", POWER, ["D2.1", (65.0, 24.0)], auto=True, goal_layer="B.Cu"),
    ]

    # -- everything that simply has to arrive ------------------------------
    nsleep_pickup = (35.25, 22.75)
    nsleep_landing = (37.1, 24.5)
    vias += [
        Via("nSLEEP", x=nsleep_pickup[0], y=nsleep_pickup[1]),
        Via("nSLEEP", x=nsleep_landing[0], y=nsleep_landing[1]),
    ]
    tracks += [
        Track("AOUT1", "F.Cu", POWER, [west["2"], "J2.2"], auto=True),
        Track("AOUT2", "F.Cu", POWER, [west["4"], "J2.1"], auto=True),
        Track("BOUT2", "F.Cu", POWER, [west["5"], "J3.2"], auto=True),
        Track("BOUT1", "F.Cu", POWER, [west["7"], "J3.1"], auto=True),
        Track("nSLEEP", "F.Cu", SIG, ["U1.1", (34.775, 23.225), nsleep_pickup]),
        Track(
            "nSLEEP",
            "B.Cu",
            SIG,
            [nsleep_pickup, (35.35, 22.75), nsleep_landing],
            keep_layer=True,
        ),
        Track("nSLEEP", "F.Cu", SIG, [nsleep_landing, (37.1, 37.64), "J4.2"]),
        Track("nFAULT", "F.Cu", SIG, [west["8"], "J4.7"], auto=True),
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
            board=(26.0, 28.13, 0.0),
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
            board=(10.0, 4.0, 0.0),
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
            board=(42.0, 4.0, 0.0),
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
            # Mirrored so the pins face the fuse to the right of it.
            sheet=(60.0, 40.0),
            mirror="y",
            board=(72.0, 8.0, 270.0),
            fields={
                "MPN": "1729128",
                "Manufacturer": "Phoenix Contact",
                "Datasheet": "https://www.phoenixcontact.com/product/1729128",
            },
        ),
        # The resettable fuse between the terminal and the diode: a short on
        # the carrier trips it instead of the bench supply, and it comes back
        # by itself. D1 already blocks a reversed supply, so this board gets a
        # fuse and no clamp.
        Part(
            "F1",
            "Device:Polyfuse",
            "0.75A",
            "Fuse:Fuse_1812_4532Metric",
            # 7.62 mm pin to pin on either side: each pin runs a 2.54 mm stub
            # before its wire, and two stubs closer than that draw over each
            # other.
            sheet=(76.2, 39.37),
            # Pin 2 towards the terminal on both the sheet and the board: the
            # footprint's pad 2 is the right-hand one, the symbol's pin 2 at
            # 270 degrees is the left-hand one, and the terminal is right of
            # the fuse on the board and left of it on the sheet.
            angle=270.0,
            board=(62.0, 8.0, 0.0),
            fields={
                "Current": "0.75A",
                "MPN": "MF-MSMF075-2",
                "Manufacturer": "Bourns",
                "Datasheet": "https://www.bourns.com/docs/product-datasheets/mfmsmf.pdf",
            },
        ),
        Part(
            "D1",
            "Device:D_Schottky",
            "SS14",
            "Diode_SMD:D_SMA",
            sheet=(91.44, 39.37),
            mirror="y",
            # Anode towards the fuse, cathode towards the module: the supply
            # enters from the right, so the diode faces the way the current
            # flows and nothing has to loop round it. At 180 degrees it faced
            # the other way and the rail crossed its own body to get there.
            board=(54.0, 8.0, 0.0),
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
            # Out of the header's legend strip. Every one of J4's twenty pins
            # carries its net name in the strip to its right, and at x=48
            # this capacitor stood across the rows of pins 3 and 4, so those
            # two legends had nowhere to print but on it - or, before the
            # placer was told a legend has to name the nearest pin, one pin
            # along, where they named the wrong one. Below the diode at
            # 53.5, its supply pad is the one nearest the diode's, three and
            # a half millimetres straight up.
            board=(53.5, 11.5, 0.0),
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
            # 54.5, not 48: pin 6's legend is ADC_VREF, the longest name on
            # the header, and it needs the strip clear to x=52.4 on its row.
            board=(54.5, 17.0, 0.0),
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
            sheet=(203.2, 60.96),
            # 60, not 58: two millimetres east with the capacitor, so the two
            # courtyards keep their gap.
            board=(60.0, 17.0, 0.0),
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
            sheet=(196.85, 78.74),
            angle=90.0,
            board=(66.0, 17.0, 180.0),
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

    nets["+5V"] = ["J1.1", "F1.2"]
    nets["5V_FUSED"] = ["F1.1", "D1.2"]
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
                # Right of the fuse's own flag and symbol, which want the room
                # above the supply row.
                (104.14, 17.78),
                [
                    "5 V in feeds VSYS through F1 and D1. D1 is the datasheet's own",
                    "arrangement: it keeps USB and the external supply from fighting",
                    "when both are plugged in, and blocks a reversed supply. F1 is a",
                    "0.75 A resettable fuse: a short on the carrier trips it, not the",
                    "bench supply. C1 22 uF / 16 V is the bulk that goes with them.",
                ],
            ),
            (
                (172.72, 38.1),
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
                # The left column, not under the AGND note: fifty-eight
                # characters starting at the right-hand column run 67 mm and
                # end inside the title block, and no amount of sliding fixes
                # a line that is wider than the paper left beside it.
                (20.32, 163.0),
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
        # The input rail is two pins facing each other across the fuse: drawn
        # as the wire it is, not as two supply symbols whose taps would run
        # into each other along the same line.
        wired_power=("+5V",),
        board_size=(80.0, 60.0),
        tracks=[],
        vias=[],
        pour=(1.2, 1.2, 78.8, 58.8),
        mounting=Mounting(),
        fiducials=3,
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
        # Terminal, fuse and diode sit in one line at y = 8 with a few
        # millimetres between pads - less than the search wants to leave a
        # 3.4 mm pad by - so both links are stated rather than searched for.
        Track("+5V", "F.Cu", POWER, ["J1.1", "F1.2"]),
        Track("5V_FUSED", "F.Cu", POWER, ["F1.1", "D1.2"]),
        # The capacitor hangs off the diode's pad, three and a half
        # millimetres straight down. Not diode-to-capacitor-to-header: a
        # 1210's pad sits 0.02 mm off the routing grid, and two links landing
        # on it from opposite sides meet in a 0.02 mm zigzag at the pad
        # centre that `route.odd_angle` reads as a 92 degree corner.
        Track("VSYS", "F.Cu", POWER, ["D1.1", "J4.2"], auto=True),
        Track("VSYS", "F.Cu", POWER, ["C1.1", "D1.1"], auto=True),
        Track("+3V3", "F.Cu", POWER, ["C2.1", "J4.5"], auto=True),
        Track("+3V3", "F.Cu", 0.4, ["C2.1", "R1.1"], auto=True),
        Track("LED_P", "F.Cu", SIG, ["R1.2", "D3.2"], auto=True),
    ]
    # Ground: every pad drops straight through to the plane under it.
    for pad, target in (
        ("C1.2", (51.0, 14.0)),
        ("C2.2", (50.5, 17.0)),
        ("D3.1", (70.0, 21.0)),
        ("J1.2", (66.0, 14.0)),
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
            (5.0, 17.0, 0.0),
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
            (11.0, 17.0, 90.0),
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
            (18.0, 23.0, 0.0),
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
            (17.0, 17.0, 0.0),
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
            (25.0, 17.0, 0.0),
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
            (21.0, 10.0, 0.0),
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
            (26.0, 24.0, 0.0),
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
            board=(33.0, 17.0, 0.0),
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
            # Beside the escape column it feeds, not across the board from it.
            # U1's supply pin is the middle of its west row and the row is
            # walled in by the signal chain U1 sits in: from the north-east
            # the only way to the column was round the east edge of the board
            # and back, 56 mm of copper for an 8 mm pin pair, which is what
            # `route.wander` reported. TP1 gave up the corner for it.
            (29.0, 12.5, 0.0),
            Voltage="25V",
            Tolerance="10%",
            MPN="CL21B104KBCNNNC",
            Manufacturer="Samsung",
            Datasheet=SAMSUNG.format("CL21B104KBCNNNC"),
        ),
        # The amplifier does not meet the cable directly: R8 isolates its
        # output from whatever capacitance hangs on the far side of J3, and
        # from a short there. 100 ohms against C6's 1 uF is nothing at 1 kHz
        # and against the 100k bleed it is not a divider.
        _passive(
            "R8",
            "Device:R",
            "100R",
            "Resistor_SMD:R_0805_2012Metric",
            # U1 draws a 6.35 mm stub off its output pin; R8's own stub has
            # to start clear of the end of it.
            (182.88, 100.33),
            (41.0, 17.0, 270.0),
            angle=90.0,
            Tolerance="1%",
            Power="0.125W",
            MPN="RC0805FR-07100RL",
            Manufacturer="Yageo",
            Datasheet=YAGEO,
        ),
        _passive(
            "C6",
            "Device:C",
            "1u",
            "Capacitor_SMD:C_0805_2012Metric",
            (199.39, 100.33),
            (44.5, 17.0, 90.0),
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
            (51.0, 17.0, 0.0),
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
            # Far enough from the fuse that the two VIN labels between them
            # do not print over each other.
            (210.82, 35.56),
            (9.0, 7.0, 0.0),
            MPN="1729128",
            Manufacturer="Phoenix Contact",
            Datasheet="https://www.phoenixcontact.com/product/1729128",
        ),
        # Fuse and TVS on the supply. The op-amps draw microamps, so 500 mA is
        # already generous; the TVS stands off 5 V and is what a reversed
        # supply flows through on its way to opening the fuse. It does not
        # hold the rail under the MCP6001's 7 V maximum against a sustained
        # overvoltage - nothing this small does - and the sheet says so.
        _passive(
            "F1",
            "Device:Fuse",
            "500mA",
            "Fuse:Fuse_1206_3216Metric",
            (185.42, 35.56),
            (20.0, 5.0, 0.0),
            angle=270.0,
            Current="500mA",
            MPN="0466.500NR",
            Manufacturer="Littelfuse",
            Datasheet=LITTELFUSE_466,
        ),
        _passive(
            "D3",
            "Device:D_Zener",
            "SMAJ5.0A",
            "Diode_SMD:D_SMA",
            (168.91, 41.91),
            (26.5, 5.0, 0.0),
            angle=270.0,
            Voltage="5V",
            Power="400W",
            MPN="SMAJ5.0A",
            Manufacturer="Littelfuse",
            Datasheet=LITTELFUSE_SMAJ,
        ),
        _passive(
            "R3",
            "Device:R",
            "100k",
            "Resistor_SMD:R_0805_2012Metric",
            (74.93, 152.4),
            (7.0, 30.0, 90.0),
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
            (7.0, 36.0, 90.0),
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
            (12.0, 34.0, 0.0),
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
            board=(26.0, 32.0, 0.0),
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
            (9.0, 25.0, 90.0),
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
            (46.0, 24.0, 90.0),
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
            (14.0, 29.0, 0.0),
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
            # Moved out of the corner beside U1's west escape column so C5 can
            # have it: the supply pin needs a cap it can reach, the test point
            # only needs a probe.
            board=(24.0, 12.0, 0.0),
            no_connect=False,
        ),
        Part(
            "TP2",
            "Connector:TestPoint",
            "TP",
            "TestPoint:TestPoint_Pad_D1.5mm",
            # Between the amplifier and R8 on the sheet, so it reads what the
            # amplifier makes rather than what the cable sees; on the wire
            # between the two pins' stubs, not on either stub.
            sheet=(175.26, 87.63),
            board=(44.0, 12.0, 0.0),
            no_connect=False,
        ),
        Part(
            "TP3",
            "Connector:TestPoint",
            "TP",
            "TestPoint:TestPoint_Pad_D1.5mm",
            sheet=(168.91, 168.91),
            # Beside the reference buffer, not in the strip along the bottom
            # edge: that strip is where the board writes its own name, and at
            # (31, 36) the test point stood in the middle of it, so the name
            # printed across the pad - readable on the bare board, and under
            # the probe the moment anyone used it.
            board=(36.0, 30.0, 0.0),
            no_connect=False,
        ),
    ]

    nets = {
        "VIN": ["J2.1", "F1.1"],
        "+5V": ["F1.2", "D3.1", "R3.1", "C5.1", "C7.1", "U1.2", "U2.2"],
        "GND": [
            "J2.2",
            "D3.2",
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
        "OUT": ["U1.1", "U1.4", "C1.2", "R8.1", "TP2.1"],
        "OUT_R": ["R8.2", "C6.1"],
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
                    "R8 100R keeps the cable's capacitance, and a short",
                    "at J3, off U1's output.",
                ],
            ),
            (
                (150.0, 25.4),
                [
                    "F1 500 mA and D3 (5 V standoff): reversed leads flow",
                    "through D3 and open F1. D3 does not hold the rail under",
                    "the op-amps' 7 V maximum against a sustained overvoltage.",
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
        power_flags=[("+5V", "F1.2"), ("GND", "J2.2")],
        board_size=(58.0, 42.0),
        # The strip under the supply terminal's body, below its own pads. The
        # rail to the second amplifier reaches for it every time - it is the
        # short way across - and copper under a screw terminal cannot be
        # probed or reworked without taking the terminal off the board.
        keepouts=((6.0, 8.5, 17.0, 12.1),),
        # The three connectors, whole, closed to every net but their own.
        # With the rail routed first it took the short way under the input
        # terminal's shell to reach the regulator side - one segment of
        # `route.under_package`, and the reason the rule measures a
        # connector against its courtyard.
        body_keepout=("J1", "J2", "J3"),
        # First pick of the board, beyond the rail the widths already give
        # it: the signal path. On a filter that is what the board is for -
        # the input, the two filter nodes, the output and the reference it
        # is all measured against. Routed after the rail's own links the
        # two filter nodes toured, 6.5x and 7.7x, under both terminals. The
        # bias divider's midpoint and the output coupling are the plain
        # links and go round.
        priority_nets=("IN", "IN_DC", "X", "FILT_IN", "OUT", "VREF"),
        tracks=[],
        vias=[
            # mid-board ties between the faces: the signal row slices the
            # front pour, and these give its pieces a short way to the plane
            Via("GND", x=13.0, y=21.0),
            Via("GND", x=22.0, y=21.0),
            Via("GND", x=33.0, y=24.0),
            Via("GND", x=44.0, y=20.0),
        ],
        pour=(1.2, 1.2, 56.8, 40.8),
        mounting=Mounting(),
        fiducials=3,
        notes_at=(18.0, 20.0),
    ).snapped()

    SIG = 0.3
    POWER = 0.5
    # A SOT-23-5 puts three pads on one side at 0.95 mm, and the middle one is
    # the supply. Nothing can reach it except straight out, so the row leaves as
    # a stated fan and the router picks the nets up clear of the package - the
    # same reason the motor driver's TSSOP does, two sizes down.
    escapes: list[Track] = []
    ends: dict[str, dict[str, tuple[float, float]]] = {}
    # U1's inverting pin keeps its place in the east column: it carries the
    # output on to C6 as well, so the escape is copper the board needs. U2's
    # does not - its only connection is the feedback wrap round the package,
    # and an escape drawn out to the column for it is copper going nowhere
    # with the wrap crossing it to get back.
    for ref, (cx, cy, _), east_pins in (
        ("U1", (33.0, 17.0, 0), ["5", "4"]),
        ("U2", (26.0, 32.0, 0), ["5"]),
    ):
        west, ends[f"{ref}w"] = fan(
            design,
            ref,
            ["1", "2", "3"],
            lead=cx - 3.4,
            column=cx - 6.0,
            pitch=1.9,
            centre=cy,
            width=SIG,
            widths={"2": POWER},
        )
        east, ends[f"{ref}e"] = fan(
            design,
            ref,
            east_pins,
            lead=cx + 3.4,
            column=cx + 6.0,
            pitch=2.8,
            centre=cy,
            width=SIG,
            # pin 5 is the ground return, and it leaves at the width it keeps
            widths={"5": POWER},
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
        Track("OUT", "F.Cu", SIG, ["TP2.1", "R8.1"], auto=True),
        # VREF and its taps run at signal width end to end: the escape from
        # the SOT-23-5 is 0.3 mm whatever the link says, and a run that steps
        # to 0.5 at the first corner past it is a step nobody chose. The
        # reference is a buffered half-rail carrying microamps; 0.3 is honest.
        Track("VREF", "F.Cu", SIG, ["TP3.1", "C2.2"], auto=True),
        Track("FILT_IN", "F.Cu", SIG, ["C2.1", u1w["3"]], auto=True),
        # U1's wrap runs escape to escape: its inverting pin has an escape
        # anyway, because it carries the output on to C6. U2's runs pad to
        # pad, because U2's inverting pin has nothing but the wrap and its
        # escape would be copper the wrap then has to cross to get back -
        # asked for between the columns there, the wrap went round the board.
        Track("OUT", "F.Cu", SIG, [u1w["1"], u1e["4"]], auto=True),
        Track("OUT", "F.Cu", SIG, [u1w["1"], "C1.2"], auto=True),
        Track("OUT", "F.Cu", SIG, [u1e["4"], "R8.1"], auto=True),
        Track("OUT_R", "F.Cu", SIG, ["R8.2", "C6.1"], auto=True),
        Track("OUT_AC", "F.Cu", SIG, ["C6.2", "J3.1"], auto=True),
        Track("OUT_AC", "F.Cu", SIG, ["J3.1", "R6.1"], auto=True),
        Track("VREF", "F.Cu", SIG, ["U2.1", "U2.4"], auto=True),
        Track("VREF", "F.Cu", SIG, [u2w["1"], "C2.2"], auto=True),
        Track("VREF", "F.Cu", SIG, [u2w["1"], "R5.2"], auto=True),
        Track("MID", "F.Cu", SIG, ["R3.2", "R4.1"], auto=True),
        Track("MID", "F.Cu", SIG, ["R4.1", "C4.1"], auto=True),
        Track("MID", "F.Cu", SIG, ["C4.1", u2w["3"]], auto=True),
        Track("VIN", "F.Cu", POWER, ["J2.1", "F1.1"], auto=True),
        Track("+5V", "F.Cu", POWER, ["F1.2", "D3.1"], auto=True),
        Track("+5V", "F.Cu", POWER, ["F1.2", "C5.1"], auto=True),
        Track("+5V", "F.Cu", POWER, ["C5.1", u1w["2"]], auto=True),
        # Down the corridor between the terminal's body and the filter's first
        # row, then left. Sent straight at C7 the rail cuts the corner off J2's
        # courtyard, and copper under a screw terminal cannot be probed or
        # reworked without taking the terminal off - `route.under_package`.
        Track("+5V", "F.Cu", POWER, ["C5.1", "C7.1"], auto=True),
        Track("+5V", "F.Cu", POWER, ["C7.1", u2w["2"]], auto=True),
        # ...and the divider's feed keeps the rail's width to the junction:
        # a 0.3 branch butt-joined onto 0.5 trunk mid-run is the same
        # nobody-chose-this step, seen from the other side.
        Track("+5V", "F.Cu", POWER, ["C7.1", "R3.1"], auto=True),
    ]
    # Each ground pad drops to the plane a couple of millimetres away, on the
    # side away from the signal it returns: the loop closes at the part. The
    # two that start at the end of an escape keep the escape's width: a run
    # that steps from 0.3 to 0.5 halfway along is a step nobody chose, and the
    # 0.65 mm row it left is what set the width in the first place.
    for pad, target, width in (
        ("J1.2", (8.0, 22.0), POWER),
        ("D3.2", (31.0, 5.0), POWER),
        ("J3.2", (49.0, 22.0), POWER),
        ("R6.2", (46.0, 20.0), POWER),
        ("R7.2", (9.0, 30.0), POWER),
        ("J2.2", (14.0, 6.0), POWER),
        ("C5.2", (31.0, 11.0), POWER),
        ("C7.2", (14.0, 33.0), POWER),
        ("C4.2", (15.0, 37.0), POWER),
        ("R4.2", (11.0, 38.0), POWER),
        (u1e["5"], (39.0, 13.0), POWER),
        (u2e["5"], (36.0, 34.0), POWER),
    ):
        tracks.append(Track("GND", "F.Cu", width, [pad, target], auto=True, goal_layer="B.Cu"))
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
                [(196.0, 110.0), (196.0, 200.0), (56.0, 110.0), (112.0, 40.0)], start=1
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
            # Right of the fuse's own supply bus and below the terminal's
            # ground bus: J1, F1 and U3 read left to right as the supply
            # flows, and nothing of theirs lands on anybody else's row.
            sheet=(68.58, 46.99),
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
            sheet=(196.0, 258.0),
            # 62, not 72: the SPI port is on the FPGA's south edge, and at
            # 72 the bus needed 29 mm to reach it - `layout.connection_span`
            # before a single track was drawn.
            board=(40.0, 62.0, 0.0),
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
            sheet=(56.0, 150.0),
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
            # Mirrored so its pins face the fuse to its right, the way the
            # other boards draw their input. Its ground runs out along a bus
            # past the fuse before it turns, so nothing else may sit on that
            # row for the next thirty millimetres.
            sheet=(27.94, 39.37),
            mirror="y",
            board=(8.0, 8.0, 270.0),
            fields={
                "MPN": "1729128",
                "Manufacturer": "Phoenix Contact",
                "Datasheet": "https://www.phoenixcontact.com/product/1729128",
            },
        ),
        # Fuse and TVS on the 3.3 V input. The whole board draws well under
        # 100 mA, so 500 mA is the fuse; the TVS is the lowest standoff the
        # SMAJ series comes in, which leaves a 3.3 V rail well inside it. It
        # is what reversed leads flow through, and it does not hold the rail
        # under the FPGA's 3.6 V maximum - the sheet says so.
        Part(
            "F1",
            "Device:Fuse",
            "500mA",
            "Fuse:Fuse_1206_3216Metric",
            # 10.16 mm pin to pin from the terminal: each pin runs a 2.54 mm
            # stub, and the VIN label between them wants room of its own.
            sheet=(46.99, 39.37),
            angle=90.0,
            board=(17.0, 10.0, 0.0),
            fields={
                "Current": "500mA",
                "MPN": "0466.500NR",
                "Manufacturer": "Littelfuse",
                "Datasheet": LITTELFUSE_466,
            },
        ),
        Part(
            "D3",
            "Device:D_Zener",
            "SMAJ5.0A",
            "Diode_SMD:D_SMA",
            # Well below the fuse: the terminal's ground bus and its flag own
            # the rows right under it, and the regulator prints its value into
            # the space to the fuse's lower right.
            sheet=(39.37, 60.96),
            angle=270.0,
            board=(23.5, 10.0, 0.0),
            fields={
                "Voltage": "5V",
                "Power": "400W",
                "MPN": "SMAJ5.0A",
                "Manufacturer": "Littelfuse",
                "Datasheet": LITTELFUSE_SMAJ,
            },
        ),
        Part(
            "J2",
            "Connector:Conn_01x03_Pin",
            "AUDIO OUT",
            "Connector_PinHeader_2.54mm:PinHeader_1x03_P2.54mm_Vertical",
            # 388, not 395: the GND symbol lands to the connector's right, and
            # at 395 its printed name crossed the right frame strip of the A3.
            sheet=(388.0, 110.0),
            board=(95.0, 38.0, 0.0),
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
            sheet=(268.0, 240.0),
            # 66, not 74: eight millimetres west, still at the bottom edge
            # the cable arrives from, and it is eight millimetres off every
            # span this header is one end of.
            board=(66.0, 66.0, 0.0),
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
        cap("C1", "10u", (84.0, 48.0), (7.5, 17.5, 0.0), "16V", "CL10A106MQ8NNNC"),
        cap("C2", "100n", (100.0, 48.0), (7.5, 21.0, 0.0), "25V", "CL10B104KB8NNNC"),
        cap("C3", "10u", (63.5, 62.0), (22.0, 30.0, 0.0), "16V", "CL10A106MQ8NNNC"),
        cap("C4", "100n", (87.63, 62.0), (25.0, 38.5, 90.0), "25V", "CL10B104KB8NNNC"),
        cap("C5", "100n", (196.0, 48.0), (56.0, 47.0, 270.0), "25V", "CL10B104KB8NNNC"),
        cap("C17", "10u", (180.0, 48.0), (59.0, 47.0, 270.0), "16V", "CL10A106MQ8NNNC"),
        res("R3", "100R", (164.0, 48.0), (63.0, 54.0, 90.0), "RC0603FR-07100RL"),
        res("R4", "10k", (276.0, 232.0), (56.0, 74.0, 0.0), "RC0603FR-0710KL"),
        cap("C6", "100n", (244.0, 84.0), (57.0, 50.0, 0.0), "25V", "CL10B104KB8NNNC"),
        cap("C7", "100n", (244.0, 108.0), (61.0, 50.0, 0.0), "25V", "CL10B104KB8NNNC"),
        cap("C8", "100n", (236.0, 258.0), (46.0, 66.0, 0.0), "25V", "CL10B104KB8NNNC"),
        # C9 sits clear of R5's label on the sheet; on the board it stays
        # against the oscillator's supply pin.
        cap("C9", "100n", (96.52, 150.0), (36.0, 14.0, 0.0), "25V", "CL10B104KB8NNNC"),
        # The oscillator's output leaves through R5: 33 ohms at the source
        # damps the edge into the 30 mm of track to the FPGA, so the clock
        # arrives once rather than ringing. R6 holds the codec muted until
        # the FPGA is configured and drives XSMT high itself.
        res("R5", "33R", (71.12, 149.86), (31.0, 18.5, 0.0), "RC0603FR-0733RL"),
        # R6 stays north of the corridor between the FPGA and the codec.
        # In the corridor it is nearer XSMT's own escape and blocks the lane
        # four other nets use to cross - the router could not seat its own
        # ground stub there, let alone the rest. The distance costs XSMT a
        # `route.wander`; the corridor would cost five nets a detour each.
        res("R6", "10k", (287.02, 127.0), (61.0, 33.5, 0.0), "RC0603FR-0710KL"),
        cap("C10", "100n", (244.0, 132.0), (60.0, 30.0, 0.0), "25V", "CL10B104KB8NNNC"),
        cap("C11", "100n", (300.0, 62.0), (85.0, 35.0, 0.0), "25V", "CL10B104KB8NNNC"),
        cap("C16", "100n", (328.0, 62.0), (89.0, 49.0, 0.0), "25V", "CL10B104KB8NNNC"),
        cap("C12", "2u2", (296.0, 158.0), (60.0, 54.0, 0.0), "16V", "CL10A225KO8NNNC"),
        cap("C13", "2u2", (324.0, 158.0), (89.0, 41.0, 90.0), "16V", "CL10A225KO8NNNC"),
        cap("C14", "2u2", (352.0, 158.0), (89.0, 45.5, 90.0), "16V", "CL10A225KO8NNNC"),
        # CRESET runs from the header to the FPGA, and on a board this wide
        # that is one 46 mm hop however the two are placed. Its pull-up is
        # the third node on the net, so standing it between them makes the
        # hop two, and a 10k pull-up does not care where it sits.
        res("R1", "10k", (112.0, 232.0), (52.0, 58.0, 0.0), "RC0603FR-0710KL"),
        res("R2", "10k", (140.0, 232.0), (34.0, 22.0, 0.0), "RC0603FR-0710KL"),
        cap("C15", "100n", (148.0, 62.0), (57.0, 54.0, 180.0), "25V", "CL10B104KB8NNNC"),
    ]

    nets = {
        "VIN": ["J1.1", "F1.1"],
        "+3V3": [
            "F1.2",
            "D3.1",
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
            "D3.2",
            "R6.2",
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
        # the clock leaves the oscillator through its series resistor
        "OSC_OUT": ["X1.3", "R5.1"],
        "CLK12": ["R5.2", "U1.37"],
        # Soft mute under the FPGA's control: R6 holds it low - muted - until
        # the configured design drives it, so the DAC comes up silent instead
        # of with the rail.
        "XSMT": ["U1.31", "U2.17", "R6.1"],
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
        # no de-emphasis, normal filter. The mute is the exception, above.
        "FMT": ["U2.16"],
        "DEMP": ["U2.10"],
        "FLT": ["U2.11"],
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
        ("U4.3", "+3V3"),
        ("U4.7", "+3V3"),
        ("X1.1", "+3V3"),
    ):
        nets[rail].append(pin)
    for name in ("FMT", "DEMP", "FLT", "WP", "HOLD", "OSC_EN"):
        del nets[name]

    design = Design(
        name="fpga-audio",
        title="iCE40UP5K to PCM5102A, I2S out",
        rev="A",
        company="kicad_skills examples",
        notes=[],
        note_blocks=[
            (
                (17.78, 232.0),
                [
                    "A 0.5 mm pitch QFN with pads on four sides is not a two layer",
                    "board. A real iCE40 design drops each pin into an inner layer;",
                    "this one has no inner layer, so all 48 escape on the top at",
                    "0.2 mm track and 0.2 mm clearance - a fine-line process, and",
                    "the reason a 7 mm chip needs 25 mm of board around it.",
                ],
            ),
            (
                (100.0, 78.0),
                [
                    "F1 500 mA and D3 (5 V standoff) guard the input:",
                    "reversed leads flow through D3 and open F1. D3 does",
                    "not hold the rail under the FPGA's 3.6 V maximum.",
                    "C1 10u + C2 100n: the 3.3 V input, at U3.",
                    "C3/C4: the 1.2 V core rail it makes. C15 sits",
                    "on U1's VCC pins; VCCPLL is filtered from the",
                    "core rail through R3, C17 and C5 at the pin -",
                    "core switching noise stays out of the PLL.",
                ],
            ),
            (
                (17.78, 170.0),
                [
                    "R5 33R at X1's output damps the 12 MHz edge into",
                    "the 30 mm run to the FPGA: the clock arrives once.",
                ],
            ),
            (
                (258.0, 152.0),
                [
                    "C6/C7, C10: one 100n per FPGA I/O-bank",
                    "supply pin, beside the bank they feed.",
                ],
            ),
            (
                (296.0, 186.0),
                [
                    "U2's mode pins are strapped, not driven: 16-bit I2S,",
                    "no de-emphasis, normal filter. XSMT is the exception:",
                    "R6 holds it low so the DAC comes up muted, and the",
                    "configured FPGA un-mutes it - no power-up pop.",
                    "C11/C16 bypass its supplies; C12-C14 are the charge",
                    "pump and LDO reservoirs the datasheet asks for.",
                ],
            ),
            (
                (17.78, 258.0),
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
        power_flags=[("+3V3", "F1.2"), ("GND", "J1.2")],
        board_size=(100.0, 84.0),
        label_nets=("I2S_SCK", "I2S_BCK", "I2S_DIN", "I2S_LRCK", "XSMT"),
        # No foreign copper under the boot flash or the DAC: their bellies
        # are the strips a rail sneaks through when everything else is full,
        # and a rail under a part it does not feed is `route.under_package` -
        # the plane cannot get between them on two layers. Fencing U4 alone
        # just moved the 1.2 V rail under U2, which is the worse place: the
        # DAC is the one analogue part on the board.
        route_keepout=("U4", "U2"),
        # The two headers, whole. Both are 1x0N verticals, so there is no strip
        # between pad rows for `route_keepout` to close, and the cold route put
        # +3V3 under J3's shell on its way south and across J2's on its way to
        # the audio pins - nine segments of `route.under_package`. The rail goes
        # round them now.
        body_keepout=("J2", "J3"),
        # First pick of the board. The three rails have to be named: on this
        # board +3V3 and +1V2 are distributed at signal width (the
        # `track.thin_power` waiver says why), so the width alone does not
        # mark them, and routed after the clocks and the bus the codec's own
        # supply pickup found no lane left between the package and the jack.
        # Then the 12 MHz clock and its buffered copy, and the four I2S lines
        # that have to arrive together. The boot bus is not among them - it
        # runs once at power-up, and the return-path waiver says so - and
        # neither are the line outputs, which are audio-rate analogue.
        priority_nets=(
            "+3V3",
            "+1V2",
            "VCCPLL",
            "CLK12",
            "OSC_OUT",
            "I2S_SCK",
            "I2S_BCK",
            "I2S_DIN",
            "I2S_LRCK",
        ),
        # The outer ring of the pour, closed to the router. On the widest
        # board in the set the perimeter is the emptiest lane there is, and
        # the router took it twice - 20.5 mm of the bottom edge for SPI_SS
        # and 11.5 mm of the right for SPI_SO, which is `layout.pour_edge_cut`
        # and the reason that rule exists. Reporting it after the fact is the
        # review's job; not building it is this file's. 1.7 mm so a track of
        # any width these boards use still leaves the ring its millimetre.
        keepouts=(
            (1.2, 1.2, 2.9, 82.8),
            (97.1, 1.2, 98.8, 82.8),
            (1.2, 1.2, 98.8, 2.9),
            (1.2, 81.1, 98.8, 82.8),
        ),
        tracks=[],
        vias=[],
        pour=(1.2, 1.2, 98.8, 82.8),
        mounting=Mounting(),
        fiducials=3,
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
        column=82.5,
        pitch=1.0,
        centre=40.0,
        width=SIG,
    )
    escape("U3", ["1", "2", "3"], lead=11.4, column=9.0, pitch=1.9, centre=24.0, width=SIG)
    escape("U3", ["5", "4"], lead=16.6, column=19.0, pitch=2.8, centre=24.0, width=SIG)
    escape(
        "U4",
        ["1", "2", "3", "4"],
        lead=36.1,
        column=33.5,
        pitch=2.0,
        centre=62.0,
        width=SIG,
    )
    escape(
        "U4",
        ["8", "7", "6", "5"],
        lead=43.9,
        column=46.5,
        pitch=2.0,
        centre=62.0,
        width=SIG,
    )

    # The exposed pad is the ground, and it is stitched rather than routed.
    vias = [Via("GND", x=cx + dx, y=cy + dy) for dx in (-1.0, 0.0, 1.0) for dy in (-1.0, 0.0, 1.0)]

    # A decoupling capacitor's ground via sits against its own pad, on the far
    # side from the supply pad. The loop the capacitor exists to close runs
    # pad, via, plane; a via at the end of a routed track puts that track's
    # inductance inside the loop, which is what `layout.decoupling_via` measures.
    anchored: list[Track] = []
    placed: set[str] = set()
    # Every decoupling capacitor on the board, not the handful the loop began
    # with. The rest were dropping to the plane through a routed stub with the
    # via wherever the search chose to spend it, and once a via may no longer
    # land in a pad (`via.in_pad`) the search ran out of room beside the
    # densest of them altogether. Placing the via is both the fix and the
    # better layout: the loop is pad, via, plane, with nothing routed in it.
    for cref in (
        "C1",
        "C2",
        "C3",
        "C9",
        "C10",
        "C11",
        "C15",
        "C4",
        "C5",
        "C6",
        "C7",
        "C8",
        "C12",
        "C14",
        "C16",
        "C17",
    ):
        supply = pad_position(design, f"{cref}.1")
        ground = pad_position(design, f"{cref}.2")
        length = math.hypot(ground[0] - supply[0], ground[1] - supply[1]) or 1.0
        away = ((ground[0] - supply[0]) / length, (ground[1] - supply[1]) / length)
        site = anchor_site(design, f"{cref}.2", away, [*escapes, *anchored], vias)
        if site is None:
            # Nowhere legal beside this one: it keeps the routed drop below,
            # and the search spends the via where it finds room.
            continue
        offset = (round(site[0] - ground[0], 4), round(site[1] - ground[1], 4))
        vias.append(Via("GND", pad=f"{cref}.2", offset=offset))
        anchored.append(Track("GND", "F.Cu", 0.4, [f"{cref}.2", site]))
        placed.add(cref)

    # The 1.2 V rail gets a stated spine, the way the motor board states its
    # VM link. Its consumers sit on both sides of the FPGA, and every
    # east-west lane south of the package is a comb of SPI escapes - routed
    # link by link the rail toured the south edge of the board to get
    # across (122 mm for 39). The one corridor nothing else can use is under
    # the FPGA's own die: the QFN's pads are surface copper, the strip
    # between its south pad row and its ground-via grid is empty on the
    # back, and the rail is the package's own supply, so nothing foreign
    # runs under anything. One straight stroke, back side, a via at each
    # end; the links then tap it wherever is nearest.
    # The west via sits west of the escape column (x = 27), because the
    # column is a comb of horizontal escape lines at every half-millimetre
    # of y and a through via parked in the comb lands on whichever line owns
    # that lane. The east via stops short of the east pad row by its own
    # clearance, and the whole stroke sits at 42.25 - a quarter-millimetre
    # off the south pad row's reach (their inner ends are at y = 43.01, and
    # at 42.5 the via missed them by a tenth), and still on the router's
    # grid, which is what lets a tee land on the stroke at all.
    SPINE_1V2 = ((25.5, 42.25), (42.2, 42.25))
    vias.append(Via("+1V2", x=SPINE_1V2[0][0], y=SPINE_1V2[0][1]))
    # No via on the east end: the exposed pad owns the die centre on the
    # front - a through via there is a short against U1's ground paddle -
    # and none is needed, because the east tap is a back-side link that
    # starts exactly where the stroke ends.
    anchored.append(Track("+1V2", "B.Cu", POWER, [SPINE_1V2[0], SPINE_1V2[1]]))

    # Every endpoint goes through `end`, which returns the far end of a pin's
    # escape when it has one and the pad itself when it does not.
    tracks = [*escapes, *anchored]
    routes = [
        ("VIN", POWER, [("J1.1", "F1.1")]),
        # The input rail is wide as far as the capacitors; into the regulator
        # it goes at the SOT-23-5's own escape width, because a 0.4 mm run
        # landing on a 0.2 mm neck steps down in the open, and the neck is
        # what sets the current anyway. The whole board draws under 100 mA.
        # Down the right of the terminal and in from below: sent straight at
        # C1 the rail crosses J1's own body, and a screw terminal has to come
        # off the board before anyone can see the copper under it.
        # Round the terminal's right-hand side and in from below. The short
        # way from the fuse to the bulk capacitor is under J1's body, and a
        # screw terminal has to come off the board before anyone can see the
        # copper under it - `route.under_package`. The waypoint is what makes
        # the search go round rather than through.
        (
            "+3V3",
            POWER,
            [("F1.2", "D3.1"), ("F1.2", (18.4, 20.0)), ((18.4, 20.0), "C1.1"), ("C1.1", "C2.1")],
        ),
        ("+3V3", SIG, [("C1.1", "U3.1"), ("C2.1", "U3.3")]),
        (
            "+3V3",
            SIG,
            [
                ("C2.1", "R2.1"),
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
                ("R2.1", "C8.1"),
                # ...and this is what joins the input side to the bank supplies.
                # Without it +3V3 is two islands that the schematic calls one net.
                ("C8.1", "U1.22"),
                ("C8.1", "U4.8"),
                ("C8.1", "U4.3"),
                ("C8.1", "R1.1"),
                ("U4.3", "U4.7"),
                ("R2.1", "C9.1"),
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
                ("C15.1", "U1.30"),
                ("C15.1", "R3.1"),
            ],
        ),
        # One link into each end of the stated spine, in place of the
        # C4-to-C15 haul that had to cross the SPI comb. (The east tap is
        # stated separately above: it has to leave on the back.) At power
        # width, because that is what it lands on - routed at signal width it
        # stepped 0.20 to 0.40 mm four millimetres short of the spine, which
        # is `route.width_step`: a change nobody chose, in the open.
        ("+1V2", POWER, [("C4.1", SPINE_1V2[0])]),
        ("VCCPLL", SIG, [("R3.2", "C17.1"), ("C17.1", "C5.1"), ("C5.1", "U1.29")]),
        ("SPI_SS", SIG, [("U1.16", "U4.1"), ("U4.1", "J3.1"), ("J3.1", "R4.2")]),
        ("+3V3", SIG, [("C8.1", "R4.1")]),
        ("SPI_SCK", SIG, [("U1.15", "U4.6"), ("U4.6", "J3.2")]),
        ("SPI_SI", SIG, [("U1.17", "U4.5"), ("U4.5", "J3.3")]),
        ("SPI_SO", SIG, [("U1.14", "U4.2"), ("U4.2", "J3.4")]),
        ("CRESET", SIG, [("U1.8", "R1.2"), ("R1.2", "J3.5")]),
        ("CDONE", SIG, [("U1.7", "R2.2")]),
        ("OSC_OUT", SIG, [("X1.3", "R5.1")]),
        ("CLK12", SIG, [("R5.2", "U1.37")]),
        ("XSMT", SIG, [("U1.31", "R6.1"), ("R6.1", "U2.17")]),
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

    # The tap from the spine's east via leaves on the back: the via sits in
    # the pocket between the QFN's own pad rows, and on the front the rows
    # are the wall - the first regeneration proved there is no lane. The
    # goal stays on the front because C15's pad is front copper.
    tracks.append(Track("+1V2", "B.Cu", SIG, [SPINE_1V2[1], "C15.1"], auto=True, goal_layer="F.Cu"))

    # A capacitor appears here only if nothing legal stood beside its ground
    # pad: normally its via is placed against the pad above, which is the loop
    # the part exists to close. The rest are the grounds a stub genuinely has
    # to carry - a connector pin, an oscillator can, the codec's own pads.
    for pad, target in (
        ("C4.2", (22.5, 36.0)),
        ("C5.2", (56.0, 40.8)),
        ("C17.2", (59.0, 40.8)),
        ("C6.2", (59.0, 43.5)),
        ("C7.2", (60.5, 46.5)),
        ("C8.2", (46.0, 68.5)),
        ("C16.2", (89.0, 52.0)),
        ("C12.2", (60.0, 53.0)),
        ("C14.2", (91.5, 47.0)),
        ("J1.2", (12.0, 12.0)),
        ("D3.2", (27.0, 10.0)),
        ("R6.2", (58.5, 33.5)),
        ("U3.2", (6.0, 24.0)),
        ("X1.2", (30.0, 10.0)),
        ("U4.4", (32.0, 66.0)),
        # The codec's grounds - two real ones and three mode pins strapped low -
        # drop through beside their own escapes rather than walking west into a
        # corridor that four other nets are already using.
        ("U2.19", (62.5, 43.5)),
        ("U2.11", (62.5, 35.5)),
        ("U2.16", (62.5, 40.5)),
        ("U2.10", (86.0, 33.5)),
        ("U2.9", (86.0, 37.5)),
        ("U2.3", (86.0, 42.5)),
        ("J2.2", (95.5, 45.5)),
        ("J3.6", (78.0, 70.0)),
    ):
        if pad.partition(".")[0] in placed:
            continue
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


def _mating_room(part: Part) -> float:
    """What a part needs above its courtyard for the thing that plugs into it.

    A screw terminal's wires leave horizontally from its face, a barrel jack
    swallows a plug the size of the jack again, and a screw driven beside
    either can only be driven before the cable goes on - which on a board that
    gets serviced is never. A pin header is the other case: the socket that
    fits it lands inside the same outline the header already draws, so it asks
    for nothing beyond the screw head's own room, and treating the two alike
    is what left a carrier lined with headers with two mounting holes on one
    edge.
    """
    if part.ref[:1].upper() not in ("J", "P"):
        return 0.0
    library = part.footprint.lower()
    if any(name in library for name in ("pinheader", "pinsocket", "conn_01x", "conn_02x")):
        return 0.0
    return CONNECTOR_ACCESS


def _corner_spots(
    design: Design,
    count: int,
    keep: float,
    extra: Sequence[tuple[float, float]] = (),
    extra_keep: float = 0.0,
) -> list[tuple[float, float]]:
    """Up to ``count`` free positions round the edge, nearest corner first.

    Shared by the screw holes and the fiducials, which want the same thing: a
    place at the edge of the board where nothing else is, and where what the
    part *brings with it* - a washer, a camera's clear view - also fits.

    The corners are where both belong, so each corner is what the search asks
    for; what it settles for is the nearest free point on a ring round the
    board. A corner with a screw terminal in it has no room at the corner
    itself and plenty fifteen millimetres along the edge, and a board whose
    whole perimeter is taken has room one ring further in. Walking the ring
    rather than sliding along two edges is what keeps a crowded board from
    coming back with one screw hole in it: the answer is usually there, a
    little further round than the old search was willing to look.
    """
    width, height = design.board_size
    inset = keep + 0.6
    # A connector is judged by what plugs into it. The shell, the wires leaving
    # a screw terminal and the fingers that fit both live above the courtyard,
    # so a screw tucked against one can only be driven before the cable goes
    # on - and on a board that gets serviced, that is never.
    taken = [(*_part_extent(design, part), _mating_room(part)) for part in design.footprints()]
    for track in design.tracks:
        points = [resolve(design, point) for point in track.points]
        half = track.width / 2
        for a, b in pairwise(points):
            taken.append(
                (
                    min(a[0], b[0]) - half,
                    min(a[1], b[1]) - half,
                    max(a[0], b[0]) + half,
                    max(a[1], b[1]) + half,
                    0.0,
                )
            )

    def free(cx: float, cy: float) -> bool:
        return all(
            not (
                box[0] - keep - box[4] <= cx <= box[2] + keep + box[4]
                and box[1] - keep - box[4] <= cy <= box[3] + keep + box[4]
            )
            for box in taken
        )

    grid = design.board_grid or 0.5

    def snap(value: float) -> float:
        return round(round(value / grid) * grid, 3)

    # The rings: the edge first, then two steps inboard. A hole belongs at the
    # edge, so an inner ring only ever wins when the edge has nothing left.
    rings: list[list[tuple[float, float]]] = []
    for depth in (0.0, 3.0, 6.0):
        left, top = inset + depth, inset + depth
        right, bottom = width - inset - depth, height - inset - depth
        if right - left < 2 * keep or bottom - top < 2 * keep:
            break
        ring: list[tuple[float, float]] = []
        steps = max(2, int((right - left) / 0.5))
        for index in range(steps + 1):
            x = snap(left + (right - left) * index / steps)
            ring.append((x, snap(top)))
            ring.append((x, snap(bottom)))
        steps = max(2, int((bottom - top) / 0.5))
        for index in range(steps + 1):
            y = snap(top + (bottom - top) * index / steps)
            ring.append((snap(left), y))
            ring.append((snap(right), y))
        rings.append(ring)

    corners = [
        (inset, inset),
        (width - inset, inset),
        (inset, height - inset),
        (width - inset, height - inset),
    ][:count]
    found: list[tuple[float, float]] = []

    def usable(point: tuple[float, float]) -> bool:
        return (
            free(*point)
            and all(math.dist(point, other) > keep + extra_keep for other in extra)
            and all(math.dist(point, other) > 2 * keep for other in found)
        )

    for corner in corners:
        # This corner's own quarter of the board, and nowhere else. A board
        # bolts down flat when the screws are spread, and a search that asked
        # only for the nearest free point put three of the four along one edge
        # - each of them the nearest thing to a different corner, all of them
        # in the same place. A quarter with no room gives no screw: three that
        # hold the board flat beat four that hold one side of it.
        quarters = [
            [
                point
                for point in ring
                if (point[0] <= width / 2) == (corner[0] <= width / 2)
                and (point[1] <= height / 2) == (corner[1] <= height / 2)
            ]
            for ring in rings
        ]
        spot = None
        for ring in quarters:
            spot = next(
                (
                    point
                    for point in sorted(ring, key=lambda p: math.dist(p, corner))
                    if usable(point)
                ),
                None,
            )
            if spot is not None:
                break
        if spot is not None:
            found.append(spot)
    return found


def place_fiducials(design: Design) -> Design:
    """Three targets for the assembly machine to find the board with.

    A pick-and-place aligns to the *board*, not to the drawing: it looks for
    two or three copper dots in a bare mask window and works out where
    everything else is from them. Without them the machine has only the board
    outline, which is cut to a tolerance ten times looser than the placement
    it is being asked for, and a 0.5 mm pitch part lands where the router
    happened to leave it.

    Three, near three different corners, so the machine gets rotation as well
    as offset - and near the corners rather than in the middle because that is
    where the arithmetic is most sensitive and where a board has room.
    """
    if not design.fiducials or not design.pour:
        return design
    holes = [part.board[:2] for part in design.footprints() if part.ref.startswith("H")]
    spots = _corner_spots(
        design,
        design.fiducials,
        keep=2.0,
        extra=holes,
        # The screw's own half, so the washer never lands on the target the
        # placement machine is about to photograph.
        extra_keep=FASTENER_HEAD / 2 + FASTENER_GAP,
    )
    if not spots:
        return design
    parts = list(design.parts)
    sheet_x, sheet_y = _free_sheet_row(design, len(spots))
    for index, (x, y) in enumerate(spots, start=1):
        parts.append(
            Part(
                f"FID{index}",
                "Mechanical:Fiducial",
                "Fiducial",
                "Fiducial:Fiducial_1mm_Mask2mm",
                sheet=(sheet_x + (index - 1) * 12.7, sheet_y),
                board=(x, y, 0.0),
                show_value=False,
                show_reference=False,
                fields={"MPN": "n/a", "Manufacturer": "n/a"},
            )
        )
    keepouts = tuple(design.keepouts) + tuple(
        (x - 2.0, y - 2.0, x + 2.0, y + 2.0) for x, y in spots
    )
    return replace(design, parts=parts, keepouts=keepouts)


def _free_sheet_row(design: Design, count: int) -> tuple[float, float]:
    """Somewhere on the sheet with room for a row of ``count`` symbols.

    The screw holes are not part of the circuit and belong wherever the
    drawing has space, but "wherever" has to be found rather than typed: a
    row parked on top of the power section is `readability.text_over_text`,
    and the same coordinate is free on one board and taken on the next.
    """
    _stubs, boxes = _sheet_obstacles(design)
    # A symbol with no pins draws no stub and so leaves no box behind it: the
    # screw holes were invisible to this, and the fiducials were placed on top
    # of them.
    boxes = list(boxes) + [
        (part.sheet[0] - 5.08, part.sheet[1] - 7.62, part.sheet[0] + 5.08, part.sheet[1] + 7.62)
        for part in design.parts
        if not symbol_pins(part.lib_id, part.unit)
    ]
    width = (count - 1) * 12.7 + 10.16
    sheet_w, sheet_h = {"A4": (297.0, 210.0), "A3": (420.0, 297.0)}.get(
        design.paper, (297.0, 210.0)
    )
    # Well clear of the bottom of the page: the frame strip and the title
    # block live there, and a symbol prints its value below itself - the first
    # row tried was inside the frame and every fiducial reported
    # `readability.margin_intrusion`.
    for row in range(8):
        y = round((sheet_h - 60.0 - row * 15.24) / GRID) * GRID
        for column in range(24):
            x = round((25.4 + column * 12.7) / GRID) * GRID
            if x + width > sheet_w - 15.0:
                break  # the row has run off the page
            box = (x - 5.08, y - 10.16, x + width, y + 11.43)
            if all(
                not (box[0] < b[2] and b[0] < box[2] and box[1] < b[3] and b[1] < box[3])
                for b in boxes
            ):
                return (x, y)
    return (25.4, round((sheet_h - 25.4) / GRID) * GRID)


def mount_holes(design: Design) -> Design:
    """Put the board's screw holes where the layout has left room for them.

    A board with no way to bolt it down is a board somebody will bolt down
    anyway, through whatever hole they can find. Four is the usual answer and
    the corners are the usual place; `_corner_spots` finds which of them the
    parts have left free.

    The holes are parts, so they reach the schematic too and the board keeps
    its parity; they are placed before routing, so the search treats them as
    the obstacles they are.
    """
    spec = design.mounting
    if spec is None or not design.pour:
        return design
    holes = _corner_spots(design, spec.count, spec.keep)
    if not holes:
        return design
    library = "MountingHole_Pad" if spec.grounded else "MountingHole"
    footprint = f"MountingHole:MountingHole_{spec.drill}mm_M3" + ("_Pad" if spec.grounded else "")
    parts = list(design.parts)
    nets = {name: list(nodes) for name, nodes in design.nets.items()}
    sheet_x, sheet_y = design.mounting_sheet or _free_sheet_row(design, len(holes))
    for index, (x, y) in enumerate(holes, start=1):
        ref = f"H{index}"
        parts.append(
            Part(
                ref,
                f"Mechanical:{library}",
                "M3",
                footprint,
                sheet=(sheet_x + (index - 1) * 12.7, sheet_y),
                board=(x, y, 0.0),
                show_value=False,
                fields={"MPN": "n/a", "Manufacturer": "n/a"},
            )
        )
        if spec.grounded:
            nets.setdefault(POUR_NET, []).append(f"{ref}.1")
    keepouts = tuple(design.keepouts) + tuple(
        (x - spec.keep, y - spec.keep, x + spec.keep, y + spec.keep) for x, y in holes
    )
    return replace(design, parts=parts, nets=nets, keepouts=keepouts)


def _kicad_fill(board: Path) -> None:
    """Hand the written board to KiCad's own zone filler.

    `pcbnew` ships in both container images and its `ZONE_FILLER` runs without
    a display, which an earlier note in this file said it could not. Filling
    here rather than computing the polygons ourselves is what makes the
    committed fill and the fill DRC checks the same object: `pcb drc
    --refill-zones` replaces whatever the file holds, so a fill of our own
    devising was checked by nobody, and it drifted - copper 0.075 mm from a pad
    on a board reported clean.

    The board is saved by the same KiCad that generated the rest of it, so the
    file format does not move; running the generator under the older matrix
    version keeps it readable by both, as the fixture requires.
    """
    import importlib
    import sys as _sys

    if "/usr/lib/python3/dist-packages" not in _sys.path:
        _sys.path.append("/usr/lib/python3/dist-packages")
    try:
        pcbnew = importlib.import_module("pcbnew")
    except ImportError:  # pragma: no cover - only outside the container
        print(
            f"{board.stem}: pcbnew is not importable, the zones stay unfilled "
            "(run the generator in the container)",
            file=sys.stderr,
        )
    else:
        loaded = pcbnew.LoadBoard(str(board))
        if not pcbnew.ZONE_FILLER(loaded).Fill(loaded.Zones()):
            raise SystemExit(f"{board.stem}: KiCad's zone filler refused the board")
        loaded.Save(str(board))
    _canonicalize_board_uuids(board)


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
    board = root / f"{design.name}.kicad_pcb"
    emit_board(design, board)
    _kicad_fill(board)


def main(argv: list[str] | None = None) -> int:
    global ROUTE_CACHE

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", help="directory to write the examples into")
    parser.add_argument("--only", choices=sorted(DESIGNS), help="generate just this design")
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
    cache_mode = parser.add_mutually_exclusive_group()
    cache_mode.add_argument(
        "--no-route-cache",
        action="store_true",
        help="route from scratch, ignoring what an earlier run found",
    )
    cache_mode.add_argument(
        "--require-route-cache",
        action="store_true",
        help="fail on a route-cache miss instead of silently routing again",
    )
    parser.add_argument(
        "--route-cache-dir",
        type=Path,
        default=ROUTE_CACHE,
        help="directory for route results and learned orders (default: %(default)s)",
    )
    args = parser.parse_args(argv)
    ROUTE_CACHE = args.route_cache_dir

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
            # The screw holes go in before the router runs, not after: they are
            # obstacles, and a hole placed into a board already full of copper
            # has nowhere left to go.
            place_fiducials(
                mount_holes(replace(builder().snapped(), provenance=stamp, date=args.generated_on))
            ),
            use_cache=not args.no_route_cache,
            require_cache=args.require_route_cache,
        )
        write_variant(design, out / name / "reviewed")
        # the degraded variant is *meant* to be wrong, so it is not checked
        write_variant(degrade(design), out / name / "as-generated", check=False)
        print(f"{name}: wrote as-generated/ and reviewed/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

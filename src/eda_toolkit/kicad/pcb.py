"""Read ``.kicad_pcb`` files: stackup, footprints, pads, tracks, vias, zones."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..util import EdaError
from . import outline as outline_geom
from . import s_expression as sexp
from .s_expression import SNode


@dataclass
class Pad:
    number: str
    type: str  # smd | thru_hole | np_thru_hole | connect
    shape: str
    x: float
    y: float
    angle: float
    size: tuple[float, float]
    drill: float | None
    layers: list[str]
    net: str = ""
    net_code: int = 0
    roundrect_rratio: float = 0.0

    @property
    def annular_ring(self) -> float | None:
        if self.drill is None:
            return None
        return (min(self.size) - self.drill) / 2.0

    @property
    def corner_radius(self) -> float:
        """How far the copper is cut back at each corner of the pad's extent.

        A bounding box is a poor stand-in for a pad when the question is
        clearance: the corner of a roundrect 0603 land is a quarter of a
        millimetre away from where its box says it is, which is the whole
        clearance rule. Rectangles return nought, so measuring against the box
        minus this radius, plus the radius back as a disc, is exact for every
        shape these boards use.
        """
        if self.shape in ("circle", "oval"):
            return min(self.size) / 2.0
        if self.shape == "roundrect":
            return min(self.size) * self.roundrect_rratio
        return 0.0

    def bbox(self, angle_offset: float = 0.0, margin: float = 0.0) -> tuple[float, ...]:
        """Axis-aligned extent of the pad, exact for a rotated rectangle.

        ``angle_offset`` is the parent footprint's orientation: the pad angle in
        the file is relative to the footprint, so the two add up.
        """
        rad = math.radians(self.angle + angle_offset)
        cos_a, sin_a = abs(math.cos(rad)), abs(math.sin(rad))
        w, h = self.size
        ex = (w * cos_a + h * sin_a) / 2 + margin
        ey = (w * sin_a + h * cos_a) / 2 + margin
        return (self.x - ex, self.y - ey, self.x + ex, self.y + ey)


@dataclass
class Footprint:
    ref: str
    value: str
    lib_id: str
    x: float
    y: float
    angle: float
    layer: str
    pads: list[Pad] = field(default_factory=list)
    attributes: list[str] = field(default_factory=list)
    uuid: str = ""
    # The courtyard outline in board coordinates, as points. It is what the
    # part actually occupies - a screw terminal's body reaches well past its
    # pads - so anything asking "how much room does this take" wants this and
    # not the pad extent.
    courtyard: list[tuple[float, float]] = field(default_factory=list)

    @property
    def side(self) -> str:
        return "bottom" if self.layer.startswith("B.") else "top"

    def courtyard_box(self) -> tuple[float, float, float, float] | None:
        """The courtyard's bounding box, or the pads' when it has none."""
        points = self.courtyard
        if not points:
            boxes = [pad.bbox(angle_offset=self.angle) for pad in self.pads]
            if not boxes:
                return None
            return (
                min(b[0] for b in boxes),
                min(b[1] for b in boxes),
                max(b[2] for b in boxes),
                max(b[3] for b in boxes),
            )
        return (
            min(p[0] for p in points),
            min(p[1] for p in points),
            max(p[0] for p in points),
            max(p[1] for p in points),
        )

    @property
    def is_smd(self) -> bool:
        if "smd" in self.attributes:
            return True
        return bool(self.pads) and all(p.type == "smd" for p in self.pads)

    @property
    def dnp(self) -> bool:
        return "dnp" in self.attributes or "exclude_from_bom" in self.attributes

    def nets(self) -> set[str]:
        return {p.net for p in self.pads if p.net}

    def to_dict(self) -> dict[str, Any]:
        return {
            "ref": self.ref,
            "value": self.value,
            "lib_id": self.lib_id,
            "position": [round(self.x, 3), round(self.y, 3)],
            "angle": self.angle,
            "layer": self.layer,
            "side": self.side,
            "pads": len(self.pads),
            "attributes": self.attributes,
            "nets": sorted(self.nets()),
        }


@dataclass
class Track:
    start: tuple[float, float]
    end: tuple[float, float]
    width: float
    layer: str
    net_code: int
    net: str = ""
    kind: str = "segment"  # segment | arc

    @property
    def length(self) -> float:
        return math.dist(self.start, self.end)


@dataclass
class Via:
    x: float
    y: float
    size: float
    drill: float
    layers: list[str]
    net_code: int
    net: str = ""
    type: str = "through"

    @property
    def annular_ring(self) -> float:
        return (self.size - self.drill) / 2.0


@dataclass
class Zone:
    net: str
    layers: list[str]
    filled: bool  # has computed fill polygons in the file
    priority: int = 0
    keepout: bool = False
    fill_enabled: bool = True
    # The drawn outline, and the computed fill per layer. The difference between
    # the two is where the pour was asked for and is not: the clearance cuts.
    outline: list[tuple[float, float]] = field(default_factory=list)
    fills: list[tuple[str, list[tuple[float, float]]]] = field(default_factory=list)


@dataclass
class Board:
    path: Path
    version: int
    generator: str
    layers: list[dict[str, str]] = field(default_factory=list)
    setup: dict[str, Any] = field(default_factory=dict)
    nets: dict[int, str] = field(default_factory=dict)
    footprints: list[Footprint] = field(default_factory=list)
    tracks: list[Track] = field(default_factory=list)
    vias: list[Via] = field(default_factory=list)
    zones: list[Zone] = field(default_factory=list)
    edges: list[dict[str, Any]] = field(default_factory=list)
    silk_texts: list[dict[str, Any]] = field(default_factory=list)
    stackup: list[dict[str, Any]] = field(default_factory=list)
    _segments: list[tuple[tuple[float, float], tuple[float, float]]] | None = field(
        default=None, repr=False, compare=False
    )
    _closed: bool | None = field(default=None, repr=False, compare=False)

    # -- derived -----------------------------------------------------------
    @property
    def copper_layers(self) -> list[str]:
        return [
            layer["name"]
            for layer in self.layers
            if layer.get("type") == "signal" or layer["name"].endswith(".Cu")
        ]

    def edge_segments(self) -> list[tuple[tuple[float, float], tuple[float, float]]]:
        """Edge.Cuts flattened to straight segments (arcs and circles included)."""
        if self._segments is None:
            self._segments = outline_geom.flatten(self.edges)
        return self._segments

    def outline_closed(self) -> bool:
        if self._closed is None:
            self._closed = outline_geom.is_closed(self.edge_segments())
        return self._closed

    def edge_clearance_at(self, x: float, y: float) -> float:
        """Signed distance from a point to the outline: negative means outside."""
        return outline_geom.clearance((x, y), self.edge_segments(), closed=self.outline_closed())

    def outline_bbox(self) -> tuple[float, float, float, float] | None:
        return outline_geom.bbox(self.edge_segments())

    def size_mm(self) -> tuple[float, float] | None:
        bbox = self.outline_bbox()
        if not bbox:
            return None
        return (round(bbox[2] - bbox[0], 3), round(bbox[3] - bbox[1], 3))

    def footprint_by_ref(self, ref: str) -> Footprint | None:
        for fp in self.footprints:
            if fp.ref == ref:
                return fp
        return None

    def pads_on_net(self, net: str) -> list[tuple[Footprint, Pad]]:
        return [(fp, pad) for fp in self.footprints for pad in fp.pads if pad.net == net]


def _xy(node: SNode | None) -> tuple[float, float, float]:
    if node is None:
        return (0.0, 0.0, 0.0)
    atoms = [a for a in node.atoms() if isinstance(a, (int, float))]
    while len(atoms) < 3:
        atoms.append(0.0)
    return (float(atoms[0]), float(atoms[1]), float(atoms[2]))


def _rotate(x: float, y: float, angle_deg: float) -> tuple[float, float]:
    """KiCad's RotatePoint: positive angles turn counter-clockwise on screen."""
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    return (x * cos_a + y * sin_a, -x * sin_a + y * cos_a)


def is_silk_layer(layer: str) -> bool:
    return "SilkS" in layer or "Silkscreen" in layer


def _layer_list(node: SNode | None) -> list[str]:
    if node is None:
        return []
    return [str(a) for a in node.atoms()]


def _is_hidden(node: SNode) -> bool:
    """``hide`` is a bare atom up to KiCad 7 and ``(hide yes)`` from KiCad 8."""
    if node.flag("hide"):
        return True
    return any(str(a) == "hide" for a in node.atoms())


def _text_effects(node: SNode) -> tuple[float, float, float]:
    """``(height, width, thickness)`` in mm; KiCad writes ``(size height width)``."""
    effects = node.child("effects")
    font = effects.child("font") if effects else None
    size = font.child("size") if font else None
    atoms = [a for a in (size.atoms() if size else []) if isinstance(a, (int, float))]
    height = float(atoms[0]) if atoms else 0.0
    width = float(atoms[1]) if len(atoms) > 1 else height
    thickness = float(font.value("thickness", default=0.0) or 0.0) if font else 0.0
    return (height, width, thickness)


def _silk_text(node: SNode, text: str, fp: Footprint | None) -> dict[str, Any]:
    """One silkscreen text entry, positioned in board coordinates."""
    tx, ty, angle = _xy(node.child("at"))
    if fp is not None:
        # Footprint text is stored in footprint coordinates and turns with the
        # part, exactly like a pad does.
        rx, ry = _rotate(tx, ty, fp.angle)
        tx, ty = fp.x + rx, fp.y + ry
    height, width, thickness = _text_effects(node)
    return {
        "text": text,
        "layer": str(node.value("layer", default="")),
        "x": tx,
        "y": ty,
        "angle": angle,
        "height": height,
        "width": width,
        "thickness": thickness,
        "hidden": _is_hidden(node),
        "footprint": fp.ref if fp is not None else "",
    }


def _net_of(node: SNode, nets: dict[int, str]) -> tuple[int, str]:
    """The ``(net ...)`` of a board item, in either of KiCad's two spellings.

    Usually ``(net <code>)`` with the name looked up in the table, but the
    name-only form ``(net "VCC")`` also exists in the wild - the
    pic_programmer demo is written that way - and reading the name as a code
    used to crash the parse of the whole board.
    """
    atoms = node.child("net").atoms() if node.child("net") else []
    if atoms and isinstance(atoms[0], (int, float)):
        code = int(atoms[0])
        return code, str(atoms[1]) if len(atoms) > 1 else nets.get(code, "")
    if atoms:
        name = str(atoms[0])
        return next((c for c, n in nets.items() if n == name), 0), name
    return 0, ""


def parse(path: str | os.PathLike[str]) -> Board:
    p = Path(path)
    if not p.exists():
        raise EdaError(f"no such board: {p}")
    root = sexp.load(p)
    if root.name != "kicad_pcb":
        raise EdaError(f"{p} is not a kicad_pcb document (root: {root.name})")

    board = Board(
        path=p,
        version=int(root.value("version", default=0) or 0),
        generator=str(root.value("generator", default="")),
    )

    general = root.child("general")
    if general:
        board.setup["thickness"] = general.value("thickness")

    layers_node = root.child("layers")
    if layers_node:
        for layer in layers_node.children():
            atoms = layer.atoms()
            board.layers.append(
                {
                    "id": layer.name,
                    "name": str(atoms[0]) if atoms else layer.name,
                    "type": str(atoms[1]) if len(atoms) > 1 else "",
                    # KiCad keeps a canonical name plus an optional display name
                    # ("F.SilkS" / "F.Silkscreen"); kicad-cli accepts either.
                    "user_name": str(atoms[2]) if len(atoms) > 2 else "",
                }
            )

    setup = root.child("setup")
    if setup:
        for key in (
            "pad_to_mask_clearance",
            "solder_mask_min_width",
            "allow_soldermask_bridges_in_footprints",
        ):
            val = setup.value(key)
            if val is not None:
                board.setup[key] = val
        stackup = setup.child("stackup")
        if stackup:
            for layer in stackup.children("layer"):
                board.stackup.append(
                    {
                        "name": str(layer.atom(0, "")),
                        "type": str(layer.value("type", default="")),
                        "thickness": layer.value("thickness"),
                        "material": layer.value("material"),
                        "epsilon_r": layer.value("epsilon_r"),
                    }
                )
            board.setup["copper_finish"] = stackup.value("copper_finish")

    for net in root.children("net"):
        atoms = net.atoms()
        if len(atoms) >= 2:
            board.nets[int(atoms[0])] = str(atoms[1])
        elif atoms:
            board.nets[int(atoms[0])] = ""

    for fp_node in root.children("footprint"):
        x, y, angle = _xy(fp_node.child("at"))
        props = {}
        for prop in fp_node.children("property"):
            atoms = prop.atoms()
            if len(atoms) >= 2:
                props[str(atoms[0])] = str(atoms[1])
        attrs_node = fp_node.child("attr")
        fp = Footprint(
            ref=props.get("Reference", ""),
            value=props.get("Value", ""),
            lib_id=str(fp_node.atom(0, "")),
            x=x,
            y=y,
            angle=angle,
            layer=str(fp_node.value("layer", default="F.Cu")),
            attributes=[str(a) for a in attrs_node.atoms()] if attrs_node else [],
            uuid=str(fp_node.value("uuid", default="")),
        )
        for shape in ("fp_line", "fp_rect", "fp_poly"):
            for node in fp_node.children(shape):
                if not str(node.value("layer", default="")).endswith(".CrtYd"):
                    continue
                raw: list[tuple[float, float]] = []
                for key in ("start", "end", "center"):
                    child = node.child(key)
                    if child:
                        cx, cy, _ = _xy(child)
                        raw.append((cx, cy))
                pts_node = node.child("pts")
                if pts_node:
                    for xy in pts_node.children("xy"):
                        atoms = [a for a in xy.atoms() if isinstance(a, (int, float))]
                        if len(atoms) >= 2:
                            raw.append((float(atoms[0]), float(atoms[1])))
                for cx, cy in raw:
                    gx, gy = _rotate(cx, cy, angle)
                    fp.courtyard.append((gx + fp.x, gy + fp.y))

        for pad_node in fp_node.children("pad"):
            atoms = pad_node.atoms()
            px, py, pangle = _xy(pad_node.child("at"))
            size_node = pad_node.child("size")
            size_atoms = size_node.atoms() if size_node else [0, 0]
            drill_node = pad_node.child("drill")
            drill = None
            if drill_node:
                drill_atoms = [a for a in drill_node.atoms() if isinstance(a, (int, float))]
                if drill_atoms:
                    drill = float(drill_atoms[0])
            net_code, net_name = _net_of(pad_node, board.nets)
            # Pad coordinates are relative to the footprint origin and rotated by
            # the footprint orientation. KiCad's RotatePoint works on a Y-down
            # canvas, hence the sign pattern below.
            gx, gy = _rotate(px, py, fp.angle)
            gx += fp.x
            gy += fp.y
            fp.pads.append(
                Pad(
                    number=str(atoms[0]) if atoms else "",
                    type=str(atoms[1]) if len(atoms) > 1 else "",
                    shape=str(atoms[2]) if len(atoms) > 2 else "",
                    x=gx,
                    y=gy,
                    angle=pangle,
                    size=(
                        float(size_atoms[0]),
                        float(size_atoms[1]) if len(size_atoms) > 1 else float(size_atoms[0]),
                    ),
                    drill=drill,
                    layers=_layer_list(pad_node.child("layers")),
                    net=net_name,
                    net_code=net_code,
                    roundrect_rratio=float(
                        pad_node.value("roundrect_rratio", default=0.0) or 0.0
                    ),
                )
            )
        board.footprints.append(fp)

        # Reference/value text: KiCad <= 7 used fp_text, KiCad >= 8 uses property.
        for text_node in fp_node.walk("fp_text"):
            if is_silk_layer(str(text_node.value("layer", default=""))):
                board.silk_texts.append(_silk_text(text_node, str(text_node.atom(1, "")), fp))
        for prop_node in fp_node.children("property"):
            if is_silk_layer(str(prop_node.value("layer", default=""))):
                board.silk_texts.append(_silk_text(prop_node, str(prop_node.atom(1, "")), fp))

    for seg in root.children("segment"):
        sx, sy, _ = _xy(seg.child("start"))
        ex, ey, _ = _xy(seg.child("end"))
        code, _ = _net_of(seg, board.nets)
        board.tracks.append(
            Track(
                (sx, sy),
                (ex, ey),
                float(seg.value("width", default=0) or 0),
                str(seg.value("layer", default="")),
                code,
                board.nets.get(code, ""),
            )
        )
    for arc in root.children("arc"):
        sx, sy, _ = _xy(arc.child("start"))
        ex, ey, _ = _xy(arc.child("end"))
        code, _ = _net_of(arc, board.nets)
        board.tracks.append(
            Track(
                (sx, sy),
                (ex, ey),
                float(arc.value("width", default=0) or 0),
                str(arc.value("layer", default="")),
                code,
                board.nets.get(code, ""),
                kind="arc",
            )
        )

    for via in root.children("via"):
        vx, vy, _ = _xy(via.child("at"))
        code, _ = _net_of(via, board.nets)
        via_type = "through"
        for candidate in ("blind", "micro"):
            if via.child(candidate) is not None or candidate in [str(a) for a in via.atoms()]:
                via_type = candidate
        board.vias.append(
            Via(
                vx,
                vy,
                float(via.value("size", default=0) or 0),
                float(via.value("drill", default=0) or 0),
                _layer_list(via.child("layers")),
                code,
                board.nets.get(code, ""),
                via_type,
            )
        )

    # A footprint may carry zones of its own - a module's pad keep-out, most
    # often - and KiCad stores those in *board* coordinates, not footprint
    # ones. They are board items in every way that matters here, so they are
    # read alongside the top-level ones; a placer that forgets to move them
    # leaves the keep-out sitting at the origin, off the board, which is
    # exactly what `layout.zone_outside_outline` is for.
    for zone in [
        *root.children("zone"),
        *(z for fp in root.children("footprint") for z in fp.children("zone")),
    ]:
        code, _ = _net_of(zone, board.nets)
        fill = zone.child("fill")
        fill_atoms = fill.atoms() if fill else []

        def _points(node) -> list[tuple[float, float]]:
            pts = node.child("pts")
            out = []
            for xy in pts.children("xy") if pts else []:
                atoms = [a for a in xy.atoms() if isinstance(a, (int, float))]
                if len(atoms) >= 2:
                    out.append((float(atoms[0]), float(atoms[1])))
            return out

        outline: list[tuple[float, float]] = []
        for poly in zone.children("polygon"):
            outline = _points(poly)
            break
        fills = []
        for filled_poly in zone.walk("filled_polygon"):
            layer = str(filled_poly.value("layer", default=""))
            points = _points(filled_poly)
            if layer and points:
                fills.append((layer, points))
        board.zones.append(
            Zone(
                net=str(zone.value("net_name", default=board.nets.get(code, ""))),
                layers=_layer_list(zone.child("layers")) or _layer_list(zone.child("layer")),
                filled=bool(fills),
                priority=int(zone.value("priority", default=0) or 0),
                keepout=zone.child("keepout") is not None,
                fill_enabled=bool(fill_atoms and fill_atoms[0] is True),
                outline=outline,
                fills=fills,
            )
        )

    for tag in ("gr_line", "gr_arc", "gr_rect", "gr_circle", "gr_poly", "gr_curve"):
        for node in root.children(tag):
            if str(node.value("layer", default="")) != "Edge.Cuts":
                continue
            edge: dict[str, Any] = {"type": tag}
            # Keep the shape's own vocabulary (centre, mid, ...) rather than a
            # flat point list: an arc is not its three points, and a circle is
            # not its centre and rim point.
            for key, name in (
                ("start", "start"),
                ("end", "end"),
                ("center", "centre"),
                ("mid", "mid"),
            ):
                child = node.child(key)
                if child is not None:
                    x, y, _ = _xy(child)
                    edge[name] = (x, y)
            polyline: list[tuple[float, float]] = []
            pts = node.child("pts")
            if pts is not None:
                for xy in pts.children("xy"):
                    atoms = xy.atoms()
                    polyline.append((float(atoms[0]), float(atoms[1])))
            if polyline:
                edge["polyline"] = polyline
            if tag == "gr_circle" and "centre" in edge and "end" in edge:
                edge["radius"] = math.dist(edge["centre"], edge["end"])
            segments = outline_geom.flatten([edge])
            edge["points"] = [seg[0] for seg in segments] + ([segments[-1][1]] if segments else [])
            if edge["points"]:
                board.edges.append(edge)

    for text in root.children("gr_text"):
        if is_silk_layer(str(text.value("layer", default=""))):
            board.silk_texts.append(_silk_text(text, str(text.atom(0, "")), None))

    return board


def find_board(target: str | os.PathLike[str]) -> Path:
    """Accept a project dir, .kicad_pro or .kicad_pcb and return the board file."""
    p = Path(target)
    if p.is_dir():
        boards = sorted(p.glob("*.kicad_pcb"))
        if not boards:
            raise EdaError(f"no .kicad_pcb found in {p}")
        return boards[0]
    if p.suffix == ".kicad_pro":
        board = p.with_suffix(".kicad_pcb")
        if not board.exists():
            raise EdaError(f"no board next to {p}")
        return board
    if p.suffix != ".kicad_pcb":
        raise EdaError(f"expected a .kicad_pcb file, got {p}")
    if not p.exists():
        raise EdaError(f"no such board: {p}")
    return p


def summary(board: Board) -> dict[str, Any]:
    size = board.size_mm()
    track_widths = sorted({round(t.width, 4) for t in board.tracks})
    drills = sorted(
        {round(v.drill, 4) for v in board.vias}
        | {round(p.drill, 4) for fp in board.footprints for p in fp.pads if p.drill}
    )
    return {
        "board": str(board.path),
        "version": board.version,
        "generator": board.generator,
        "size_mm": list(size) if size else None,
        "copper_layers": board.copper_layers,
        "layer_count": len(board.copper_layers),
        "stackup": board.stackup,
        "footprints": len(board.footprints),
        "smd_footprints": len([f for f in board.footprints if "smd" in f.attributes]),
        "through_hole_footprints": len(
            [f for f in board.footprints if "through_hole" in f.attributes]
        ),
        "top_side": len([f for f in board.footprints if f.side == "top"]),
        "bottom_side": len([f for f in board.footprints if f.side == "bottom"]),
        "pads": sum(len(f.pads) for f in board.footprints),
        "nets": len([n for n in board.nets.values() if n]),
        "tracks": len(board.tracks),
        "track_length_mm": round(sum(t.length for t in board.tracks), 2),
        "vias": len(board.vias),
        "zones": len(board.zones),
        "track_widths_mm": track_widths,
        "drill_sizes_mm": drills,
        "silk_texts": len(board.silk_texts),
    }

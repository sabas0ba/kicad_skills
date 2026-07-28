"""Read ``.kicad_pcb`` files: stackup, footprints, pads, tracks, vias, zones."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..util import EdaError
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

    @property
    def annular_ring(self) -> float | None:
        if self.drill is None:
            return None
        return (min(self.size) - self.drill) / 2.0


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

    @property
    def side(self) -> str:
        return "bottom" if self.layer.startswith("B.") else "top"

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

    # -- derived -----------------------------------------------------------
    @property
    def copper_layers(self) -> list[str]:
        return [
            layer["name"]
            for layer in self.layers
            if layer.get("type") == "signal" or layer["name"].endswith(".Cu")
        ]

    def outline_bbox(self) -> tuple[float, float, float, float] | None:
        xs: list[float] = []
        ys: list[float] = []
        for edge in self.edges:
            for x, y in edge["points"]:
                xs.append(x)
                ys.append(y)
        if not xs:
            return None
        return (min(xs), min(ys), max(xs), max(ys))

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
            net_node = pad_node.child("net")
            net_code = int(net_node.atom(0, 0)) if net_node else 0
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
                    net=str(net_node.atom(1, "")) if net_node else "",
                    net_code=net_code,
                )
            )
        board.footprints.append(fp)

        # Reference/value text: KiCad <= 7 used fp_text, KiCad >= 8 uses property.
        for text_node in fp_node.walk("fp_text"):
            layer = str(text_node.value("layer", default=""))
            if is_silk_layer(layer):
                tx, ty, _ = _xy(text_node.child("at"))
                board.silk_texts.append(
                    {
                        "text": str(text_node.atom(1, "")),
                        "layer": layer,
                        "x": fp.x + tx,
                        "y": fp.y + ty,
                        "footprint": fp.ref,
                    }
                )
        for prop_node in fp_node.children("property"):
            layer = str(prop_node.value("layer", default=""))
            if is_silk_layer(layer):
                tx, ty, _ = _xy(prop_node.child("at"))
                board.silk_texts.append(
                    {
                        "text": str(prop_node.atom(1, "")),
                        "layer": layer,
                        "x": fp.x + tx,
                        "y": fp.y + ty,
                        "footprint": fp.ref,
                    }
                )

    for seg in root.children("segment"):
        sx, sy, _ = _xy(seg.child("start"))
        ex, ey, _ = _xy(seg.child("end"))
        code = int(seg.value("net", default=0) or 0)
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
        code = int(arc.value("net", default=0) or 0)
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
        code = int(via.value("net", default=0) or 0)
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

    for zone in root.children("zone"):
        code = int(zone.value("net", default=0) or 0)
        fill = zone.child("fill")
        fill_atoms = fill.atoms() if fill else []
        board.zones.append(
            Zone(
                net=str(zone.value("net_name", default=board.nets.get(code, ""))),
                layers=_layer_list(zone.child("layers")) or _layer_list(zone.child("layer")),
                filled=any(True for _ in zone.walk("filled_polygon")),
                priority=int(zone.value("priority", default=0) or 0),
                keepout=zone.child("keepout") is not None,
                fill_enabled=bool(fill_atoms and fill_atoms[0] is True),
            )
        )

    for tag in ("gr_line", "gr_arc", "gr_rect", "gr_circle", "gr_poly", "gr_curve"):
        for node in root.children(tag):
            if str(node.value("layer", default="")) != "Edge.Cuts":
                continue
            points: list[tuple[float, float]] = []
            for key in ("start", "end", "center", "mid"):
                child = node.child(key)
                if child is not None:
                    x, y, _ = _xy(child)
                    points.append((x, y))
            pts = node.child("pts")
            if pts is not None:
                for xy in pts.children("xy"):
                    atoms = xy.atoms()
                    points.append((float(atoms[0]), float(atoms[1])))
            if tag == "gr_circle" and len(points) >= 2:
                cx, cy = points[0]
                r = math.dist(points[0], points[1])
                points = [(cx - r, cy - r), (cx + r, cy + r)]
            if points:
                board.edges.append({"type": tag, "points": points})

    for text in root.children("gr_text"):
        layer = str(text.value("layer", default=""))
        if is_silk_layer(layer):
            tx, ty, _ = _xy(text.child("at"))
            board.silk_texts.append(
                {"text": str(text.atom(0, "")), "layer": layer, "x": tx, "y": ty, "footprint": ""}
            )

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

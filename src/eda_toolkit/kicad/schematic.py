"""Read ``.kicad_sch`` files: symbols, properties, wires, labels and pin geometry."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from ..util import EdaError
from . import s_expression as sexp
from .s_expression import SNode

TOL = 0.01  # mm - KiCad schematic grid is 1.27 mm, so this is generous


@dataclass(frozen=True)
class Pin:
    number: str
    name: str
    electrical_type: str
    x: float
    y: float
    unit: int

    @property
    def xy(self) -> tuple[float, float]:
        return (self.x, self.y)


@dataclass
class Symbol:
    uuid: str
    lib_id: str
    reference: str
    value: str
    footprint: str = ""
    datasheet: str = ""
    unit: int = 1
    x: float = 0.0
    y: float = 0.0
    angle: float = 0.0
    mirror: str = ""
    dnp: bool = False
    in_bom: bool = True
    on_board: bool = True
    exclude_from_sim: bool = False
    properties: dict[str, str] = field(default_factory=dict)
    pins: list[Pin] = field(default_factory=list)
    sheet: str = ""

    @property
    def is_power(self) -> bool:
        return self.lib_id.lower().startswith("power:")

    @property
    def is_power_flag(self) -> bool:
        """PWR_FLAG marks a net as driven; unlike other power symbols it does
        not give the net its name."""
        return self.lib_id.upper().endswith(":PWR_FLAG") or self.value.upper() == "PWR_FLAG"

    @property
    def library(self) -> str:
        return self.lib_id.split(":", 1)[0]

    @property
    def unannotated(self) -> bool:
        return self.reference.endswith("?") or not self.reference

    def to_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "value": self.value,
            "lib_id": self.lib_id,
            "footprint": self.footprint,
            "datasheet": self.datasheet,
            "unit": self.unit,
            "position": [round(self.x, 3), round(self.y, 3)],
            "angle": self.angle,
            "mirror": self.mirror,
            "dnp": self.dnp,
            "in_bom": self.in_bom,
            "on_board": self.on_board,
            "sheet": self.sheet,
            "uuid": self.uuid,
            "pin_count": len(self.pins),
            "properties": self.properties,
        }


@dataclass
class Label:
    text: str
    kind: str  # local | global | hierarchical | netclass
    x: float
    y: float
    sheet: str = ""


@dataclass
class Wire:
    points: list[tuple[float, float]]
    kind: str = "wire"  # wire | bus
    sheet: str = ""


@dataclass
class Sheet:
    name: str
    filename: str
    uuid: str
    parent: str = ""


@dataclass
class SchematicDoc:
    path: Path
    version: int
    generator: str
    symbols: list[Symbol] = field(default_factory=list)
    labels: list[Label] = field(default_factory=list)
    wires: list[Wire] = field(default_factory=list)
    junctions: list[tuple[float, float]] = field(default_factory=list)
    no_connects: list[tuple[float, float]] = field(default_factory=list)
    sheets: list[Sheet] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    title_block: dict[str, str] = field(default_factory=dict)

    def symbol_by_ref(self, ref: str) -> Symbol | None:
        for s in self.symbols:
            if s.reference == ref:
                return s
        return None


def transform_pin(px: float, py: float, sym_x: float, sym_y: float, angle: float,
                  mirror: str) -> tuple[float, float]:
    """Library coordinates -> sheet coordinates.

    KiCad stores library symbols with Y pointing up and sheets with Y pointing
    down, hence the final sign flip. ``mirror`` is the symbol's ``(mirror x|y)``
    flag; it is applied after rotation, which matches KiCad for the 0/90/180/270
    orientations used in practice.
    """
    rad = math.radians(angle)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    rx = px * cos_a - py * sin_a
    ry = px * sin_a + py * cos_a
    if mirror == "x":
        ry = -ry
    elif mirror == "y":
        rx = -rx
    return (sym_x + rx, sym_y - ry)


def _prop_map(node: SNode) -> dict[str, str]:
    props: dict[str, str] = {}
    for prop in node.children("property"):
        atoms = prop.atoms()
        if len(atoms) >= 2:
            props[str(atoms[0])] = str(atoms[1])
    return props


def _lib_symbol_pins(lib_node: SNode) -> dict[int, list[Pin]]:
    """Collect pins per unit from a ``lib_symbols`` entry."""
    per_unit: dict[int, list[Pin]] = {}
    for sub in lib_node.children("symbol"):
        sub_name = str(sub.atom(0, ""))
        m = re.search(r"_(\d+)_(\d+)$", sub_name)
        unit = int(m.group(1)) if m else 0
        for pin in sub.children("pin"):
            atoms = pin.atoms()
            etype = str(atoms[0]) if atoms else "unspecified"
            at = pin.child("at")
            if at is None:
                continue
            coords = at.atoms()
            number_node = pin.child("number")
            name_node = pin.child("name")
            per_unit.setdefault(unit, []).append(
                Pin(
                    number=str(number_node.atom(0, "")) if number_node else "",
                    name=str(name_node.atom(0, "")) if name_node else "",
                    electrical_type=etype,
                    x=float(coords[0]),
                    y=float(coords[1]),
                    unit=unit,
                )
            )
    return per_unit


def _points(node: SNode) -> list[tuple[float, float]]:
    pts = node.child("pts")
    if pts is None:
        return []
    out = []
    for xy in pts.children("xy"):
        atoms = xy.atoms()
        if len(atoms) >= 2:
            out.append((float(atoms[0]), float(atoms[1])))
    return out


def parse(path: str | Path) -> SchematicDoc:
    """Parse a single ``.kicad_sch`` file."""
    p = Path(path)
    if not p.exists():
        raise EdaError(f"no such schematic: {p}")
    root = sexp.load(p)
    if root.name != "kicad_sch":
        raise EdaError(f"{p} is not a kicad_sch document (root: {root.name})")

    doc = SchematicDoc(
        path=p,
        version=int(root.value("version", default=0) or 0),
        generator=str(root.value("generator", default="")),
    )

    tb = root.child("title_block")
    if tb:
        for key in ("title", "date", "rev", "company"):
            val = tb.value(key)
            if val is not None:
                doc.title_block[key] = str(val)
        for comment in tb.children("comment"):
            atoms = comment.atoms()
            if len(atoms) >= 2:
                doc.title_block[f"comment{atoms[0]}"] = str(atoms[1])

    lib_pins: dict[str, dict[int, list[Pin]]] = {}
    lib_symbols = root.child("lib_symbols")
    if lib_symbols:
        for lib in lib_symbols.children("symbol"):
            lib_pins[str(lib.atom(0, ""))] = _lib_symbol_pins(lib)

    for node in root.children("symbol"):
        lib_id = str(node.value("lib_id", default=""))
        at = node.child("at")
        coords = at.atoms() if at else [0, 0, 0]
        props = _prop_map(node)
        mirror_node = node.child("mirror")
        unit = int(node.value("unit", default=1) or 1)
        sym = Symbol(
            uuid=str(node.value("uuid", default="")),
            lib_id=lib_id,
            reference=props.get("Reference", ""),
            value=props.get("Value", ""),
            footprint=props.get("Footprint", ""),
            datasheet=props.get("Datasheet", ""),
            unit=unit,
            x=float(coords[0]) if len(coords) > 0 else 0.0,
            y=float(coords[1]) if len(coords) > 1 else 0.0,
            angle=float(coords[2]) if len(coords) > 2 else 0.0,
            mirror=str(mirror_node.atom(0, "")) if mirror_node else "",
            dnp=node.flag("dnp"),
            in_bom=node.value("in_bom", default=True) is not False,
            on_board=node.value("on_board", default=True) is not False,
            exclude_from_sim=node.flag("exclude_from_sim"),
            properties=props,
            sheet=p.name,
        )
        units = lib_pins.get(lib_id, {})
        for source_unit in (0, unit):
            for pin in units.get(source_unit, []):
                ax, ay = transform_pin(pin.x, pin.y, sym.x, sym.y, sym.angle, sym.mirror)
                sym.pins.append(
                    Pin(pin.number, pin.name, pin.electrical_type, ax, ay, unit)
                )
        doc.symbols.append(sym)

    kinds = {"label": "local", "global_label": "global", "hierarchical_label": "hierarchical",
             "netclass_flag": "netclass"}
    for tag, kind in kinds.items():
        for node in root.children(tag):
            at = node.child("at")
            coords = at.atoms() if at else [0, 0]
            doc.labels.append(
                Label(text=str(node.atom(0, "")), kind=kind,
                      x=float(coords[0]), y=float(coords[1]), sheet=p.name)
            )

    for tag, kind in (("wire", "wire"), ("bus", "bus")):
        for node in root.children(tag):
            pts = _points(node)
            if len(pts) >= 2:
                doc.wires.append(Wire(points=pts, kind=kind, sheet=p.name))

    for node in root.children("junction"):
        at = node.child("at")
        if at:
            atoms = at.atoms()
            doc.junctions.append((float(atoms[0]), float(atoms[1])))

    for node in root.children("no_connect"):
        at = node.child("at")
        if at:
            atoms = at.atoms()
            doc.no_connects.append((float(atoms[0]), float(atoms[1])))

    for node in root.children("sheet"):
        props = _prop_map(node)
        doc.sheets.append(
            Sheet(
                name=props.get("Sheetname", props.get("Sheet name", "")),
                filename=props.get("Sheetfile", props.get("Sheet file", "")),
                uuid=str(node.value("uuid", default="")),
                parent=p.name,
            )
        )

    for node in root.children("text"):
        value = node.atom(0, "")
        if value:
            doc.texts.append(str(value))

    return doc


def find_root_schematic(target: str | Path) -> Path:
    """Accept a project dir, a .kicad_pro or a .kicad_sch and return the root sheet."""
    p = Path(target)
    if p.is_dir():
        pro = sorted(p.glob("*.kicad_pro"))
        if pro:
            sch = pro[0].with_suffix(".kicad_sch")
            if sch.exists():
                return sch
        sch_files = sorted(p.glob("*.kicad_sch"))
        if not sch_files:
            raise EdaError(f"no .kicad_sch found in {p}")
        return sch_files[0]
    if p.suffix == ".kicad_pro":
        sch = p.with_suffix(".kicad_sch")
        if not sch.exists():
            raise EdaError(f"no schematic next to {p}")
        return sch
    if p.suffix != ".kicad_sch":
        raise EdaError(f"expected a .kicad_sch file, got {p}")
    if not p.exists():
        raise EdaError(f"no such schematic: {p}")
    return p


def parse_project(target: str | Path) -> list[SchematicDoc]:
    """Parse the root sheet plus every hierarchical sub-sheet it references."""
    root_path = find_root_schematic(target)
    seen: set[Path] = set()
    queue = [root_path]
    docs: list[SchematicDoc] = []
    while queue:
        path = queue.pop(0).resolve()
        if path in seen or not path.exists():
            continue
        seen.add(path)
        doc = parse(path)
        docs.append(doc)
        for sheet in doc.sheets:
            if sheet.filename:
                queue.append(path.parent / sheet.filename)
    return docs


# -- connectivity fallback -------------------------------------------------


class _UnionFind:
    def __init__(self) -> None:
        self.parent: dict[Any, Any] = {}

    def find(self, item: Any) -> Any:
        self.parent.setdefault(item, item)
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != root:
            self.parent[item], item = root, self.parent[item]
        return root

    def union(self, a: Any, b: Any) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _key(pt: tuple[float, float]) -> tuple[int, int]:
    return (round(pt[0] / TOL), round(pt[1] / TOL))


def _on_segment(pt: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> bool:
    (px, py), (ax, ay), (bx, by) = pt, a, b
    if min(ax, bx) - TOL > px or px > max(ax, bx) + TOL:
        return False
    if min(ay, by) - TOL > py or py > max(ay, by) + TOL:
        return False
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    length = math.hypot(bx - ax, by - ay) or 1.0
    return abs(cross) / length <= TOL


LABEL_PRIORITY = {"power": 0, "global": 1, "hierarchical": 2, "local": 3}


def build_netlist(docs: Iterable[SchematicDoc]) -> dict[str, Any]:
    """Best-effort connectivity from geometry.

    ``kicad-cli sch export netlist`` is authoritative and is preferred whenever
    the container is available; this fallback exists so that the pure-python
    tooling (and the test-suite) still works without KiCad installed.  Sheet-to-
    sheet connections are resolved through power symbols and global labels only.
    """
    docs = list(docs)
    uf = _UnionFind()
    named: dict[Any, tuple[int, str]] = {}
    pin_nodes: list[tuple[Any, str, str, str, str]] = []  # key, ref, pin number, pin name, etype

    for doc in docs:
        endpoints: list[tuple[tuple[float, float], Any]] = []
        segments: list[tuple[tuple[float, float], tuple[float, float]]] = []

        for wire in doc.wires:
            if wire.kind != "wire":
                continue
            for a, b in zip(wire.points, wire.points[1:]):
                uf.union((doc.path.name, _key(a)), (doc.path.name, _key(b)))
                segments.append((a, b))
                endpoints.append((a, (doc.path.name, _key(a))))
                endpoints.append((b, (doc.path.name, _key(b))))

        loose: list[tuple[tuple[float, float], Any]] = []

        for sym in doc.symbols:
            for pin in sym.pins:
                key = (doc.path.name, _key(pin.xy))
                loose.append((pin.xy, key))
                if sym.is_power_flag:
                    continue  # drives the net, but never names it
                if sym.is_power:
                    net_name = sym.value or sym.reference
                    prio = LABEL_PRIORITY["power"]
                    current = named.get(uf.find(key))
                    if current is None or prio < current[0]:
                        named[key] = (prio, net_name)
                else:
                    pin_nodes.append((key, sym.reference, pin.number, pin.name, pin.electrical_type))

        for label in doc.labels:
            if label.kind == "netclass":
                continue
            key = (doc.path.name, _key((label.x, label.y)))
            loose.append(((label.x, label.y), key))
            prio = LABEL_PRIORITY.get(label.kind, 9)
            existing = named.get(key)
            if existing is None or prio < existing[0]:
                named[key] = (prio, label.text)

        for junction in doc.junctions:
            loose.append((junction, (doc.path.name, _key(junction))))

        for point, key in loose:
            for a, b in segments:
                if _on_segment(point, a, b):
                    uf.union(key, (doc.path.name, _key(a)))

    # merge cross-sheet nets that share a global/power name
    by_name: dict[str, Any] = {}
    for key, (prio, name) in named.items():
        if prio <= LABEL_PRIORITY["hierarchical"]:
            if name in by_name:
                uf.union(by_name[name], key)
            else:
                by_name[name] = key

    groups: dict[Any, dict[str, Any]] = {}
    for key, (prio, name) in named.items():
        root = uf.find(key)
        entry = groups.setdefault(root, {"name": None, "priority": 99, "nodes": []})
        if prio < entry["priority"]:
            entry["priority"] = prio
            entry["name"] = name

    for key, ref, number, pin_name, etype in pin_nodes:
        root = uf.find(key)
        entry = groups.setdefault(root, {"name": None, "priority": 99, "nodes": []})
        entry["nodes"].append({"ref": ref, "pin": number, "pin_name": pin_name, "type": etype})

    nets = []
    auto = 0
    for entry in groups.values():
        if not entry["nodes"]:
            continue
        name = entry["name"]
        if not name:
            first = sorted(entry["nodes"], key=lambda n: (n["ref"], n["pin"]))[0]
            auto += 1
            name = f"Net-({first['ref']}-Pad{first['pin']})"
        nets.append({"name": name, "nodes": sorted(entry["nodes"], key=lambda n: (n["ref"], n["pin"]))})

    # nets with the same resolved name are the same net
    merged: dict[str, list[dict[str, str]]] = {}
    for net in nets:
        merged.setdefault(net["name"], []).extend(net["nodes"])
    return {
        "source": "geometry-fallback",
        "nets": [
            {"name": name, "nodes": nodes, "pin_count": len(nodes)}
            for name, nodes in sorted(merged.items())
        ],
    }

"""Netlist access: KiCad XML netlists plus a geometry fallback."""

from __future__ import annotations

import os
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from ..util import EdaError
from . import kicad_cli, schematic


def parse_kicadxml(path: str | os.PathLike[str]) -> dict[str, Any]:
    """Parse the ``kicadxml`` netlist exported by ``kicad-cli sch export netlist``."""
    tree = ET.parse(str(path))
    root = tree.getroot()
    if root.tag != "export":
        raise EdaError(f"{path} is not a KiCad XML netlist (root: {root.tag})")

    components: list[dict[str, Any]] = []
    for comp in root.findall("./components/comp"):
        libsource = comp.find("libsource")
        sheetpath = comp.find("sheetpath")
        props = {
            p.get("name", ""): p.get("value", "")
            for p in comp.findall("property")
        }
        components.append(
            {
                "reference": comp.get("ref", ""),
                "value": (comp.findtext("value") or "").strip(),
                "footprint": (comp.findtext("footprint") or "").strip(),
                "datasheet": (comp.findtext("datasheet") or "").strip(),
                "description": (comp.findtext("description") or "").strip(),
                "lib_id": (
                    f"{libsource.get('lib', '')}:{libsource.get('part', '')}" if libsource is not None else ""
                ),
                "sheet": sheetpath.get("names", "/") if sheetpath is not None else "/",
                "dnp": props.get("dnp", "").lower() in ("1", "yes", "true") or "dnp" in props,
                "properties": props,
            }
        )

    nets: list[dict[str, Any]] = []
    for net in root.findall("./nets/net"):
        nodes = [
            {
                "ref": n.get("ref", ""),
                "pin": n.get("pin", ""),
                "pin_name": n.get("pinfunction", ""),
                "type": n.get("pintype", ""),
            }
            for n in net.findall("node")
        ]
        nets.append(
            {
                "name": net.get("name", "") or f"net-{net.get('code', '?')}",
                "code": net.get("code", ""),
                "nodes": nodes,
                "pin_count": len(nodes),
            }
        )

    return {"source": "kicad-cli", "components": components, "nets": nets}


def get(target: str | os.PathLike[str], *, prefer_cli: bool = True) -> dict[str, Any]:
    """Return the netlist for a schematic/project, using kicad-cli when possible."""
    sch = schematic.find_root_schematic(target)
    if prefer_cli and kicad_cli.available():
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "netlist.xml"
            kicad_cli.export_netlist(sch, out, fmt="kicadxml")
            data = parse_kicadxml(out)
            data["schematic"] = str(sch)
            return data

    docs = schematic.parse_project(sch)
    data = schematic.build_netlist(docs)
    data["schematic"] = str(sch)
    data["components"] = [
        s.to_dict() for doc in docs for s in doc.symbols if not s.is_power
    ]
    return data


def nets_of(netlist: dict[str, Any], reference: str) -> dict[str, str]:
    """Map ``pin number -> net name`` for one component."""
    out: dict[str, str] = {}
    for net in netlist.get("nets", []):
        for node in net["nodes"]:
            if node["ref"] == reference:
                out[node["pin"]] = net["name"]
    return out


POWER_NET_RE = r"^(\+?\d+(\.\d+)?V\d*|VCC|VDD|VBUS|VIN|VOUT|VBAT|AVDD|DVDD|VDDA|VDDIO|PWR|\+?V[A-Z0-9_]*)$"
GROUND_NET_RE = r"^(GND|GNDA|AGND|DGND|PGND|VSS|VSSA|EARTH|0)$"


def classify_net(name: str) -> str:
    import re

    upper = name.upper().lstrip("/")
    if re.match(GROUND_NET_RE, upper):
        return "ground"
    if re.match(POWER_NET_RE, upper):
        return "power"
    return "signal"

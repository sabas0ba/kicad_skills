"""Minimal s-expression reader for KiCad files (.kicad_sch / .kicad_pcb / .kicad_pro).

The parser is intentionally dependency free and lossless enough for review work:
lists become :class:`SNode`, atoms become ``str``/``float``/``int``/``bool``.
Quoted strings keep their value; bare atoms keep their text unless numeric.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_NUM_RE = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


class SExpressionError(ValueError):
    pass


@dataclass
class SNode:
    """A parsed s-expression list: ``(name arg arg ...)``."""

    name: str
    args: list[Any] = field(default_factory=list)

    # -- navigation helpers -------------------------------------------------
    def children(self, name: str | None = None) -> list[SNode]:
        out = [a for a in self.args if isinstance(a, SNode)]
        if name is not None:
            out = [a for a in out if a.name == name]
        return out

    def child(self, name: str) -> SNode | None:
        for a in self.args:
            if isinstance(a, SNode) and a.name == name:
                return a
        return None

    def value(self, name: str, index: int = 0, default: Any = None) -> Any:
        node = self.child(name)
        if node is None:
            return default
        values = [a for a in node.args if not isinstance(a, SNode)]
        if index < len(values):
            return values[index]
        return default

    def atoms(self) -> list[Any]:
        return [a for a in self.args if not isinstance(a, SNode)]

    def atom(self, index: int = 0, default: Any = None) -> Any:
        atoms = self.atoms()
        return atoms[index] if index < len(atoms) else default

    def walk(self, name: str | None = None) -> Iterator[SNode]:
        """Depth-first traversal over this node and all descendants."""
        if name is None or self.name == name:
            yield self
        for a in self.args:
            if isinstance(a, SNode):
                yield from a.walk(name)

    def flag(self, name: str) -> bool:
        """True when ``(name yes)`` / bare ``name`` is present."""
        node = self.child(name)
        if node is None:
            return False
        atoms = node.atoms()
        return not atoms or atoms[0] in (True, "yes", "true")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"SNode({self.name!r}, {len(self.args)} args)"


def _atom(token: str) -> Any:
    if _NUM_RE.match(token):
        if re.fullmatch(r"[+-]?\d+", token):
            return int(token)
        return float(token)
    if token == "yes":
        return True
    if token == "no":
        return False
    return token


def loads(text: str) -> SNode:
    """Parse a full KiCad s-expression document."""
    pos, length = 0, len(text)
    stack: list[SNode] = []
    root: SNode | None = None

    while pos < length:
        ch = text[pos]
        if ch.isspace():
            pos += 1
            continue
        if ch == "(":
            pos += 1
            while pos < length and text[pos].isspace():
                pos += 1
            start = pos
            while pos < length and not text[pos].isspace() and text[pos] not in "()\"":
                pos += 1
            node = SNode(text[start:pos])
            if stack:
                stack[-1].args.append(node)
            elif root is None:
                root = node
            else:
                raise SExpressionError("multiple root expressions")
            stack.append(node)
            continue
        if ch == ")":
            if not stack:
                raise SExpressionError(f"unbalanced ')' at offset {pos}")
            stack.pop()
            pos += 1
            continue
        if ch == '"':
            pos += 1
            buf: list[str] = []
            while pos < length:
                c = text[pos]
                if c == "\\" and pos + 1 < length:
                    nxt = text[pos + 1]
                    buf.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
                    pos += 2
                    continue
                if c == '"':
                    pos += 1
                    break
                buf.append(c)
                pos += 1
            else:
                raise SExpressionError("unterminated string")
            if not stack:
                raise SExpressionError("string outside of a list")
            stack[-1].args.append("".join(buf))
            continue
        # bare atom
        start = pos
        while pos < length and not text[pos].isspace() and text[pos] not in "()":
            pos += 1
        token = text[start:pos]
        if not stack:
            raise SExpressionError(f"atom {token!r} outside of a list")
        stack[-1].args.append(_atom(token))

    if stack:
        raise SExpressionError(f"unbalanced '(' - {len(stack)} list(s) still open")
    if root is None:
        raise SExpressionError("empty document")
    return root


def load(path: str | Path) -> SNode:
    return loads(Path(path).read_text(encoding="utf-8", errors="replace"))


def dumps(node: Any, indent: int = 0) -> str:
    """Serialise back to text (round-trip is semantic, not byte exact)."""
    pad = "  " * indent
    if isinstance(node, SNode):
        parts = [f"{pad}({node.name}"]
        simple = all(not isinstance(a, SNode) for a in node.args)
        if simple:
            for a in node.args:
                parts.append(" " + _dump_atom(a))
            parts.append(")")
            return "".join(parts)
        for a in node.args:
            if isinstance(a, SNode):
                parts.append("\n" + dumps(a, indent + 1))
            else:
                parts.append(" " + _dump_atom(a))
        parts.append(f"\n{pad})")
        return "".join(parts)
    return pad + _dump_atom(node)


def _dump_atom(value: Any) -> str:
    if value is True:
        return "yes"
    if value is False:
        return "no"
    if isinstance(value, (int, float)):
        return repr(value) if isinstance(value, float) else str(value)
    text = str(value)
    if text and re.fullmatch(r"[A-Za-z0-9_.+*/<>=:$-]+", text):
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'

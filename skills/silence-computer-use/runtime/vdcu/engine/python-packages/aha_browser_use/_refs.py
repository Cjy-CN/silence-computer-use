"""Native AhaBUA node refs whose locator ownership remains entirely Native."""

from __future__ import annotations

import re
from dataclasses import dataclass

from ._common import BrowserRefError, Ref, RefList


_REF_RE = re.compile(r"^n(?:[1-9]\d*|u[1-9]\d*)$")


@dataclass(frozen=True)
class ParsedRef:
    native_node_id: str

    def target(self):
        return {"nodeId": self.native_node_id}


def parse_ref(value) -> ParsedRef:
    if isinstance(value, RefList):
        if not value:
            raise BrowserRefError("find() returned no refs; take a fresh snapshot")
        value = value[0]
    elif isinstance(value, (list, tuple)):
        if len(value) != 1:
            raise BrowserRefError("expected exactly one ref")
        value = value[0]
    text = str(value or "").strip()
    match = _REF_RE.fullmatch(text)
    if match is None:
        raise BrowserRefError(
            "AhaBUA ref must be a Native node ID such as n15; take a fresh snapshot"
        )
    return ParsedRef(text)


def is_node_ref(value) -> bool:
    """Return whether ``value`` has the exact public AhaBUA node-ref shape."""
    return isinstance(value, str) and _REF_RE.fullmatch(value) is not None


def make_ref(node) -> Ref:
    node_id = str(node or "").strip()
    if _REF_RE.fullmatch(node_id) is None:
        raise BrowserRefError(
            "native node id must have the exact form nN (or nuN for an unresolved node)"
        )
    return Ref(node_id)


__all__ = ["ParsedRef", "is_node_ref", "make_ref", "parse_ref"]

"""Structured Python results for Native AhaBUA page observations."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

from ._refs import make_ref


_DERIVED_NODE_KEYS = (
    "ref",
    "node_id",
    "parent_ref",
    "depth",
    "popup",
    "children",
    "children_refs",
    "scope_index",
)


def _document_generation(data: Mapping[str, Any]) -> int:
    try:
        return int(data.get("documentGeneration") or 0)
    except (TypeError, ValueError):
        return 0


class ObservationNode(Mapping[str, Any]):
    """One semantic Snapshot node with mapping and attribute access."""

    __slots__ = (
        "_data",
        "ref",
        "node_id",
        "parent_ref",
        "depth",
        "popup",
        "children",
        "children_refs",
        "scope_index",
        "_focused_ids",
        "_current_url",
    )

    def __init__(
        self,
        data: Mapping[str, Any],
        *,
        parent_ref=None,
        depth: int = 0,
        popup: bool = False,
        focused_ids: frozenset[str] = frozenset(),
        current_url: str = "",
    ):
        self._data = data
        self.node_id = str(data.get("id") or "")
        self.ref = make_ref(self.node_id) if self.node_id else None
        self.parent_ref = parent_ref
        self.depth = int(depth)
        self.popup = bool(popup)
        raw_scope_index = data.get("scopeIndex")
        self.scope_index = (
            int(raw_scope_index) if raw_scope_index is not None else None
        )
        self._focused_ids = focused_ids
        self._current_url = str(current_url)
        self.children: tuple[ObservationNode, ...] = ()
        self.children_refs: tuple = ()

    @property
    def data(self) -> Mapping[str, Any]:
        return self._data

    @property
    def role(self) -> str:
        return str(self._data.get("role") or "")

    @property
    def name(self) -> str:
        return str(self._data.get("name") or "")

    @property
    def text(self) -> str:
        return str(self._data.get("text") or "")

    @property
    def value(self) -> str:
        return str(self._data.get("value") or "")

    @property
    def bounds(self):
        return self._data.get("bounds")

    @property
    def interaction(self):
        return self._data.get("interaction") or {}

    @property
    def form_control(self):
        return self._data.get("formControl") or {}

    @property
    def link(self):
        return self._data.get("link") or {}

    @property
    def scroller(self):
        return self._data.get("scroller") or {}

    def __getitem__(self, key: str) -> Any:
        if key in _DERIVED_NODE_KEYS:
            return getattr(self, key)
        return self._data[key]

    def __iter__(self) -> Iterator[str]:
        yield from self._data
        for key in _DERIVED_NODE_KEYS:
            if key not in self._data:
                yield key

    def __len__(self) -> int:
        return len(self._data) + sum(
            key not in self._data for key in _DERIVED_NODE_KEYS
        )

    def __getattr__(self, name: str) -> Any:
        try:
            return self._data[name]
        except KeyError:
            raise AttributeError(name) from None

    def __str__(self) -> str:
        from ._snapshot import _format_node_line

        return _format_node_line(
            self._data,
            focused_ids=self._focused_ids,
            current_url=self._current_url,
        )

    def serialize(self) -> str:
        """Serialize this node and its descendants in Snapshot line format."""
        from ._snapshot import _append_rows

        rows: list[dict[str, Any]] = []
        _append_rows(
            rows,
            self._data,
            focused_ids=self._focused_ids,
            current_url=self._current_url,
        )
        return "\n".join(row["line"] for row in rows)

    def __repr__(self) -> str:
        return str(self)


def _build_nodes(data: Mapping[str, Any], *, query: bool):
    flattened: list[ObservationNode] = []
    state = data.get("pageState") or {}
    focused_ids = frozenset(
        str(value)
        for value in (
            state.get("focusedNodeId"),
            state.get("accessibilityFocusedNodeId"),
        )
        if value
    )
    current_url = str(data.get("url") or "")

    def build(raw, *, parent_ref=None, depth=0, popup=False):
        if not isinstance(raw, Mapping):
            return None
        node = ObservationNode(
            raw,
            parent_ref=parent_ref,
            depth=depth,
            popup=popup,
            focused_ids=focused_ids,
            current_url=current_url,
        )
        flattened.append(node)
        children = tuple(
            child
            for child in (
                build(
                    item,
                    parent_ref=node.ref,
                    depth=depth + 1,
                    popup=popup,
                )
                for item in raw.get("children") or ()
            )
            if child is not None
        )
        node.children = children
        node.children_refs = tuple(
            child.ref for child in children if child.ref is not None
        )
        return node

    page_root = None
    popup_root = None
    query_roots: tuple[ObservationNode, ...] = ()
    if query:
        query_roots = tuple(
            node
            for node in (
                build(raw) for raw in data.get("queryResults") or ()
            )
            if node is not None
        )
    else:
        page_root = build(data.get("content"))
        popup = data.get("popup") or {}
        if isinstance(popup, Mapping):
            popup_root = build(popup.get("content"), popup=True)
    roots = query_roots or tuple(
        root for root in (page_root, popup_root) if root is not None
    )
    return page_root, popup_root, roots, tuple(flattened)


class SnapshotResult(str):
    """String-compatible Snapshot plus its structured result data."""

    def __new__(cls, observation: str, *, data, shown_refs=()):
        value = super().__new__(cls, str(observation))
        value.observation = str(observation)
        value.full_text = str(observation)
        value.mode = "full"
        value.changes = tuple((data.get("diff") or {}).get("changes") or ())
        value.removed_refs = tuple(
            make_ref(node_id) for node_id in (data.get("diff") or {}).get("removed") or ()
        )
        value.data = data
        value.raw = data
        value.document_generation = _document_generation(data)
        value.scope = f"d{value.document_generation}"
        (
            value.root,
            value.popup_root,
            value.roots,
            value.nodes,
        ) = _build_nodes(data, query=False)
        value.by_ref = {
            str(node.ref): node for node in value.nodes if node.ref is not None
        }
        value.refs = tuple(
            node.ref for node in value.nodes if node.ref is not None
        )
        value.shown_refs = tuple(shown_refs)
        observed_range = data.get("within") or {}
        value.scope_refs = tuple(
            make_ref(str(node_id))
            for node_id in observed_range.get("rootNodeIds") or ()
            if node_id
        )
        value.scope_truncated = bool(observed_range.get("truncated"))
        value.url = str(data.get("url") or "")
        value.title = str(data.get("title") or "")
        value.view = str(data.get("view") or "all")
        value.viewport = data.get("viewport") or {}
        value.page_state = data.get("pageState") or {}
        value.popup = data.get("popup") or {}
        value.stats = data.get("compressionStats") or {}
        value.may_be_incomplete = bool(data.get("mayBeIncomplete"))
        return value

    @property
    def text(self) -> str:
        return str(self)

    def node(self, ref):
        return self.by_ref.get(str(ref))


class FindResult(list):
    """List-compatible find refs plus Native nodes and query metadata."""

    def __init__(self, values=(), *, text: str = "", data=None):
        super().__init__(values)
        self._text = str(text)
        self.data = data or {}
        self.raw = self.data
        self.document_generation = _document_generation(self.data)
        self.scope = f"d{self.document_generation}"
        _, _, self.roots, self.nodes = _build_nodes(self.data, query=True)
        self.by_ref = {
            str(node.ref): node for node in self.nodes if node.ref is not None
        }
        self.all_refs = tuple(
            node.ref for node in self.nodes if node.ref is not None
        )
        self.metadata = self.data.get("queryMetadata") or {}
        self.matches = tuple(
            self._match(value) for value in self.metadata.get("matches") or ()
        )
        self.match_refs = tuple(
            match["ref"]
            for match in self.matches
            if isinstance(match, Mapping) and match.get("ref") is not None
        )
        self.url = str(self.data.get("url") or "")
        self.title = str(self.data.get("title") or "")
        self.viewport = self.data.get("viewport") or {}
        self.stats = self.data.get("compressionStats") or {}
        self.may_be_incomplete = bool(self.data.get("mayBeIncomplete"))

    def __str__(self) -> str:
        return self._text or super().__str__()

    @property
    def first(self):
        return self[0] if self else None

    def _match(self, value):
        if not isinstance(value, Mapping):
            return value
        result = dict(value)
        node_id = str(value.get("nodeId") or "")
        if node_id:
            result["ref"] = make_ref(node_id)
        return result

    def node(self, ref):
        return self.by_ref.get(str(ref))


__all__ = ["FindResult", "ObservationNode", "SnapshotResult"]

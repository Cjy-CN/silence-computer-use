"""Structured APC content extraction and compact Markdown presentation."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from ._refs import make_ref
from ._url_display import same_origin_relative_url


_MAX_DISPLAY_URL_CHARS = 50


def _clip(value: Any, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _yaml_string(value: Any, limit: int) -> str:
    return json.dumps(_clip(value, limit), ensure_ascii=False)


def _escape_link_url(value: str) -> str:
    return (
        value.replace("\\", "%5C")
        .replace("<", "%3C")
        .replace(">", "%3E")
        .replace(" ", "%20")
        .replace("(", "%28")
        .replace(")", "%29")
    )


def _join_piece(parts: list[str], piece: str) -> None:
    if not piece:
        return
    if parts:
        previous = parts[-1]
        if (
            piece[0] not in ",.;:!?%)]}、，。；：！？）】》"
            and previous[-1] not in "([{（【《"
        ):
            parts.append(" ")
    parts.append(piece)


def _join_separator(rendered: str, piece: str) -> str:
    if (
        rendered
        and piece
        and piece[0] not in ",.;:!?%)]}、，。；：！？）】》"
        and rendered[-1] not in "([{（【《"
    ):
        return " "
    return ""


def _text_prefix(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"

    prefix = text[: limit - 1].rstrip()
    boundary = max(
        prefix.rfind(" "),
        prefix.rfind("\t"),
        prefix.rfind("\n"),
    )
    if boundary >= max(1, len(prefix) // 2):
        prefix = prefix[:boundary].rstrip()
    prefix = prefix.rstrip("\\")
    return (prefix + "…") if prefix else "…"


def _display_href(value: Any, *, current_url: str) -> str:
    href = str(value or "")
    relative = same_origin_relative_url(href, current_url)
    display = _escape_link_url(relative if relative is not None else href)
    if len(display) <= _MAX_DISPLAY_URL_CHARS:
        return display
    return display[: _MAX_DISPLAY_URL_CHARS - 3] + "..."


def _inline_markdown(
    value: Any,
    *,
    current_url: str = "",
    table: bool = False,
    show_link_targets: bool = False,
) -> str:
    if not isinstance(value, (list, tuple)):
        return ""
    parts: list[str] = []
    for raw_segment in value:
        if not isinstance(raw_segment, Mapping):
            continue
        text = " ".join(str(raw_segment.get("text") or "").split())
        if not text:
            continue
        href = (
            _display_href(raw_segment.get("href"), current_url=current_url)
            if show_link_targets
            else ""
        )
        piece = f"{text} → {href}" if href else text
        _join_piece(parts, piece)
    result = "".join(parts)
    if table:
        result = result.replace("|", "\\|").replace("\n", "<br>")
    return result


def _inline_markdown_prefix(
    value: Any,
    *,
    max_chars: int,
    current_url: str = "",
    table: bool = False,
    show_link_targets: bool = False,
) -> tuple[str, bool]:
    full = _inline_markdown(
        value,
        current_url=current_url,
        table=table,
        show_link_targets=show_link_targets,
    )
    if len(full) <= max_chars:
        return full, False
    if max_chars <= 0 or not isinstance(value, (list, tuple)):
        return "", True

    rendered = ""
    for raw_segment in value:
        if not isinstance(raw_segment, Mapping):
            continue
        text = " ".join(str(raw_segment.get("text") or "").split())
        if not text:
            continue
        href = (
            _display_href(raw_segment.get("href"), current_url=current_url)
            if show_link_targets
            else ""
        )
        piece = f"{text} → {href}" if href else text
        if table:
            piece = piece.replace("|", "\\|").replace("\n", "<br>")
        separator = _join_separator(rendered, piece)
        if len(rendered) + len(separator) + len(piece) <= max_chars:
            rendered += separator + piece
            continue

        remaining = max_chars - len(rendered) - len(separator)
        if remaining > 0:
            # A partially emitted link target could consume most of the budget.
            # Fall back to its visible text.
            fallback = text.replace("|", "\\|") if table else text
            rendered += separator + _text_prefix(fallback, remaining)
        return rendered, True
    return rendered, True


def _table_parts(
    block: Mapping[str, Any],
    *,
    current_url: str = "",
) -> tuple[str, list[list[str]], int, int]:
    caption = " ".join(str(block.get("caption") or "").split())
    raw_rows = block.get("rows") or ()
    rows: list[tuple[bool, list[str]]] = []
    for raw_row in raw_rows:
        if not isinstance(raw_row, Mapping):
            continue
        cells = [
            _inline_markdown(cell, current_url=current_url, table=True)
            for cell in (raw_row.get("cells") or ())
        ]
        rows.append((bool(raw_row.get("header")), cells))

    title = f"**Table: {caption}**" if caption else "**Table**"
    row_parts: list[list[str]] = []
    widths = {len(cells) for _header, cells in rows if cells}
    rectangular = len(widths) == 1 and bool(widths)
    has_header = bool(rows and rows[0][0])
    if rectangular and has_header:
        width = next(iter(widths))
        row_parts.append(
            [
                "| " + " | ".join(rows[0][1]) + " |",
                "| " + " | ".join("---" for _ in range(width)) + " |",
            ]
        )
        for _header, cells in rows[1:]:
            row_parts.append(["| " + " | ".join(cells) + " |"])
    else:
        for index, (header, cells) in enumerate(rows, 1):
            role = "header" if header else f"row {index}"
            row_parts.append([f"- {role}: " + " | ".join(cells)])

    source_rows = int(block.get("sourceRows") or len(rows))
    return title, row_parts, len(rows), source_rows


def _render_table(block: Mapping[str, Any], *, current_url: str = "") -> str:
    title, row_parts, captured_rows, source_rows = _table_parts(
        block, current_url=current_url
    )
    lines = [title]
    for row_part in row_parts:
        lines.extend(row_part)
    if bool(block.get("truncated")) or captured_rows < source_rows:
        lines.append(
            f"> [Table truncated: {captured_rows} of {source_rows} rows captured.]"
        )
    return "\n".join(lines)


def _table_display_marker(
    shown_rows: int, captured_rows: int, source_rows: int
) -> str:
    marker = (
        f"> [Table display truncated: {shown_rows} of {captured_rows} "
        "captured rows shown"
    )
    if captured_rows < source_rows:
        marker += f"; {captured_rows} of {source_rows} source rows captured"
    return marker + ".]"


def _render_table_prefix(
    block: Mapping[str, Any], max_chars: int, *, current_url: str = ""
) -> str:
    full = _render_table(block, current_url=current_url)
    if len(full) <= max_chars:
        return full
    if max_chars <= 0:
        return ""

    title, row_parts, captured_rows, source_rows = _table_parts(
        block, current_url=current_url
    )
    lines = [title]
    shown_rows = 0
    for row_part in row_parts:
        marker = _table_display_marker(
            shown_rows + 1, captured_rows, source_rows
        )
        candidate = "\n".join((*lines, *row_part, marker))
        if len(candidate) > max_chars:
            break
        lines.extend(row_part)
        shown_rows += 1

    marker = _table_display_marker(shown_rows, captured_rows, source_rows)
    output = "\n".join((*lines, marker))
    if len(output) <= max_chars:
        return output

    caption = " ".join(str(block.get("caption") or "").split())
    title_budget = max_chars - len(marker) - 1
    if caption and title_budget > len("**Table: **"):
        title = (
            "**Table: "
            + _text_prefix(caption, title_budget - len("**Table: **"))
            + "**"
        )
        return "\n".join((title, marker))
    if len(marker) <= max_chars:
        return marker
    return ""


def _block_indent(block: Mapping[str, Any]) -> str:
    depth = max(1, int(block.get("depth") or 1))
    return "  " * (depth - 1)


def _indent_block(rendered: str, indent: str) -> str:
    if not rendered or not indent:
        return rendered
    return "\n".join(
        indent + line if line else line for line in rendered.splitlines()
    )


def _render_block(block: Any, *, current_url: str = "") -> str:
    if not isinstance(block, Mapping):
        return ""
    block_type = str(block.get("type") or "")
    if block_type == "table":
        return _indent_block(
            _render_table(block, current_url=current_url),
            _block_indent(block),
        )

    text = _inline_markdown(
        block.get("content"),
        current_url=current_url,
        show_link_targets=block_type == "link",
    )
    if not text:
        return ""
    if block_type == "heading":
        rendered = f"## {text}"
    elif block_type == "list":
        marker = "1." if bool(block.get("ordered")) else "-"
        rendered = f"{marker} {text}"
    else:
        rendered = text
    if bool(block.get("truncated")):
        rendered += "\n\n> [Block truncated by the Native text budget.]"
    return _indent_block(rendered, _block_indent(block))


def _render_block_prefix(
    block: Any, max_chars: int, *, current_url: str = ""
) -> str:
    if not isinstance(block, Mapping) or max_chars <= 0:
        return ""
    if str(block.get("type") or "") == "table":
        indent = _block_indent(block)
        rendered = _indent_block(
            _render_table_prefix(
                block,
                max(0, max_chars - len(indent)),
                current_url=current_url,
            ),
            indent,
        )
        return _text_prefix(rendered, max_chars)

    full = _render_block(block, current_url=current_url)
    if len(full) <= max_chars:
        return full

    block_type = str(block.get("type") or "")
    if block_type == "heading":
        prefix = "## "
    elif block_type == "list":
        marker = "1." if bool(block.get("ordered")) else "-"
        prefix = f"{marker} "
    else:
        prefix = ""
    prefix = _block_indent(block) + prefix
    if len(prefix) >= max_chars:
        return ""

    text, _ = _inline_markdown_prefix(
        block.get("content"),
        max_chars=max_chars - len(prefix),
        current_url=current_url,
        show_link_targets=block_type == "link",
    )
    return prefix + text if text else ""


def _truncation_reasons(data: Mapping[str, Any], render_truncated: bool) -> list[str]:
    budget = data.get("budget") or {}
    reasons: list[str] = []
    if isinstance(budget, Mapping) and budget.get("nodeTruncated"):
        reasons.append("native_node_budget")
    if isinstance(budget, Mapping) and budget.get("textTruncated"):
        reasons.append("native_text_budget")
    if data.get("scopeTruncated"):
        reasons.append("scope_match_budget")
    if data.get("mayBeIncomplete") and not reasons:
        reasons.append("capture_incomplete")
    if render_truncated:
        reasons.append("render_char_budget")
    return reasons


def _header(
    data: Mapping[str, Any],
    *,
    shown_blocks: int,
    total_blocks: int,
    render_truncated: bool,
) -> str:
    budget = data.get("budget") or {}
    source_nodes = int(budget.get("sourceNodes") or 0) if isinstance(budget, Mapping) else 0
    captured_nodes = (
        int(budget.get("capturedNodes") or 0) if isinstance(budget, Mapping) else 0
    )
    captured_bytes = (
        int(budget.get("capturedBytes") or 0) if isinstance(budget, Mapping) else 0
    )
    reasons = _truncation_reasons(data, render_truncated)
    types = ", ".join(str(value) for value in (data.get("types") or ()))
    return "\n".join(
        (
            "---",
            f"title: {_yaml_string(data.get('title'), 120)}",
            f"url: {_yaml_string(data.get('url'), 240)}",
            f"types: [{types}]",
            f"scopes: {len(data.get('scopeNodeIds') or ())}",
            f"truncated: {'true' if reasons else 'false'}",
            f"blocks: {shown_blocks}/{total_blocks}",
            f"apc_nodes: {source_nodes}",
            f"captured_units: {captured_nodes}",
            f"captured_bytes: {captured_bytes}",
            f"reasons: [{', '.join(reasons)}]",
            "---",
        )
    )


def format_extracted_content(data: Mapping[str, Any], *, max_chars: int) -> str:
    current_url = str(data.get("url") or "")
    block_entries = [
        (block, rendered)
        for block in (data.get("blocks") or ())
        if (rendered := _render_block(block, current_url=current_url))
    ]
    total_blocks = len(block_entries)
    full_header = _header(
        data,
        shown_blocks=total_blocks,
        total_blocks=total_blocks,
        render_truncated=False,
    )
    native_reasons = _truncation_reasons(data, False)
    native_tail = (
        "> [Content truncated here: " + ", ".join(native_reasons) + ".]"
        if native_reasons
        else ""
    )
    full_parts = [full_header, *(rendered for _block, rendered in block_entries)]
    if native_tail:
        full_parts.append(native_tail)
    full_output = "\n\n".join(full_parts)
    if len(full_output) <= max_chars:
        return full_output

    render_reasons = _truncation_reasons(data, True)
    tail = "> [Content truncated here: " + ", ".join(render_reasons) + ".]"
    # Reserving the header with all blocks shown is conservative: replacing its
    # numerator with the actual shown count can only shorten it.
    reserved_header = _header(
        data,
        shown_blocks=total_blocks,
        total_blocks=total_blocks,
        render_truncated=True,
    )
    content_budget = max_chars - len(reserved_header) - len(tail) - 4
    rendered_prefix: list[str] = []
    shown_blocks = 0
    used_chars = 0
    for block, rendered in block_entries:
        separator_chars = 2 if rendered_prefix else 0
        remaining = content_budget - used_chars - separator_chars
        if remaining <= 0:
            break
        if len(rendered) <= remaining:
            rendered_prefix.append(rendered)
            shown_blocks += 1
            used_chars += separator_chars + len(rendered)
            continue

        partial = _render_block_prefix(
            block, remaining, current_url=current_url
        )
        if partial:
            rendered_prefix.append(partial)
            shown_blocks += 1
        break

    header = _header(
        data,
        shown_blocks=shown_blocks,
        total_blocks=total_blocks,
        render_truncated=True,
    )
    output = "\n\n".join((header, *rendered_prefix, tail))
    if len(output) <= max_chars:
        return output

    compact = "\n".join(
        (
            "---",
            "truncated: true",
            "blocks: 0/" + str(total_blocks),
            "reasons: [render_char_budget]",
            "---",
        )
    )
    return compact if len(compact) <= max_chars else _text_prefix(compact, max_chars)


def _plain_text(segments: Any) -> str:
    if not isinstance(segments, (list, tuple)):
        return ""
    parts: list[str] = []
    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        text = " ".join(str(segment.get("text") or "").split())
        if text:
            _join_piece(parts, text)
    return "".join(parts)


def _links(segments: Any) -> list[dict[str, str]]:
    if not isinstance(segments, (list, tuple)):
        return []
    return [
        {
            "text": " ".join(str(segment.get("text") or "").split()),
            "href": str(segment["href"]),
        }
        for segment in segments
        if isinstance(segment, Mapping) and segment.get("href")
    ]


def _dedupe_links(links: Sequence[Mapping[str, Any]]) -> tuple[dict[str, str], ...]:
    output: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for link in links:
        item = {
            "text": str(link.get("text") or ""),
            "href": str(link.get("href") or ""),
        }
        key = (item["text"], item["href"])
        if not item["href"] or key in seen:
            continue
        seen.add(key)
        output.append(item)
    return tuple(output)


class ContentBlock:
    """One directly consumable, document-ordered content block."""

    __slots__ = (
        "type",
        "text",
        "links",
        "scope_index",
        "scope_ref",
        "truncated",
        "caption",
        "rows",
        "header_rows",
        "depth",
        "ordered",
        "_source",
        "_current_url",
    )

    def __init__(
        self,
        data: Mapping[str, Any],
        scope_refs: Sequence[Any],
        *,
        current_url: str = "",
    ):
        self.type = str(data.get("type") or "")
        raw_scope_index = data.get("scopeIndex")
        self.scope_index = (
            int(raw_scope_index) if raw_scope_index is not None else None
        )
        self.scope_ref = (
            scope_refs[self.scope_index]
            if self.scope_index is not None
            and 0 <= self.scope_index < len(scope_refs)
            else None
        )
        self.truncated = bool(data.get("truncated"))
        self.caption = " ".join(str(data.get("caption") or "").split())
        self.depth = max(0, int(data.get("depth") or 0))
        self.ordered = bool(data.get("ordered"))

        links = _links(data.get("content"))
        rows: list[tuple[str, ...]] = []
        header_rows: list[int] = []
        if self.type == "table":
            for raw_row in data.get("rows") or ():
                if not isinstance(raw_row, Mapping):
                    continue
                row = tuple(
                    _plain_text(cell) for cell in raw_row.get("cells") or ()
                )
                if raw_row.get("header"):
                    header_rows.append(len(rows))
                rows.append(row)
                for cell in raw_row.get("cells") or ():
                    links.extend(_links(cell))
            text_parts = [self.caption]
            text_parts.extend(cell for row in rows for cell in row)
            self.text = " ".join(part for part in text_parts if part)
        else:
            self.text = _plain_text(data.get("content"))
        self.links = _dedupe_links(links)
        self.rows = tuple(rows)
        self.header_rows = tuple(header_rows)
        self._source = data
        self._current_url = current_url

    def __str__(self) -> str:
        return self.markdown()

    def markdown(self) -> str:
        """Return this block in the same Markdown format as its parent result."""
        return _render_block(self._source, current_url=self._current_url)

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "type": self.type,
            "text": self.text,
            "links": [dict(link) for link in self.links],
            "scope_index": self.scope_index,
            "scope_ref": str(self.scope_ref) if self.scope_ref else None,
            "truncated": self.truncated,
            "depth": self.depth,
        }
        if self.type == "table":
            value.update(
                caption=self.caption,
                rows=[list(row) for row in self.rows],
                header_rows=list(self.header_rows),
            )
        elif self.type == "list":
            value.update(ordered=self.ordered)
        return value

    def __repr__(self) -> str:
        return (
            f"ContentBlock(type={self.type!r}, text={_clip(self.text, 80)!r}, "
            f"scope_ref={self.scope_ref!r})"
        )


class ContentExtractionResult(Sequence[ContentBlock]):
    """A flat content sequence with a bounded Markdown string view."""

    __slots__ = (
        "_wire",
        "_max_chars",
        "blocks",
        "types",
        "scope_refs",
        "scope_truncated",
        "title",
        "url",
        "truncated",
        "incomplete",
        "source_nodes",
        "captured_nodes",
        "captured_bytes",
    )

    def __init__(self, data: Mapping[str, Any], *, max_chars: int):
        self._wire = dict(data)
        self._max_chars = int(max_chars)
        self.title = str(data.get("title") or "")
        self.url = str(data.get("url") or "")
        self.scope_refs = tuple(
            make_ref(str(node_id))
            for node_id in data.get("scopeNodeIds") or ()
            if node_id
        )
        self.blocks = tuple(
            ContentBlock(block, self.scope_refs, current_url=self.url)
            for block in data.get("blocks") or ()
            if isinstance(block, Mapping)
        )
        self.types = tuple(str(value) for value in data.get("types") or ())
        self.scope_truncated = bool(data.get("scopeTruncated"))
        self.truncated = bool(data.get("truncated")) or self.scope_truncated
        self.incomplete = bool(data.get("mayBeIncomplete"))
        budget = data.get("budget") or {}
        self.source_nodes = (
            int(budget.get("sourceNodes") or 0)
            if isinstance(budget, Mapping)
            else 0
        )
        self.captured_nodes = (
            int(budget.get("capturedNodes") or 0)
            if isinstance(budget, Mapping)
            else 0
        )
        self.captured_bytes = (
            int(budget.get("capturedBytes") or 0)
            if isinstance(budget, Mapping)
            else 0
        )

    def __getitem__(self, index):
        return self.blocks[index]

    def __iter__(self) -> Iterator[ContentBlock]:
        return iter(self.blocks)

    def __len__(self) -> int:
        return len(self.blocks)

    def __str__(self) -> str:
        return self.markdown()

    def markdown(self, max_chars: int | None = None) -> str:
        budget = self._max_chars if max_chars is None else int(max_chars)
        budget = max(100, min(budget, 40000))
        return format_extracted_content(self._wire, max_chars=budget)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "types": list(self.types),
            "scope_refs": [str(ref) for ref in self.scope_refs],
            "scope_truncated": self.scope_truncated,
            "truncated": self.truncated,
            "incomplete": self.incomplete,
            "source_nodes": self.source_nodes,
            "captured_nodes": self.captured_nodes,
            "captured_bytes": self.captured_bytes,
            "blocks": [block.to_dict() for block in self.blocks],
        }

    def blocks_for(self, scope) -> tuple[ContentBlock, ...]:
        if isinstance(scope, int) and not isinstance(scope, bool):
            scope_index = scope
        else:
            scope_text = str(scope)
            scope_index = next(
                (
                    index
                    for index, ref in enumerate(self.scope_refs)
                    if str(ref) == scope_text
                ),
                -1,
            )
        return tuple(
            block for block in self.blocks if block.scope_index == scope_index
        )


__all__ = ["ContentBlock", "ContentExtractionResult", "format_extracted_content"]

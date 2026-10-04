"""Formatting for Native AhaBUA observations and semantic queries."""

from __future__ import annotations

import json
import math
import re
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from ._common import CssLocator, emit_observation
from ._observation import FindResult, SnapshotResult
from ._refs import make_ref, parse_ref
from ._transport import CLIENT
from ._url_display import same_origin_relative_url


_OBSERVATION_TIMEOUT_MS = 60000
_SNAPSHOT_VIEWS = frozenset({"all", "outline", "interactive"})
_QUERY_SEARCH_TYPES = frozenset({"text", "interaction", "all"})
_MIN_OBSERVATION_CHARS = 1000
_MAX_FIND_CHARS = 30000
_MAX_DISPLAYED_SELECT_OPTIONS = 5
_MAX_SELECT_OPTION_FIELD_CHARS = 80
_MAX_CHECKBOX_VALUE_CHARS = 25
_MAX_LINK_TARGET_CHARS = 30
_OUTLINE_EMPTY_NODE_BOUNDARIES = frozenset(
    {
        "alert_dialog",
        "complementary",
        "dialog",
        "document",
        "iframe",
        "main",
        "menu",
        "navigation",
        "root",
        "search",
        "scroll",
        "table",
        "toolbar",
        "web_area",
    }
)
_DISPLAY_ROLE_ALIASES = {
    "anchor": "a",
    "cell": "td",
    "column_header": "th",
    "container": "box",
    "generic_container": "box",
    "heading": "h",
    "image": "img",
    "link": "a",
    "list_item": "li",
    "navigation": "nav",
    "ordered_list": "ol",
    "paragraph": "p",
    "root": "doc",
    "row_header": "th",
    "static_text": "text",
    "table_cell": "td",
    "table_row": "tr",
    "table_row_group": "tbody",
    "unordered_list": "ul",
    "web_area": "doc",
}
_REDUNDANT_FORM_TYPES = frozenset(
    {
        ("button", "button_button"),
        ("button", "input_button"),
        ("checkbox", "input_checkbox"),
        ("combobox", "select_one"),
        ("listbox", "select_multiple"),
        ("radio", "input_radio"),
        ("searchbox", "input_search"),
        ("slider", "input_range"),
        ("spinbutton", "input_number"),
        ("textbox", "input_text"),
    }
)
_SEMANTIC_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def _bounded_integer(value: Any, *, name: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return max(minimum, min(value, maximum))


def _choice(value: Any, *, name: str, choices: frozenset[str]) -> str:
    selected = str(value).strip().lower()
    if selected not in choices:
        allowed = ", ".join(sorted(choices))
        raise ValueError(f"{name} must be one of: {allowed}")
    return selected


def _compact(value: Any, limit: int = 120) -> str:
    return " ".join(str(value or "").split())[:limit]


def _display_role(role: Any) -> str:
    semantic_role = _compact(role) or "node"
    return _DISPLAY_ROLE_ALIASES.get(semantic_role, semantic_role)


def _node_name(node: dict[str, Any]) -> str:
    name = _compact(node.get("name") or node.get("text") or node.get("value"))
    if name:
        return name
    children = node.get("children") or ()
    if len(children) == 1 and str(node.get("role") or "") in {
        "heading",
        "label",
        "paragraph",
    }:
        return _node_name(children[0])
    return ""


def _truncate_with_ellipsis(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


def _checkbox_display_name(
    node: dict[str, Any],
    *,
    role: str,
    form: dict[str, Any],
    fallback: str,
) -> str:
    if role != "checkbox" and form.get("type") != "input_checkbox":
        return fallback
    value = " ".join(str(form.get("value") or "").split())
    if len(value) <= _MAX_CHECKBOX_VALUE_CHARS:
        return fallback
    raw_value = (
        node.get("name") or node.get("text") or node.get("value") or ""
    )
    raw_name = " ".join(str(raw_value).split())
    if value not in raw_name:
        return fallback
    abbreviated = _truncate_with_ellipsis(value, _MAX_CHECKBOX_VALUE_CHARS)
    return _compact(raw_name.replace(value, abbreviated, 1))


def _display_url(
    value: Any,
    *,
    current_url: str = "",
    limit: int = 120,
) -> tuple[str, bool]:
    url = " ".join(str(value or "").split())
    relative_url = same_origin_relative_url(url, current_url)
    if relative_url is not None:
        url = relative_url
    if len(url) <= limit:
        return url, False
    try:
        parsed = urlsplit(url)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            compact = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
            suffix = "?…" if parsed.query else ("#…" if parsed.fragment else "")
            room = max(1, limit - len(suffix) - 1)
            if len(compact) > room:
                compact = compact[:room] + "…"
            return compact + suffix, True
    except ValueError:
        pass
    return url[: max(1, limit - 1)] + "…", True


def _display_link_target(value: Any, *, current_url: str) -> str:
    url = " ".join(str(value or "").split())
    if not url:
        return ""
    try:
        if urlsplit(url).scheme.casefold() == "javascript":
            return ""
    except ValueError:
        pass
    display_url, _ = _display_url(
        url,
        current_url=current_url,
        limit=_MAX_LINK_TARGET_CHARS,
    )
    if not display_url:
        return ""
    if re.search(r'[\s"<>]', display_url):
        return json.dumps(display_url, ensure_ascii=False)
    return display_url


def _semantic_tokens(value: str) -> tuple[str, ...]:
    return tuple(_SEMANTIC_TOKEN_RE.findall(value.casefold()))


def _is_redundant_text_child(
    parent: dict[str, Any], child: dict[str, Any]
) -> bool:
    if not parent.get("id") or not child.get("id"):
        return False
    if str(child.get("role") or "") not in {"static_text", "text"}:
        return False
    if child.get("children"):
        return False
    if child.get("formControl") or child.get("link") or child.get("scroller"):
        return False
    interaction = child.get("interaction") or {}
    if interaction.get("clickable") or interaction.get("focusable"):
        return False
    parent_name = _node_name(parent)
    child_name = _node_name(child)
    if not parent_name or not child_name:
        return False
    if child_name == parent_name:
        return True

    parent_interaction = parent.get("interaction") or {}
    if not (
        parent.get("formControl")
        or parent.get("link")
        or parent_interaction.get("clickable")
        or parent_interaction.get("focusable")
    ):
        return False

    parent_tokens = _semantic_tokens(parent_name)
    child_tokens = _semantic_tokens(child_name)
    if not child_tokens or len(child_tokens) > len(parent_tokens):
        return False
    width = len(child_tokens)
    return any(
        parent_tokens[index : index + width] == child_tokens
        for index in range(len(parent_tokens) - width + 1)
    )


def _finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _scroll_number(value: float) -> str:
    rounded = round(value)
    if abs(value - rounded) < 0.05:
        return str(int(rounded))
    return f"{value:.1f}".rstrip("0").rstrip(".")


def _scroll_axis(
    scroller: dict[str, Any],
    *,
    axis: str,
    current_key: str,
    extent_key: str,
    viewport_key: str,
) -> str:
    current = _finite_number(scroller.get(current_key))
    extent = _finite_number(scroller.get(extent_key))
    viewport = _finite_number(scroller.get(viewport_key))
    if current is None or extent is None or viewport is None:
        return ""
    maximum = max(0.0, extent - viewport)
    if maximum <= 1.0:
        return ""
    return f"{axis}={_scroll_number(current)}/{_scroll_number(maximum)}"


def _scroll_flags(scroller: dict[str, Any]) -> list[str]:
    axes = []
    if scroller.get("userScrollableVertical"):
        axes.append(
            _scroll_axis(
                scroller,
                axis="y",
                current_key="scrollY",
                extent_key="scrollHeight",
                viewport_key="viewportHeight",
            )
        )
    if scroller.get("userScrollableHorizontal"):
        axes.append(
            _scroll_axis(
                scroller,
                axis="x",
                current_key="scrollX",
                extent_key="scrollWidth",
                viewport_key="viewportWidth",
            )
        )
    return [axis for axis in axes if axis]


def _form_type_flag(role: str, form: dict[str, Any]) -> str:
    form_type = str(form.get("type") or "")
    if not form_type or (role, form_type) in _REDUNDANT_FORM_TYPES:
        return ""
    return f"type={form_type}"


def _select_option_field(value: Any) -> str:
    field = _compact(value, _MAX_SELECT_OPTION_FIELD_CHARS)
    if not field:
        return '""'
    for delimiter in ("\\", "|", "=>", "[", "]"):
        field = field.replace(delimiter, "\\" + delimiter)
    if field[0] in {"*", "!"}:
        field = "\\" + field
    return field


def _format_select_option(option: dict[str, Any]) -> str:
    text = _select_option_field(option.get("text"))
    value = _select_option_field(option.get("value"))
    payload = text if text == value else f"{text}=>{value}"
    state = ("*" if option.get("selected") else "") + (
        "!" if option.get("disabled") else ""
    )
    return state + payload


def _select_option_flags(form: dict[str, Any]) -> list[str]:
    if form.get("type") not in {"select_one", "select_multiple"}:
        return []
    options = form.get("options")
    if not isinstance(options, (list, tuple)) or not options:
        return []

    displayed: list[str] = []
    for option in options[:_MAX_DISPLAYED_SELECT_OPTIONS]:
        if not isinstance(option, dict):
            continue
        displayed.append(_format_select_option(option))

    flags = []
    if displayed:
        options_flag = "options=[" + "|".join(displayed) + "]"
        remaining = len(options) - _MAX_DISPLAYED_SELECT_OPTIONS
        if remaining > 0:
            options_flag += f"+{remaining}"
        flags.append(options_flag)
    return flags


def _node_line(
    node: dict[str, Any],
    *,
    focused_ids: frozenset[str],
    current_url: str,
) -> str:
    ref = make_ref(node.get("id"))
    semantic_role = _compact(node.get("role")) or "node"
    role = _display_role(semantic_role)
    form = node.get("formControl") or {}
    name = _checkbox_display_name(
        node,
        role=semantic_role,
        form=form,
        fallback=_node_name(node),
    )
    parts = [str(ref), role]
    if name:
        parts.append(json.dumps(name, ensure_ascii=False))
    interaction = node.get("interaction") or {}
    link = node.get("link") or {}
    scroller = node.get("scroller") or {}
    flags = []
    if node.get("visible") is False:
        flags.append("not-visible")
    if node.get("id") in focused_ids:
        flags.append("focused")
    if interaction.get("disabled"):
        flags.append("disabled")
    if interaction.get("editable"):
        flags.append("edit")
    form_type_flag = _form_type_flag(semantic_role, form)
    if form_type_flag:
        flags.append(form_type_flag)
    placeholder = _compact(form.get("placeholder"))
    if placeholder and placeholder != name:
        flags.append("ph=" + json.dumps(placeholder, ensure_ascii=False))
    if form.get("checked"):
        flags.append("checked")
    if form.get("required"):
        flags.append("required")
    flags.extend(_select_option_flags(form))
    link_target = _display_link_target(
        link.get("url"), current_url=current_url
    )
    context = _compact(node.get("context"), 320)
    if context and context != name:
        flags.append("ctx=" + json.dumps(context, ensure_ascii=False))
    flags.extend(_scroll_flags(scroller))
    if link_target:
        parts.extend(("->", link_target))
    if flags:
        parts.append("[" + " ".join(flags) + "]")
    return " ".join(parts)


def _context_node_line(node: dict[str, Any]) -> str:
    role = _display_role(node.get("role"))
    name = _compact(node.get("name") or node.get("text") or node.get("value"))
    parts = ["~", role]
    if name:
        parts.append(json.dumps(name, ensure_ascii=False))
    return " ".join(parts)


def _format_node_line(
    node: dict[str, Any],
    *,
    focused_ids: frozenset[str] = frozenset(),
    current_url: str = "",
) -> str:
    if node.get("id"):
        return _node_line(
            node,
            focused_ids=focused_ids,
            current_url=current_url,
        )
    return _context_node_line(node)


def _keep_empty_outline_node(node: dict[str, Any]) -> bool:
    role = _compact(node.get("role")) or "node"
    scroller = node.get("scroller") or {}
    return role in _OUTLINE_EMPTY_NODE_BOUNDARIES or bool(
        scroller.get("userScrollableHorizontal")
        or scroller.get("userScrollableVertical")
    )


def _is_empty_node_line(node: dict[str, Any], line: str) -> bool:
    role = _display_role(node.get("role"))
    if node.get("id"):
        return line == f"{make_ref(node.get('id'))} {role}"
    return line == f"~ {role}"


def _append_rows(
    rows: list[dict[str, Any]],
    node: dict[str, Any],
    *,
    focused_ids: frozenset[str],
    current_url: str,
    depth: int = 0,
    popup: bool = False,
    view: str = "all",
) -> None:
    if not isinstance(node, dict):
        return
    line = _format_node_line(
        node,
        focused_ids=focused_ids,
        current_url=current_url,
    )
    keep_node = (
        view != "outline"
        or not _is_empty_node_line(node, line)
        or _keep_empty_outline_node(node)
    )
    if keep_node:
        rows.append(
            {
                "node": node,
                "popup": popup,
                "line": " " * depth + line,
            }
        )
    child_depth = depth + 1 if keep_node else depth
    for child in node.get("children") or ():
        if _is_redundant_text_child(node, child):
            continue
        _append_rows(
            rows,
            child,
            focused_ids=focused_ids,
            current_url=current_url,
            depth=child_depth,
            popup=popup,
            view=view,
        )


def _header(
    result: dict[str, Any],
    *,
    query: bool,
    scope: str,
    view: str,
    shown: int,
    available: int,
    within: str,
) -> str:
    stats = result.get("compressionStats") or {}
    viewport = result.get("viewport") or {}
    parts = ["@find" if query else "@page", f"scope={scope}"]
    if not query:
        parts.append(f"view={view}")
        parts.append(f"within={within}")
        if stats:
            parts.append(f"total_nodes={stats.get('sourceNodes', '?')}")
    elif stats:
        parts.append(f"nodes={stats.get('returnedNodes', '?')}")
        parts.append(f"cap={stats.get('maxNodes', '?')}")
    parts.append(f"shown={shown}/{available}")
    metadata = (result.get("queryMetadata") or {}) if query else {}
    if metadata:
        count = f"{metadata.get('returnedCount', '?')}"
        parts.append(f"matches={count}{'+' if metadata.get('hasMore') else ''}")
        parts.append(f"search={metadata.get('searchType', '?')}")
    if result.get("mayBeIncomplete"):
        parts.append("incomplete=true")
    reductions = stats.get("removedNodesByStrategy") or {}
    if reductions.get("enforceMaxNodes"):
        parts.append(f"node_more={reductions['enforceMaxNodes']}")
    selection = ((result.get("pageState") or {}).get("selection") or {}).get("text")
    if selection:
        parts.append(
            "selection=" + json.dumps(_compact(selection), ensure_ascii=False)
        )
    parts.extend(
        [
            "title=" + json.dumps(_compact(result.get("title"), 160), ensure_ascii=False),
            "url=" + _compact(result.get("url"), 400),
            f"viewport={viewport.get('width', '?')}x{viewport.get('height', '?')}",
        ]
    )
    return " ".join(parts)


def format_snapshot(
    result: dict[str, Any],
    *,
    query: bool = False,
    view: str = "outline",
    max_chars: int,
) -> tuple[str, str, list]:
    generation = int(result.get("documentGeneration") or 0)
    scope = f"d{generation}"
    state = result.get("pageState") or {}
    focused_ids = frozenset(
        str(value)
        for value in (
            state.get("focusedNodeId"),
            state.get("accessibilityFocusedNodeId"),
        )
        if value
    )
    current_url = str(result.get("url") or "")
    effective_view = str(result.get("view") or view)
    row_view = "all" if query else effective_view
    observed_range = result.get("within") or {}
    within_node_ids = tuple(
        str(value) for value in (observed_range.get("rootNodeIds") or ()) if value
    )
    if within_node_ids:
        shown_roots = ",".join(str(make_ref(value)) for value in within_node_ids[:3])
        within = f"[{shown_roots}{',…' if len(within_node_ids) > 3 else ''}]"
    else:
        within = str(observed_range.get("kind") or "viewport")
    popup = result.get("popup") or {}
    rows: list[dict[str, Any]] = []
    if not query and isinstance(popup.get("content"), dict):
        _append_rows(
            rows,
            popup["content"],
            focused_ids=focused_ids,
            current_url=current_url,
            popup=True,
            view=row_view,
        )
    roots = (result.get("queryResults") or []) if query else [result.get("content")]
    for root in roots:
        if isinstance(root, dict):
            _append_rows(
                rows,
                root,
                focused_ids=focused_ids,
                current_url=current_url,
                view=row_view,
            )

    has_popup = bool(popup)
    header_reserve = len(
        _header(
            result,
            query=query,
            scope=scope,
            view=effective_view,
            shown=len(rows),
            available=len(rows),
            within=within,
        )
    )
    footer_reserve = len(
        f"@more rows={len(rows)} reason=chars max_chars={max_chars}"
    )
    popup_reserve = 40 if has_popup else 0
    row_budget = max(0, max_chars - header_reserve - footer_reserve - popup_reserve - 4)
    shown: list[dict[str, Any]] = []
    used = 0
    for row in rows:
        cost = len(row["line"]) + 1
        if used + cost > row_budget:
            break
        shown.append(row)
        used += cost

    omitted = len(rows) - len(shown)
    header = _header(
        result,
        query=query,
        scope=scope,
        view=effective_view,
        shown=len(shown),
        available=len(rows),
        within=within,
    )
    lines = [header]
    if any(row["popup"] for row in shown):
        opener = popup.get("openerNodeId")
        opener_ref = str(make_ref(opener)) if opener else "none"
        lines.append(f"@popup opener={opener_ref}")
    lines.extend(row["line"] for row in shown)
    if omitted:
        lines.append(f"@more rows={omitted} reason=chars max_chars={max_chars}")
    shown_ids = {
        str(row["node"]["id"])
        for row in shown
        if row["node"].get("id")
    }
    if query:
        matches = (result.get("queryMetadata") or {}).get("matches") or ()
        refs = [
            make_ref(node_id)
            for match in matches
            if isinstance(match, dict)
            and (node_id := str(match.get("nodeId") or "")) in shown_ids
        ]
    else:
        refs = [
            make_ref(row["node"]["id"])
            for row in shown
            if row["node"].get("id")
        ]
    return scope, "\n".join(lines), refs


def _removed_ref_ranges(refs: list[str]) -> list[tuple[str, int]]:
    groups: list[list[str]] = []
    for ref in sorted(refs, key=lambda value: (len(value), value)):
        if (
            groups
            and re.fullmatch(r"n(?:0|[1-9][0-9]*)", ref)
            and re.fullmatch(r"n(?:0|[1-9][0-9]*)", groups[-1][-1])
            and int(ref[1:]) == int(groups[-1][-1][1:]) + 1
        ):
            groups[-1].append(ref)
        else:
            groups.append([ref])
    return [
        (group[0] if len(group) == 1 else f"{group[0]}-{group[-1]}", len(group))
        for group in groups
    ]


def _format_native_diff(
    result: dict[str, Any], *, max_chars: int
) -> tuple[str, list, bool]:
    """Render Native changes; no previous result or Python-side comparison."""
    explanation = (
        "Diff from the previous snapshot of the same scope/view: "
        "+ added, ~ changed, > moved (current content), "
        "- left this observation (ID ranges). "
        "Indentation shows tree depth; unchanged nodes are omitted. "
        "@more marks truncated changes."
    )
    state = result.get("pageState") or {}
    delta = result["diff"]
    popup = result.get("popup") or {}
    nodes = {}
    pending = [(result.get("content"), 0), (popup.get("content"), 0)]
    while pending:
        node, depth = pending.pop()
        if isinstance(node, dict):
            nodes[node["id"]] = (node, depth)
            pending.extend((child, depth + 1) for child in node.get("children") or ())
    focused = frozenset(
        str(node_id) for node_id in (
            state.get("focusedNodeId"), state.get("accessibilityFocusedNodeId")
        ) if node_id
    )
    rows = [
        (f"- {label}", None, count)
        for label, count in _removed_ref_ranges(delta["removed"])
    ]
    for change in delta["changes"]:
        node, depth = nodes[change["nodeId"]]
        prefix = {"added": "+", "updated": "~", "moved": ">"}[change["kind"]]
        line = _format_node_line(
            node, focused_ids=focused, current_url=result.get("url") or "",
        )
        rows.append((
            f"{prefix} {' ' * depth}{line}",
            make_ref(node["id"]),
            1,
        ))
    focus_summary = "\n".join(
        "@focused " + (
            _format_node_line(nodes[ref][0], current_url=result.get("url") or "")
            if ref in nodes else ref
        )
        for ref in sorted(focused)
    ) or "@focused none"
    summary = "@popup " + (
        "opener=" + str(popup.get("openerNodeId") or "none") if popup else "none"
    )
    if not rows:
        summary += "\n@no_change nodes"
    available = len(delta["removed"]) + len(delta["changes"])
    header = _header(
        result, query=False, scope=f"d{result.get('documentGeneration') or 0}",
        view=result.get("view") or "all",
        shown=available, available=available,
        within=(result.get("within") or {}).get("kind") or "viewport",
    ).replace("@page ", "@diff ", 1)
    footer = f"@more changes={available} reason=chars max_chars={max_chars}"
    if (len(header) + len(explanation) + len(focus_summary)
            + len(summary) + len(footer) + 5 > max_chars):
        # A long input value must not turn focus metadata into a budget overrun.
        focus_summary = "@focused " + (",".join(sorted(focused)) or "none")
    summary = focus_summary + "\n" + summary
    header = _truncate_with_ellipsis(
        header, max_chars - len(explanation) - len(summary) - len(footer) - 4
    )
    used = len(header) + len(explanation) + len(summary) + len(footer) + 4
    shown = []
    for row in rows:
        if used + len(row[0]) + 1 > max_chars:
            break
        shown.append(row)
        used += len(row[0]) + 1
    shown_count = sum(count for _, _, count in shown)
    header = header.replace(f"shown={available}/{available}",
                            f"shown={shown_count}/{available}", 1)
    lines = [header, explanation, summary]
    removed = [line[2:] for line, ref, _ in shown if ref is None]
    if removed:
        lines.append("- " + ", ".join(removed))
    lines.extend(line for line, ref, _ in shown if ref is not None)
    if len(shown) < len(rows):
        lines.append(
            f"@more changes={available - shown_count} reason=chars max_chars={max_chars}"
        )
    return (
        "\n".join(lines),
        [ref for _, ref, _ in shown if ref is not None],
        len(shown) == len(rows),
    )


def snapshot(
    *,
    view: str,
    within,
    max_chars: int,
    diff: bool | Literal["auto"],
) -> SnapshotResult:
    if not isinstance(diff, bool) and diff != "auto":
        raise TypeError('diff must be "auto", True or False')
    view = _choice(view, name="view", choices=_SNAPSHOT_VIEWS)
    whole_page = within == "full"
    if whole_page:
        CLIENT.require_feature("snapshot", "documentObservation")
    if view != "all":
        CLIENT.require_feature("snapshot", "observationViews")
    if within in ("viewport", "full"):
        observation_range = {"kind": within}
        if diff:
            CLIENT.require_feature("snapshot", "snapshotDiff")
    else:
        CLIENT.require_feature("snapshot", "observationTarget")
        if isinstance(within, CssLocator):
            observation_range = {"kind": "css", **within.target()["css"]}
        else:
            observation_range = {"kind": "ref", **parse_ref(within).target()}
    page_id = CLIENT.ensure_page().id
    result, _ = CLIENT.invoke(
        "snapshot", "page.observe",
        page_id=page_id,
        params={
            "within": observation_range,
            "diff": diff is not False,
            "view": view,
            # Native retains its own fixed node safety limit. Character count is
            # the only model-facing Snapshot volume control.
            "includeScreenshot": False,
            "waitForPageStable": True,
            "timeoutMs": _OBSERVATION_TIMEOUT_MS,
        },
    )
    scope, body, refs = format_snapshot(
        result, view=view, max_chars=max_chars,
    )
    # emit_observation("dom", body, scope=scope)
    if diff is not False and isinstance(result.get("diff"), dict):
        text, diff_refs, complete = _format_native_diff(result, max_chars=max_chars)
        # Compare the actual renderings, not node counts or truncated changes.
        if diff is True or (
            complete
            and len(text) * 10 <= len(body) * 7
            and len(body.encode("utf-8")) - len(text.encode("utf-8")) >= 1000
        ):
            value = SnapshotResult(text, data=result, shown_refs=diff_refs)
            value.full_text = body
            value.mode = "diff"
            return value
    return SnapshotResult(body, data=result, shown_refs=refs)


def find(
    query: str,
    *,
    limit: int = 10,
    search_type: str = "all",
    max_chars: int = 8000,
) -> FindResult:
    limit = _bounded_integer(limit, name="limit", minimum=1, maximum=1000)
    search_type = _choice(
        search_type, name="search_type", choices=_QUERY_SEARCH_TYPES
    )
    max_chars = _bounded_integer(
        max_chars,
        name="max_chars",
        minimum=_MIN_OBSERVATION_CHARS,
        maximum=_MAX_FIND_CHARS,
    )
    result, _ = CLIENT.invoke(
        "find", "page.query",
        params={
            "query": str(query),
            "maxNodes": limit,
            "searchType": search_type,
            "waitForPageStable": True,
            "timeoutMs": _OBSERVATION_TIMEOUT_MS,
        },
    )
    scope, body, refs = format_snapshot(
        result, query=True, max_chars=max_chars
    )
    # emit_observation("dom", body, scope=scope)
    return FindResult(refs, text=body, data=result)


__all__ = ["FindResult", "SnapshotResult", "find", "format_snapshot", "snapshot"]

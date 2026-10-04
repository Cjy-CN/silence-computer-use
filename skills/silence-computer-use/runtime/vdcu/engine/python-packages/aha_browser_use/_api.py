"""CNGC-compatible public API implemented by the Native AhaBUA toolset."""

from __future__ import annotations

import base64
import csv
import io
import json
import math
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from . import _native, _seed
from ._common import (
    BrowserRefError,
    BrowserSessionError,
    BrowserUseError,
    CssLocator,
    CssLocatorList,
    ElementTarget,
    Ref,
    RefList,
    ScreenshotPath,
    StaleRefError,
    TabRecord,
    coded_error,
    default_screenshot_path,
    emit_image,
    install_executor as _install_executor,
    public_tool,
    unsupported,
)
from ._content import ContentBlock, ContentExtractionResult
from ._refs import is_node_ref, make_ref as _make_ref, parse_ref
from ._routes import PUBLIC_TOOLS, ROUTES
from ._observation import FindResult, ObservationNode, SnapshotResult
from ._snapshot import find as _find, snapshot as _snapshot
from ._transport import CLIENT


ERR_REF_STALE = "BU_REF_STALE"
ERR_REF_SHAPE = "BU_REF_SHAPE"
ERR_SESSION_STALE = "BU_SESSION_STALE"
ERR_WAIT_SECONDS = "BU_WAIT_SECONDS"
ERR_NAV_WAIT = "BU_NAV_WAIT_TIMEOUT"
ERR_SELECT_NO_OPTION = "BU_SELECT_NO_OPTION"
ERR_SELECT_AMBIGUOUS = "BU_SELECT_AMBIGUOUS"
ERR_DOWNLOAD_BLOCKED = "BU_DOWNLOAD_BLOCKED"
ERR_DOWNLOAD_TIMEOUT = "BU_DOWNLOAD_TIMEOUT"
ERR_DOWNLOAD_TARGET = "BU_DOWNLOAD_TARGET"
ERR_READ_FIELDS = "BU_READ_FIELDS"
ERR_READ_LIMIT = "BU_READ_LIMIT"
ERR_READ_TARGET = "BU_READ_TARGET"
ERR_EXPORT_LIMIT = "BU_EXPORT_LIMIT"
ERR_EXTRACT_TYPES = "BU_EXTRACT_TYPES"
ERR_SCREENSHOT_DIMENSION = "BU_SCREENSHOT_DIMENSION"
ERR_COORDINATE_SPACE = "BU_COORDINATE_SPACE"
ERR_SCROLL_DIRECTION = "BU_SCROLL_DIRECTION"
ERR_SCROLL_AMOUNT = "BU_SCROLL_AMOUNT"

_SCROLL_NO_EFFECT_DESCRIPTIONS = {
    "actor_kScrollNoScrollingElement": (
        "The viewport document has no scrolling element."
    ),
    "actor_kScrollTargetNotUserScrollable": (
        "The target has no user-scrollable overflow in the requested direction."
    ),
    "actor_kScrollOffsetDidNotChange": (
        "The scroll offset did not change; the target may already be at the "
        "requested boundary."
    ),
}

_UNSET = object()
_READ_PROPERTIES = frozenset(
    {
        "text",
        "value",
        "href",
        "src",
        "tag",
        "tagName",
        "role",
        "type",
        "name",
        "id",
        "title",
        "alt",
        "placeholder",
        "aria_label",
        "checked",
        "disabled",
        "selected",
        "readOnly",
        "visible",
    }
)
_READ_FIELD_ALIASES = {"ariaLabel": "aria_label", "readonly": "readOnly"}
_CLICK_MAX_COUNT = 5
_CLICK_INTERVAL_SECONDS = 0.03
_CSS_SELECTOR_PREFIXES = ("#", ".", "[")
_CSS_SELECTOR_OPERATORS = frozenset("*>+~:,")


def _json_payload(result):
    # Native AhaBUA operations return their typed result object directly.  The
    # ``json`` envelope exists only on compatibility operations which carry a
    # JSON string.  Preserve direct results instead of treating a missing
    # compatibility envelope as a null payload.
    if not isinstance(result, dict) or "json" not in result:
        return result
    raw = result.get("json")
    if not isinstance(raw, str):
        return raw
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return raw
    if isinstance(value, dict) and set(value).issuperset({"valueType", "value"}):
        return value.get("value")
    return value


def _bounded_number(
    value,
    *,
    name: str,
    minimum: float,
    maximum: float,
    code: str | None = None,
) -> float:
    message = f"{name} must be a number in [{minimum}, {maximum}]"
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        if code:
            raise coded_error(TypeError, code, message)
        raise TypeError(message)
    number = float(value)
    if not math.isfinite(number) or not minimum <= number <= maximum:
        if code:
            raise coded_error(ValueError, code, message)
        raise ValueError(message)
    return number


def _bounded_integer(
    value,
    *,
    name: str,
    minimum: int,
    maximum: int,
    code: str | None = None,
) -> int:
    message = f"{name} must be an integer in [{minimum}, {maximum}]"
    if isinstance(value, bool) or not isinstance(value, int):
        if code:
            raise coded_error(TypeError, code, message)
        raise TypeError(message)
    return max(minimum, min(value, maximum))


def _seconds_to_clamped_timeout_ms(seconds, *, name="timeout") -> int:
    return max(
        1,
        int(
            _bounded_number(
                seconds,
                name=name,
                minimum=0,
                maximum=180,
                code=ERR_WAIT_SECONDS,
            )
            * 1000
        ),
    )


def _png_size(data: bytes):
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    except Exception:
        pass
    return None, None


def _max_dimension(value) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise TypeError("max_dim must be a non-negative integer")
    dimension = int(value)
    if dimension < 0:
        raise ValueError("max_dim must be non-negative")
    return dimension


def _click_count(value, *, name: str = "clicks") -> int:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    return max(1, min(int(value), _CLICK_MAX_COUNT))


def _click_button(value) -> str:
    button = "left" if value is None else str(value).strip().lower()
    if not button:
        button = "left"
    if button not in {"left", "right"}:
        raise ValueError("button must be left or right")
    return button


def _is_css_selector_text(value: str) -> bool:
    return value.startswith(_CSS_SELECTOR_PREFIXES) or any(
        char.isspace() or char in _CSS_SELECTOR_OPERATORS for char in value
    )


def _css_string(value: str) -> str:
    return (
        value
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\a ")
        .replace("\r", "\\d ")
        .replace("\f", "\\c ")
    )


def _bare_id_selector(value: str) -> str:
    return f'[id="{_css_string(value)}"]'


def _looks_like_legacy_scoped_ref(value: str) -> bool:
    prefix, sep, _node = value.partition(":")
    return bool(sep) and len(prefix) > 1 and prefix[0] == "d" and prefix[1:].isdigit()


def _is_select_no_option_error(error: BaseException) -> bool:
    code = str(getattr(error, "code", ""))
    return "SelectNoSuchOption" in code


def _is_ref_error(error: BaseException) -> bool:
    if isinstance(error, BrowserRefError):
        return True
    return str(getattr(error, "code", "")) in {
        ERR_REF_SHAPE,
        ERR_REF_STALE,
    }


def _act(
    tool: str,
    action: dict,
    *,
    timeout: float = 15.0,
    wait_stable: bool = False,
    page_id: str | None = None,
):
    CLIENT.require_action(tool, str(action["kind"]))
    try:
        result, _ = CLIENT.invoke(
            tool,
            "page.act",
            params={
                "action": action,
                "waitForPageStable": bool(wait_stable),
                "snapshotAfter": "none",
                "timeoutMs": _seconds_to_clamped_timeout_ms(timeout),
            },
            page_id=page_id,
            mutating=True,
        )
    except BrowserUseError as error:
        if (
            action["kind"] in {"click", "type"}
            and error.code == "actor_kElementOffscreen"
        ):
            error.args = (
                f"{error}\nHint: Target has no usable interaction point. "
                "It may require opening a menu, panel or tab before interaction.",
            )
        raise
    return result


def _node_action(tool: str, kind: str, ref: ElementTarget | str, **values):
    action = {"kind": kind, "target": _element_target(ref)}
    action.update(values)
    return _act(tool, action)


def _element_target(value: ElementTarget | str, *, match="unique"):
    if isinstance(value, CssLocator):
        return value.target()
    return parse_ref(value).target()


def _click_target(value: ElementTarget | str):
    if isinstance(value, CssLocator):
        if value.match == "all":
            raise BrowserUseError(
                "click CSS locator must use unique or first match policy",
                code=ERR_READ_TARGET,
            )
        return value.target()

    if isinstance(value, (RefList, list, tuple)):
        return parse_ref(value).target()

    if not isinstance(value, str):
        raise BrowserUseError(
            "click target must be a ref, ref sequence, CSS selector string, "
            "or CssLocator; "
            f"got {value.__class__.__name__}",
            code=ERR_READ_TARGET,
        )

    text = str(value or "").strip()
    if not text:
        return parse_ref(text).target()

    try:
        return parse_ref(text).target()
    except BrowserRefError:
        if _looks_like_legacy_scoped_ref(text):
            raise

    selector = text if _is_css_selector_text(text) else _bare_id_selector(text)
    return CssLocator(selector, match="unique").target()


def _main_frame_selector(
    locator: CssLocator, *, tool: str, allow_all: bool = False
) -> str:
    if locator.match == "all" and not allow_all:
        raise BrowserUseError(
            f"{tool} CSS locator must use unique or first match policy",
            code=ERR_READ_TARGET,
        )
    if locator.frame_path:
        raise BrowserUseError(
            f"{tool} does not support frame_path targets",
            code=ERR_READ_TARGET,
        )
    return locator.selector


def _selector_from_css_locator_result(value, *, tool: str) -> str:
    candidates = []
    if isinstance(value, dict):
        primary = value.get("primary")
        if isinstance(primary, dict):
            candidates.append(primary)
        candidates.extend(
            candidate for candidate in value.get("candidates") or ()
            if isinstance(candidate, dict)
        )
    for candidate in candidates:
        locator = candidate.get("locator")
        if not isinstance(locator, dict):
            continue
        if locator.get("framePath"):
            raise BrowserUseError(
                f"{tool} does not support frame targets",
                code=ERR_READ_TARGET,
            )
        selector = str(locator.get("selector") or "")
        if selector:
            return selector
    raise BrowserUseError(
        f"{tool} target selector was not found",
        code=ERR_READ_TARGET,
    )


def _get_css_locator_result(ref, *, max_candidates=8, page_id: str | None = None):
    result, _ = CLIENT.invoke(
        "get_css_locator",
        "page.getSelector",
        params={
            "target": parse_ref(ref).target(),
            "maxCandidates": max_candidates,
        },
        page_id=page_id,
    )
    return _json_payload(result)


def _dialog_click_selector(value) -> str:
    if isinstance(value, CssLocator):
        return _main_frame_selector(value, tool="click_and_handle_dialog")

    if not isinstance(value, (RefList, list, tuple)):
        text = str(value or "").strip()
        if text:
            try:
                parse_ref(text)
            except BrowserRefError:
                if _looks_like_legacy_scoped_ref(text):
                    raise
                return (
                    text if _is_css_selector_text(text) else _bare_id_selector(text)
                )

    return _selector_from_css_locator_result(
        _get_css_locator_result(value, max_candidates=4),
        tool="click_and_handle_dialog",
    )


def _screenshot_selector(value, *, page_id: str | None = None) -> str:
    if isinstance(value, CssLocator):
        return _main_frame_selector(value, tool="screenshot")

    if not isinstance(value, (RefList, list, tuple)):
        text = str(value or "").strip()
        if text:
            try:
                parse_ref(text)
            except BrowserRefError:
                if _looks_like_legacy_scoped_ref(text):
                    raise
                return (
                    text if _is_css_selector_text(text) else _bare_id_selector(text)
                )

    return _selector_from_css_locator_result(
        _get_css_locator_result(value, max_candidates=4, page_id=page_id),
        tool="screenshot",
    )


def _element_screenshot_clip(ref, *, page_id: str) -> dict:
    selector = _screenshot_selector(ref, page_id=page_id)
    expression = (
        "(()=>{const e=document.querySelector(%s);"
        "if(!e)return null;"
        "e.scrollIntoView({block:'center',inline:'center'});"
        "const b=e.getBoundingClientRect();"
        "if(!(b.width>0&&b.height>0))return null;"
        "return JSON.stringify({x:scrollX+b.left,y:scrollY+b.top,"
        "width:b.width,height:b.height,scale:1});})()"
    ) % json.dumps(selector)
    response = _native.send_cdp(
        "screenshot",
        "Runtime.evaluate",
        {
            "expression": expression,
            "awaitPromise": False,
            "returnByValue": True,
        },
        page_id=page_id,
        retryable=False,
    )
    if not isinstance(response, dict) or response.get("exceptionDetails"):
        raise BrowserUseError(
            "screenshot target clip evaluation failed",
            code=ERR_READ_TARGET,
        )
    result = response.get("result")
    raw = result.get("value") if isinstance(result, dict) else None
    if not raw or raw == "null":
        raise BrowserUseError(
            "screenshot target was not found or has no rendered box",
            code=ERR_READ_TARGET,
        )
    try:
        value = json.loads(str(raw))
        width = float(value["width"])
        height = float(value["height"])
        x = float(value["x"])
        y = float(value["y"])
        scale = float(value.get("scale", 1.0))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise BrowserUseError(
            "screenshot target clip is invalid",
            code=ERR_SCREENSHOT_DIMENSION,
        ) from exc
    if not all(math.isfinite(number) for number in (x, y, width, height, scale)):
        raise BrowserUseError(
            "screenshot target clip is invalid",
            code=ERR_SCREENSHOT_DIMENSION,
        )
    if width <= 0 or height <= 0 or scale <= 0:
        raise BrowserUseError(
            "screenshot target was not found or has no rendered box",
            code=ERR_READ_TARGET,
        )
    return {
        "x": max(0.0, x),
        "y": max(0.0, y),
        "width": width,
        "height": height,
        "scale": scale,
    }


def _schedule_dialog_click(selector: str):
    expression = (
        "(()=>{const e=document.querySelector(%s);"
        "if(!e)return false;"
        "setTimeout(()=>e.click(),0);"
        "return true;})()"
    ) % json.dumps(selector)
    result = _native.send_cdp(
        "click_and_handle_dialog",
        "Runtime.evaluate",
        {
            "expression": expression,
            "awaitPromise": False,
            "returnByValue": True,
        },
        retryable=False,
    )
    if isinstance(result, dict):
        evaluation = result.get("result")
        if isinstance(evaluation, dict) and evaluation.get("value") is False:
            raise BrowserUseError(
                "click_and_handle_dialog target was not found",
                code=ERR_READ_TARGET,
            )
    return result


def _read_field(value):
    field = str(value)
    field = _READ_FIELD_ALIASES.get(field, field)
    if field in _READ_PROPERTIES or field.startswith("attribute:"):
        return field
    return f"attribute:{field}"


def _point(x, y):
    nx = _bounded_number(
        x,
        name="x",
        minimum=0,
        maximum=1000,
        code=ERR_COORDINATE_SPACE,
    )
    ny = _bounded_number(
        y,
        name="y",
        minimum=0,
        maximum=1000,
        code=ERR_COORDINATE_SPACE,
    )
    width, height = _native.page_size()
    return {"x": nx * width / 1000.0, "y": ny * height / 1000.0}


def _query_css(
    selector,
    *,
    limit=50,
    fields=None,
    timeout=15.0,
    tool="read_all",
    frame_path=(),
    match="all",
):
    result, _ = CLIENT.invoke(
        tool,
        "page.queryCss",
        params={
            "selector": str(selector),
            "framePath": list(frame_path),
            "match": str(match),
            "limit": _bounded_integer(
                limit, name="limit", minimum=1, maximum=1000
            ),
            "fields": list(fields or ()),
            "timeoutMs": _seconds_to_clamped_timeout_ms(timeout),
        },
    )
    value = _json_payload(result)
    if not isinstance(value, dict):
        value = {}
    value.setdefault("documentGeneration", result.get("documentGeneration"))
    return value


def _read_elements(
    tool: str,
    ref: ElementTarget | str,
    fields=(),
    *,
    field_queries=None,
    max_chars=4000,
    limit=None,
    outermost_only=False,
):
    params = {
        "mode": "element",
        "target": _element_target(ref),
        "maxChars": _bounded_integer(
            max_chars, name="max_chars", minimum=1, maximum=40000
        ),
    }
    if field_queries is None:
        params["fields"] = list(fields)
    else:
        params["fieldQueries"] = list(field_queries)
    if limit is not None:
        params["limit"] = _bounded_integer(
            limit, name="limit", minimum=1, maximum=200
        )
    if outermost_only:
        params["outermostOnly"] = True
    result, _ = CLIENT.invoke(
        tool,
        "page.read",
        params=params,
    )
    value = _json_payload(result)
    elements = value.get("elements") if isinstance(value, dict) else None
    return list(elements) if isinstance(elements, list) else []


def _read_element(
    tool: str,
    ref: ElementTarget | str,
    fields=(),
    *,
    field_queries=None,
    max_chars=4000,
):
    elements = _read_elements(
        tool,
        ref,
        fields,
        field_queries=field_queries,
        max_chars=max_chars,
    )
    return elements[0] if elements else {}


def css(selector, *, frame_path=(), match="unique"):
    """Build a persistent CSS locator for page.read/page.act calls."""
    return CssLocator(selector, frame_path=frame_path, match=match)


@public_tool("get_css_locator")
def get_css_locator(ref, *, max_candidates=8):
    """Return unique CSS locator candidates ordered with the primary first."""
    max_candidates = _bounded_integer(
        max_candidates,
        name="max_candidates",
        minimum=1,
        maximum=16,
    )
    result = _get_css_locator_result(ref, max_candidates=max_candidates)
    if not isinstance(result, dict):
        return CssLocatorList()

    raw_candidates = []
    primary = result.get("primary")
    if isinstance(primary, dict):
        raw_candidates.append(primary)
    raw_candidates.extend(
        candidate
        for candidate in result.get("candidates") or ()
        if isinstance(candidate, dict)
    )

    locators = []
    seen = set()
    for candidate in raw_candidates:
        if candidate.get("unique") is not True:
            continue
        value = candidate.get("locator")
        if not isinstance(value, dict):
            raise BrowserUseError(
                "Native selector candidate has no CSS locator",
                code=ERR_READ_TARGET,
            )
        try:
            locator = CssLocator(
                value.get("selector"),
                frame_path=value.get("framePath", ()),
                match=value.get("match", "unique"),
            )
        except (TypeError, ValueError) as error:
            raise BrowserUseError(
                f"Native selector candidate is invalid: {error}",
                code=ERR_READ_TARGET,
            ) from error
        key = (locator.frame_path, locator.selector, locator.match)
        if key not in seen:
            seen.add(key)
            locators.append(locator)
    return CssLocatorList(locators)


@public_tool("snapshot")
def snapshot(
    *,
    view: Literal["all", "outline", "interactive"] | None = None,
    within: ElementTarget | str | None = None,
    max_chars: int | None = None,
    diff: bool | Literal["auto"] | None = None,
) -> SnapshotResult:
    return _snapshot(
        view=view,
        within=within,
        max_chars=max_chars,
        diff=diff,
    )


@public_tool("webmcp_tools")
def webmcp_tools():
    CLIENT.require_feature("webmcp_tools", "webmcp")
    page_id = CLIENT.ensure_page().id
    result, _ = CLIENT.invoke(
        "webmcp_tools",
        "page.webmcp_tools",
        page_id=page_id,
        params={
            "timeoutMs": _seconds_to_clamped_timeout_ms(15.0),
        },
    )
    tools = result.get("webMcpTools") if isinstance(result, Mapping) else ()
    return tuple(tool for tool in tools or () if isinstance(tool, Mapping))


@public_tool("webmcp_call", action=True)
def webmcp_call(
    tool_name,
    input_arguments=None,
):
    CLIENT.require_feature("webmcp_call", "webmcp")
    if input_arguments is None:
        arguments = {}
    elif isinstance(input_arguments, Mapping):
        arguments = dict(input_arguments)
    elif isinstance(input_arguments, str):
        try:
            parsed_arguments = json.loads(input_arguments)
        except json.JSONDecodeError as error:
            raise TypeError("input_arguments JSON string must encode an object") from error
        if not isinstance(parsed_arguments, dict):
            raise TypeError("input_arguments JSON string must encode an object")
        arguments = input_arguments
    else:
        raise TypeError("input_arguments must be a mapping, JSON object string, or None")
    action = {
        "kind": "webmcp",
        "toolName": str(tool_name),
        "inputArguments": arguments,
    }
    result, _ = CLIENT.invoke(
        "webmcp_call",
        "page.act",
        params={
            "action": action,
            "snapshotAfter": "none",
            "timeoutMs": _seconds_to_clamped_timeout_ms(15.0),
        },
        mutating=True,
    )
    web_mcp_result = result.get("webMcpResult") if isinstance(result, Mapping) else None
    if not isinstance(web_mcp_result, Mapping):
        raise BrowserUseError(
            "Native WebMCP call did not return a WebMCP result",
            code="BU_PROTOCOL",
        )
    raw = web_mcp_result.get("result")
    return _json_payload({"json": raw}) if isinstance(raw, str) else raw


@public_tool("find")
def find(query, *, limit=10, search_type="all", max_chars=8000) -> FindResult:
    return _find(
        query,
        limit=limit,
        search_type=search_type,
        max_chars=max_chars,
    )


@public_tool("text")
def text(ref: ElementTarget | str, *, max_chars=4000):
    CLIENT.require_feature("text", "elementRead")
    value = _read_element("text", ref, ("text",), max_chars=max_chars)
    if isinstance(value, dict):
        text_value = value.get("text")
        if text_value:
            return text_value
        try:
            fallback = _read_element(
                "text", ref, ("value",), max_chars=max_chars
            )
        except BrowserUseError as error:
            if getattr(error, "code", "") != ERR_READ_TARGET:
                raise
            return text_value or ""
        return fallback.get("value") or text_value or ""
    return value or ""


@public_tool("get")
def get(ref: ElementTarget | str, attr):
    CLIENT.require_feature("get", "elementRead")
    field = _read_field(attr)
    value = _read_element("get", ref, (field,))
    return value.get(field) if isinstance(value, dict) else value


@public_tool("get_page_text")
def get_page_text(*, within: ElementTarget | str | None = None, max_chars=8000):
    CLIENT.require_feature("get_page_text", "documentRead")
    max_chars = _bounded_integer(
        max_chars, name="max_chars", minimum=1, maximum=40000
    )
    if within is not None:
        if isinstance(within, CssLocator) and within.match == "all":
            elements = _read_elements(
                "get_page_text",
                within,
                ("text",),
                max_chars=max_chars,
                limit=200,
                outermost_only=True,
            )
            texts = [
                element.get("text")
                for element in elements
                if isinstance(element, dict) and element.get("text")
            ]
            return "\n\n".join(texts)[:max_chars]
        value = _read_element(
            "get_page_text", within, ("text",), max_chars=max_chars
        )
        return value.get("text", "") if isinstance(value, dict) else ""
    result, _ = CLIENT.invoke(
        "get_page_text",
        "page.read",
        params={
            "mode": "documentText",
            "maxChars": max_chars,
        },
    )
    value = _json_payload(result)
    return value.get("text", "") if isinstance(value, dict) else str(value or "")


def _compact_repeated_text(rows, text_fields=("text",)):
    for row in rows:
        if not isinstance(row, dict):
            continue
        for field in text_fields:
            value = row.get(field)
            if isinstance(value, str):
                row[field] = " ".join(value.split())
            elif isinstance(value, list):
                row[field] = [
                    " ".join(item.split()) if isinstance(item, str) else item
                    for item in value
                ]
    return rows


def _read_all_csv_cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return " | ".join(_read_all_csv_cell(item) for item in value)
    return str(value)


class ReadAllResult(list):
    """List-like read result that prints as compact CSV."""

    def __init__(self, rows=(), *, columns=()):
        super().__init__(rows)
        self.columns = tuple(dict.fromkeys(str(column) for column in columns))

    def __str__(self):
        columns = list(self.columns)
        for row in self:
            if not isinstance(row, Mapping):
                continue
            for name in row:
                if name not in columns:
                    columns.append(name)
        if not columns:
            return ""

        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        writer.writerow(columns)
        for row in self:
            record = row if isinstance(row, Mapping) else {}
            writer.writerow(
                [_read_all_csv_cell(record.get(name)) for name in columns]
            )
        return output.getvalue().rstrip("\n")


# Only read_all opts into this internal Native field; public "text" is unchanged.
_READ_ALL_TEXT_FIELD = "textWithFallback"


def _structured_read_fields(fields):
    if not isinstance(fields, Mapping):
        return None
    if not 1 <= len(fields) <= 32:
        error = ValueError("mapped fields must contain 1 to 32 entries")
        error.code = ERR_READ_FIELDS
        raise error

    queries = []
    text_fields = []
    for name, spec in fields.items():
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 128
            or not all(character.isascii() and (character.isalnum() or character in "_-") for character in name)
        ):
            error = ValueError(
                "mapped field names must use 1 to 128 ASCII letters, digits, '_' or '-'"
            )
            error.code = ERR_READ_FIELDS
            raise error
        if name in {"index", "nodeId", "documentGeneration", "ref", "truncated"}:
            error = ValueError(f"mapped field name {name!r} is reserved")
            error.code = ERR_READ_FIELDS
            raise error
        if not isinstance(spec, Mapping):
            error = TypeError(
                f"mapped field {name!r} must be an object with selector and field"
            )
            error.code = ERR_READ_FIELDS
            raise error
        selector = spec.get("selector")
        if not isinstance(selector, str) or not selector or len(selector) > 8192:
            error = ValueError(
                f"mapped field {name!r} requires a non-empty CSS selector"
            )
            error.code = ERR_READ_FIELDS
            raise error
        field = _read_field(spec.get("field", "text"))
        match_limit = _bounded_integer(
            spec.get("limit", 1),
            name=f"mapped field {name!r} limit",
            minimum=1,
            maximum=20,
        )
        queries.append(
            {
                "name": name,
                "selector": selector,
                "field": _READ_ALL_TEXT_FIELD if field == "text" else field,
                "limit": match_limit,
            }
        )
        if field == "text":
            text_fields.append(name)
    return queries, tuple(text_fields)


def _read_all_limit(value) -> int:
    return _bounded_integer(
        value,
        name="read_all limit",
        minimum=1,
        maximum=300,
        code=ERR_READ_LIMIT,
    )


def _read_all_field_list(fields):
    if fields is None:
        values = ["text", "href"]
    elif isinstance(fields, str):
        values = [fields]
    elif isinstance(fields, (list, tuple)):
        values = list(fields)
    else:
        raise coded_error(
            TypeError,
            ERR_READ_FIELDS,
            "read_all fields must be a field name, a field-name list, or a mapping",
        )
    requested = []
    wire_fields = []
    text_fields = []
    for field in values:
        name = str(field or "").strip()
        if not name:
            raise coded_error(
                ValueError,
                ERR_READ_FIELDS,
                "read_all fields cannot contain empty field names",
            )
        if name == "ref":
            raise coded_error(
                ValueError,
                ERR_READ_FIELDS,
                "read_all field name 'ref' is reserved",
            )
        wire_field = _read_field(name)
        requested.append(name)
        wire_fields.append(
            _READ_ALL_TEXT_FIELD if wire_field == "text" else wire_field
        )
        if wire_field == "text":
            text_fields.append(name)
    if not requested:
        raise coded_error(
            ValueError,
            ERR_READ_FIELDS,
            "read_all fields cannot be empty",
        )
    return requested, wire_fields, tuple(text_fields)


@public_tool("read_all")
def read_all(
    selector_or_refs: ElementTarget | str | list[Ref] | tuple[Ref, ...],
    fields=None,
    limit=50,
    *,
    max_chars=12000,
) -> ReadAllResult:
    CLIENT.require_feature("read_all", "elementRead")
    max_chars = _bounded_integer(
        max_chars, name="max_chars", minimum=1, maximum=40000
    )
    row_limit = _read_all_limit(limit)
    structured = _structured_read_fields(fields)
    if structured is None:
        requested, wire_fields, text_fields = _read_all_field_list(fields)
        field_queries = None
    else:
        requested = []
        wire_fields = []
        field_queries, text_fields = structured
    if isinstance(selector_or_refs, CssLocator) or (
        isinstance(selector_or_refs, str) and not is_node_ref(selector_or_refs)
    ):
        locator = (
            selector_or_refs
            if isinstance(selector_or_refs, CssLocator)
            else CssLocator(selector_or_refs, match="all")
        )
        if locator.match != "all":
            locator = CssLocator(
                locator.selector, frame_path=locator.frame_path, match="all"
            )
        params = {
            "mode": "element",
            "target": locator.target(),
            "limit": row_limit,
            "maxChars": max_chars,
        }
        if field_queries is None:
            params["fields"] = wire_fields
        else:
            params["fieldQueries"] = field_queries
        result, _ = CLIENT.invoke(
            "read_all",
            "page.read",
            params=params,
        )
        value = _json_payload(result)
        rows = list(value.get("elements") or ()) if isinstance(value, dict) else []
    elif isinstance(selector_or_refs, (list, tuple, RefList)):
        refs = list(selector_or_refs)[:row_limit]
        rows = [
            _read_element(
                "read_all",
                ref,
                wire_fields,
                field_queries=field_queries,
                max_chars=max_chars,
            )
            for ref in refs
        ]
    else:
        rows = [
            _read_element(
                "read_all",
                selector_or_refs,
                wire_fields,
                field_queries=field_queries,
                max_chars=max_chars,
            )
        ]
    for row in rows:
        if not isinstance(row, dict):
            continue
        for metadata in ("index", "nodeId", "documentGeneration", "truncated"):
            row.pop(metadata, None)
        for requested_field, wire_field in zip(requested, wire_fields):
            if requested_field != wire_field and wire_field in row:
                row[requested_field] = row.pop(wire_field)
    columns = (
        ("ref", *(query["name"] for query in field_queries))
        if field_queries is not None
        else ("ref", *requested)
    )
    return ReadAllResult(
        _compact_repeated_text(rows, text_fields),
        columns=columns,
    )


@public_tool("content_export")
def content_export(max_chars=3000):
    max_chars = _bounded_integer(
        max_chars,
        name="max_chars",
        minimum=200,
        maximum=40000,
        code=ERR_EXPORT_LIMIT,
    )
    CLIENT.require_feature("content_export", "documentRead")
    result, _ = CLIENT.invoke(
        "content_export", "page.read", params={"mode": "documentContent", "maxChars": max_chars}
    )
    value = _json_payload(result)
    return value.get("content", value) if isinstance(value, dict) else value


_EXTRACT_CONTENT_TYPES = ("heading", "paragraph", "list", "link", "table")


def _extract_content_types(value) -> tuple[str, ...]:
    message = (
        "types must be 'all', one of heading/paragraph/list/link/table, "
        "or a non-empty sequence of those values"
    )
    if value == "all":
        return _EXTRACT_CONTENT_TYPES
    if isinstance(value, str):
        values = (value,)
    elif isinstance(value, (list, tuple, set, frozenset)):
        values = tuple(value)
    else:
        raise coded_error(TypeError, ERR_EXTRACT_TYPES, message)
    if not values or any(not isinstance(item, str) for item in values):
        raise coded_error(TypeError, ERR_EXTRACT_TYPES, message)
    unknown = set(values) - set(_EXTRACT_CONTENT_TYPES)
    if unknown:
        raise coded_error(ValueError, ERR_EXTRACT_TYPES, message)
    return tuple(item for item in _EXTRACT_CONTENT_TYPES if item in values)


@public_tool("extract_content")
def extract_content(types="all", max_chars=4000, *, within=None):
    """Return flat, document-ordered content blocks and bounded Markdown."""
    content_types = _extract_content_types(types)
    max_chars = _bounded_integer(
        max_chars,
        name="max_chars",
        minimum=100,
        maximum=40000,
        code=ERR_EXPORT_LIMIT,
    )
    CLIENT.require_feature("extract_content", "contentExtraction")
    target = None
    if within is not None:
        CLIENT.require_feature("extract_content", "contentExtractionTarget")
        target = _element_target(within)
    result, _ = CLIENT.invoke(
        "extract_content",
        "page.read",
        params={
            "mode": "extractContent",
            "types": list(content_types),
            "maxChars": max_chars,
            **({"target": target} if target else {}),
        },
    )
    value = _json_payload(result)
    if isinstance(value, dict):
        value = value.get("content", value)
    if not isinstance(value, Mapping):
        raise BrowserUseError("Native content extraction returned an invalid result")
    return ContentExtractionResult(value, max_chars=max_chars)


@public_tool("http_get")
def http_get(url, headers=None, timeout=20.0):
    return _seed.call_direct("http_get", url, headers=headers, timeout=timeout)


@public_tool("console_messages")
def console_messages(level=None):
    return _native.console_messages(level)


@public_tool("network_requests")
def network_requests(url_contains=None):
    return _native.network_requests(url_contains)


def _page_metric(value):
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)) and math.isfinite(value):
        return value
    return 0


def _normalize_page_info(value):
    if not isinstance(value, Mapping):
        return value
    record = dict(value)
    if "dialog" in record and len(record) == 1:
        return record
    width = _page_metric(record.get("w"))
    height = _page_metric(record.get("h"))
    page_width = _page_metric(record.get("pw"))
    page_height = _page_metric(record.get("ph"))
    if width > 0 and page_width < width:
        record["pw"] = width
    if height > 0 and page_height < height:
        record["ph"] = height
    return record


@public_tool("page_info")
def page_info():
    page = CLIENT.ensure_page()
    width, height = _native.page_size(page.id)
    metrics = _native.send_cdp("page_info", "Page.getLayoutMetrics", page_id=page.id)
    history = _native.send_cdp("page_info", "Page.getNavigationHistory", page_id=page.id)
    content = metrics.get("cssContentSize") or metrics.get("contentSize") or {}
    viewport = metrics.get("cssVisualViewport") or metrics.get("visualViewport") or {}
    entries = history.get("entries") or []
    index = history.get("currentIndex")
    entry = entries[index] if isinstance(index, int) and 0 <= index < len(entries) else {}
    return _normalize_page_info(
        {
            "url": entry.get("url") or page.url,
            "title": entry.get("title") or page.title,
            "w": width,
            "h": height,
            "sx": viewport.get("pageX", 0),
            "sy": viewport.get("pageY", 0),
            "pw": content.get("width", width),
            "ph": content.get("height", height),
        }
    )


@public_tool("new_tab", action=True)
def new_tab(url="about:blank"):
    return _native.new_tab(url)


@public_tool("list_tabs")
def list_tabs(include_chrome=False):
    return _native.list_tabs(include_chrome)


@public_tool("switch_tab", action=True)
def switch_tab(target):
    return _native.switch_tab(target)


@public_tool("current_tab")
def current_tab():
    return _native.current_tab()


@public_tool("close_tab", action=True)
def close_tab(target_id=None):
    return _native.close_tab(target_id)


@public_tool("ensure_real_tab", action=True)
def ensure_real_tab():
    return _native.ensure_real_tab()


@public_tool("resync", action=True)
def resync(*, timeout=10.0):
    tabs = _native.list_tabs()
    if not tabs:
        raise BrowserSessionError("no browser tab is available")
    visible = None
    focused = None
    expression = "JSON.stringify({v:document.visibilityState,f:document.hasFocus()})"
    for tab in tabs:
        try:
            raw = _native.send_cdp(
                "resync", "Runtime.evaluate",
                {"expression": expression, "returnByValue": True},
                page_id=tab.target_id,
            )
            value = ((raw.get("result") or {}).get("value"))
            state = json.loads(value) if isinstance(value, str) else (value or {})
        except Exception:
            continue
        if state.get("v") == "visible":
            visible = visible or tab
            if state.get("f"):
                focused = tab
                break
    chosen = focused or visible or tabs[0]
    _native.switch_tab(chosen)
    _native.send_cdp("resync", "Page.bringToFront", page_id=chosen.target_id)
    wait_for_load(timeout=timeout)
    return _native.current_tab()


@public_tool("click", action=True)
def click(ref: ElementTarget | str, *, button="left", clicks=1):
    count = _click_count(clicks)
    normalized_button = _click_button(button)
    target = _click_target(ref)
    for index in range(1, count + 1):
        _act(
            "click",
            {
                "kind": "click",
                "target": target,
                "button": normalized_button,
                "clickCount": 2 if index == 2 else 1,
            },
        )
        if index != count:
            time.sleep(_CLICK_INTERVAL_SECONDS)
    return None


@public_tool("type", action=True)
def type(  # noqa: A001
    ref: ElementTarget | str,
    text=_UNSET,
    *,
    replace=True,
    submit=None,
    timeout=15.0,
):
    value = "" if text is _UNSET else str(text)
    action = {
        "kind": "type",
        "target": _element_target(ref),
        "text": value,
        "replace": bool(replace),
        "submit": submit is True,
    }
    _act("type", action, timeout=timeout)
    return None


@public_tool("select", action=True)
def select(ref: ElementTarget | str, value):
    try:
        _node_action("select", "select", ref, value=str(value))
        return None
    except BrowserUseError as error:
        if _is_select_no_option_error(error):
            raise BrowserUseError(
                f"select option {value!r} was not found for the target element",
                code=ERR_SELECT_NO_OPTION,
            ) from error
        raise


@public_tool("upload", action=True)
def upload(ref, path):
    page = CLIENT.ensure_page()
    selector = _selector_from_css_locator_result(
        _get_css_locator_result(ref, max_candidates=4, page_id=page.id),
        tool="upload",
    )
    _set_file_input_files("upload", selector, path, page_id=page.id)
    return None


@public_tool("hover", action=True)
def hover(ref: ElementTarget | str):
    _node_action("hover", "moveMouse", ref)
    return None


def _handle_dialog_impl(*, accept=True, prompt_text=None, timeout=5.0):
    deadline = time.monotonic() + max(0.0, float(timeout))
    params = {"accept": bool(accept)}
    if prompt_text is not None:
        params["promptText"] = str(prompt_text)
    while time.monotonic() <= deadline:
        opened = None
        for dialog in _native.dialogs().values():
            if getattr(dialog, "is_open", False):
                opened = dialog
                break
        try:
            _native.send_cdp("handle_dialog", "Page.handleJavaScriptDialog", params)
        except Exception as error:
            if "No dialog is showing" not in str(error):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.05, remaining))
            continue
        if opened is not None:
            return {
                "type": getattr(opened, "type", ""),
                "message": getattr(opened, "message", ""),
                "accepted": bool(accept),
            }
        return {
            "type": "unknown",
            "message": "",
            "accepted": bool(accept),
        }
    raise BrowserUseError(
        "no JavaScript dialog appeared before timeout",
        code="BU_DIALOG_NOT_FOUND",
    )


@public_tool("handle_dialog", action=True)
def handle_dialog(*, accept=True, prompt_text=None, timeout=5.0):
    return _handle_dialog_impl(accept=accept, prompt_text=prompt_text, timeout=timeout)


@public_tool("click_and_handle_dialog", action=True)
def click_and_handle_dialog(
    ref: ElementTarget | str, *, accept=True, prompt_text=None, timeout=5.0
):
    _schedule_dialog_click(_dialog_click_selector(ref))
    return _handle_dialog_impl(accept=accept, prompt_text=prompt_text, timeout=timeout)


@public_tool("click_xy", action=True)
def click_xy(x, y):
    _act("click_xy", {"kind": "click", "target": {"point": _point(x, y)}})
    return None


@public_tool("left_double", action=True)
def left_double(x, y):
    _act(
        "left_double", {"kind": "click", "target": {"point": _point(x, y)}, "clickCount": 2}
    )
    return None


@public_tool("right_single", action=True)
def right_single(x, y):
    _act(
        "right_single", {"kind": "click", "target": {"point": _point(x, y)}, "button": "right"}
    )
    return None


@public_tool("drag", action=True)
def drag(x1, y1, x2, y2, *, steps=12):
    del steps
    _act(
        "drag", {"kind": "drag", "from": {"point": _point(x1, y1)}, "to": {"point": _point(x2, y2)}}
    )
    return None


@public_tool("scroll", action=True)
def scroll(
    ref: ElementTarget | str | None = None,
    *,
    x=None,
    y=None,
    direction=None,
    amount=3,
):
    """Scroll the main page, a ref/CSS ancestor, or at viewport coordinates."""
    if direction is None and isinstance(ref, str) and ref.lower() in {"up", "down", "left", "right"}:
        raise BrowserUseError(
            f"scroll(): {ref!r} was bound to ref; direction is missing (keyword-only). "
            f"Use bu.scroll(direction={ref.lower()!r}) for the page, or "
            f"bu.scroll(target, direction={ref.lower()!r}) for an element. "
            "No scroll was dispatched.",
            code=ERR_SCROLL_DIRECTION,
        )
    direction = str(direction).lower()
    if direction not in {"up", "down", "left", "right"}:
        raise BrowserUseError(
            "direction must be up, down, left, or right",
            code=ERR_SCROLL_DIRECTION,
        )
    if isinstance(amount, bool) or not isinstance(amount, int) or not 1 <= amount <= 10:
        raise BrowserUseError(
            "amount must be an integer in [1, 10]",
            code=ERR_SCROLL_AMOUNT,
        )
    action = {
        "kind": "scroll",
        "direction": direction,
        "distance": amount * 120.0,
    }
    if ref is not None:
        if x is not None or y is not None:
            raise ValueError("scroll accepts either ref/CSS locator or x/y coordinates, not both")
        action["target"] = _element_target(ref)
    elif x is not None or y is not None:
        if x is None or y is None:
            raise ValueError("scroll coordinates require both x and y")
        action["target"] = {"point": _point(x, y)}
    try:
        _act("scroll", action)
    except BrowserUseError as error:
        code = str(getattr(error, "code", ""))
        description = _SCROLL_NO_EFFECT_DESCRIPTIONS.get(code)
        if description is None:
            raise
        print(f"[scroll no-op code={code}] {description}")
    return None


@public_tool("scroll_into_view", action=True)
def scroll_into_view(ref: ElementTarget | str):
    """Ensure a Native ref or CSS locator is visible in its scroll containers."""
    _node_action("scroll_into_view", "scrollTo", ref)
    return None


@public_tool("navigate", action=True)
def navigate(target):
    value = str(target).strip()
    normalized = value.lower()
    if normalized in {"back", "forward", "reload"}:
        _act("navigate", {"kind": normalized}, timeout=60.0)
        return None

    CLIENT.require_action("navigate", "navigate")
    page, created = CLIENT.ensure_page_for_navigation(value)
    if not created:
        return _act(
            "navigate",
            {"kind": "navigate", "url": value},
            timeout=60.0,
            page_id=page.id,
        )

    # Page creation already starts the requested URL. Starting a second Native
    # navigate here races the host's initial navigation on a fresh WebContents.
    # Readiness and observation remain explicit wait_for_load()/snapshot() calls.
    return {
        "actionCompleted": True,
        "pageId": page.id,
        "effects": [{"type": "navigationStarted", "url": value}],
    }


@public_tool("wait")
def wait(seconds=3):
    return _seed.call_direct("wait", seconds)


@public_tool("wait_for_load")
def wait_for_load(timeout=5.0, since=None):
    del since
    timeout = min(
        _bounded_number(
            timeout, name="timeout", minimum=0, maximum=math.inf,
            code=ERR_WAIT_SECONDS,
        ),
        5.0,
    )
    result = _act(
        "wait_for_load",
        {"kind": "wait", "durationMs": 0},
        timeout=timeout,
    )
    settlement = result.get("settlement")
    if settlement == "ready":
        return True
    if settlement == "timedOut":
        return False
    if settlement == "targetClosed":
        raise BrowserSessionError("browser page closed while waiting for load")
    raise BrowserUseError(
        "Native wait result did not include a valid settlement status",
        code="BU_PROTOCOL",
    )


@public_tool("wait_for_element")
def wait_for_element(selector, timeout=10.0, visible=False):
    deadline = time.monotonic() + max(0.0, float(timeout))
    while True:
        remaining = deadline - time.monotonic()
        value = _query_css(
            selector,
            limit=1,
            fields=["visible"] if visible else [],
            timeout=min(2.0, max(0.1, remaining)),
            tool="wait_for_element",
        )
        elements = value.get("elements") or []
        if elements and (not visible or elements[0].get("visible") is True):
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(0.1, remaining))


@public_tool("wait_for_network_idle")
def wait_for_network_idle(timeout=10.0, idle_ms=500):
    return _native.wait_for_network_idle(timeout, idle_ms)


@public_tool("press_key", action=True)
def press_key(key):
    _act("press_key", {"kind": "pressKey", "key": str(key)})
    return None


@public_tool("hotkey", action=True)
def hotkey(*keys):
    modifiers = 0
    normal = None
    for key in keys:
        value = str(key).lower()
        if value in {"alt", "option"}:
            modifiers |= 1
        elif value in {"ctrl", "control"}:
            modifiers |= 2
        elif value in {"meta", "cmd", "command", "win"}:
            modifiers |= 4
        elif value == "shift":
            modifiers |= 8
        else:
            normal = key
    if normal is None:
        return None
    _act("hotkey", {
        "kind": "pressKey", "key": str(normal), "modifiers": modifiers,
    })
    return None


@public_tool("screenshot")
def screenshot(path=None, max_dim=1800, *, tag=None, show=True, ref=None):
    max_dimension = _max_dimension(max_dim)
    page = CLIENT.ensure_page()
    clip = _element_screenshot_clip(ref, page_id=page.id) if ref is not None else None
    params = {"format": "png", "captureBeyondViewport": bool(clip)}
    if clip:
        params["clip"] = clip
    encoded = CLIENT.module.call(
        "capture_screenshot",
        page.id,
        params,
        max_dimension,
    )
    data = base64.b64decode(encoded)
    if not data:
        raise BrowserUseError("Native screenshot returned no image", code="BU_SCREENSHOT_EMPTY")
    width, height = _png_size(data)
    if width is None or height is None:
        raise BrowserUseError(
            "Native screenshot did not return a PNG with readable dimensions",
            code=ERR_SCREENSHOT_DIMENSION,
        )
    output = Path(path).expanduser().resolve() if path is not None else default_screenshot_path()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)
    if show:
        emit_image(
            data,
            tag=tag,
            width=width,
            height=height,
            scope="element" if clip else "viewport",
        )
    return ScreenshotPath(str(output), width, height)


def _valid_download_url(value):
    try:
        parsed = urlsplit(str(value).strip())
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            raise ValueError
    except ValueError as exc:
        raise BrowserUseError("download target must be a ref or absolute HTTP(S) URL", code=ERR_DOWNLOAD_TARGET) from exc
    return str(value).strip()


def _completed_download_record(record):
    if not isinstance(record, dict):
        raise BrowserUseError(
            "download returned an invalid result; no completed file is available",
            code=ERR_DOWNLOAD_BLOCKED,
        )
    state = str(record.get("state") or "unknown")
    if state != "completed":
        raise BrowserUseError(
            f"download did not complete (state={state}); no path may be used",
            code=ERR_DOWNLOAD_BLOCKED,
        )
    invalid_fields = [
        field for field in ("guid", "url", "filename", "path", "source")
        if not isinstance(record.get(field), str) or not record[field]
    ]
    byte_count = record.get("bytes")
    if (
        not isinstance(byte_count, int)
        or isinstance(byte_count, bool)
        or byte_count < 0
    ):
        invalid_fields.append("bytes")
    path = str(record.get("path") or "")
    if (
        path.lower().endswith(".crdownload")
        or not os.path.isabs(path)
        or not os.path.isfile(path)
    ):
        invalid_fields.append("path")
    if invalid_fields:
        raise BrowserUseError(
            "download reported completion without a valid final record "
            f"({', '.join(sorted(set(invalid_fields)))}); no path may be used",
            code=ERR_DOWNLOAD_BLOCKED,
        )
    return record


@public_tool("download", action=True)
def download(ref_or_url, *, filename=None, timeout=30.0):
    """Wait for a direct download and return its validated final file record."""
    try:
        if isinstance(timeout, bool):
            raise TypeError
        timeout = float(timeout)
    except (TypeError, ValueError) as error:
        raise coded_error(
            TypeError, ERR_DOWNLOAD_TIMEOUT, "download timeout must be seconds in [0, 180]"
        ) from error
    timeout = _bounded_number(
        timeout, name="download timeout", minimum=0, maximum=180,
        code=ERR_DOWNLOAD_TIMEOUT,
    )
    if filename is not None and not isinstance(filename, str):
        raise coded_error(
            TypeError, ERR_DOWNLOAD_TARGET, "download filename must be a string or None"
        )
    value = str(ref_or_url).strip()
    if urlsplit(value).scheme and not is_node_ref(value):
        url = value
    else:
        CLIENT.require_feature("download", "elementRead")
        try:
            result = _read_element("download", ref_or_url, ("href", "src"))
        except BaseException as error:
            if _is_ref_error(error):
                raise BrowserUseError(
                    "download target must be a valid ref with href/src or an absolute HTTP(S) URL",
                    code=ERR_DOWNLOAD_TARGET,
                ) from error
            raise
        url = (result.get("href") or result.get("src")) if isinstance(result, dict) else ""
    record = _native.direct_download(_valid_download_url(url), filename, timeout)
    return _completed_download_record(record)


@public_tool("wait_for_download")
def wait_for_download(timeout=30.0):
    return _seed.call_direct("wait_for_download", timeout=timeout)


@public_tool("js", action=True)
def js(expression, target_id=None):
    page = CLIENT.ensure_page(target_id)
    result, _ = CLIENT.invoke(
        "js", "page.evaluate",
        page_id=page.id,
        params={"script": str(expression), "timeoutMs": 15000},
        mutating=True,
    )
    return _json_payload(result)


@public_tool("cdp", action=True)
def cdp(method, session_id=None, **params):
    return _native.raw_cdp(method, session_id=session_id, **params)


@public_tool("iframe_target")
def iframe_target(url_substr):
    return _native.iframe_target(url_substr)


@public_tool("upload_file", action=True)
def upload_file(selector, path):
    _set_file_input_files("upload_file", selector, path)
    return None


def _normalize_upload_files(path):
    if path is None:
        raise BrowserUseError(
            "upload path must be non-empty",
            code=ERR_READ_TARGET,
        )
    if isinstance(path, (str, bytes, os.PathLike)):
        items = [path]
    elif isinstance(path, (list, tuple)):
        items = list(path)
        if not items:
            raise BrowserUseError(
                "upload path list must be non-empty",
                code=ERR_READ_TARGET,
            )
    else:
        raise BrowserUseError(
            "upload path must be a file path or a non-empty list",
            code=ERR_READ_TARGET,
        )

    files = []
    for item in items:
        if item is None:
            raise BrowserUseError(
                "upload path item must be non-empty",
                code=ERR_READ_TARGET,
            )
        try:
            file_path = os.fsdecode(os.fspath(item))
        except TypeError as error:
            raise BrowserUseError(
                "upload path item must be a filesystem path",
                code=ERR_READ_TARGET,
            ) from error
        if not file_path:
            raise BrowserUseError(
                "upload path item must be non-empty",
                code=ERR_READ_TARGET,
            )
        absolute = os.path.abspath(file_path)
        if not os.path.isfile(absolute):
            raise BrowserUseError(
                f"upload file does not exist: {absolute}",
                code=ERR_READ_TARGET,
            )
        files.append(absolute)
    return files


def _set_file_input_files(tool: str, selector, path, *, page_id: str | None = None):
    selector_text = str(selector or "")
    if not selector_text:
        raise BrowserUseError("file input selector must be non-empty", code=ERR_READ_TARGET)
    files = _normalize_upload_files(path)
    document = _native.send_cdp(tool, "DOM.getDocument", {"depth": 0}, page_id=page_id)
    root = (document.get("root") or {}).get("nodeId")
    if not root:
        raise BrowserUseError("file input document root was not found", code=ERR_READ_TARGET)
    match = _native.send_cdp(
        tool,
        "DOM.querySelector",
        {"nodeId": root, "selector": selector_text},
        page_id=page_id,
    )
    node_id = match.get("nodeId")
    if not node_id:
        raise BrowserUseError("file input selector did not match", code=ERR_READ_TARGET)
    _native.send_cdp(
        tool,
        "DOM.setFileInputFiles",
        {"nodeId": node_id, "files": files},
        page_id=page_id,
    )


@public_tool("install_executor")
def install_executor(executor, *, strict=False):
    return _install_executor(executor, strict=strict)


@public_tool("make_ref")
def make_ref(node):
    return _make_ref(node)


PUBLIC_FUNCTIONS = {
    name: value for name, value in globals().items() if name in PUBLIC_TOOLS and callable(value)
}
if frozenset(PUBLIC_FUNCTIONS) != PUBLIC_TOOLS:
    raise RuntimeError(
        "AhaBUA public handler coverage mismatch: "
        f"missing={PUBLIC_TOOLS - frozenset(PUBLIC_FUNCTIONS)!r} "
        f"extra={frozenset(PUBLIC_FUNCTIONS) - PUBLIC_TOOLS!r}"
    )


__all__ = [
    *sorted(PUBLIC_TOOLS),
    "BrowserRefError", "BrowserSessionError", "BrowserUseError", "CssLocator", "CssLocatorList",
    "ElementTarget",
    "Ref", "RefList",
    "ContentBlock", "ContentExtractionResult", "FindResult", "ObservationNode", "ReadAllResult",
    "SnapshotResult",
    "ScreenshotPath", "StaleRefError", "TabRecord",
    "ERR_REF_STALE", "ERR_REF_SHAPE", "ERR_SESSION_STALE",
    "ERR_WAIT_SECONDS", "ERR_NAV_WAIT", "ERR_SELECT_NO_OPTION", "ERR_SELECT_AMBIGUOUS",
    "ERR_DOWNLOAD_BLOCKED", "ERR_DOWNLOAD_TIMEOUT", "ERR_DOWNLOAD_TARGET",
    "ERR_READ_FIELDS", "ERR_READ_LIMIT", "ERR_READ_TARGET", "ERR_EXPORT_LIMIT",
    "ERR_EXTRACT_TYPES",
    "ERR_COORDINATE_SPACE", "ERR_SCROLL_DIRECTION", "ERR_SCROLL_AMOUNT",
    "css", "get_css_locator",
]

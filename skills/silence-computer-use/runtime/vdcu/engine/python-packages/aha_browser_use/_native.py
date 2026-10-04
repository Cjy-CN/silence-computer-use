"""Native command 1-14 compatibility and the explicitly allowlisted CDP seam."""

from __future__ import annotations

import time
from typing import Any

from ._common import BrowserSessionError, BrowserUseError, tab_record
from ._transport import CLIENT


_CDP_ALLOWLIST = {
    "page_info": {"Page.getLayoutMetrics", "Page.getNavigationHistory"},
    "resync": {"Runtime.evaluate", "Page.bringToFront"},
    "click_and_handle_dialog": {"Runtime.evaluate"},
    "screenshot": {"Runtime.evaluate"},
    "handle_dialog": {"Page.handleJavaScriptDialog"},
    "iframe_target": {"Target.getTargets"},
    "upload": {"DOM.getDocument", "DOM.querySelector", "DOM.setFileInputFiles"},
    "upload_file": {"DOM.getDocument", "DOM.querySelector", "DOM.setFileInputFiles"},
}
_CDP_MUTATIONS = {
    "Page.bringToFront", "Page.handleJavaScriptDialog", "Input.dispatchKeyEvent",
    "Input.insertText", "DOM.setFileInputFiles",
}
_NETWORK_IDLE_POLL_INTERVAL = 0.1
_PAGE_SESSION_PREFIX = "gac-page:"


# Aha page IDs are the public tab IDs, but they are not Chrome TargetIDs.
# Keep the old GAC page-session convention so switch_tab() can return a
# session-like value without trying to attach Chrome to a non-Chrome target.
def _page_session(page_id: str) -> str:
    return _PAGE_SESSION_PREFIX + str(page_id)


def _page_from_session(session_id: str | None) -> str | None:
    if isinstance(session_id, str) and session_id.startswith(_PAGE_SESSION_PREFIX):
        return session_id[len(_PAGE_SESSION_PREFIX):]
    return None


def _find_page_by_id(page_id: str | None):
    target_id = str(page_id or "")
    if not target_id:
        return None
    for page in CLIENT.pages():
        if page.id == target_id:
            return page
    return None


def _target_filter_accepts(info: dict[str, Any], filters: Any) -> bool:
    if not isinstance(filters, list) or not filters:
        return True
    included_types = {
        item.get("type")
        for item in filters
        if isinstance(item, dict) and item.get("type") and not item.get("exclude")
    }
    excluded_types = {
        item.get("type")
        for item in filters
        if isinstance(item, dict) and item.get("type") and item.get("exclude")
    }
    target_type = info.get("type")
    if target_type in excluded_types:
        return False
    return not included_types or target_type in included_types


def _target_infos(params: dict[str, Any]) -> dict[str, Any]:
    pages = CLIENT.pages()
    selected_page = CLIENT._find_page(pages=pages)
    selected_id = selected_page.id if selected_page is not None else None
    infos = [
        {
            "targetId": page.id,
            "type": "page",
            "title": getattr(page, "title", ""),
            "url": getattr(page, "url", ""),
            "attached": page.id == selected_id,
            "canAccessOpener": False,
        }
        for page in pages
    ]
    if selected_id:
        try:
            raw = CLIENT.module.call(
                "send_cdp_command",
                selected_id,
                "",
                "Target.getTargets",
                dict(params),
                retryable=True,
            )
            for info in (raw or {}).get("targetInfos", []):
                if isinstance(info, dict) and info.get("type") != "page":
                    infos.append(info)
        except Exception:
            pass
    filters = params.get("filter")
    return {
        "targetInfos": [
            info for info in infos if _target_filter_accepts(info, filters)
        ]
    }


def _handle_target_command(
    method: str,
    params: dict[str, Any],
    session_id: str | None,
):
    if method == "Target.getTargets":
        return True, _target_infos(params)
    if method == "Target.createTarget":
        page_id = CLIENT.module.call(
            "create_page", str(params.get("url") or "about:blank"), "",
            retryable=False,
        )
        CLIENT.select_page(page_id)
        return True, {"targetId": page_id}

    target_id = str(params.get("targetId") or "")
    page = _find_page_by_id(target_id)
    if method == "Target.activateTarget" and page is not None:
        CLIENT.select_page(page.id)
        return True, {}
    if method == "Target.attachToTarget" and page is not None:
        return True, {"sessionId": _page_session(page.id)}
    if method == "Target.closeTarget" and page is not None:
        CLIENT.module.call("close_page", page.id, retryable=False)
        if getattr(CLIENT, "_selected_page_id", None) == page.id:
            CLIENT.clear_selected()
        return True, {"success": True}
    if method == "Target.detachFromTarget":
        target_session = str(params.get("sessionId") or session_id or "")
        if _page_from_session(target_session):
            return True, {}
    return False, None


def send_cdp(
    tool: str,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    page_id: str | None = None,
    session_id: str = "",
    retryable: bool | None = None,
):
    if method not in _CDP_ALLOWLIST.get(tool, set()):
        raise BrowserUseError(
            f"CDP method {method} is not an allowed route for aha_browser_use.{tool}",
            code="UNSUPPORTED",
        )
    session_page_id = _page_from_session(session_id)
    page = CLIENT.ensure_page(page_id or session_page_id)
    return CLIENT.module.call(
        "send_cdp_command", page.id,
        "" if session_page_id else session_id,
        method, dict(params or {}),
        retryable=(
            method not in _CDP_MUTATIONS if retryable is None else retryable
        ),
    )


def raw_cdp(method: str, session_id: str | None = None, **params):
    method = str(method)
    params = dict(params)
    handled, result = _handle_target_command(method, params, session_id)
    if handled:
        return result
    session_page_id = _page_from_session(session_id)
    page = CLIENT.ensure_page(session_page_id)
    return CLIENT.module.call(
        "send_cdp_command", page.id,
        "" if session_page_id else str(session_id or ""),
        method, params,
        retryable=False,
    )


def new_tab(url: str = "about:blank"):
    page_id = CLIENT.module.call("create_page", str(url), "", retryable=False)
    CLIENT.select_page(page_id)
    return page_id


def list_tabs(include_chrome: bool = False):
    del include_chrome
    return [
        tab_record({
            "targetId": page.id,
            "url": getattr(page, "url", ""),
            "title": getattr(page, "title", ""),
            "status": getattr(getattr(page, "status", None), "name", ""),
        })
        for page in CLIENT.pages()
    ]


def current_tab():
    page = CLIENT.ensure_page()
    return tab_record({"targetId": page.id, "url": page.url, "title": page.title})


def switch_tab(target):
    if isinstance(target, bool):
        error = TypeError("target must be a tab record, target id, or list_tabs() index")
        error.code = "BU_TAB_TARGET"
        raise error
    if isinstance(target, int):
        tabs = list_tabs()
        try:
            target = tabs[target]
        except IndexError as exc:
            error = IndexError("tab index is out of range; call list_tabs() first")
            error.code = "BU_TAB_INDEX"
            raise error from exc
    if isinstance(target, dict):
        target = target.get("targetId") or target.get("target_id") or target.get("id")
    page = CLIENT.select_page(str(target or ""))
    # select_page() is the Native page-owner operation.  Calling
    # Target.attachToTarget here would treat the Aha page id as a Chrome
    # target id and fail with "No target with given id found".
    return _page_session(page.id)


def close_tab(target_id=None):
    if isinstance(target_id, dict):
        target_id = target_id.get("targetId") or target_id.get("target_id") or target_id.get("id")
    page = CLIENT.ensure_page(str(target_id) if target_id else None)
    CLIENT.module.call("close_page", page.id, retryable=False)
    CLIENT.clear_selected()
    remaining = list_tabs()
    if remaining:
        switch_tab(remaining[-1])
    return remaining


def ensure_real_tab():
    return CLIENT.ensure_page().id


def page_size(page_id: str | None = None):
    page = CLIENT.ensure_page(page_id)
    return CLIENT.module.call("get_page_size", page.id)


def console_messages(level=None):
    page = CLIENT.ensure_page()
    output = []
    for item in CLIENT.module.call("drain_console_logs", page.id):
        record = {
            "level": getattr(item, "type", ""),
            "text": getattr(item, "text", ""),
            "timestamp": getattr(item, "timestamp", None),
        }
        if level is None or record["level"] == level:
            output.append(record)
    return output


def _network_record(request_id, item):
    return {
        "url": getattr(item, "url", ""),
        "method": getattr(item, "method", ""),
        "status": getattr(item, "status", ""),
        "request_id": str(request_id),
        "create_time": getattr(item, "create_time", None),
        "update_time": getattr(item, "update_time", None),
    }


def _drain_network_requests(page=None):
    page = page or CLIENT.ensure_page()
    values = CLIENT.module.call("drain_network_requests", page.id)
    return {
        str(request_id): _network_record(request_id, item)
        for request_id, item in values.items()
    }


def network_requests(url_contains=None):
    output = []
    for item in _drain_network_requests().values():
        record = {
            "url": item["url"],
            "method": item["method"],
            "status": item["status"],
            "request_id": item["request_id"],
        }
        if url_contains is None or str(url_contains) in record["url"]:
            output.append(record)
    return output


def dialogs():
    page = CLIENT.ensure_page()
    return CLIENT.module.call("get_page_dialogs", page.id)


def wait_for_network_idle(timeout: float = 10.0, idle_ms: int = 500):
    deadline = time.monotonic() + max(0.0, float(timeout))
    idle_time = max(0.0, float(idle_ms)) / 1000.0
    page = CLIENT.ensure_page()
    inflight = set()
    baseline_inflight = set()
    request_states = {}
    last_activity = time.monotonic()
    first_sample = True
    while True:
        requests = _drain_network_requests(page)
        current = time.monotonic()
        first_empty_sample = first_sample and not requests
        if first_empty_sample:
            last_activity = current
        for request_id, request in requests.items():
            status = str(request.get("status") or "").lower()
            state = (
                status,
                request.get("create_time"),
                request.get("update_time"),
            )
            previous_state = request_states.get(request_id)
            if previous_state != state:
                request_states[request_id] = state
                last_activity = current
                if first_sample and status == "inflight":
                    baseline_inflight.add(request_id)
                    continue
                baseline_inflight.discard(request_id)
            if status == "inflight":
                if request_id not in baseline_inflight:
                    inflight.add(request_id)
            else:
                inflight.discard(request_id)
                baseline_inflight.discard(request_id)
        if (
            not first_empty_sample
            and not inflight
            and current - last_activity >= idle_time
        ):
            return True
        if current >= deadline:
            break
        first_sample = False
        wait_time = min(_NETWORK_IDLE_POLL_INTERVAL, deadline - current)
        if not inflight:
            wait_time = min(
                wait_time,
                max(last_activity + idle_time - current, 0.0),
            )
        time.sleep(max(wait_time, 0.0))
    raise BrowserUseError("network did not become idle before timeout", code="BU_NAV_WAIT_TIMEOUT")


def wait_for_download(timeout: float = 30.0):
    deadline = time.monotonic() + max(0.0, float(timeout))
    while time.monotonic() <= deadline:
        record = CLIENT.module.call("take_next_download")
        if record is not None:
            return record
        time.sleep(0.1)
    raise BrowserUseError("no browser download completed before timeout", code="BU_DOWNLOAD_TIMEOUT")


def iframe_target(url_substr):
    response = send_cdp("iframe_target", "Target.getTargets") or {}
    target_infos = response.get("targetInfos") or []
    needle = str(url_substr)
    for target in target_infos:
        if not isinstance(target, dict):
            continue
        if target.get("type") == "iframe" and needle in str(target.get("url") or ""):
            target_id = str(target.get("targetId") or "")
            return target_id or None
    return None


def direct_download(url: str, filename: str | None, timeout: float):
    page = CLIENT.ensure_page()
    guid = CLIENT.module.call(
        "start_direct_download", page.id, str(url), str(filename or ""), retryable=False
    )
    deadline = time.monotonic() + timeout
    while True:
        record = CLIENT.module.call("take_direct_download", guid)
        # Native exposes progress without consuming it. Keep polling this GUID;
        # the public API validates terminal or malformed records before use.
        if record is not None and (
            not isinstance(record, dict) or record.get("state") != "in_progress"
        ):
            return record
        if time.monotonic() >= deadline:
            break
        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
    raise BrowserUseError("direct download did not complete before timeout", code="BU_DOWNLOAD_TIMEOUT")


__all__ = [
    "close_tab", "console_messages", "current_tab", "dialogs", "direct_download",
    "ensure_real_tab", "iframe_target", "list_tabs", "network_requests", "new_tab", "page_size",
    "raw_cdp", "send_cdp", "switch_tab", "wait_for_download", "wait_for_network_idle",
]

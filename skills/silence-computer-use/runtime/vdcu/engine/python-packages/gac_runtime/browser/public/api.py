"""Public Browser Use API assembled from focused implementation modules."""

from ..domain.core import *
from ..services.actions import *
from ..services.devtools import *
from ..services.input import *
from ..services.keyboard import *
from ..services.navigation import *
from ..services.tabs import *
from ..services.snapshot import *
from ..services.reads import *
from ..services.diagnostics import *


def _env_snapshot():
    """Best-effort browser state for the host-side ``[change phase=…]`` baseline.

    ``collected`` says whether the probe reached Chrome at all. An unreachable
    endpoint otherwise looks exactly like a browser with no tabs, and the host's
    diff then reports every tab as closed — a change a barrier would act on, out
    of a probe that never looked.
    """
    collected = True
    try:
        tabs = _bh.list_tabs(include_chrome=False)
    except Exception:
        tabs = []
        collected = False
    try:
        current = _bh.current_tab()
    except Exception:
        current = {}
        collected = False
    return {
        "collected": collected,
        "current": {
            "target_id": current.get("targetId") or current.get("target_id") or "",
            "url": current.get("url") or "",
            "title": current.get("title") or "",
        },
        "tabs": [
            {
                "target_id": tab.get("targetId") or tab.get("target_id") or "",
                "url": tab.get("url") or "",
                "title": tab.get("title") or "",
            }
            for tab in tabs
        ],
    }

# Apply the runtime guard/audit last so it also covers helpers re-exported directly
# from gac_runtime.browser.  Observation functions write to the typed observation
# channel; actions stay quiet on success and enter the shared audit stream.
_ACTION_APIS = (
    "click", "hover", "type", "select", "upload",
    "handle_dialog", "click_and_handle_dialog",
    "click_xy", "left_double", "right_single", "drag", "scroll",
    "new_tab", "switch_tab", "close_tab", "ensure_real_tab", "resync",
    "hotkey", "press_key", "navigate", "download",
    "js", "cdp", "fill_input", "upload_file",
)
_OBSERVE_APIS = (
    "page_info", "list_tabs", "current_tab",
    "snapshot", "get", "text",
    "find", "get_page_text", "console_messages", "network_requests", "http_get",
    "read_all", "content_export", "wait_for_download",
    "iframe_target", "wait_for_load", "wait_for_element", "wait_for_network_idle",
)
for _api_name in _ACTION_APIS:
    globals()[_api_name] = _wrap_api(_api_name, globals()[_api_name], action=True)
for _api_name in _OBSERVE_APIS:
    globals()[_api_name] = _wrap_api(_api_name, globals()[_api_name], action=False)
for _api_name in ("screenshot", "wait"):
    globals()[_api_name] = _wrap_trace_api(_api_name, globals()[_api_name])


# tabs: keep our verb+noun names (list_tabs / new_tab / close_tab) — no mcp-style alias.
# JS execution: use bu.js rather than a second host-specific script tool.


__all__ = [
    # observe (primary)
    "snapshot", "text", "get", "http_get",
    "find", "get_page_text", "read_all", "content_export", "console_messages", "network_requests",
    # page and tab context
    "page_info", "new_tab", "list_tabs", "switch_tab", "current_tab",
    "close_tab", "ensure_real_tab", "resync",
    # ref actions (the default path)
    "click", "type", "select", "upload", "hover",
    "handle_dialog", "click_and_handle_dialog",
    # coordinate actions (canvas / no-DOM escape hatch)
    "click_xy", "left_double", "right_single", "drag", "scroll",
    # navigation and waits
    "navigate", "wait", "wait_for_load", "wait_for_element",
    "wait_for_network_idle",
    # keyboard
    "hotkey", "press_key",
    # screenshot
    "screenshot",
    # downloads
    "download", "wait_for_download",
    # low-level entries and helpers
    "js", "cdp", "fill_input", "iframe_target",
    "upload_file", "install_executor",
    # result helpers
    "Ref", "RefList", "ScreenshotPath", "TabRecord", "make_ref",
    # stable error codes + the exceptions that carry them
    "BrowserUseError", "BrowserSessionError", "BrowserRefError",
    "UnscopedRefError", "StaleRefError",
    "ERR_REF_STALE", "ERR_REF_UNSCOPED", "ERR_REF_SHAPE", "ERR_SESSION_STALE",
    "ERR_NAV_WAIT", "ERR_SELECT_NO_OPTION", "ERR_SELECT_AMBIGUOUS",
    "ERR_DOWNLOAD_BLOCKED", "ERR_DOWNLOAD_TIMEOUT", "ERR_DOWNLOAD_TARGET",
    "ERR_READ_FIELDS", "ERR_READ_LIMIT", "ERR_READ_TARGET", "ERR_EXPORT_LIMIT",
]


_API_SUGGESTIONS = {
    "get_url": ('bu.page_info()["url"]', 'bu.current_tab()["url"]'),
    "get_current_url": ('bu.page_info()["url"]', 'bu.current_tab()["url"]'),
    "get_title": ('bu.page_info()["title"]', 'bu.current_tab()["title"]'),
    "get_tabs": ('bu.list_tabs()',),
    "evaluate": ('bu.js(code)',),
    "open_new_tab": ('bu.new_tab(url)',),
    "press": ('bu.press_key(key)',),
    "scroll_down": ('bu.scroll(x, y, "down")',),
    "switch_to_tab": ('bu.switch_tab(index_or_tab)',),
    "get_text": ('bu.text(ref)', 'bu.get_page_text()'),
}


def _api_signature(name):
    value = globals().get(name)
    try:
        return "bu.%s%s" % (name, _inspect.signature(value))
    except (TypeError, ValueError):
        return "bu.%s" % name


def __getattr__(name):
    """Fail unknown public API calls with stable, actionable suggestions."""
    if str(name).startswith("__"):
        raise AttributeError(name)
    suggestions = list(_API_SUGGESTIONS.get(str(name), ()))
    if not suggestions:
        public = [
            item
            for item in __all__
            if _inspect.isroutine(globals().get(item))
        ]
        suggestions = [
            _api_signature(item)
            for item in _difflib.get_close_matches(str(name), public, n=3, cutoff=0.5)
        ]
    message = "gac_runtime: unknown public API bu.%s." % name
    if suggestions:
        message += " Did you mean: %s?" % ", ".join(suggestions)
    else:
        message += " Read the cngc-bu-browser skill for the supported API list."
    raise _coded_error(AttributeError, "BU_UNKNOWN_API", message)

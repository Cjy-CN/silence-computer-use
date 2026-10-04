"""Low-level tab metadata, attachment, and lifecycle primitives."""

from .config import INTERNAL
from .config import MARK_TABS
from .transport import cdp
from .transport import send as _send


def page_info():
    """Return page URL, title, viewport, scroll, and document dimensions.

    If a native dialog is open, return its metadata instead because the page's
    JavaScript thread is blocked until the dialog is handled.
    """
    dialog = _send({"meta": "pending_dialog"}).get("dialog")
    if dialog:
        return {"dialog": dialog}
    return _send({"meta": "page_info"})


def _is_agent_startup_placeholder(title, url):
    url = str(url or "")
    return str(title or "").startswith("Starting agent ") and (
        url in ("", "about:blank") or url.startswith("about:blank#")
    )


def list_tabs(include_chrome=True):
    out = []
    targets = _send({"meta": "list_pages"}).get("pages") or []
    for target in targets:
        if target.get("type", "page") != "page":
            continue
        url = target.get("url", "")
        if _is_agent_startup_placeholder(target.get("title", ""), url):
            continue
        if not include_chrome and url.startswith(INTERNAL):
            continue
        out.append(
            {
                "targetId": target["targetId"],
                "target_id": target["targetId"],
                "title": target.get("title", ""),
                "url": url,
            }
        )
    return out


def current_tab():
    result = _send({"meta": "current_tab"})
    return {
        "targetId": result["targetId"],
        "target_id": result["targetId"],
        "url": result["url"],
        "title": result["title"],
    }


def _mark_tab():
    """Prepend a marker to the tab title when configured."""
    if not MARK_TABS:
        return
    try:
        cdp(
            "Runtime.evaluate",
            expression=(
                "if(!document.title.startsWith('\U0001F434'))"
                "document.title='\U0001F434 '+document.title"
            ),
        )
    except Exception:
        pass


def switch_tab(target):
    if isinstance(target, (list, tuple)):
        if len(target) != 1:
            raise ValueError(
                f"switch_tab expected one tab, got {len(target)}; choose one list item"
            )
        target = target[0]
    if isinstance(target, dict):
        target_id = target.get("targetId") or target.get("target_id")
    else:
        target_id = target
    response = _send({"meta": "switch_page", "page_id": target_id})
    _mark_tab()
    return response.get("session_id") or ""


def new_tab(url="about:blank"):
    return _send({"meta": "create_page", "url": url})["targetId"]


def close_tab(target=None):
    """Close a tab, defaulting to the currently attached tab."""
    if isinstance(target, dict):
        target_id = target.get("targetId") or target.get("target_id")
    else:
        target_id = target
    if target_id is None:
        target_id = current_tab()["targetId"]
    _send({"meta": "close_page", "page_id": target_id})


def ensure_real_tab():
    """Switch to a real page, creating a neutral data page when none exists."""
    tabs = list_tabs(include_chrome=False)
    if not tabs:
        new_tab("data:text/html,<title>Browser Use</title>")
        return current_tab()
    try:
        current = current_tab()
        if current["url"] and not current["url"].startswith(INTERNAL):
            return current
    except Exception:
        pass
    switch_tab(tabs[0]["targetId"])
    return tabs[0]

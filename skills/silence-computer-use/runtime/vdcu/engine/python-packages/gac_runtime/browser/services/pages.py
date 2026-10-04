"""Page navigation, tab lifecycle, synchronization, and waits."""

from ..domain.core import *
from .navigation import navigate


def page_info():
    """{url,title,w,h,sx,sy,pw,ph} — viewport + scroll + page size."""
    return _bh.page_info()


def list_tabs(include_chrome=False):
    """List real page tabs (chrome:// internal pages excluded by default)."""
    return [_tab_record(tab) for tab in _bh.list_tabs(include_chrome=include_chrome)]


def switch_tab(target):
    """Switch to a tab record/target id, or an index into ``list_tabs()``."""
    if isinstance(target, bool):
        raise _coded_error(
            TypeError,
            "BU_TAB_TARGET",
            "gac_runtime.switch_tab: target must be a tab record, target "
            "id, or integer list_tabs() index.",
        )
    if isinstance(target, int):
        tabs = list_tabs()
        if not -len(tabs) <= target < len(tabs):
            raise _coded_error(
                IndexError,
                "BU_TAB_INDEX",
                "gac_runtime.switch_tab: tab index is out of range. Call "
                "bu.list_tabs(), inspect the returned list, then pass a valid index.",
            )
        target = tabs[target]
    return _backend_switch_tab(target)


def current_tab():
    """The tab BU is attached to, {targetId,url,title}.

    This is the attached CDP target. A popup or external tab/window change can move
    the visible page without updating that attachment; call ``bu.resync()`` when
    the current target is no longer the visible page.
    """
    return _tab_record(_bh.current_tab())


def close_tab(target_id=None):
    """Close a tab by targetId (default: the current tab). Returns the remaining tabs.

    Pass a targetId from ``list_tabs()``; omit to close the tab you're on. Closing
    the ATTACHED tab kills the CDP session, so this re-attaches deterministically —
    the tab that took the closed one's position, or the last remaining tab — rather
    than leaving the daemon to bind whichever page it finds first. Every ref minted
    before the close belongs to the closed document and is refused afterwards.
    """
    if isinstance(target_id, dict):
        tid = target_id.get("targetId") or target_id.get("target_id") or target_id.get("id")
    elif isinstance(target_id, (list, tuple)):
        tid = _tab_record(target_id[0]).target_id if len(target_id) == 1 else None
    else:
        tid = target_id
    before = [_tab_record(tab) for tab in _bh.list_tabs(include_chrome=False)]
    try:
        attached = _bh.current_tab().get("targetId") or ""
    except Exception:  # noqa: BLE001 — a dead session still allows an explicit close
        attached = ""
    tid = tid or attached
    with _dispatch("close_tab", [_audit_value(tid)]):
        _bh.close_tab(tid)
    if tid == attached:
        closed_at = next((i for i, tab in enumerate(before) if tab.target_id == tid), None)
        remaining = [tab for tab in before if tab.target_id != tid]
        if remaining:
            index = 0 if closed_at is None else min(closed_at, len(remaining) - 1)
            with _dispatch("switch_tab", [_audit_value(remaining[index].target_id)]):
                _bh.switch_tab(remaining[index].target_id)
    return [_tab_record(tab) for tab in _bh.list_tabs(include_chrome=False)]


#: Which tab is on screen: exactly one page per Chrome window reports
#: visibilityState 'visible', and document.hasFocus() breaks ties across windows.
_TAB_VISIBILITY_JS = (
    "(() => JSON.stringify({v: document.visibilityState, f: document.hasFocus()}))()"
)


def resync(*, timeout=10.0):
    """Re-attach BU to the tab Chrome is actually showing, then report the context.

    A target=_blank popup or an external tab/window change can leave BU attached to
    a page that is no longer visible. Run this after such a change or a
    BU_SESSION_STALE error: list_tabs -> pick the visible tab -> switch_tab -> wait
    for readiness.

    Returns the fresh tab record. Refs from before a resync that changed tabs are
    stale by construction — take a new bu.snapshot() before acting.
    """
    tabs = [_tab_record(tab) for tab in _bh.list_tabs(include_chrome=False)]
    if not tabs:
        raise BrowserUseError(
            "gac_runtime.resync: Chrome has no real page tab. Open one with "
            "bu.navigate(url) before acting."
        )
    visible = None
    focused = None
    for tab in tabs:
        try:
            raw = _bh.js(_TAB_VISIBILITY_JS, target_id=tab.target_id)
            state = _json.loads(raw) if isinstance(raw, str) else (raw or {})
        except Exception:  # noqa: BLE001 — an unreachable tab simply is not the answer
            continue
        if not isinstance(state, dict):
            continue
        if state.get("v") == "visible":
            visible = visible or tab
            if state.get("f"):
                focused = tab
                break
    chosen = focused or visible or tabs[0]
    with _dispatch("switch_tab", [_audit_value(chosen.target_id)]):
        _bh.switch_tab(chosen.target_id)
    _bh.wait_for_load(timeout=timeout)
    record = _tab_record(_bh.current_tab())
    state = _scope_state(mint=True)
    # Printed, not observed: this is a sentence about where the library landed,
    # not a body with refs or pixels in it. See _emit_notice.
    _emit_notice(
        "resync attached=%s url=%s title=%s scope=%s tabs=%d"
        % (record.target_id, record.get("url") or "", record.get("title") or "",
           state.get("scope") or _NO_DOC_SCOPE, len(tabs))
    )
    return record

def wait(seconds=3):
    """Sleep `seconds` (let the page settle). Prefer wait_for_load / wait_for_element."""
    if isinstance(seconds, bool):
        raise _coded_error(
            TypeError,
            "BU_WAIT_SECONDS",
            "gac_runtime.wait: seconds must be a number in [0, 180], "
            "not a boolean.",
        )
    try:
        value = float(seconds)
    except (TypeError, ValueError) as exc:
        raise _coded_error(
            TypeError,
            "BU_WAIT_SECONDS",
            "gac_runtime.wait: seconds must be a number in [0, 180]. "
            "Millisecond values must be divided by 1000.",
        ) from exc
    if not _math.isfinite(value) or not 0 <= value <= _MAX_WAIT_SECONDS:
        raise _coded_error(
            ValueError,
            "BU_WAIT_SECONDS",
            "gac_runtime.wait: seconds must be in [0, 180]. Millisecond "
            "values such as 10000 must be divided by 1000.",
        )
    _time.sleep(value)


def wait_for_load(timeout=15.0, since=None):
    """Wait for page load with a timeout expressed in seconds."""
    if isinstance(timeout, bool):
        raise _coded_error(
            TypeError,
            "BU_WAIT_SECONDS",
            "gac_runtime.wait_for_load: timeout must be seconds in "
            "[0, 180], not a boolean.",
        )
    try:
        value = float(timeout)
    except (TypeError, ValueError) as exc:
        raise _coded_error(
            TypeError,
            "BU_WAIT_SECONDS",
            "gac_runtime.wait_for_load: timeout must be seconds in [0, 180].",
        ) from exc
    if not _math.isfinite(value) or not 0 <= value <= _MAX_WAIT_SECONDS:
        raise _coded_error(
            ValueError,
            "BU_WAIT_SECONDS",
            "gac_runtime.wait_for_load: timeout must be seconds in [0, 180]. "
            "Values such as 10000 or 15000 are milliseconds.",
        )
    return _backend_wait_for_load(timeout=value, since=since)

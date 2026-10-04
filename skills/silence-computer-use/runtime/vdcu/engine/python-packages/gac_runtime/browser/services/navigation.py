"""Navigation and wait policies."""

from ..domain.core import *


def wait(seconds=3):
    """Sleep `seconds`; prefer wait_for_load or wait_for_element."""
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
    return _bh.wait_for_load(timeout=value, since=since)


def wait_for_element(selector, timeout=10.0, visible=False):
    """Wait until a selector matches, optionally requiring visibility."""
    return _bh.wait_for_element(selector, timeout=timeout, visible=visible)


def wait_for_network_idle(timeout=10.0, idle_ms=500):
    """Wait until observed network requests finish and remain idle."""
    return _bh.wait_for_network_idle(timeout=timeout, idle_ms=idle_ms)


def navigate(target):
    """Navigate to a URL, or perform the literal back/forward/reload action."""
    normalized = str(target).strip().lower()
    if normalized == "reload":
        before = _bh.document_epoch()
        with _dispatch("navigate", [normalized]):
            _bh.cdp("Page.reload")
        if not _bh.wait_for_load(since=before):
            raise BrowserUseError(
                "gac_runtime: reload did not produce a new document within "
                "the wait window. The page is NOT known to have reloaded: "
                "re-observe with bu.page_info() / bu.snapshot() before acting.",
                code=ERR_NAV_WAIT,
            )
        return

    if normalized in ("back", "forward"):
        before = _bh.document_epoch()
        with _dispatch("navigate", [normalized]):
            _bh.cdp(
                "Page.goBack" if normalized == "back" else "Page.goForward"
            )
        if not _bh.wait_for_load(since=before):
            raise BrowserUseError(
                f"gac_runtime: history {normalized} did not produce a new "
                "document within the wait window — there may be no such history entry, "
                "or the navigation is still in flight. The page is NOT known to have "
                "changed: re-observe with bu.page_info() / bu.snapshot() before acting.",
                code=ERR_NAV_WAIT,
            )
        return
    try:
        _bh.current_tab()
        has_tab = True
    except RuntimeError as exc:
        if "BU_NO_ACTIVE_PAGE" not in str(exc):
            raise
        has_tab = False
    with _dispatch("navigate", [_audit_value(target)]):
        return _bh.goto_url(target) if has_tab else _bh.new_tab(target)

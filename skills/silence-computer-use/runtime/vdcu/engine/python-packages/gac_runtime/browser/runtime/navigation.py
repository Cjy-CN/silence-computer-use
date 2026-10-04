"""Low-level document navigation and readiness primitives."""

import json
import time

from .cdp import js
from .transport import cdp
from .transport import drain_network_requests


_NETWORK_IDLE_POLL_INTERVAL = 0.1


def goto_url(url):
    return cdp("Page.navigate", url=url)


def wait(seconds=1.0):
    time.sleep(seconds)


def document_epoch():
    """Return the main frame's loaderId and URL."""
    tree = cdp("Page.getFrameTree").get("frameTree") or {}
    frame = (tree.get("frame") or {}) if isinstance(tree, dict) else {}
    return (frame.get("loaderId") or "", frame.get("url") or "")


def wait_for_load(timeout=15.0, since=None):
    """Wait for an optional document swap, then for readyState=complete."""
    deadline = time.monotonic() + timeout
    if since is not None:
        while time.monotonic() < deadline:
            if document_epoch() != since:
                break
            time.sleep(0.15)
        else:
            return False
    while time.monotonic() < deadline:
        if js("document.readyState") == "complete":
            return True
        time.sleep(0.3)
    return False


def wait_for_element(selector, timeout=10.0, visible=False):
    """Poll until querySelector(selector) matches, or the timeout expires."""
    if visible:
        check = (
            f"(()=>{{const e=document.querySelector({json.dumps(selector)});"
            f"if(!e)return false;"
            f"if(typeof e.checkVisibility==='function')"
            f"return e.checkVisibility({{checkOpacity:true,checkVisibilityCSS:true}});"
            f"const s=getComputedStyle(e);"
            f"return s.display!=='none'&&s.visibility!=='hidden'&&s.opacity!=='0'}})()"
        )
    else:
        check = f"!!document.querySelector({json.dumps(selector)})"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if js(check):
            return True
        time.sleep(0.3)
    return False


def wait_for_network_idle(timeout=10.0, idle_ms=500):
    """Wait for all observed requests to finish and remain quiet for ``idle_ms``."""
    deadline = time.monotonic() + timeout
    idle_time = max(float(idle_ms), 0.0) / 1000.0
    inflight = set()
    request_states = {}
    last_activity = time.monotonic()
    first_sample = True

    while True:
        requests = drain_network_requests()
        current = time.monotonic()
        first_empty_sample = first_sample and not requests
        if first_empty_sample:
            # The first IPC round trip may consume the whole idle window before
            # GAC publishes a just-started request. Start the quiet window after
            # that sample and require at least one follow-up drain.
            last_activity = current
        for request_id, request in requests.items():
            status = str(request.get("status") or "").lower()
            state = (
                status,
                request.get("create_time"),
                request.get("update_time"),
            )
            if request_states.get(request_id) != state:
                request_states[request_id] = state
                last_activity = current
            if status == "inflight":
                inflight.add(request_id)
            else:
                inflight.discard(request_id)

        if (
            not first_empty_sample
            and not inflight
            and current - last_activity >= idle_time
        ):
            return True
        if current >= deadline:
            return False

        first_sample = False
        wait_time = min(_NETWORK_IDLE_POLL_INTERVAL, deadline - current)
        if not inflight:
            wait_time = min(
                wait_time,
                max(last_activity + idle_time - current, 0.0),
            )
        time.sleep(max(wait_time, 0.0))

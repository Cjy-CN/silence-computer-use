"""Internal request bridge from runtime helpers to the configured backend."""

import time

from .. import backend


SOCK = backend.description()


def send(request, timeout=5.0):
    response = backend.request(request, timeout=timeout)
    if "error" in response:
        raise RuntimeError(response["error"])
    return response


def cdp(method, session_id=None, **params):
    return send(
        {"method": method, "params": params, "session_id": session_id}
    ).get("result", {})


def _capture_screenshot(params, *, max_dimension=0):
    return send(
        {
            "meta": "capture_screenshot",
            "params": params,
            "max_dimension": max_dimension,
        }
    )


def drain_console_logs():
    return send({"meta": "drain_console_logs"}).get("logs") or []


def drain_network_requests():
    return send({"meta": "drain_network_requests"}).get("requests") or {}


def pending_dialog():
    """Read a pending native JavaScript dialog without issuing page CDP."""
    return send({"meta": "pending_dialog"}).get("dialog")


def handle_javascript_dialog(*, accept=True, prompt_text=None, timeout=5.0):
    """Wait for and handle a dialog even if GAC metadata arrives late."""
    deadline = time.monotonic() + max(0.1, float(timeout))
    params = {"accept": bool(accept)}
    if prompt_text is not None:
        params["promptText"] = str(prompt_text)

    info = None
    while True:
        value = pending_dialog()
        if isinstance(value, dict):
            info = value
        try:
            cdp("Page.handleJavaScriptDialog", **params)
        except RuntimeError as exc:
            if "No dialog is showing" not in str(exc):
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            time.sleep(min(0.05, remaining))
            continue

        if info is not None:
            return info
        return {
            "is_open": True,
            "result": False,
            "type": "unknown",
            "message": "",
            "user_input": "",
            "default_prompt": "",
            "has_browser_handler": None,
        }


def wait_for_download(timeout=30.0):
    value = max(0.0, float(timeout))
    return send(
        {"meta": "wait_for_download", "timeout": value},
        timeout=max(5.0, value + 2.0),
    ).get("download")


def download_url(url, filename=None, timeout=30.0):
    value = max(0.0, float(timeout))
    return send(
        {
            "meta": "download_url",
            "url": str(url or ""),
            "filename": str(filename or ""),
            "timeout": value,
        },
        timeout=max(5.0, value + 2.0),
    ).get("download")

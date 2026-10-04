"""CLI bridge from ``sandbox.bash.run`` to the GAC Runtime cell protocol."""

from __future__ import annotations

import base64
import json
import sys
import traceback
from typing import Any

from . import debug_trace
from .cell import DEFAULT_FILENAME, run_code
from .envelope import (
    ENVELOPE_VERSION,
    observations_from_envelope,
    render_envelope,
)

def decode_request(encoded_request: str) -> tuple[str, str, int | None]:
    request_bytes = base64.b64decode(encoded_request, validate=True)
    request = json.loads(request_bytes.decode("utf-8"))
    if not isinstance(request, dict):
        raise TypeError("cell request must be a JSON object")
    code = request.get("code")
    filename = request.get("filename", DEFAULT_FILENAME)
    timeout_ms = request.get("timeout_ms")
    if not isinstance(code, str):
        raise TypeError("cell request field 'code' must be a string")
    if not isinstance(filename, str) or not filename:
        raise TypeError("cell request field 'filename' must be a non-empty string")
    if timeout_ms is not None and (
        not isinstance(timeout_ms, int)
        or isinstance(timeout_ms, bool)
        or timeout_ms <= 0
    ):
        raise TypeError("cell request field 'timeout_ms' must be a positive integer")
    return code, filename, timeout_ms


def _short_url(value: Any, limit: int = 140) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _diff_browser_environment(before: dict[str, Any], after: dict[str, Any]) -> str:
    previous = before.get("browser") or {}
    current = after.get("browser") or {}
    if current.get("collected") is False:
        return ""
    previous_tabs = {
        str(tab.get("target_id") or ""): tab
        for tab in previous.get("tabs") or []
        if isinstance(tab, dict) and tab.get("target_id")
    }
    current_tabs = {
        str(tab.get("target_id") or ""): tab
        for tab in current.get("tabs") or []
        if isinstance(tab, dict) and tab.get("target_id")
    }
    changes: list[str] = []
    opened = [current_tabs[key] for key in current_tabs if key not in previous_tabs]
    closed = [previous_tabs[key] for key in previous_tabs if key not in current_tabs]
    if opened:
        changes.append(
            "browser_opened: "
            + " · ".join(
                f"{tab.get('title') or '(untitled)'} [{_short_url(tab.get('url'))}]"
                for tab in opened
            )
        )
    if closed:
        changes.append(
            "browser_closed: "
            + " · ".join(str(tab.get("title") or "(untitled)") for tab in closed)
        )
    previous_page = previous.get("current") or {}
    current_page = current.get("current") or {}
    if current_page and current_page != previous_page:
        changes.append(
            "browser: %s [%s]"
            % (
                current_page.get("title") or "(untitled)",
                _short_url(current_page.get("url")),
            )
        )
    return " · ".join(changes)


def _image_references(execution: dict[str, Any]) -> list[dict[str, Any]]:
    """Keep image bytes out of stdout; the background host reads these paths."""

    images: list[dict[str, Any]] = []
    for image in execution.get("images") or []:
        if not isinstance(image, dict):
            continue
        path = str(image.get("path") or "")
        images.append({**image, "path": path, "data": path})
    return images


def _execute_request(encoded_request: str) -> tuple[dict[str, Any], str]:
    code, filename, timeout_ms = decode_request(encoded_request)
    execution = run_code(code, filename=filename, timeout_ms=timeout_ms)
    images = _image_references(execution)
    observations = observations_from_envelope(execution, images)
    in_cell_change = _diff_browser_environment(
        execution.get("env_before") or {},
        execution.get("env") or {},
    )
    changes = {"in_cell": in_cell_change} if in_cell_change else {}
    content, is_error = render_envelope(
        execution,
        images=images,
        observations=observations,
        env_changes=changes,
        timeout_ms=timeout_ms if execution.get("timed_out") else None,
    )
    profile = execution.get("gac_profile")
    profile_name = str(profile.get("name") or "") if isinstance(profile, dict) else ""
    return {"content": content, "is_error": is_error}, profile_name


def execute_request(encoded_request: str) -> dict[str, Any]:
    """Return a Tool Result whose image data temporarily contains file paths."""

    result, _ = _execute_request(encoded_request)
    return result


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    real_stdout = sys.stdout
    gac_profile = ""
    try:
        if len(args) != 1:
            raise ValueError("usage: python -m gac_runtime.runner <base64-json-request>")
        result, gac_profile = _execute_request(args[0])
    except BaseException as error:
        execution = {
            "v": ENVELOPE_VERSION,
            "ok": False,
            "stdout": "",
            "stderr": "",
            "exc": {
                "type": type(error).__name__,
                "message": str(error),
                "lineno": None,
                "traceback": traceback.format_exc().rstrip(),
            },
            "actions": [],
            "observations": [],
        }
        content, _ = render_envelope(execution)
        result = {
            "content": content,
            "is_error": True,
        }
    serialized_result = json.dumps(
        result,
        ensure_ascii=False,
        default=str,
        separators=(",", ":"),
    )
    serialized_stdout = serialized_result + "\n"
    debug_trace.record_llm_result(
        result,
        serialized_stdout,
        gac_profile=gac_profile,
    )
    real_stdout.write(serialized_stdout)
    real_stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Cell result wire format and model-facing rendering.

This module renders deterministic ``[result]``/``[output]``/``[obs]``/
``[error]`` labels for consumers. Execution belongs to :mod:`gac_runtime.cell`;
the direct-JSON command-line transport belongs to :mod:`gac_runtime.runner`.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .gac_profile import (
    DEFAULT_GAC_PROFILE,
    GacProfile,
    gac_profile_from_envelope,
)


ENVELOPE_VERSION = 1
STDOUT_RENDER_LIMIT = DEFAULT_GAC_PROFILE.stdout_render_limit
STDERR_RENDER_LIMIT = DEFAULT_GAC_PROFILE.stderr_render_limit
ERROR_RENDER_LIMIT = DEFAULT_GAC_PROFILE.error_render_limit
OBS_TEXT_RENDER_LIMIT = DEFAULT_GAC_PROFILE.observation_render_limit
PROGRESS_OPS_MAX = DEFAULT_GAC_PROFILE.progress_ops_max
OP_LABEL_LIMIT = DEFAULT_GAC_PROFILE.operation_label_limit

_STABLE_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,47}\Z")
_ACTOR_CODE_RE = re.compile(r"^actor_k[A-Z][A-Za-z0-9]{0,56}\Z")
_CAMEL_BOUNDARY_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_DOC_SCOPE_RE = re.compile(r"^d\d{1,9}$")
_IMAGE_SCOPES = ("screen", "viewport", "element", "app", "tooltip")
_STATE_CHANGED_CODES = frozenset(
    {
        "CU_AX_ELEMENT_INVALID",
        "CU_APP_CONTEXT_STALE",
        "CU_APP_POINT_OCCLUDED",
        "CU_SCREENSHOT_TARGET_MISMATCH",
        "CU_WINDOW_BLOCKED_BY_MODAL",
        "CU_WINDOW_GONE",
    }
)
_INVALID_ACTION_CODES = frozenset(
    {
        "CU_AX_ACTION_REFUSED",
        "CU_AX_APP_NOT_RUNNING",
        "CU_AX_APP_NOT_SURFACE",
        "CU_AX_ARG_SHAPE",
        "CU_AX_FIND_EMPTY",
        "CU_APP_TARGET_REQUIRED",
        "CU_APP_TARGET_SHAPE",
        "CU_BAD_HOTKEY",
        "CU_INVALID_ACTION",
        "CU_SCROLL_DELTA",
        "CU_SCREENSHOT_NOT_INTERACTIVE",
        "CU_SCREENSHOT_TARGET_REQUIRED",
        "CU_SCROLL_PAGE",
        "CU_UNKNOWN_API",
        "CU_WAIT_SECONDS",
        "CU_WINDOW_UNKNOWN",
        "INVALID_OR_EXPIRED_REF",
        "PY_COORDINATE_SPACE_ERROR",
    }
)
_BLOCKED_CODES = frozenset(
    {
        "CNGC_MACOS_GUI_BYPASS",
        "CU_APP_NO_EFFECT_REPEAT",
    }
)
_BLOCKED_URL_RE = re.compile(
    r"(?://|^)(?:account|passport|signin|login|verify)\.|"
    r"/(?:login|signin|sign_in|sign-in|sorry|passport|verify)(?:[/.?]|$)|"
    r"[/_-]captcha|unhuman|humancheck|risk_handler|need_login=true",
    re.IGNORECASE,
)
_UNREACHABLE_URL_RE = re.compile(
    r"^(?:chrome-error://|about:neterror|chrome://network-error)"
)
DOM_TRUNCATION_RECOVERY = (
    '[recovery kind=dom] The DOM observation was clipped. Use bu.find("target"), '
    "bu.read_all(selector_or_refs), or bu.content_export(max_chars=...) to narrow "
    "the read; do not repeat the same full snapshot."
)
AX_TREE_TRUNCATION_RECOVERY = (
    "[recovery kind=ax_tree] The AX observation was clipped. Use "
    "cu.get_app_state(app_id, window_id=...) to narrow the read; do not repeat "
    "the same full snapshot."
)


def _truncate_counted(text: str, limit: int) -> tuple[str, int]:
    if not text or len(text) <= limit:
        return text, 0
    keep = max(limit - 64, 0)
    head = keep * 2 // 3
    tail = keep - head
    omitted = len(text) - head - tail
    suffix = text[-tail:] if tail else ""
    return f"{text[:head]}\n... {omitted} characters cut ...\n{suffix}", omitted


def _output_text(
    envelope: dict[str, Any],
    profile: GacProfile,
) -> tuple[str, int]:
    stdout = str(envelope.get("stdout") or "").rstrip()
    stderr = str(envelope.get("stderr") or "").rstrip()
    stderr, omitted = _truncate_counted(stderr, profile.stderr_render_limit)
    if stdout and stderr:
        return f"{stdout}\n{stderr}", omitted
    return stdout or stderr, omitted


def _error_text(envelope: dict[str, Any]) -> str:
    exception = envelope.get("exc")
    if not isinstance(exception, dict):
        return ""
    traceback_text = str(exception.get("traceback") or "").rstrip()
    if traceback_text:
        return traceback_text
    error_type = str(exception.get("type") or "Error")
    message = str(exception.get("message") or "")
    return f"{error_type}: {message}".strip().rstrip(":")


def _is_stable_error_code(code: Any) -> bool:
    return isinstance(code, str) and bool(
        _STABLE_CODE_RE.match(code) or _ACTOR_CODE_RE.match(code)
    )


def _error_code(envelope: dict[str, Any]) -> str:
    exception = envelope.get("exc")
    exception = exception if isinstance(exception, dict) else {}
    code = str(exception.get("code") or "")
    if _is_stable_error_code(code):
        return code
    error_type = str(exception.get("type") or "Error")
    return "PY_%s" % _CAMEL_BOUNDARY_RE.sub("_", error_type).upper()


def _result_status(
    envelope: dict[str, Any],
    *,
    frame_missing: bool,
    timed_out: bool,
) -> str:
    if timed_out:
        return "timeout"
    if frame_missing:
        return "error"
    exception = envelope.get("exc")
    if not isinstance(exception, dict) or not exception:
        return "ok" if bool(envelope.get("ok", True)) else "error"
    code = _error_code(envelope)
    if code in _STATE_CHANGED_CODES:
        return "state_changed"
    if code in _INVALID_ACTION_CODES:
        return "invalid_action"
    if code in _BLOCKED_CODES:
        return "blocked"
    return "error"


def _condition_text(envelope: dict[str, Any]) -> str:
    exception = envelope.get("exc")
    if not isinstance(exception, dict):
        return ""
    return str(exception.get("message") or "").strip()


def _operation_label(action: dict[str, Any], profile: GacProfile) -> str:
    library = str(action.get("lib") or "")
    operation = str(action.get("op") or "").strip()
    if not operation:
        return ""
    arguments = action.get("args")
    arguments = list(arguments) if isinstance(arguments, (list, tuple)) else []
    if library == "bu":
        identity = str(arguments[0]) if arguments else ""
        label = (
            f"{operation} {identity.lstrip('@')}"
            if re.match(r"^@?(?:d\d{1,9}:)?e\d+$", identity)
            else operation
        )
    else:
        label = f"{library}.{operation}" if library else operation
    limit = profile.operation_label_limit
    return label if len(label) <= limit else label[: limit - 1] + "…"


def _progress_line(envelope: dict[str, Any], profile: GacProfile) -> str:
    actions = [
        action
        for action in (envelope.get("actions") or [])
        if isinstance(action, dict)
        and action.get("state")
        not in ("attempted", "dispatched", "rejected")
    ]
    total = envelope.get("actions_total")
    confirmed = total if isinstance(total, int) and not isinstance(total, bool) else len(actions)
    possible = envelope.get("actions_possible")
    possible = (
        possible
        if isinstance(possible, int) and not isinstance(possible, bool)
        else 0
    )
    if not confirmed and not possible:
        return ""
    labels = [
        label
        for label in (_operation_label(action, profile) for action in actions)
        if label
    ]
    parts = [f"confirmed={confirmed}"]
    if (
        labels
        and confirmed <= profile.progress_ops_max
        and confirmed == len(labels)
    ):
        parts.append('ops="%s"' % " · ".join(labels))
    if labels:
        parts.append('last="%s"' % labels[-1])
    parts.append(f"possible={possible}")
    return "[progress %s]" % " ".join(parts)


def _superseded(observation: dict[str, Any]) -> bool:
    value = observation.get("superseded")
    if value is not None:
        return bool(value)
    return str(observation.get("phase") or "") == "pre"


def blocked_page_kind(text: str) -> str:
    match = re.search(r"\burl=(\S+)", text or "")
    url = match.group(1) if match else ""
    if _UNREACHABLE_URL_RE.match(url):
        return "unreachable"
    if _BLOCKED_URL_RE.search(url):
        return "auth"
    return ""


def _observation_label(observation: dict[str, Any]) -> str:
    kind = observation.get("kind")
    kind = kind if kind in ("dom", "image") else "dom"
    scope = str(observation.get("scope") or "")
    label_kind = kind
    if kind == "image":
        scope = scope if scope in _IMAGE_SCOPES else "screen"
    elif scope in ("ax_tree", "window_changes"):
        label_kind = "text"
    elif not _DOC_SCOPE_RE.match(scope):
        scope = "d0"
    label = f"[obs type={label_kind} scope={scope}"
    blocked = str(observation.get("blocked") or "")
    if kind == "dom" and blocked:
        label += f" blocked={blocked}"
    if scope == "window_changes":
        count = len(str(observation.get("text") or "").splitlines())
        label += f" status=added count={count}"
    if _superseded(observation):
        label += " superseded"
    tag = str(observation.get("tag") or "").strip()
    if tag:
        label += ' tag="%s"' % tag.replace('"', "'")
    return label + "]"


def _dedupe_output(text: str, observations: list[dict[str, Any]]) -> str:
    """Remove one exact stdout echo for every typed text observation."""

    if not text:
        return text
    payloads = []
    for observation in observations:
        body = str(observation.get("text") or "").strip("\n")
        if body.strip():
            payloads.append(body.splitlines())
    lines = text.splitlines()
    keep = [True] * len(lines)
    for payload in payloads:
        span = len(payload)
        digest = hashlib.sha256("\n".join(payload).encode("utf-8")).digest()
        for index in range(len(lines) - span + 1):
            if not all(keep[index : index + span]):
                continue
            window = "\n".join(lines[index : index + span]).encode("utf-8")
            if hashlib.sha256(window).digest() == digest:
                keep[index : index + span] = [False] * span
                break
    return "\n".join(line for line, retained in zip(lines, keep) if retained).strip("\n")


def observations_from_envelope(
    envelope: dict[str, Any],
    images: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Resolve ordered wire observations and attach host-read image bytes."""

    images = [image for image in (images or []) if isinstance(image, dict)]
    images_by_path = {
        str(image.get("path") or ""): image
        for image in images
        if image.get("path")
    }
    observations: list[dict[str, Any]] = []
    for record in envelope.get("observations") or []:
        if not isinstance(record, dict):
            continue
        item: dict[str, Any] = {
            "kind": str(record.get("kind") or "dom"),
            "scope": str(record.get("scope") or ""),
            "superseded": _superseded(record),
        }
        if item["kind"] == "image":
            image = images_by_path.get(str(record.get("path") or ""), {})
            item["data"] = str(image.get("data") or "")
            item["tag"] = str(record.get("tag") or image.get("tag") or "")
            item["mime_type"] = str(
                record.get("mime_type") or image.get("mime_type") or "image/png"
            )
            if item["scope"] in ("app", "tooltip"):
                item["width"] = record.get("width") or image.get("w")
                item["height"] = record.get("height") or image.get("h")
            state = record.get("state")
            if isinstance(state, dict):
                item["state"] = state
        else:
            item["text"] = str(record.get("text") or "")
            item["clipped"] = int(record.get("clipped") or 0)
            item["blocked"] = blocked_page_kind(item["text"])
            state = record.get("state")
            if isinstance(state, dict):
                item["state"] = state
        observations.append(item)
    return observations


def _model_windows(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    windows: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            return []
        window_id = item.get("id")
        title = item.get("title")
        focused = item.get("focused")
        if (
            isinstance(window_id, bool)
            or not isinstance(window_id, int)
            or not isinstance(title, str)
            or not isinstance(focused, bool)
        ):
            return []
        windows.append(
            {
                "id": window_id,
                "title": title,
                "focused": focused,
            }
        )
    return windows


def _model_app_state(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    app = value.get("app")
    if not isinstance(app, dict):
        return None
    bundle_id = app.get("bundle_id")
    if not isinstance(bundle_id, str) or not bundle_id.strip():
        return None
    return {
        "app": {"bundle_id": bundle_id},
        "windows": _model_windows(value.get("windows")),
    }


def _model_ax_state(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    app = value.get("app")
    if not isinstance(app, dict):
        return None
    bundle_id = app.get("bundle_id")
    name = app.get("name")
    pid = value.get("pid")
    app_pid = app.get("pid")
    focused = value.get("focused_element_index")
    view = value.get("view")
    windows_value = value.get("windows")
    if (
        not isinstance(bundle_id, str)
        or not bundle_id.strip()
        or not isinstance(name, str)
        or isinstance(pid, bool)
        or not isinstance(pid, int)
        or isinstance(app_pid, bool)
        or not isinstance(app_pid, int)
        or app_pid != pid
        or (
            focused is not None
            and (isinstance(focused, bool) or not isinstance(focused, int))
        )
        or view not in ("full", "diff")
        or not isinstance(windows_value, list)
    ):
        return None
    windows: list[dict[str, int]] = []
    for item in windows_value:
        if not isinstance(item, dict):
            return None
        window_id = item.get("id")
        if isinstance(window_id, bool) or not isinstance(window_id, int):
            return None
        windows.append({"id": window_id})
    return {
        "pid": pid,
        "app": {"bundle_id": bundle_id, "name": name, "pid": app_pid},
        "focused_element_index": focused,
        "view": view,
        "windows": windows,
    }


def render_envelope(
    envelope: dict[str, Any],
    *,
    images: list[dict[str, Any]] | None = None,
    observations: list[dict[str, Any]] | None = None,
    env_changes: dict[str, str] | None = None,
    timeout_ms: int | None = None,
    gac_profile: GacProfile | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Render an execution envelope to CNGC-compatible labelled content."""

    profile = gac_profile or gac_profile_from_envelope(envelope)
    frame_missing = bool(envelope.get("frame_missing"))
    timed_out = bool(envelope.get("timed_out")) or timeout_ms is not None
    status = _result_status(
        envelope,
        frame_missing=frame_missing,
        timed_out=timed_out,
    )
    error_text = _error_text(envelope) if status == "error" else ""
    condition_text = _condition_text(envelope) if status not in {"ok", "error"} else ""
    is_error = status in {"error", "invalid_action", "blocked", "timeout"}
    segments = [f"[result status={status}]"]
    if observations is None:
        observations = observations_from_envelope(envelope, images)
    if env_changes is None:
        env_changes = {}
    omitted: dict[str, int] = {}

    output, cut = _output_text(envelope, profile)
    if cut:
        omitted["output"] = cut
    output = _dedupe_output(output, observations)
    output, cut = _truncate_counted(output, 8_000)
    if cut:
        omitted["output"] = omitted.get("output", 0) + cut
    if output:
        segments.append(f"[output]\n{output}")

    if is_error:
        progress = _progress_line(envelope, profile)
        if progress:
            segments.append(progress)

    if error_text and not timed_out:
        error_text, cut = _truncate_counted(error_text, profile.error_render_limit)
        if cut:
            omitted["error"] = cut
        exception = envelope.get("exc")
        exception = exception if isinstance(exception, dict) else {}
        label = f"[error code={_error_code(envelope)}"
        lineno = exception.get("lineno")
        if isinstance(lineno, int) and not isinstance(lineno, bool):
            label += f" line={lineno}"
        segments.append(f"{label}]\n{error_text}")
    elif frame_missing and not timed_out:
        segments.append(
            "[error code=CELL_NO_RESULT_FRAME]\n"
            "The Python cell produced no complete result frame."
        )
    elif condition_text and not timed_out:
        condition_text, cut = _truncate_counted(
            condition_text,
            profile.error_render_limit,
        )
        if cut:
            omitted["error"] = cut
        exception = envelope.get("exc")
        exception = exception if isinstance(exception, dict) else {}
        label = f"[{status} code={_error_code(envelope)}"
        lineno = exception.get("lineno")
        if isinstance(lineno, int) and not isinstance(lineno, bool):
            label += f" line={lineno}"
        segments.append(f"{label}]\n{condition_text}")

    current_app_state = None
    current_app_is_ax = False
    for observation in reversed(observations):
        if _superseded(observation):
            continue
        if (
            observation.get("kind") == "dom"
            and observation.get("scope") == "ax_tree"
        ):
            candidate = _model_ax_state(observation.get("state"))
            if candidate is not None:
                current_app_state = candidate
                current_app_is_ax = True
                break
    if current_app_state is None:
        for observation in reversed(observations):
            if (
                _superseded(observation)
                or observation.get("kind") != "image"
                or not str(observation.get("data") or "")
            ):
                continue
            candidate = _model_app_state(observation.get("state"))
            if candidate is not None:
                current_app_state = candidate
                break
    if current_app_state is not None:
        segments.append(
            "[app_state]\n"
            + json.dumps(
                current_app_state,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )

    content: list[dict[str, Any]] = []

    def _flush() -> None:
        text = "\n".join(segment for segment in segments if segment)
        segments.clear()
        if text:
            content.append({"type": "text", "text": text})

    for observation in observations:
        label = _observation_label(observation)
        if observation.get("kind") == "image":
            image_data = str(observation.get("data") or "")
            if not image_data:
                segments.append(f"{label}\n(unavailable)")
                continue
            _flush()
            content.append({"type": "text", "text": label})
            content.append(
                {
                    "type": "image",
                    "data": image_data,
                    "mimeType": observation.get("mime_type") or "image/png",
                }
            )
            continue
        is_ax_tree = (
            observation.get("kind") == "dom"
            and observation.get("scope") == "ax_tree"
        )
        text = str(observation.get("text") or "")
        body, cut = (text, 0) if is_ax_tree else _truncate_counted(
            text,
            profile.observation_render_limit,
        )
        cut += int(observation.get("clipped") or 0)
        if cut:
            kind = "ax_tree" if is_ax_tree else "dom"
            omitted[kind] = omitted.get(kind, 0) + cut
        segments.append(f"{label}\n{body}" if body else label)

    for phase in ("pre_cell", "in_cell"):
        change = str(env_changes.get(phase) or "").strip()
        if change:
            segments.append(f"[change phase={phase}] {change}")

    wire_truncated = envelope.get("truncated")
    if isinstance(wire_truncated, dict):
        for kind in ("output", "dom", "observations", "images", "error"):
            value = wire_truncated.get(kind)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                omitted[kind] = omitted.get(kind, 0) + value
    for kind in (
        "output",
        "dom",
        "ax_tree",
        "observations",
        "images",
        "error",
    ):
        if omitted.get(kind):
            segments.append(f"[truncated kind={kind} omitted={omitted[kind]}]")
            if kind == "dom":
                segments.append(DOM_TRUNCATION_RECOVERY)
            elif kind == "ax_tree":
                segments.append(AX_TREE_TRUNCATION_RECOVERY)

    if current_app_state is not None:
        bundle_id = json.dumps(
            current_app_state["app"]["bundle_id"],
            ensure_ascii=False,
        )
        if current_app_is_ax:
            pid = current_app_state["app"]["pid"]
            segments.append(
                f"[next] READY: reuse bundle_id={bundle_id}; take element_index "
                "values from the newest AX tree. If a process pin is active, "
                f"also reuse pid={pid} for every app-bound call; otherwise omit "
                f"pid. Call get_app_state(bundle_id, pid={pid}) after a transition "
                "while pinned, or get_app_state(bundle_id) when unpinned or "
                "switching apps."
            )
        else:
            segments.append(
                f"[next] READY: reuse bundle_id={bundle_id} from the newest app "
                "state and ground coordinates in the image above it. Call "
                "get_app_state(bundle_id) again only after a visual transition or "
                "to switch apps; do not repeat list_apps() until discovery returns "
                "to COLD."
            )

    _flush()
    return content, is_error


def render_text(envelope: dict[str, Any]) -> tuple[str, bool]:
    """Flatten text content for the flow-web test panel."""

    content, is_error = render_envelope(envelope)
    text = "\n".join(
        str(item.get("text") or "")
        for item in content
        if item.get("type") == "text"
    )
    return text, is_error

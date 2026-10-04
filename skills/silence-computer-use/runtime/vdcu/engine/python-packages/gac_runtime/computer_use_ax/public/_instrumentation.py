"""Cell audit and observation hooks for the AX Computer Use facade."""

from __future__ import annotations

import functools
import inspect
import json

from collections.abc import Mapping
from typing import Any

from ..services.api import AppState


_EXECUTOR = None


def install_executor(executor):
    """Install the cell-local action and observation sink."""

    global _EXECUTOR
    previous = _EXECUTOR
    _EXECUTOR = executor
    return previous


def restore_executor(previous) -> None:
    """Restore the previous cell-local action and observation sink."""

    global _EXECUTOR
    _EXECUTOR = previous


def _model_state(state: AppState) -> dict[str, Any]:
    app = state.get("app")
    return {
        "pid": state.get("pid"),
        "app": {
            "app_id": app.get("app_id") if isinstance(app, Mapping) else "",
            "bundle_id": app.get("bundle_id") if isinstance(app, Mapping) else "",
            "name": app.get("name") if isinstance(app, Mapping) else "",
            "pid": app.get("pid") if isinstance(app, Mapping) else None,
        },
        "focused_element_index": state.get("focused_element_index"),
        "view": state.get("view"),
        "windows": [
            {"id": window.get("id")}
            for window in state.get("windows") or ()
            if isinstance(window, Mapping)
        ],
    }


def _added_window_text(state: AppState) -> list[str]:
    windows = state.get("added_windows")
    if not windows:
        return []
    lines = []
    for window in windows:
        lines.append(
            json.dumps(
                {
                    "window_id": window.get("window_id"),
                    "app_id": window.get("app_id"),
                    "title": window.get("title"),
                    "z_order": window.get("z_order"),
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
    return lines


def _emit_observation(state: AppState) -> None:
    if _EXECUTOR is None:
        return
    model_state = _model_state(state)
    added_window_text = _added_window_text(state)
    if added_window_text:
        _EXECUTOR.emit_observation(
            "dom",
            "window_changes",
            "\n".join(added_window_text),
        )
    _EXECUTOR.emit_observation(
        "dom",
        "ax_tree",
        state.tree,
        state=model_state,
    )
    frames = state.get("screenshots") or (
        [state._frame] if state._frame is not None else []
    )
    for frame in frames:
        _EXECUTOR.emit_image(
            bytes(frame),
            mime_type=frame.mime_type,
            scope="app",
            state=model_state,
        )


def _audit_arguments(
    name: str,
    bound: inspect.BoundArguments,
) -> list[Any]:
    arguments = bound.arguments
    if name in {"type_text", "select_text"}:
        text = arguments.get("text")
        return [f"chars={len(text) if isinstance(text, str) else 0}"]
    index = arguments.get("element_index")
    return [index] if index is not None else []


def _record(name: str, arguments: list[Any], state: str) -> None:
    if _EXECUTOR is None:
        return
    try:
        _EXECUTOR.record("cu_ax", name, arguments, state=state)
    except Exception:
        pass


def wrap_action(name: str, function):
    """Record one AX action without changing its signature or validation."""

    signature = inspect.signature(function)

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        bound = signature.bind(*args, **kwargs)
        bound.apply_defaults()
        arguments = _audit_arguments(name, bound)
        _record(name, arguments, "attempted")
        try:
            result = function(*args, **kwargs)
        except Exception:
            _record(name, arguments, "rejected")
            raise
        _record(name, arguments, "completed")
        return result

    return wrapped


def wrap_app_state(function):
    """Emit the tree text and optional app frame returned by an observation."""

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        state = function(*args, **kwargs)
        _emit_observation(state)
        return state

    return wrapped

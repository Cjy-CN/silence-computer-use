"""Windows Computer Use action audit and screen observations."""

from __future__ import annotations

import functools
import inspect

from dataclasses import dataclass
from typing import Any

from ..services._errors import coded_error


_EXECUTOR = None


@dataclass
class _InstalledState:
    executor: Any


def install_executor(executor) -> _InstalledState:
    """Install a cell-local action and image sink."""

    global _EXECUTOR
    previous = _InstalledState(_EXECUTOR)
    _EXECUTOR = executor
    return previous


def restore_executor(previous: _InstalledState) -> None:
    global _EXECUTOR
    _EXECUTOR = previous.executor


def _audit_value(value: Any) -> str:
    text = str(value)
    return text if len(text) <= 80 else text[:80] + "..."


def _audit_arguments(
    name: str,
    bound: inspect.BoundArguments,
) -> list[Any]:
    arguments = bound.arguments
    if name == "type":
        value = arguments.get("text")
        text = "" if value is None else str(value)
        return [f"chars={len(text)}"]
    if name == "hotkey":
        return [_audit_value(key) for key in list(arguments.get("keys") or ())[:8]]

    values: list[Any] = []
    for value in arguments.values():
        if len(values) >= 4:
            break
        values.append(
            value if isinstance(value, (int, float)) else _audit_value(value)
        )
    return values


def _record(name: str, arguments: list[Any], state: str) -> None:
    if _EXECUTOR is None:
        return
    try:
        _EXECUTOR.record("cu", name, arguments, state=state)
    except Exception:
        pass


def _emit_frame(frame, *, tag: str | None) -> None:
    if _EXECUTOR is None:
        raise RuntimeError(
            "Computer Use screenshot requested show=True without an image sink"
        )
    _EXECUTOR.emit_image(
        bytes(frame),
        tag=tag,
        width=frame.width,
        height=frame.height,
        scope="screen",
    )


def _bind_arguments(name: str, signature, args, kwargs):
    try:
        return signature.bind(*args, **kwargs)
    except TypeError as error:
        raise coded_error(
            TypeError,
            name,
            "CU_INVALID_ACTION",
            str(error),
        ) from error


def wrap_public_call(name: str, function):
    """Code invalid Windows public API call shapes without auditing a GUI action."""

    signature = inspect.signature(function)

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        _bind_arguments(name, signature, args, kwargs)
        return function(*args, **kwargs)

    return wrapped


def wrap_action(name: str, function):
    """Audit one public Windows GUI action without changing its signature."""

    signature = inspect.signature(function)

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        bound = _bind_arguments(name, signature, args, kwargs)
        bound.apply_defaults()
        audit_arguments = _audit_arguments(name, bound)
        _record(name, audit_arguments, "attempted")
        try:
            result = function(*args, **kwargs)
        except Exception:
            _record(name, audit_arguments, "rejected")
            raise
        _record(name, audit_arguments, "completed")
        return result

    return wrapped


def wrap_screenshot(function):
    """Emit a returned Windows desktop frame when ``show`` is truthy."""

    signature = inspect.signature(function)

    @functools.wraps(function)
    def wrapped(*args, **kwargs):
        bound = _bind_arguments("screenshot", signature, args, kwargs)
        bound.apply_defaults()
        frame = function(*args, **kwargs)
        if bool(bound.arguments["show"]):
            value = bound.arguments["tag"]
            _emit_frame(
                frame,
                tag=None if value is None else str(value),
            )
        return frame

    return wrapped


def auto_observe_after_cell(
    *,
    error_code: str = "",
    timed_out: bool = False,
) -> dict[str, Any]:
    """Keep the cell-runtime hook stable without implicit screen capture."""

    return {"attempted": False}

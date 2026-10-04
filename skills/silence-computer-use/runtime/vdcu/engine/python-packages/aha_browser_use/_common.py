"""Public models, errors, tracing, and computer-use result delivery."""

from __future__ import annotations

import functools
import importlib
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable, TypeAlias


class BrowserUseError(RuntimeError):
    code = "BU_ERROR"

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class BrowserSessionError(BrowserUseError):
    code = "BU_SESSION_STALE"


class BrowserRefError(ValueError):
    code = "BU_REF_SHAPE"

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code


class StaleRefError(BrowserRefError):
    code = "BU_REF_STALE"


class Ref(str):
    @property
    def ref(self) -> str:
        return str(self)

    @property
    def id(self) -> str:
        return str(self)


class RefList(list):
    def __init__(self, values=(), *, observation: str = ""):
        super().__init__(values)
        self.observation = observation

    @property
    def first(self):
        return self[0] if self else None

    def __str__(self) -> str:
        return self.observation or super().__str__()


class CssLocator:
    """A CSS locator resolved by Native AhaBUA in the current document."""

    _MATCH_POLICIES = frozenset({"unique", "first", "all"})

    def __init__(self, selector, *, frame_path=(), match="unique"):
        selector = str(selector or "")
        match = str(match or "")
        if not selector:
            raise ValueError("selector must be non-empty")
        if match not in self._MATCH_POLICIES:
            raise ValueError("match must be unique, first, or all")
        self.selector = selector
        self.frame_path = tuple(str(item) for item in frame_path)
        if any(not item for item in self.frame_path):
            raise ValueError("frame_path must contain non-empty selectors")
        self.match = match

    def to_wire(self):
        return {
            "framePath": list(self.frame_path),
            "selector": self.selector,
            "match": self.match,
        }

    def target(self):
        return {"css": self.to_wire()}

    def __repr__(self):
        return (
            f"CssLocator({self.selector!r}, frame_path={self.frame_path!r}, "
            f"match={self.match!r})"
        )


ElementTarget: TypeAlias = Ref | CssLocator


class CssLocatorList(list):
    """Unique Native CSS locator candidates ordered by preference."""

    def __init__(self, values=()):
        locators = list(values)
        if any(not isinstance(value, CssLocator) for value in locators):
            raise TypeError("CssLocatorList values must be CssLocator instances")
        super().__init__(locators)

    @property
    def first(self):
        return self[0] if self else None


class ScreenshotPath(str):
    def __new__(cls, path: str, width: int | None = None, height: int | None = None):
        value = super().__new__(cls, str(path))
        value.path = str(path)
        value.width = int(width) if width is not None else None
        value.height = int(height) if height is not None else None
        return value

    @property
    def size(self):
        return self.width, self.height


class TabRecord(dict):
    def __getattr__(self, name: str):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


def tab_record(value: Any) -> TabRecord:
    record = TabRecord(value or {})
    target = record.get("targetId") or record.get("target_id") or record.get("id") or ""
    record.update(targetId=target, target_id=target, id=target)
    return record


_EXECUTOR = None


def _shared_core():
    return sys.modules.get("gac_runtime.browser.domain.core")


def _executor():
    if _EXECUTOR is not None:
        return _EXECUTOR
    core = _shared_core()
    getter = getattr(core, "_get_executor", None) if core is not None else None
    return getter() if callable(getter) else None


def install_executor(executor, *, strict: bool = False):
    """Install the per-cell executor used for action audit and observations."""
    del strict
    global _EXECUTOR
    previous = _EXECUTOR
    _EXECUTOR = executor
    return previous


def emit_observation(kind: str, payload: str, *, scope: str = "") -> bool:
    cell = sys.modules.get("_computer_use_tool_cell")
    sink = getattr(cell, "emit_observation", None) if cell is not None else None
    if not callable(sink):
        sink = getattr(_executor(), "emit_observation", None)
    if callable(sink):
        try:
            sink(kind=kind, scope=scope, payload=payload, superseded=None, path=None)
            return True
        except Exception:
            pass
    if payload:
        print(payload)
    return False


def emit_image(
    data: bytes,
    *,
    tag: str | None,
    width: int | None,
    height: int | None,
    scope: str = "viewport",
):
    sink = getattr(_executor(), "emit_image", None)
    if callable(sink):
        sink(data, tag=tag, width=width, height=height, scope=scope)


def default_screenshot_path() -> Path:
    root = os.environ.get("COMPUTER_USE_TOOL_SPOOL") or tempfile.gettempdir()
    return Path(root) / f"aha_bua_{os.getpid()}_{time.time_ns()}.png"


def unsupported(tool: str, detail: str):
    raise BrowserUseError(
        f"aha_browser_use.{tool} is unsupported by the Native AhaBUA backend: {detail}",
        code="UNSUPPORTED",
    )


def coded_error(exc_type, code: str, message: str):
    error = exc_type(message)
    error.code = code
    return error


def _debug_trace():
    try:
        return importlib.import_module("gac_runtime.debug_trace")
    except Exception:
        return None


def _record(name: str, args: tuple[Any, ...], kwargs: dict[str, Any], state: str):
    core = _shared_core()
    recorder = getattr(core, "_record", None) if core is not None else None
    if not callable(recorder):
        return
    values = [str(value)[:80] for value in args[:4]]
    values.extend(f"{key}={str(value)[:80]}" for key, value in list(kwargs.items())[:4])
    recorder(name, values, state=state)


def public_tool(name: str, *, action: bool = False):
    """Trace every public tool and audit side-effecting calls."""

    def decorate(function: Callable):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            trace = _debug_trace()
            token = trace.start_bu_call(name, args, kwargs) if trace else None
            # Backend selection is Task-scoped and immutable.  Gate every public
            # call, including LOCAL/compat routes, before it can do any work.
            from ._transport import CLIENT
            try:
                CLIENT.capabilities
                if action:
                    _record(name, args, kwargs, "attempted")
                result = function(*args, **kwargs)
            except BaseException as error:
                if trace:
                    trace.fail_bu_call(token, error)
                raise
            if action:
                _record(name, args, kwargs, "completed")
            if trace:
                trace.finish_bu_call(token, result)
            return result

        return wrapped

    return decorate


__all__ = [
    "BrowserRefError", "BrowserSessionError", "BrowserUseError", "CssLocator", "CssLocatorList",
    "ElementTarget",
    "Ref", "RefList",
    "ScreenshotPath", "StaleRefError", "TabRecord",
    "coded_error", "default_screenshot_path", "emit_image", "emit_observation",
    "install_executor", "public_tool", "tab_record", "unsupported",
]

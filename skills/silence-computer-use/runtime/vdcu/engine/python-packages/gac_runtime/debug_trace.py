"""Fail-open, opt-in tracing for Browser Use calls.

The logger writes one JSONL file for a Browser Use task/prompt and never writes
to stdout or stderr.  It is intended for explicit local debugging and remains
disabled unless ``GAC_RUNTIME_BU_TRACE`` is set to a true value.
"""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import datetime
import itertools
import json
import math
import os
import pathlib
import threading
import time
import traceback
import uuid
from typing import Any


_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
_TRACE_ENV = "GAC_RUNTIME_BU_TRACE"
_TRACE_DIR_ENV = "GAC_RUNTIME_BU_TRACE_DIR"
_TRACE_PROMPT_ID_ENV = "GAC_RUNTIME_BU_TRACE_PROMPT_ID"
_TRACE_PROMPT_IDLE_ENV = "GAC_RUNTIME_BU_TRACE_PROMPT_IDLE_SECONDS"
_DEFAULT_PROMPT_IDLE_SECONDS = 60.0
_PROMPT_TRACE_PREFIX = "bu-prompt-"
_TRACE_LOCK_NAME = ".bu-prompt-trace.lock"
_lock = threading.RLock()
_thread_state = threading.local()
_call_counter = itertools.count(1)
_trace_path: str | None = None


def _enabled() -> bool:
    return os.environ.get(_TRACE_ENV, "").strip().lower() in _TRUE_VALUES


def _safe_component(value: str, fallback: str) -> str:
    safe = "".join(
        char if char.isalnum() or char in ("-", "_", ".") else "_"
        for char in str(value or "")
    ).strip("._")
    return safe[:120] or fallback


def _resolve_trace_dir() -> str:
    configured = os.environ.get(_TRACE_DIR_ENV, "").strip()
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    cache_root = os.environ.get("CUA_CACHE_DIR", "").strip()
    if cache_root:
        return os.path.join(cache_root, "bu_tool_traces")
    return os.path.abspath(os.path.join(os.getcwd(), ".cache", "bu_tool_traces"))


def _prompt_identity() -> tuple[str, bool]:
    explicit = os.environ.get(_TRACE_PROMPT_ID_ENV, "").strip()
    if explicit:
        return explicit, True
    return os.environ.get("CUA_GAC_SESSION_ID", "").strip(), False


def _prompt_idle_seconds() -> float:
    raw = os.environ.get(_TRACE_PROMPT_IDLE_ENV, "").strip()
    if not raw:
        return _DEFAULT_PROMPT_IDLE_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_PROMPT_IDLE_SECONDS
    if not math.isfinite(value) or value < 1.0:
        return _DEFAULT_PROMPT_IDLE_SECONDS
    return value


@contextlib.contextmanager
def _trace_directory_lock(trace_dir: str):
    """Serialize prompt selection and appends across Python cell processes."""

    lock_path = os.path.join(trace_dir, _TRACE_LOCK_NAME)
    handle = open(lock_path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            deadline = time.monotonic() + 5.0
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.005)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _write_latest_path(trace_dir: str, path: str) -> None:
    latest_path = os.path.join(trace_dir, "latest.txt")
    temporary = latest_path + f".{os.getpid()}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as latest:
            latest.write(path)
            latest.write("\n")
        os.replace(temporary, latest_path)
    finally:
        try:
            if os.path.exists(temporary):
                os.unlink(temporary)
        except Exception:
            pass


def _trace_identity(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as trace:
            for line in trace:
                if line.strip():
                    event = json.loads(line)
                    return str(event.get("prompt_identity") or "")
    except Exception:
        pass
    return ""


def _recent_prompt_path(
    trace_dir: str,
    prompt_identity: str,
    *,
    ignore_idle: bool,
) -> str | None:
    """Return the active trace only when its prompt identity also matches."""

    try:
        latest_path = os.path.join(trace_dir, "latest.txt")
        with open(latest_path, encoding="utf-8") as latest:
            candidate = os.path.abspath(latest.readline().strip())
        if os.path.normcase(os.path.dirname(candidate)) != os.path.normcase(trace_dir):
            return None
        filename = os.path.basename(candidate)
        if not filename.startswith(_PROMPT_TRACE_PREFIX) or not filename.endswith(
            ".jsonl"
        ):
            return None
        if prompt_identity and _trace_identity(candidate) != prompt_identity:
            return None
        age = max(0.0, time.time() - os.path.getmtime(candidate))
        if not ignore_idle and age > _prompt_idle_seconds():
            return None
        return candidate
    except Exception:
        return None


def get_trace_path() -> str | None:
    """Return the trace shared by consecutive cells for the current prompt."""

    global _trace_path
    if not _enabled():
        return None
    with _lock:
        if _trace_path is not None:
            return _trace_path
        trace_dir = _resolve_trace_dir()
        os.makedirs(trace_dir, exist_ok=True)
        identity, explicit_identity = _prompt_identity()
        with _trace_directory_lock(trace_dir):
            _trace_path = _recent_prompt_path(
                trace_dir,
                identity,
                ignore_idle=explicit_identity,
            )
            if _trace_path is None:
                started = datetime.datetime.now(datetime.timezone.utc).strftime(
                    "%Y%m%dT%H%M%S.%fZ"
                )
                identity_suffix = _safe_component(identity, uuid.uuid4().hex[:8])
                filename = f"{_PROMPT_TRACE_PREFIX}{started}-{identity_suffix}.jsonl"
                _trace_path = os.path.join(trace_dir, filename)
            with open(_trace_path, "a", encoding="utf-8", newline="\n"):
                pass
            _write_latest_path(trace_dir, _trace_path)
        return _trace_path


def _type_name(value: Any) -> str:
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


def _jsonable(value: Any, active: set[int] | None = None) -> Any:
    """Convert an arbitrary Python value without intentionally truncating it."""

    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return {"$type": "float", "value": repr(value)}
    if isinstance(value, bytes):
        return {
            "$type": "bytes",
            "encoding": "base64",
            "size": len(value),
            "data": base64.b64encode(value).decode("ascii"),
        }
    if isinstance(value, (bytearray, memoryview)):
        raw = bytes(value)
        return {
            "$type": _type_name(value),
            "encoding": "base64",
            "size": len(raw),
            "data": base64.b64encode(raw).decode("ascii"),
        }
    if isinstance(value, pathlib.Path):
        return {"$type": _type_name(value), "value": str(value)}
    if isinstance(value, BaseException):
        return {
            "$type": _type_name(value),
            "message": str(value),
            "code": getattr(value, "code", None),
            "args": _jsonable(value.args, active),
        }

    if active is None:
        active = set()
    identity = id(value)
    if identity in active:
        return {"$type": _type_name(value), "$cycle": True}
    active.add(identity)
    try:
        if dataclasses.is_dataclass(value) and not isinstance(value, type):
            return {
                "$type": _type_name(value),
                "fields": {
                    field.name: _jsonable(getattr(value, field.name), active)
                    for field in dataclasses.fields(value)
                },
            }
        if isinstance(value, dict):
            if all(isinstance(key, str) for key in value):
                return {
                    str(key): _jsonable(item, active)
                    for key, item in value.items()
                }
            return {
                "$type": _type_name(value),
                "items": [
                    [_jsonable(key, active), _jsonable(item, active)]
                    for key, item in value.items()
                ],
            }
        if isinstance(value, list):
            return [_jsonable(item, active) for item in value]
        if isinstance(value, tuple):
            return {
                "$type": "tuple",
                "items": [_jsonable(item, active) for item in value],
            }
        if isinstance(value, (set, frozenset)):
            return {
                "$type": _type_name(value),
                "items": [_jsonable(item, active) for item in value],
            }
        attributes = getattr(value, "__dict__", None)
        if isinstance(attributes, dict):
            return {
                "$type": _type_name(value),
                "attributes": _jsonable(attributes, active),
            }
        try:
            representation = repr(value)
        except Exception as error:
            representation = f"<repr failed: {type(error).__name__}: {error}>"
        return {"$type": _type_name(value), "repr": representation}
    finally:
        active.remove(identity)


def _write_event(event: dict[str, Any]) -> None:
    """Append one event without ever changing Browser Use behavior."""

    try:
        path = get_trace_path()
        if path is None:
            return
        prompt_identity, _ = _prompt_identity()
        record = {
            "trace_version": 2,
            "timestamp_utc": datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat(),
            "pid": os.getpid(),
            "thread_id": threading.get_ident(),
            "prompt_identity": prompt_identity,
            "execution_id": os.environ.get("CUA_GAC_EXECUTION_ID", ""),
            "session_id": os.environ.get("CUA_GAC_SESSION_ID", ""),
            "runtime_source": os.path.abspath(__file__),
            **event,
        }
        line = json.dumps(
            _jsonable(record),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        with _lock:
            with _trace_directory_lock(os.path.dirname(path)):
                with open(path, "a", encoding="utf-8", newline="\n") as output:
                    output.write(line)
                    output.write("\n")
                    output.flush()
    except Exception:
        pass


def _call_stack() -> list[dict[str, Any]]:
    stack = getattr(_thread_state, "call_stack", None)
    if stack is None:
        stack = []
        _thread_state.call_stack = stack
    return stack


def _start_call(event: str, name: str, payload: dict[str, Any]):
    if not _enabled():
        return None
    stack = _call_stack()
    execution_id = os.environ.get("CUA_GAC_EXECUTION_ID", "") or str(os.getpid())
    call_id = f"{execution_id}:{next(_call_counter)}"
    token = {
        "call_id": call_id,
        "parent_call_id": stack[-1]["call_id"] if stack else None,
        "name": str(name),
        "started_ns": time.perf_counter_ns(),
    }
    stack.append(token)
    _write_event(
        {
            "event": event,
            "call_id": call_id,
            "parent_call_id": token["parent_call_id"],
            **payload,
        }
    )
    return token


def _pop_call(token) -> None:
    if token is None:
        return
    stack = _call_stack()
    if stack and stack[-1] is token:
        stack.pop()
        return
    try:
        stack.remove(token)
    except ValueError:
        pass


def _duration_ms(token) -> float:
    return (time.perf_counter_ns() - token["started_ns"]) / 1_000_000


def start_bu_call(name: str, args: tuple[Any, ...], kwargs: dict[str, Any]):
    return _start_call(
        "bu_call_start",
        name,
        {"tool": str(name), "args": args, "kwargs": kwargs},
    )


def _bu_result_payload(result: Any) -> Any:
    """Preserve model-facing text carried by printable list-compatible results."""

    if not isinstance(result, list):
        return result
    if getattr(type(result), "__str__", None) is list.__str__:
        return result
    observation = str(result)
    return {
        "$type": _type_name(result),
        "refs": list(result),
        "observation": observation,
    }


def finish_bu_call(token, result: Any) -> None:
    if token is None:
        return
    try:
        _write_event(
            {
                "event": "bu_call_end",
                "call_id": token["call_id"],
                "parent_call_id": token["parent_call_id"],
                "tool": token["name"],
                "status": "ok",
                "duration_ms": _duration_ms(token),
                "result": _bu_result_payload(result),
            }
        )
    finally:
        _pop_call(token)


def fail_bu_call(token, error: BaseException) -> None:
    if token is None:
        return
    try:
        _write_event(
            {
                "event": "bu_call_end",
                "call_id": token["call_id"],
                "parent_call_id": token["parent_call_id"],
                "tool": token["name"],
                "status": "error",
                "duration_ms": _duration_ms(token),
                "exception": error,
                "traceback": "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                ),
            }
        )
    finally:
        _pop_call(token)


def start_native_call(
    operation: str,
    *,
    page_id: str,
    params: dict[str, Any],
    retryable: bool,
):
    return _start_call(
        "native_call_start",
        operation,
        {
            "operation": str(operation),
            "page_id": str(page_id),
            "params": params,
            "retryable": bool(retryable),
        },
    )


def finish_native_call(
    token,
    *,
    response: dict[str, Any],
    result: dict[str, Any],
    binary_size: int,
) -> None:
    if token is None:
        return
    try:
        _write_event(
            {
                "event": "native_call_end",
                "call_id": token["call_id"],
                "parent_call_id": token["parent_call_id"],
                "operation": token["name"],
                "status": "ok",
                "duration_ms": _duration_ms(token),
                "response": response,
                "result": result,
                "binary_size": int(binary_size),
            }
        )
    finally:
        _pop_call(token)


def fail_native_call(token, error: BaseException) -> None:
    if token is None:
        return
    try:
        _write_event(
            {
                "event": "native_call_end",
                "call_id": token["call_id"],
                "parent_call_id": token["parent_call_id"],
                "operation": token["name"],
                "status": "error",
                "duration_ms": _duration_ms(token),
                "exception": error,
                "traceback": "".join(
                    traceback.format_exception(type(error), error, error.__traceback__)
                ),
            }
        )
    finally:
        _pop_call(token)


def record_llm_result(
    result: Any,
    serialized_stdout: str,
    *,
    gac_profile: str = "",
) -> None:
    """Record the exact JSON line emitted by runner.py to the host/LLM bridge."""

    _write_event(
        {
            "event": "llm_result",
            "gac_profile": str(gac_profile or ""),
            "result": result,
            "serialized_stdout": serialized_stdout,
        }
    )


__all__ = [
    "fail_bu_call",
    "fail_native_call",
    "finish_bu_call",
    "finish_native_call",
    "get_trace_path",
    "record_llm_result",
    "start_bu_call",
    "start_native_call",
]

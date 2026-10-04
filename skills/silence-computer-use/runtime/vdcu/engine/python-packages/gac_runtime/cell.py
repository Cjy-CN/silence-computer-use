"""In-process execution shell for one GAC Runtime Python cell.

This module owns execution and capture only.  Wire framing and labelled result
rendering live in :mod:`gac_runtime.envelope`; CLI orchestration lives in
:mod:`gac_runtime.runner`.
"""

from __future__ import annotations

import io
import linecache
import os
import signal
import sys
import tempfile
import threading
import time
import traceback
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

from .browser.domain import core
from .computer_use_ax.public import _instrumentation as cu_ax_instrumentation
from .envelope import ENVELOPE_VERSION, _is_stable_error_code
from .gac_profile import (
    DEFAULT_GAC_PROFILE,
    GacProfile,
    GacProfileState,
    gac_profile_scope,
)

if sys.platform == "win32":
    from .computer_use import _instrumentation as cu_instrumentation
else:
    cu_instrumentation = None


DEFAULT_FILENAME = "<gac-runtime-cell>"
IMAGE_DIR_ENV = "GAC_RUNTIME_IMAGE_DIR"
MAX_STDOUT = DEFAULT_GAC_PROFILE.max_stdout_chars
MAX_STDERR = DEFAULT_GAC_PROFILE.max_stderr_chars
MAX_ACTIONS = DEFAULT_GAC_PROFILE.max_actions
MAX_OBSERVATIONS = DEFAULT_GAC_PROFILE.max_observations
MAX_OBS_TEXT = DEFAULT_GAC_PROFILE.max_observation_chars
MAX_IMAGES = DEFAULT_GAC_PROFILE.max_images
IMAGE_SCOPES = ("screen", "viewport", "element", "app", "tooltip")


class CellTimeout(BaseException):
    """Internal wall-clock interrupt that is rendered as ``status=timeout``."""


class CellExecutor:
    """Action, typed-observation, and image sink installed for one cell."""

    def __init__(self, profile_state: GacProfileState | None = None) -> None:
        self._profile_state = profile_state or GacProfileState()
        self.actions: list[dict[str, Any]] = []
        self.actions_total = 0
        self.actions_possible = 0
        self.observations: list[dict[str, Any]] = []
        self.images: list[dict[str, Any]] = []
        self.actions_dropped = 0
        self.observations_dropped = 0
        self.images_dropped = 0
        self._started_at = time.monotonic()
        self._image_dir = ""
        self._image_number = 0

    @property
    def gac_profile(self) -> GacProfile:
        return self._profile_state.profile

    def record(
        self,
        library: str,
        operation: str,
        arguments: Any = None,
        state: str | None = None,
    ) -> None:
        values = (
            list(arguments)
            if isinstance(arguments, (list, tuple))
            else ([] if arguments is None else [arguments])
        )
        entry: dict[str, Any] = {
            "lib": str(library or ""),
            "op": str(operation or ""),
            "args": values,
            "t_ms": int((time.monotonic() - self._started_at) * 1000),
        }
        if state:
            entry["state"] = state
        if state in ("attempted", "dispatched"):
            self.actions_possible = max(self.actions_possible, 1)
        elif state == "rejected":
            self.actions_possible = 0
        else:
            self.actions_total += 1
            self.actions_possible = 0
        if len(self.actions) < self.gac_profile.max_actions:
            self.actions.append(entry)
        else:
            self.actions_dropped += 1

    def emit_observation(
        self,
        kind: str,
        scope: str,
        payload: Any = "",
        superseded: bool | None = None,
        path: str | None = None,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        observation: dict[str, Any] = {
            "kind": kind if kind in ("dom", "image") else "dom",
            "scope": str(scope or ""),
            "superseded": superseded,
            "after": self.actions_total,
        }
        if observation["kind"] == "image":
            observation["path"] = str(path or "")
            if isinstance(state, dict):
                observation["state"] = state
        else:
            text = payload if isinstance(payload, str) else str(payload)
            limit = self.gac_profile.max_observation_chars
            if len(text) > limit:
                observation["clipped"] = len(text) - limit
                text = text[:limit]
            observation["text"] = text
            if isinstance(state, dict):
                observation["state"] = state
        self._append_observation(observation)
        return observation

    def emit_image(
        self,
        data: bytes,
        *,
        tag: str | None = None,
        width: int | None = None,
        height: int | None = None,
        mime_type: str = "image/png",
        scope: str | None = None,
        superseded: bool | None = None,
        path: str | None = None,
        state: dict[str, Any] | None = None,
    ) -> None:
        self._image_number += 1
        image_path = str(path or "")
        if not image_path:
            if not self._image_dir:
                configured_dir = os.environ.get(IMAGE_DIR_ENV, "").strip()
                if configured_dir:
                    configured_dir = os.path.abspath(
                        os.path.expanduser(configured_dir)
                    )
                    os.makedirs(configured_dir, exist_ok=True)
                self._image_dir = tempfile.mkdtemp(
                    prefix="gac-runtime-cell-",
                    dir=configured_dir or None,
                )
            image_path = os.path.join(
                self._image_dir,
                f"shot_{self._image_number:02d}"
                + (".jpg" if mime_type == "image/jpeg" else ".png"),
            )
            with open(image_path, "wb") as image_file:
                image_file.write(data)
        image = {
            "path": image_path,
            "tag": str(tag or ""),
            "w": width,
            "h": height,
            "mime_type": mime_type,
        }
        self.images.append(image)
        observation: dict[str, Any] = {
            "kind": "image",
            "scope": scope if scope in IMAGE_SCOPES else "screen",
            "superseded": superseded,
            "after": self.actions_total,
            "path": image_path,
            "tag": str(tag or ""),
            "width": width,
            "height": height,
            "mime_type": mime_type,
        }
        if isinstance(state, dict):
            observation["state"] = state
        self._append_observation(observation)
        while len(self.images) > self.gac_profile.max_images:
            removed = self.images.pop(0)
            self.images_dropped += 1
            self.observations = [
                item
                for item in self.observations
                if item.get("path") != removed.get("path")
            ]

    def _append_observation(self, observation: dict[str, Any]) -> None:
        self.observations.append(observation)
        if len(self.observations) > self.gac_profile.max_observations:
            self.observations.pop(0)
            self.observations_dropped += 1

    def finalize(self) -> list[dict[str, Any]]:
        profile = self.gac_profile
        if len(self.actions) > profile.max_actions:
            self.actions_dropped += len(self.actions) - profile.max_actions
            del self.actions[profile.max_actions :]
        while len(self.images) > profile.max_images:
            removed = self.images.pop(0)
            self.images_dropped += 1
            self.observations = [
                item
                for item in self.observations
                if item.get("path") != removed.get("path")
            ]
        if len(self.observations) > profile.max_observations:
            dropped = len(self.observations) - profile.max_observations
            self.observations_dropped += dropped
            del self.observations[:dropped]
        for observation in self.observations:
            if observation.get("kind") != "image":
                text = str(observation.get("text") or "")
                if len(text) > profile.max_observation_chars:
                    clipped = len(text) - profile.max_observation_chars
                    observation["text"] = text[: profile.max_observation_chars]
                    observation["clipped"] = (
                        int(observation.get("clipped") or 0) + clipped
                    )
            if observation.get("superseded") is None:
                observation["superseded"] = (
                    int(observation.pop("after", 0)) < self.actions_total
                )
            observation.pop("after", None)
        return self.observations


def _format_exception(error: BaseException, filename: str) -> dict[str, Any]:
    try:
        frames = [
            frame
            for frame in traceback.extract_tb(error.__traceback__)
            if frame.filename == filename
        ]
    except Exception:
        frames = []
    lineno = frames[-1].lineno if frames else getattr(error, "lineno", None)
    try:
        if frames:
            body = "".join(traceback.format_list(frames))
            tail = "".join(traceback.format_exception_only(type(error), error))
            traceback_text = "Traceback (most recent call last):\n" + body + tail
        else:
            traceback_text = "".join(
                traceback.format_exception_only(type(error), error)
            )
    except Exception:
        traceback_text = f"{type(error).__name__}: {error}"
    result: dict[str, Any] = {
        "type": type(error).__name__,
        "message": str(error),
        "lineno": lineno,
        "traceback": traceback_text.rstrip(),
    }
    code = getattr(error, "code", None)
    if (
        not isinstance(error, SystemExit)
        and _is_stable_error_code(code)
    ):
        result["code"] = code
    return result


def _environment_snapshot() -> dict[str, Any]:
    try:
        from .browser.public.api import _env_snapshot

        return {"browser": _env_snapshot()}
    except Exception:
        return {}


def _run_code_in_profile(
    code: str,
    *,
    profile_state: GacProfileState,
    filename: str = DEFAULT_FILENAME,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    """Compile and execute ``code`` in this process and return its raw envelope."""

    stdout_buffer = io.StringIO()
    stderr_buffer = io.StringIO()
    source = code if code.endswith("\n") else f"{code}\n"
    linecache.cache[filename] = (
        len(source),
        None,
        source.splitlines(keepends=True),
        filename,
    )
    namespace: dict[str, Any] = {
        "__builtins__": __builtins__,
        "__file__": filename,
        "__name__": "__main__",
        "__package__": None,
    }
    executor = CellExecutor(profile_state)
    environment_before = _environment_snapshot()
    previous_executor = core._get_executor()
    previous_strict = core._STRICT_EXECUTOR
    previous_argv = sys.argv
    sys.argv = [filename]
    core.install_executor(executor, strict=True)
    previous_cu_state = (
        cu_instrumentation.install_executor(executor)
        if cu_instrumentation is not None
        else None
    )
    previous_cu_ax_state = cu_ax_instrumentation.install_executor(executor)
    error: BaseException | None = None
    timed_out = False
    auto_observation: dict[str, Any] | None = None
    started_at = time.monotonic()
    previous_alarm_handler: Any = None
    previous_timer: tuple[float, float] | None = None
    can_interrupt = bool(
        timeout_ms
        and hasattr(signal, "SIGALRM")
        and threading.current_thread() is threading.main_thread()
    )

    def _interrupt(_signum: int, _frame: Any) -> None:
        raise CellTimeout()

    try:
        try:
            if can_interrupt:
                previous_alarm_handler = signal.getsignal(signal.SIGALRM)
                signal.signal(signal.SIGALRM, _interrupt)
                previous_timer = signal.setitimer(
                    signal.ITIMER_REAL,
                    timeout_ms / 1000,
                )
            with redirect_stdout(stdout_buffer), redirect_stderr(stderr_buffer):
                try:
                    compiled = compile(source, filename, "exec")
                    exec(compiled, namespace, namespace)
                except BaseException as caught:
                    error = caught
                    timed_out = isinstance(caught, CellTimeout)
        finally:
            if can_interrupt:
                signal.setitimer(signal.ITIMER_REAL, 0)
                if previous_alarm_handler is not None:
                    signal.signal(signal.SIGALRM, previous_alarm_handler)
                if previous_timer and previous_timer[0] > 0:
                    signal.setitimer(signal.ITIMER_REAL, *previous_timer)
        if cu_instrumentation is not None:
            auto_observation = cu_instrumentation.auto_observe_after_cell(
                error_code=str(getattr(error, "code", "") or ""),
                timed_out=timed_out,
            )
    finally:
        cu_ax_instrumentation.restore_executor(previous_cu_ax_state)
        if cu_instrumentation is not None:
            cu_instrumentation.restore_executor(previous_cu_state)
        core.install_executor(previous_executor, strict=previous_strict)
        sys.argv = previous_argv

    stdout = stdout_buffer.getvalue()
    stderr = stderr_buffer.getvalue()
    profile = profile_state.profile
    observations = executor.finalize()
    envelope: dict[str, Any] = {
        "v": ENVELOPE_VERSION,
        "ok": error is None,
        "timed_out": timed_out,
        "elapsed_ms": int((time.monotonic() - started_at) * 1000),
        "stdout": stdout[: profile.max_stdout_chars],
        "stderr": stderr[: profile.max_stderr_chars],
        "exc": _format_exception(error, filename) if error is not None else None,
        "actions": executor.actions,
        "actions_total": executor.actions_total,
        "actions_possible": executor.actions_possible,
        "observations": observations,
        "images": executor.images,
        "env_before": environment_before,
        "env": _environment_snapshot(),
        "gac_profile": profile.to_envelope(),
    }
    if auto_observation and auto_observation.get("attempted"):
        envelope["auto_observation"] = auto_observation
    truncated: dict[str, int] = {}
    if len(stdout) > profile.max_stdout_chars:
        truncated["output"] = len(stdout) - profile.max_stdout_chars
    if len(stderr) > profile.max_stderr_chars:
        truncated["output"] = (
            truncated.get("output", 0) + len(stderr) - profile.max_stderr_chars
        )
    if executor.actions_dropped:
        truncated["actions"] = executor.actions_dropped
    if executor.observations_dropped:
        truncated["observations"] = executor.observations_dropped
    if executor.images_dropped:
        truncated["images"] = executor.images_dropped
    if truncated:
        envelope["truncated"] = truncated
    linecache.cache.pop(filename, None)
    return envelope


def run_code(
    code: str,
    *,
    filename: str = DEFAULT_FILENAME,
    timeout_ms: int | None = None,
) -> dict[str, Any]:
    """Compile and execute ``code`` with one isolated GAC profile selection."""

    with gac_profile_scope() as profile_state:
        return _run_code_in_profile(
            code,
            profile_state=profile_state,
            filename=filename,
            timeout_ms=timeout_ms,
        )

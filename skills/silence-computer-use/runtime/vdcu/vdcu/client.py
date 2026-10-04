"""Client side: find the running session, call it, save frames.

The CLI and the MCP server are both thin clients of the daemon, so they share this
module.  A session is discovered through ``%LOCALAPPDATA%\\vdcu\\session.json``,
which is written by the daemon and ignored when its process is gone.
"""

from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request

from . import desktop as desktop_module
from .session import STATE_PATH, load_state


class NoSession(RuntimeError):
    """No live session; the caller should start one or tell the user to."""


class SessionGone(RuntimeError):
    """A session existed but stopped answering."""


class Client:
    def __init__(self, state: dict):
        self.state = state
        self.url = state["url"].rstrip("/")
        self.token = state["token"]
        self.desktop = state.get("desktop", "?")
        self.engine = state.get("engine", "stealth")
        self.mode = state.get("mode", "stealth")

    # ------------------------------------------------------------------ #
    # Discovery
    # ------------------------------------------------------------------ #

    @classmethod
    def current(cls) -> "Client":
        state = load_state()
        if not state:
            raise NoSession(
                "no live agent desktop; start one with 'vdcu enter' "
                "(or call the vd_enter tool)"
            )
        return cls(state)

    @classmethod
    def wait(cls, timeout: float = 60.0, *, log=None, daemon_pid: int | None = None) -> "Client":
        """Wait for a session to publish itself, e.g. right after starting one.

        ``daemon_pid`` lets the wait fail fast: when the daemon dies during
        start-up (the user cancelled the password box, the engine failed), the
        session will never publish — polling for it until the timeout just makes
        the CLI hang.
        """

        deadline = time.time() + timeout
        last_error = ""
        while time.time() < deadline:
            if daemon_pid and not desktop_module.win32.process_alive(daemon_pid):
                raise NoSession(
                    "daemon exited during start-up (password cancelled, another shadow "
                    "session already running, or engine failed) — see the end of "
                    "%LOCALAPPDATA%\\vdcu\\shadow_host.log for the host's own reason"
                )
            state = load_state()
            if state:
                client = cls(state)
                try:
                    client.health()
                    return client
                except (SessionGone, OSError) as error:
                    last_error = str(error)
            time.sleep(0.4)
        raise NoSession(f"session did not come up within {timeout:.0f}s {last_error}")

    # ------------------------------------------------------------------ #
    # Calls
    # ------------------------------------------------------------------ #

    def _post(self, path: str, payload: dict, timeout: float) -> dict:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.url}{path}", data=body, headers={"Content-Type": "application/json"}
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:300]
            raise SessionGone(f"session returned HTTP {error.code}: {detail}") from error
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            raise SessionGone(f"session is not answering: {error}") from error

    def rpc(self, op: str, args: dict | None = None, *, timeout: float = 180.0) -> dict:
        return self._post("/rpc", {"token": self.token, "op": op, "args": args or {}}, timeout)

    def health(self) -> dict:
        request = urllib.request.Request(f"{self.url}/health?token={self.token}")
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))

    def status(self) -> dict:
        result = self.rpc("status", timeout=20)
        return result.get("result", result)

    def frame_png(self, max_width: int = 720) -> bytes:
        request = urllib.request.Request(
            f"{self.url}/frame.png?w={max_width}&token={self.token}&t={time.time()}"
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            raise SessionGone(f"could not fetch a frame: {error}") from error

    def stop(self) -> dict:
        """Stop the session; returns the teardown report the daemon produced.

        The daemon exits the moment the session stops, which can take the HTTP
        response with it — in that case the report is read from the file the daemon
        writes just before exiting, so the caller still learns what was left behind.
        """

        try:
            response = self.rpc("stop", timeout=60)
            result = response.get("result") or response
            if isinstance(result, dict) and ("foreign_processes" in result or result.get("note")):
                return result
        except (SessionGone, NoSession):
            pass
        return read_stop_report() or {"stopping": True, "note": "session already gone"}

    def frame_path(self, tag: str = "frame") -> str:
        desktop_module.ensure_dirs()
        stamp = time.strftime("%H%M%S")
        return os.path.join(desktop_module.FRAME_DIR, f"{stamp}-{tag}-{os.getpid()}.png")


def read_stop_report(max_age: float = 120.0) -> dict | None:
    """The daemon's last teardown report, if it is recent enough to be this one."""

    from .session import LAST_STOP_PATH

    try:
        if time.time() - os.path.getmtime(LAST_STOP_PATH) > max_age:
            return None
        with open(LAST_STOP_PATH, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return None


def save_frame(
    payload: dict, *, tag: str = "frame", explicit: str | None = None, image_format: str | None = None
) -> str | None:
    """Write the frame carried by an RPC response and return its path."""

    encoded = payload.get("frame")
    if not encoded:
        return None
    desktop_module.ensure_dirs()
    detected = image_format or (payload.get("frame_meta") or {}).get("format") or "png"
    suffix = ".jpg" if detected == "jpeg" else ".png"
    if explicit:
        path = explicit
        os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    else:
        stamp = time.strftime("%H%M%S")
        path = os.path.join(desktop_module.FRAME_DIR, f"{stamp}-{tag}{suffix}")
    with open(path, "wb") as handle:
        handle.write(base64.b64decode(encoded))
    return path


def start_session(
    *,
    engine: str = "shadow",
    mode: str = "stealth",
    shell: bool = True,
    preview: bool = True,
    idle_timeout: float = 1800.0,
    max_seconds: float = 14400.0,
    name: str | None = None,
    wait: float = 90.0,
) -> dict:
    """Start the daemon as a detached process and wait until it serves.

    ``engine="shadow"`` runs the bundled runtime's own shadow-session engine (its signed
    interpreter + ``pip_sdk.dll``) and waits for authorization and rendering.
    Empty credentials can enter without a card; other accounts may need one.
    """

    if load_state():
        return {"already_running": True}

    python = desktop_module.win32.python_executable()
    command = (
        f'"{python}" -m vdcu daemon --engine {engine} --mode {mode} '
        f"--idle-timeout {idle_timeout:.0f} --max-seconds {max_seconds:.0f}"
    )
    if not shell:
        command += " --no-shell"
    if not preview:
        command += " --no-preview"
    if name:
        command += f" --name {name}"
    process = desktop_module.spawn_detached(command)
    if engine == "shadow":
        wait = max(wait, 660.0)
    client = Client.wait(wait, daemon_pid=process.pid)
    return {"already_running": False, "state": client.state}


def describe_desktop(name: str) -> dict:
    """Whether a desktop still exists, and which processes (if any) hold it.

    Called after ``leave``.  A shell session can leave the desktop object behind
    because third-party software (a tray helper, on this machine) attaches to it,
    and this package will not kill a process it cannot prove it started.  Saying so
    out loud beats a silent leftover.
    """

    from . import win32

    if name not in win32.enum_desktops():
        return {"desktop_exists": False, "held_by": {}}
    held = {
        pid: {
            "image": os.path.basename(win32.process_image_name(pid) or "?"),
            "title": info.get("title", ""),
        }
        for pid, info in win32.desktop_windows_detail(name).items()
    }
    return {"desktop_exists": True, "held_by": held}


__all__ = [
    "Client",
    "NoSession",
    "SessionGone",
    "save_frame",
    "start_session",
    "describe_desktop",
    "read_stop_report",
    "STATE_PATH",
]

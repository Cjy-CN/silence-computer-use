"""Lifecycle of the agent desktop and of the worker that lives on it.

The daemon owns a Win32 desktop handle for as long as the session lasts.  Closing
that handle destroys the desktop, so the handle *is* the session's lifetime — which
also means a crashed daemon cannot leave a desktop behind.

Everything the worker does arrives over a reverse socket connection: the worker is
created with ``CreateProcessW`` (the only way to set ``STARTUPINFO.lpDesktop``) and
dials back to the daemon.  That avoids inheriting pipe handles across that boundary
and gives the daemon an immediate EOF when the worker dies.
"""

from __future__ import annotations

import os
import queue
import socket
import socketserver
import subprocess
import sys
import threading
import time
import uuid
import ctypes

from . import win32

PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "vdcu"
)
FRAME_DIR = os.path.join(
    os.environ.get("TEMP") or os.path.expanduser("~"), "vdcu", "frames"
)


def ensure_dirs() -> None:
    os.makedirs(STATE_DIR, exist_ok=True)
    os.makedirs(FRAME_DIR, exist_ok=True)


class WorkerGone(RuntimeError):
    """The worker process died or its socket closed."""


class WorkerTimeout(TimeoutError):
    """The worker did not answer in time."""


# --------------------------------------------------------------------------- #
# The link to the worker
# --------------------------------------------------------------------------- #


class WorkerLink:
    """A request/response pipe to one worker, pumped by its socket handler thread."""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.queue: queue.Queue = queue.Queue()
        self.closed = threading.Event()
        self.info: dict = {}

    def run(self) -> None:
        import json

        while not self.closed.is_set():
            item = self.queue.get()
            if item is None:
                break
            request, future = item
            try:
                self.writer.write(json.dumps(request, ensure_ascii=False).encode("utf-8") + b"\n")
                self.writer.flush()
                line = self.reader.readline()
                if not line:
                    raise WorkerGone("worker closed the connection")
                future.set_result(json.loads(line.decode("utf-8", errors="replace")))
            except Exception as error:  # noqa: BLE001
                if not future.done():
                    future.set_exception(error)
                self.closed.set()
                break
            finally:
                if not future.done():
                    future.cancel()

    def request(self, op: str, args: dict | None = None, *, timeout: float = 60.0) -> dict:
        import concurrent.futures

        if self.closed.is_set():
            raise WorkerGone("worker connection is closed")
        future: concurrent.futures.Future = concurrent.futures.Future()
        self.queue.put(({"id": int(time.time() * 1000) % 1000000, "op": op, "args": args or {}}, future))
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as error:
            raise WorkerTimeout(f"worker did not answer {op!r} within {timeout}s") from error


class _ControlHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        import json

        server: "ControlServer" = self.server  # type: ignore[assignment]
        try:
            line = self.rfile.readline()
            if not line:
                return
            hello = json.loads(line.decode("utf-8", errors="replace"))
        except (OSError, ValueError):
            return

        if hello.get("token") != server.token:
            server.log("worker rejected: bad token")
            return

        link = WorkerLink(self.rfile, self.wfile)
        link.info = hello
        server.link = link
        server.connected.set()
        try:
            link.run()
        finally:
            server.connected.clear()


class ControlServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, token: str, log):
        self.token = token
        self.link: WorkerLink | None = None
        self.connected = threading.Event()
        self.log = log
        super().__init__(("127.0.0.1", 0), _ControlHandler)

    def handle_error(self, request, client_address):  # noqa: A002 - stdlib signature
        pass  # a dropped worker connection is reported through `connected`


# --------------------------------------------------------------------------- #
# The agent desktop
# --------------------------------------------------------------------------- #


class AgentDesktop:
    """One agent desktop plus the worker driving it."""

    def __init__(
        self,
        name: str | None = None,
        *,
        engine: str = "shadow",
        mode: str = "stealth",
        shell: bool = True,
        watchdog_seconds: float = 900.0,
        log=print,
    ):
        self.name = name or f"vdcu_{uuid.uuid4().hex[:10]}"
        self.engine = engine  # "stealth" (hidden desktop) | "shadow" (shadow RDP session)
        self.mode = mode
        self.shell = shell
        self.watchdog_seconds = watchdog_seconds
        self.log = log
        self.hdesk = None
        self.token = uuid.uuid4().hex
        self.control: ControlServer | None = None
        self.control_thread: threading.Thread | None = None
        self.worker_handle = None
        self.worker_pid: int | None = None
        self.guardian_handle = None
        self.guardian_pid: int | None = None
        self.link: WorkerLink | None = None
        self.started_at = 0.0
        self.last_used = 0.0
        self.switched = False  # takeover mode only
        self.foreign_processes: dict[int, dict] = {}

    # ------------------------------------------------------------------ #
    # Start / stop
    # ------------------------------------------------------------------ #

    def start(self, *, wait: float = 40.0) -> dict:
        ensure_dirs()
        self.started_at = self.last_used = time.time()

        if self.engine == "shadow":
            # The host only connects back after the user has typed their password
            # into its box, so the connection wait has to cover that interaction
            # (the host itself waits up to 600s for the password).
            wait = max(wait, 640.0)
            # The vendor engine provides its own isolated desktop (a shadow RDP
            # session), so there is no Win32 desktop to create here.  The host runs
            # the bundled runtime's signed interpreter — the only caller its signature
            # allow-list accepts — and talks the same worker protocol.
            self._start_control_server()
            self._start_guardian()
            self._start_shadow_host()
        else:
            self.hdesk = win32.create_desktop(
                self.name,
                win32.GENERIC_ALL | win32.DESKTOP_ENUMERATE | win32.DESKTOP_SWITCHDESKTOP,
            )
            if self.mode == "takeover":
                self.switch_to()
            self._start_control_server()
            self._start_guardian()
            self._start_worker()

        deadline = time.time() + wait
        while time.time() < deadline:
            if self.control and self.control.connected.is_set():
                break
            if self.worker_pid and not win32.process_alive(self.worker_pid):
                detail = ""
                if self.engine == "shadow":
                    # surface the host's own reason (e.g. another shadow session
                    # occupies the engine) instead of a bare "worker exited"
                    try:
                        with open(os.path.join(STATE_DIR, "shadow_host.log"),
                                  encoding="utf-8", errors="replace") as handle:
                            lines = [line for line in handle.read().splitlines() if line.strip()]
                        if lines:
                            detail = f" — host: {lines[-1][:200]}"
                    except OSError:
                        pass
                raise RuntimeError(f"worker exited during start-up{detail}")
            time.sleep(0.2)
        else:
            raise TimeoutError(f"worker did not connect within {wait}s")

        self.link = self.control.link
        ready = self.request("ping", timeout=15)
        return {"desktop": self.name, "engine": self.engine, "mode": self.mode,
                "worker": self.link.info, "ping": ready}

    def _start_shadow_host(self) -> None:
        """Spawn ``vdcu.shadow_host`` under the bundled runtime's signed sandbox interpreter."""

        try:
            from .shadow_engine import find_sandbox_python

            python = find_sandbox_python()
        except FileNotFoundError as error:
            raise RuntimeError(f"engine=shadow needs the bundled runtime's runtime: {error}") from error

        command = (
            f'"{python}" -m vdcu.shadow_host '
            f"--connect 127.0.0.1:{self.control.server_address[1]} --token {self.token}"
        )
        process = spawn_detached(command, log_name="shadow_host.log")
        self.worker_pid = process.pid
        self.log(
            f"[desktop] shadow host pid={self.worker_pid} under {os.path.basename(python)} "
            "(waiting for Windows authorization and a rendered session)"
        )

    def _start_control_server(self) -> None:
        self.control = ControlServer(self.token, self.log)
        self.control_thread = threading.Thread(target=self.control.serve_forever, daemon=True)
        self.control_thread.start()

    def _start_worker(self) -> None:
        command = (
            f'"{win32.python_executable()}" -m vdcu worker '
            f"--desktop {self.name} --mode {self.mode} "
            f"--connect 127.0.0.1:{self.control.server_address[1]} --token {self.token}"
        )
        if not self.shell:
            command += " --no-shell"
        environment = dict(os.environ)
        environment["PYTHONPATH"] = PACKAGE_ROOT + os.pathsep + environment.get("PYTHONPATH", "")
        environment["PYTHONIOENCODING"] = "utf-8"
        self.worker_handle, self.worker_pid = win32.launch_on_desktop(
            command,
            f"winsta0\\{self.name}",
            flags=win32.CREATE_NO_WINDOW | win32.CREATE_NEW_PROCESS_GROUP,
            env=environment,
            cwd=PACKAGE_ROOT,
        )
        self.log(f"[desktop] worker pid={self.worker_pid} on {self.name}")

    def _start_guardian(self) -> None:
        """A detached process that cleans up even if this daemon is killed."""

        command = (
            f'"{win32.python_executable()}" -m vdcu watchdog '
            f"--pid {win32.kernel32.GetCurrentProcessId()} "
            f"--desktop {self.name} --mode {self.mode} --worker {self.worker_pid} "
            f"--parent-seconds {self.watchdog_seconds:.0f}"
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = PACKAGE_ROOT + os.pathsep + environment.get("PYTHONPATH", "")
        try:
            self.guardian_handle, self.guardian_pid = win32.launch_on_desktop(
                command,
                None,
                flags=win32.DETACHED_PROCESS | win32.CREATE_NO_WINDOW | win32.CREATE_NEW_PROCESS_GROUP,
                env=environment,
                cwd=PACKAGE_ROOT,
            )
            if self.guardian_handle:
                win32.kernel32.CloseHandle(self.guardian_handle)
                self.guardian_handle = None
            self.log(f"[desktop] guardian pid={self.guardian_pid}")
        except win32.Win32Error as error:
            self.log(f"[desktop] guardian could not start: {error}")

    def close(self) -> None:
        tracked: list[int] = []
        tracked_images: set[str] = set()
        if self.link:
            try:
                response = self.link.request("tracked_pids", timeout=5)
                tracked = list((response.get("result") or {}).get("pids") or [])
                tracked_images = {
                    str(name).casefold()
                    for name in ((response.get("result") or {}).get("images") or [])
                    if name
                }
            except Exception:  # noqa: BLE001
                tracked = []
            try:
                self.link.request("quit", timeout=5)
            except Exception:  # noqa: BLE001
                pass
        self.link = None
        if self.control:
            try:
                self.control.shutdown()
                self.control.server_close()
            except Exception:  # noqa: BLE001
                pass
            self.control = None
        if self.worker_pid and win32.process_alive(self.worker_pid):
            self._kill(self.worker_pid)
        # Only processes proven to be ours may be killed.  "Proven" has two routes:
        # the parent chain back to something we started, or running an executable we
        # launched (Windows 11 Notepad hands its window to a process that is not a
        # child of the launcher — the parent rule alone let it hold the desktop).
        # Third-party software puts windows on the agent desktop too (a FortiTray
        # helper did) and is never touched.
        self.foreign_processes = self._reap_our_processes(
            [self.worker_pid, *tracked], images=tracked_images
        )
        if self.mode == "takeover" and self.switched:
            self.switch_back()
        if self.hdesk:
            win32.close_desktop(self.hdesk)
            self.hdesk = None
        if self.guardian_pid:
            self._kill(self.guardian_pid)
            self.guardian_pid = None

    def _reap_our_processes(
        self, roots: list[int], *, images: set[str] | None = None,
        rounds: int = 14, delay: float = 0.5,
    ) -> dict[int, dict]:
        """End the processes we started and wait for the desktop to empty.

        A process counts as ours when it descends from one of ``roots`` **or** runs
        one of the executables in ``images`` (case-insensitive basename) — the second
        rule exists because app hand-offs (Windows 11 Notepad) land in processes that
        are not our children.  Anything else is reported as foreign and left alone:
        ``return`` value lists exactly what was spared.

        A desktop object is destroyed only once nothing is attached to it, so this
        has to actually converge: each round kills the matches still alive and waits,
        and the caller closes the handle afterwards.
        """

        if not self.hdesk:
            return {}
        valid_roots = [pid for pid in roots if pid]
        wanted_images = images or set()
        foreign: dict[int, dict] = {}
        for _ in range(rounds):
            foreign = {}
            alive_ours = False
            for pid, info in win32.desktop_windows_detail(self.name).items():
                if pid == os.getpid():
                    continue
                image = os.path.basename(win32.process_image_name(pid) or "").casefold()
                ours = any(win32.is_descendant_of(pid, root) for root in valid_roots) or (
                    bool(image) and image in wanted_images
                )
                if ours:
                    self._kill(pid)
                    alive_ours = True
                else:
                    info = dict(info)
                    info["image"] = image or "?"
                    foreign[pid] = info
            if not alive_ours:
                break
            time.sleep(delay)
        if foreign:
            self.log(
                f"[desktop] {len(foreign)} window(s) on {self.name} belong to other "
                f"software and were left alone: "
                + ", ".join(sorted({entry.get('image', '?') for entry in foreign.values()}))
            )
        return foreign

    def _kill(self, pid: int) -> None:
        try:
            handle = win32.kernel32.OpenProcess(
                win32.PROCESS_TERMINATE | win32.SYNCHRONIZE, False, pid
            )
            if handle:
                win32.kernel32.TerminateProcess(handle, 1)
                win32.kernel32.CloseHandle(handle)
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ #
    # Takeover helpers (only used when mode == "takeover")
    # ------------------------------------------------------------------ #

    def switch_to(self) -> bool:
        ok = win32.switch_desktop(self.hdesk)
        self.switched = bool(ok)
        return self.switched

    def switch_back(self) -> bool:
        try:
            handle = win32.open_desktop("Default", win32.DESKTOP_SWITCHDESKTOP)
        except win32.Win32Error:
            return False
        try:
            ok = bool(win32.switch_desktop(handle))
            self.switched = not ok
            return ok
        finally:
            win32.close_desktop(handle)

    # ------------------------------------------------------------------ #
    # Requests
    # ------------------------------------------------------------------ #

    def request(self, op: str, args: dict | None = None, *, timeout: float = 60.0) -> dict:
        if not self.link:
            raise WorkerGone("no worker link")
        self.last_used = time.time()
        response = self.link.request(op, args, timeout=timeout)
        if not isinstance(response, dict):
            raise WorkerGone("malformed worker response")
        if not response.get("ok"):
            error = response.get("error") or {}
            raise WorkerError(
                error.get("code", "VD_OPERATION_FAILED"), error.get("message", "worker error")
            )
        return response

    def capture(self, *, max_width: int | None = 720, full: bool = False, timeout: float = 60.0) -> dict:
        response = self.request("capture", {"max_width": max_width, "full": full}, timeout=timeout)
        return response

    def idle_seconds(self) -> float:
        return time.time() - self.last_used

    def alive(self) -> bool:
        if not self.worker_pid or not win32.process_alive(self.worker_pid):
            return False
        return bool(self.control and self.control.connected.is_set())


class WorkerError(RuntimeError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(f"[{code}] {message}")


def spawn_detached(
    command: str, *, env_extra: dict | None = None, log_name: str | None = None
) -> subprocess.Popen:
    """Start a helper (guardian, preview) with no console window.

    ``log_name`` captures the helper's output in ``%LOCALAPPDATA%\\vdcu\\<log_name>``.
    Worth having: a detached GUI helper that dies at start-up is otherwise invisible,
    which is exactly how the preview box failed once.
    """

    environment = dict(os.environ)
    environment["PYTHONPATH"] = PACKAGE_ROOT + os.pathsep + environment.get("PYTHONPATH", "")
    if env_extra:
        environment.update(env_extra)

    if log_name:
        ensure_dirs()
        log = open(os.path.join(STATE_DIR, log_name), "ab", buffering=0)
        stdout = stderr = log
    else:
        stdout = stderr = subprocess.DEVNULL

    return subprocess.Popen(
        command,
        env=environment,
        cwd=PACKAGE_ROOT,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0),
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
    )


def local_ipc_port() -> int:
    """An ephemeral free port on loopback."""

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


__all__ = [
    "AgentDesktop",
    "WorkerError",
    "WorkerGone",
    "WorkerTimeout",
    "spawn_detached",
    "ensure_dirs",
    "STATE_DIR",
    "FRAME_DIR",
    "PACKAGE_ROOT",
]

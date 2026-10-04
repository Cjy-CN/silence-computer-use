"""The worker: the process that actually lives on the agent desktop.

Why a separate process at all
-----------------------------
Measured, not assumed: ``GetDC(NULL)`` is scoped to the desktop owned by the
calling thread, and ``BitBlt`` across desktops fails with
``ERROR_INVALID_HANDLE``.  ``SendInput`` is scoped the same way — it feeds the
*active input* desktop.  A process on the user's desktop therefore cannot capture
or drive another desktop no matter how it binds its threads; a process created
with ``STARTUPINFO.lpDesktop`` can, because it is born there.

Protocol
--------
Newline-delimited JSON on stdin/stdout::

    --> {"id": 7, "op": "click", "args": {"x": 500, "y": 250}}
    <-- {"id": 7, "ok": true, "result": {...}, "frame": "<base64 png>"}

Every response that changed the screen carries a fresh frame, which is what makes
the agent's conversation show the desktop live — the behaviour the bundled runtime shows in
its own chat.

Run directly only for debugging: ``python -m vdcu worker --desktop NAME``.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
import traceback

from . import apps, primitives, stealth, win32

PROTOCOL_VERSION = 1

# The install/skill root that contains the vdcu package — autolaunch.json lives here.
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Worker:
    def __init__(self, desktop: str, *, mode: str = "stealth", shell: bool = True):
        self.desktop = desktop
        self.mode = mode
        self.shell = shell
        self.hdesk = None
        self.executor = None
        self.started = time.time()
        self.catalog: list[dict] = []
        self.launched_pids: list[int] = []
        self.launched_images: list[str] = []
        self.lock = threading.Lock()
        self.shell_process = None
        self.info: dict = {}
        self.pending_frame: dict | None = None
        self.shell_ready = False

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> dict:
        self.hdesk = win32.open_desktop(
            self.desktop,
            win32.DESKTOP_ALL_ACCESS | win32.DESKTOP_ENUMERATE | win32.DESKTOP_SWITCHDESKTOP,
        )
        # The process was created onto this desktop; binding the thread makes the
        # intent explicit and fails loudly if it was not.
        win32.set_thread_desktop(self.hdesk)
        # Which executor depends on the mode: stealth drives the desktop with window
        # messages (the user's desktop stays untouched); takeover has switched the
        # display to this desktop and uses the real input stack, which is the only
        # reason anyone would accept the intrusion.  Routing takeover through the
        # message executor would make its documentation a lie.
        if self.mode == "takeover":
            from . import takeover

            self.executor = takeover.TakeoverExecutor(self.desktop, self.hdesk)
        else:
            self.executor = stealth.StealthExecutor(self.desktop, self.hdesk)
        primitives.use_desktop_context(self.desktop, self.hdesk)
        self._autolaunch()

        if self.shell:
            self.shell_process = self._start_shell()
            self._await_shell()

        return {
            "protocol": PROTOCOL_VERSION,
            "mode": self.mode,
            "desktop": self.desktop,
            "thread_desktop": win32.object_name(win32.thread_desktop()),
            "active_input_desktop": win32.active_input_desktop_name(),
            "shell": bool(self.shell_process),
            "shell_pid": (self.shell_process or {}).get("pid"),
            "shell_ready": self.shell_ready,
            "pid": win32.kernel32.GetCurrentProcessId(),
        }

    def _autolaunch(self) -> None:
        """Fire configured processes onto this desktop as early as possible.

        ``<vdcu root>/autolaunch.json`` lists command lines to start the moment
        the worker owns the desktop — before the shell paint wait and before the
        session is published.  Slow-booting helpers (the KimiCU agent) then
        overlap their cold start with all of that instead of being launched by
        an RPC after everything else has finished.  ``{root}`` expands to the
        vdcu root, ``{kimicu}`` to ``<root>/../cu.exe`` (normalized), and
        %-env vars expand everywhere.  Launched processes join the session job
        and are tracked like RPC-launched ones, so teardown still reaps them.
        """

        config = os.path.join(_ROOT, "autolaunch.json")
        try:
            with open(config, encoding="utf-8") as handle:
                commands = json.load(handle).get("commands", [])
        except (OSError, ValueError):
            return
        kimicu = os.path.normpath(os.path.join(_ROOT, os.pardir, "cu.exe"))
        for command in commands:
            try:
                application = os.path.expandvars(
                    str(command["application"]).replace("{root}", _ROOT).replace("{kimicu}", kimicu)
                )
                command_line = command.get("command_line")
                if command_line is not None:
                    command_line = os.path.expandvars(
                        str(command_line).replace("{root}", _ROOT).replace("{kimicu}", kimicu)
                    )
                line = application + (f" {command_line}" if command_line else "")
                handle, pid = win32.launch_on_desktop(
                    line,
                    f"winsta0\\{self.desktop}",
                    hide_window=bool(command.get("hide_window", True)),
                )
                win32.assign_to_job(apps.session_job(), handle)
                win32.kernel32.CloseHandle(handle)
                self.launched_pids.append(int(pid))
                image = line.split('"')[1] if line.startswith('"') else line.split()[0]
                self.launched_images.append(os.path.basename(image))
            except Exception as error:  # noqa: BLE001 - autolaunch must never break start()
                if os.environ.get("VDCU_DEBUG"):
                    print(f"autolaunch failed: {command}: {error}", file=sys.stderr, flush=True)

    def _await_shell(self, timeout: float = 20.0) -> bool:
        """Wait until the shell has actually painted the desktop.

        ``explorer.exe`` becomes the shell of a fresh desktop a few seconds after it
        starts.  Without this wait, a capture taken immediately after ``enter`` sees an
        empty grey plane — which is exactly how the agent desktop looked "broken" in a
        test: the shell was running, it simply had not appeared yet.
        """

        deadline = time.time() + timeout
        while time.time() < deadline:
            classes = {window["class"] for window in self.executor.windows(include_untitled=True)}
            if {"Progman", "Shell_TrayWnd"} & classes:
                self.shell_ready = True
                time.sleep(0.5)  # let the wallpaper and icons finish painting
                return True
            time.sleep(0.4)
        self.shell_ready = False
        return False

    def _start_shell(self):
        """Run explorer on the agent desktop, so it looks like a real desktop.

        This is what makes the preview show wallpaper, desktop icons and a taskbar —
        the look the bundled runtime's own preview has.  Measured: the shell really does come
        up on the hidden desktop (``Progman`` + ``Shell_TrayWnd`` owned by the new
        explorer process) and nothing appears on the user's desktop.

        It is *not* used to launch packaged apps: shell activation goes through the
        session's shell broker and lands on the user's desktop (see vdcu.apps).
        """

        try:
            handle, pid = win32.launch_on_desktop(
                "explorer.exe", f"winsta0\\{self.desktop}", hide_window=False
            )
            win32.assign_to_job(apps.session_job(), handle)
            win32.kernel32.CloseHandle(handle)
            return {"pid": pid}
        except win32.Win32Error as error:
            if os.environ.get("VDCU_DEBUG"):
                print(f"shell could not start: {error}", file=sys.stderr, flush=True)
            return None

    def close(self) -> None:
        """Stop everything this worker started, then release the desktop handle.

        Killing the launched processes is not tidiness for its own sake: a desktop
        object survives as long as a process is attached to it, so an app left
        running would keep the agent desktop alive after the session ended.  That
        was observed — a desktop from an earlier experiment was still listed after
        ``CloseDesktop`` returned success.
        """

        for pid in list(self.launched_pids):
            try:
                if win32.process_alive(pid):
                    handle = win32.kernel32.OpenProcess(
                        win32.PROCESS_TERMINATE | win32.SYNCHRONIZE, False, pid
                    )
                    if handle:
                        win32.kernel32.TerminateProcess(handle, 1)
                        win32.kernel32.CloseHandle(handle)
            except Exception:  # noqa: BLE001
                pass
        self.launched_pids = []
        if self.shell_process:
            try:
                handle = win32.kernel32.OpenProcess(win32.PROCESS_TERMINATE, False, self.shell_process["pid"])
                if handle:
                    # The shell owns Progman and the taskbar; ending it is what lets the
                    # desktop object be destroyed once our handle closes.
                    win32.kernel32.TerminateProcess(handle, 1)
                    win32.kernel32.CloseHandle(handle)
            except Exception:  # noqa: BLE001
                pass
            self.shell_process = None
        if self.hdesk:
            win32.close_desktop(self.hdesk)
            self.hdesk = None

    # ------------------------------------------------------------------ #
    # Operations
    # ------------------------------------------------------------------ #

    def dispatch(self, op: str, args: dict) -> dict:
        handler = getattr(self, f"op_{op}", None)
        if handler is None:
            raise ValueError(f"unknown op {op!r}")
        return handler(**args)

    def op_ping(self) -> dict:
        return {
            "pong": True,
            "uptime": round(time.time() - self.started, 2),
            "mode": self.mode,
            **{key: value for key, value in self.info.items() if key != "protocol"},
        }

    def op_status(self) -> dict:
        status = self.executor.status()
        ours = set(self.tracked_pids())
        foreign = {}
        for pid, info in win32.desktop_windows_detail(self.desktop).items():
            if pid in ours or any(win32.is_descendant_of(pid, root) for root in ours):
                continue
            foreign[pid] = {
                "image": os.path.basename(win32.process_image_name(pid) or "?"),
                "title": info.get("title", ""),
                "class": info.get("class", ""),
            }
        status.update(
            {
                "uptime": round(time.time() - self.started, 2),
                "desktop_exists": self.desktop in win32.enum_desktops(),
                "shell_pid": (self.shell_process or {}).get("pid"),
                "shell_ready": self.shell_ready,
                "tracked_pids": sorted(ours),
                # Third-party software really does open windows on the agent desktop
                # (a FortiTray helper did).  They are reported, never touched.
                "foreign_processes": foreign,
            }
        )
        return status

    def tracked_pids(self) -> list[int]:
        """Processes this session started: the worker, the shell, the launched apps."""

        pids = [win32.kernel32.GetCurrentProcessId(), *self.launched_pids]
        if self.shell_process:
            pids.append(int(self.shell_process["pid"]))
        return pids

    def op_tracked_pids(self) -> dict:
        return {"pids": self.tracked_pids(), "images": sorted(set(self.launched_images))}

    def op_capture(
        self, max_width: int | None = 720, full: bool = False,
        format: str = "png", quality: int = 88,
    ) -> dict:
        result = self.executor.capture_composite(
            max_width=None if full else max_width, image_format=format, quality=int(quality)
        )
        # Hand the frame to the response writer instead of letting it capture again:
        # a second capture ignored the caller's resolution (it used a hard-coded 720)
        # and silently downgraded every "native resolution" request to 512×288.
        self.pending_frame = result
        return {
            "width": result["width"],
            "height": result["height"],
            "image_width": result.get("image_width", result["width"]),
            "image_height": result.get("image_height", result["height"]),
            "format": result["format"],
            "windows_painted": result["windows_painted"],
            "windows_total": result["windows_total"],
            "paint_order": result.get("paint_order"),
            "non_blank": result["non_blank"],
        }

    def frame_for_response(self, args: dict) -> dict:
        """The frame attached to a response: the capture just made, or a fresh one."""

        if self.pending_frame is not None:
            frame = self.pending_frame
            self.pending_frame = None
            return frame
        width = args.get("frame_max_width") or args.get("max_width") or 720
        return self.executor.capture_composite(
            max_width=width,
            image_format=args.get("frame_format") or args.get("format") or "png",
        )

    def op_windows(self, include_untitled: bool = False) -> dict:
        windows = self.executor.windows(include_untitled=include_untitled)
        # Annotate the z-order rank the compositor will use: 0 is topmost.  This is
        # both diagnostic (it is how the "painted over" bug was finally pinned) and
        # useful to an agent reasoning about overlap.
        ranks = {hwnd: index for index, hwnd in enumerate(win32.z_order_handles())}
        for window in windows:
            window["z_rank"] = ranks.get(window["hwnd"])
        return {"windows": windows}

    def op_hit_test(self, x, y) -> dict:
        return self.executor.hit_test_trace(x, y)

    def op_click(self, x, y, button: str = "left", clicks: int = 1) -> dict:
        return self.executor.click(x, y, button=button, clicks=int(clicks))

    def op_double_click(self, x, y) -> dict:
        return self.executor.click(x, y, clicks=2)

    def op_move(self, x, y) -> dict:
        return self.executor.move(x, y)

    def op_drag(self, x1, y1, x2, y2, duration_ms: int = 1000) -> dict:
        return self.executor.drag(x1, y1, x2, y2, duration_ms=int(duration_ms))

    def op_scroll(self, x, y, delta, horizontal: bool = False) -> dict:
        # ``horizontal`` is the seed wire flag; direction aliases ("left"/
        # "right") inside delta imply it too — either source turns it on.
        notches, alias_horizontal = primitives._scroll_delta(delta)
        return self.executor.scroll(
            x, y, notches, horizontal=bool(horizontal) or alias_horizontal)

    def op_type(self, text: str) -> dict:
        return self.executor.type_text(str(text))

    def op_key(self, key: str) -> dict:
        vk = primitives._vk_for(str(key))
        return self.executor.key(vk)

    def op_key_down(self, key: str) -> dict:
        return self.executor.key_down(primitives._vk_for(str(key)))

    def op_key_up(self, key: str) -> dict:
        return self.executor.key_up(primitives._vk_for(str(key)))

    def op_copy_selection(self) -> dict:
        return self.executor.copy_selection()

    def op_paste_into(self, hwnd: int, text: str, use_clipboard: bool = False) -> dict:
        return self.executor.paste_into(int(hwnd), str(text), use_clipboard=bool(use_clipboard))

    def op_hotkey(self, keys: list) -> dict:
        vks = [primitives._vk_for(str(item)) for item in keys]
        return self.executor.hotkey(vks)

    def op_clipboard_get(self) -> dict:
        return {"text": self.executor.clipboard()}

    def op_clipboard_set(self, text: str) -> dict:
        self.executor.set_clipboard(str(text))
        return {"length": len(str(text))}

    def op_window_text(self, hwnd: int, limit: int = 4096) -> dict:
        return {"text": self.executor.window_text(int(hwnd), int(limit))}

    def op_children(self, hwnd: int) -> dict:
        """Child windows of one window, with class names and readable text.

        Used to reach the real control inside an app (an EDIT inside a dialog) when
        a test or an agent needs to read back what it typed.
        """

        import ctypes

        children: list[dict] = []

        def callback(child, _lparam):
            buffer = ctypes.create_unicode_buffer(256)
            win32.user32.GetClassNameW(child, buffer, 256)
            entry = {"hwnd": int(child), "class": buffer.value}
            length = win32.user32.GetWindowTextLengthW(child)
            if length > 0:
                text = ctypes.create_unicode_buffer(length + 1)
                win32.user32.GetWindowTextW(child, text, length + 1)
                entry["title"] = text.value
            children.append(entry)
            return True

        win32.user32.EnumChildWindows(
            int(hwnd), win32.WNDENUMPROC(callback), 0
        )
        return {"children": children}

    def op_close_window(self, hwnd: int) -> dict:
        return self.executor.close_window(int(hwnd))

    def op_list_apps(self, query: str = "", limit: int = 0, **_ignored) -> dict:
        """The application catalogue, optionally narrowed by ``query``.

        The extra parameters exist so the CLI and MCP can speak to either engine
        the same way: the shadow engine searches its own catalogue, this one
        filters the Start-menu catalogue in memory.
        """

        catalog = primitives.list_apps()
        self.catalog = [dict(app) for app in catalog]
        apps = self.catalog
        if query:
            needle = str(query).casefold()
            apps = [
                app for app in apps
                if needle in app["name"].casefold() or needle in app.get("key", "").casefold()
            ]
        # ``count`` is how many matched, before the limit — a caller asking for 40
        # still learns whether 5 or 500 matched.
        matched = len(apps)
        if limit:
            apps = apps[: int(limit)]
        return {"count": matched, "apps": apps}

    def op_launch(self, name: str) -> dict:
        # Resolution (exact name, then ASCII key, then unique substring) and the
        # implicit listing both live in primitives, so the CLI, the MCP server and
        # this worker cannot drift apart.
        entry = primitives._resolve(str(name))
        result = apps.launch(entry, self.desktop, hdesk=self.hdesk)
        if result.get("pid"):
            self.launched_pids.append(int(result["pid"]))
        if result.get("image"):
            self.launched_images.append(str(result["image"]))
        return result

    def op_wait(self, seconds=1) -> dict:
        primitives.wait(seconds)
        return {"waited": seconds}

    def op_actions(self, limit: int = 20) -> dict:
        return {"actions": self.executor.actions[-int(limit) :]}

    def op_quit(self) -> dict:
        return {"quitting": True}


# --------------------------------------------------------------------------- #
# Framing
# --------------------------------------------------------------------------- #

_FRAME_OPS = {
    "capture", "click", "double_click", "drag", "scroll", "type", "key", "hotkey",
    "key_down", "key_up", "copy_selection", "launch", "move", "close_window", "wait",
}


class Channel:
    """Newline-delimited JSON over any binary stream pair."""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer

    def send(self, payload: dict) -> None:
        self.writer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
        self.writer.flush()

    def requests(self):
        for raw in self.reader:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as error:
                self.send(
                    {"id": 0, "ok": False, "error": {"code": "VD_BAD_REQUEST", "message": str(error)}}
                )


def serve(channel: Channel, desktop: str, *, mode: str = "stealth", shell: bool = True) -> int:
    """Answer requests on ``channel``.

    The protocol is strictly one response per request: a worker that volunteered an
    extra message at start-up used to shift the daemon's matching by one, so a
    ``capture`` call returned the previous ``ping`` payload.  Start-up information
    therefore travels inside the ``ping`` result instead.
    """

    worker = Worker(desktop, mode=mode, shell=shell)
    startup_error: Exception | None = None
    try:
        worker.info = worker.start()
    except Exception as error:  # noqa: BLE001
        startup_error = error

    for request in channel.requests():
        request_id = request.get("id", 0)
        op = request.get("op", "")
        args = request.get("args") or {}
        if startup_error is not None:
            channel.send(
                {
                    "id": request_id,
                    "ok": False,
                    "error": {
                        "code": "VD_WORKER_START_FAILED",
                        "message": repr(startup_error),
                        "trace": "".join(
                            traceback.format_exception(
                                type(startup_error), startup_error, startup_error.__traceback__
                            )
                        )[-800:],
                    },
                }
            )
            continue
        with worker.lock:
            try:
                result = worker.dispatch(op, args)
                response = {"id": request_id, "ok": True, "result": result}
                if op in _FRAME_OPS or args.get("with_frame"):
                    capture = worker.frame_for_response(args)
                    response["frame"] = base64.b64encode(capture["frame"]).decode("ascii")
                    response["frame_meta"] = {
                        "width": capture["width"],
                        "height": capture["height"],
                        "image_width": capture.get("image_width", capture["width"]),
                        "image_height": capture.get("image_height", capture["height"]),
                        "format": capture.get("format", "png"),
                        "non_blank": capture["non_blank"],
                        "windows_total": capture["windows_total"],
                    }
            except Exception as error:  # noqa: BLE001
                code = getattr(error, "code", "VD_OPERATION_FAILED")
                response = {
                    "id": request_id,
                    "ok": False,
                    "error": {
                        "code": code,
                        "message": str(error),
                        "type": builtins_type(error).__name__,
                        "trace": traceback.format_exc(limit=3),
                    },
                }
                if os.environ.get("VDCU_DEBUG"):
                    print(response["error"]["trace"], file=sys.stderr, flush=True)
            try:
                channel.send(response)
            except OSError:
                break
        if op == "quit":
            break
    # Falling out of the loop means the socket closed: the parent is gone, so
    # release the desktop handle and let the process end.
    worker.close()
    return 0


# ``type`` is shadowed at the end of vdcu.primitives, so keep a private alias for
# the one place in this module that needs the builtin.
builtins_type = type


def connect_and_serve(
    host: str, port: int, token: str, desktop: str, *, mode: str, shell: bool = True
) -> int:
    """Reverse-connect to the daemon and serve requests over that socket.

    A socket rather than inherited pipes because the process had to be created with
    ``CreateProcessW`` (only it can set ``lpDesktop``), and handle inheritance across
    that boundary is a needless source of bugs.  The socket also gives the daemon a
    clean EOF the moment this process dies.
    """

    import socket

    deadline = time.time() + 20
    while True:
        try:
            sock = socket.create_connection((host, port), timeout=5)
            break
        except OSError:
            if time.time() > deadline:
                raise
            time.sleep(0.2)
    sock.settimeout(None)
    reader = sock.makefile("rb")
    writer = sock.makefile("wb")
    try:
        writer.write(json.dumps({"hello": "worker", "token": token, "desktop": desktop}).encode() + b"\n")
        writer.flush()
        return serve(Channel(reader, writer), desktop, mode=mode, shell=shell)
    finally:
        try:
            sock.close()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="vdcu worker", description="agent-desktop worker")
    parser.add_argument("--desktop", required=True)
    parser.add_argument("--mode", default="stealth", choices=("stealth", "takeover"))
    parser.add_argument("--no-shell", action="store_true", help="do not run explorer on the agent desktop")
    parser.add_argument("--connect", help="host:port of the daemon control socket")
    parser.add_argument("--token", default="")
    parser.add_argument("--stdio", action="store_true", help="debug: speak the protocol on stdio")
    options = parser.parse_args(argv)
    # Before anything queries screen metrics: an unaware process sees a virtualized
    # 1536×864 on a 1920×1080 display at 125% scaling, and captures come out resampled.
    win32.enable_dpi_awareness()

    if options.stdio or not options.connect:
        reader = sys.stdin.buffer
        writer = sys.stdout.buffer
        return serve(Channel(reader, writer), options.desktop, mode=options.mode,
                     shell=not options.no_shell)

    host, _, port_text = options.connect.partition(":")
    return connect_and_serve(
        host, int(port_text), options.token, options.desktop, mode=options.mode,
        shell=not options.no_shell,
    )


if __name__ == "__main__":
    raise SystemExit(main())

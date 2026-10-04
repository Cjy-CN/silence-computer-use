"""The session daemon: owns the agent desktop, and serves the agent and the viewer.

Why a daemon instead of a library call
--------------------------------------
The agent desktop must outlive a single tool call — an agent clicks, then types two
seconds later — so something has to hold the desktop handle and the worker between
calls.  That is this process.  It exposes one loopback HTTP endpoint used by three
different consumers:

* the **agent**, over ``POST /rpc`` (the CLI and the MCP server both speak it);
* the **live preview box**, over ``GET /frame.png``;
* a **browser**, over ``GET /`` — a page showing the same frames.

Nothing is bound to anything but ``127.0.0.1``, and every endpoint except the page
requires the session token that lives in ``%LOCALAPPDATA%\\vdcu\\session.json``.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import desktop as desktop_module
from .desktop import AgentDesktop, WorkerError, WorkerGone

STATE_PATH = os.path.join(desktop_module.STATE_DIR, "session.json")
LAST_STOP_PATH = os.path.join(desktop_module.STATE_DIR, "last_stop.json")

LIVE_PAGE = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>vdcu · 虚拟桌面实况</title>
<style>
  :root {{ color-scheme: light; }}
  body {{ margin:0; background:#f9f9f9; color:#000000;
         font:13px/1.5 "Segoe UI", "Microsoft YaHei", system-ui, sans-serif; }}
  header {{ padding:12px 18px; background:#ffffff; display:flex; gap:12px;
            align-items:center; flex-wrap:wrap; border-bottom:1px solid #eeeeee; }}
  .dot {{ width:16px; height:16px; border-radius:5px; background:#1070f0; display:inline-block; }}
  .muted {{ color:#8a8a8a; }}
  main {{ display:flex; gap:16px; padding:18px; align-items:flex-start; flex-wrap:wrap; }}
  .stage {{ background:#ffffff; border:1px solid #eeeeee; border-radius:16px; padding:10px; }}
  img {{ display:block; image-rendering:auto; background:#f0f0f0; border-radius:10px; }}
  aside {{ min-width:240px; flex:1; background:#ffffff; border:1px solid #eeeeee;
           border-radius:16px; padding:14px 16px; }}
  button {{ background:#f0f6ff; color:#1070f0; border:1px solid #d6e4ff; border-radius:999px;
            padding:6px 14px; cursor:pointer; font-weight:600; }}
  button:hover {{ background:#e0edff; }}
  code {{ background:#f0f0f0; padding:1px 6px; border-radius:6px; }}
  li {{ margin:2px 0; }}
</style></head>
<body>
<header>
  <span><span class="dot"></span> 虚拟桌面 <code>{desktop}</code></span>
  <span class="muted">模式 {mode}</span>
  <span class="muted">你的桌面：<code id="userdesk">-</code></span>
  <span class="muted" id="meta">等待首帧…</span>
  <span style="margin-left:auto">
    <button onclick="zoom(0.75)">缩小</button>
    <button onclick="zoom(1.3333)">放大</button>
    <button onclick="zoomTo({base_width})">原始</button>
  </span>
</header>
<main>
  <div class="stage"><img id="screen" width="{base_width}" alt="agent desktop"></div>
  <aside>
    <p class="muted">这是 agent 正在操作的虚拟桌面，你本机的桌面与鼠标键盘不受影响。</p>
    <p class="muted">最近动作</p>
    <ul id="actions" class="muted"></ul>
  </aside>
</main>
<script>
const token = {token_json};
let width = {base_width};
function url() {{ return `/frame.jpg?w=${{Math.round(width)}}&token=${{token}}&t=${{Date.now()}}`; }}
const img = document.getElementById('screen');
function tick() {{
  const next = new Image();
  next.onload = () => {{
    img.src = next.src; img.width = Math.round(width);
    document.getElementById('meta').textContent =
      `${{next.naturalWidth}}×${{next.naturalHeight}} · ${{new Date().toLocaleTimeString()}}`;
  }};
  next.src = url();
}}
function zoom(factor) {{ width = Math.max(160, Math.min(2400, width * factor)); tick(); }}
function zoomTo(value) {{ width = value; tick(); }}
function status() {{
  fetch(`/status?token=${{token}}`).then(r => r.json()).then(data => {{
    document.getElementById('userdesk').textContent = data.active_input_desktop || '-';
    const list = document.getElementById('actions');
    list.innerHTML = (data.actions || []).slice(-8).reverse()
      .map(a => `<li>${{a.op}} ${{a.x !== undefined ? `(${{a.x}}, ${{a.y}})` : ''}}</li>`).join('');
  }}).catch(() => {{}});
}}
setInterval(tick, 500); setInterval(status, 2000); tick(); status();
</script>
</body></html>
"""


class Session:
    def __init__(
        self,
        *,
        desktop_name: str | None = None,
        engine: str = "shadow",
        mode: str = "stealth",
        shell: bool = True,
        idle_timeout: float = 1800.0,
        max_seconds: float = 14400.0,
        preview: bool = True,
        log=print,
    ):
        self.desktop = AgentDesktop(
            desktop_name, engine=engine, mode=mode, shell=shell,
            watchdog_seconds=max_seconds, log=log,
        )
        self.engine = engine
        self.mode = mode
        self.shell = shell
        self.idle_timeout = idle_timeout
        self.max_seconds = max_seconds
        self.preview = preview
        self.log = log
        self.token = uuid.uuid4().hex
        self.http: ThreadingHTTPServer | None = None
        self.http_thread: threading.Thread | None = None
        self.preview_process = None
        self.started_at = 0.0
        self.frame_lock = threading.Lock()
        self.frame_cache: tuple[float, dict] | None = None
        self.last_status: dict = {}
        self.actions: list[dict] = []
        self.stopping = threading.Event()
        self._stopped = False
        self._stop_lock = threading.Lock()
        self.http_closed = threading.Event()

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> dict:
        desktop_module.ensure_dirs()
        info = self.desktop.start()
        self.started_at = time.time()
        self._start_http()
        self._write_state()
        if self.preview and self.engine != "shadow":
            # shadow 引擎的悬浮小窗 = 容器窗本身的 pip 形态（SDK 直接渲染，零延迟
            # 零模糊）；Tk 卡片小窗只服务 stealth 引擎，避免两个小窗打架
            self._spawn_preview()
        threading.Thread(target=self._watch_for_idle, daemon=True).start()
        self.log(
            f"[session] desktop={self.desktop.name} mode={self.mode} "
            f"http=127.0.0.1:{self.http.server_address[1]}"
        )
        return {
            "desktop": self.desktop.name,
            "mode": self.mode,
            "shell": self.shell,
            "url": self.url,
            "pid": os.getpid(),
            "worker_pid": self.desktop.worker_pid,
            "worker": info.get("worker"),
        }

    def stop(self, *, close_http: bool = True) -> dict:
        """Tear the session down.  Idempotent, and returns what it found.

        The teardown report matters: a stop that silently leaves a session's apps
        alive is the hardest failure to diagnose later.  When called from the HTTP
        handler, ``close_http`` is False so the response can still be written — the
        server is closed afterwards.
        """

        with self._stop_lock:
            if self._stopped:
                return {"stopping": True, "note": "already stopped"}
            self._stopped = True
        self.stopping.set()
        if self.preview_process:
            try:
                self.preview_process.terminate()
            except Exception:  # noqa: BLE001
                pass
            self.preview_process = None
        if close_http:
            self._close_http()
        report = {"stopping": True, "foreign_processes": {}}
        try:
            self.desktop.close()
            report["foreign_processes"] = {
                pid: info for pid, info in self.desktop.foreign_processes.items()
            }
        finally:
            self._clear_state()
            self._write_stop_report(report)
        return report

    def _write_stop_report(self, report: dict) -> None:
        """Persist the teardown report.

        The daemon exits as soon as the session stops, which can take an in-flight
        HTTP response down with it; clients read this file when that happens, so the
        report survives the process that produced it.
        """

        try:
            with open(LAST_STOP_PATH, "w", encoding="utf-8") as handle:
                json.dump(report, handle, ensure_ascii=False, indent=2)
        except OSError:
            pass

    def _close_http(self) -> None:
        if self.http:
            try:
                self.http.shutdown()
                self.http.server_close()
            except Exception:  # noqa: BLE001
                pass
            self.http = None
        self.http_closed.set()

    # ------------------------------------------------------------------ #
    # State file: how a CLI invocation finds this daemon
    # ------------------------------------------------------------------ #

    @property
    def url(self) -> str:
        port = self.http.server_address[1] if self.http else 0
        return f"http://127.0.0.1:{port}"

    def _write_state(self) -> None:
        payload = {
            "pid": os.getpid(),
            "port": self.http.server_address[1],
            "token": self.token,
            "desktop": self.desktop.name,
            "engine": self.engine,
            "mode": self.mode,
            "url": self.url,
            "started": self.started_at,
            "worker_pid": self.desktop.worker_pid,
        }
        with open(STATE_PATH, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)

    def _clear_state(self) -> None:
        try:
            if os.path.exists(STATE_PATH):
                with open(STATE_PATH, encoding="utf-8") as handle:
                    current = json.load(handle)
                if current.get("pid") == os.getpid():
                    os.remove(STATE_PATH)
        except (OSError, ValueError):
            pass

    def _spawn_preview(self) -> None:
        style = "pip" if self.engine == "shadow" else "card"
        command = (
            f'"{desktop_module.win32.python_executable()}" -m vdcu preview '
            f'--url {self.url} --token {self.token} --title {self.desktop.name} '
            f'--style {style}'
        )
        try:
            self.preview_process = desktop_module.spawn_detached(command, log_name="preview.log")
        except OSError as error:
            self.log(f"[session] preview could not start: {error}")

    # ------------------------------------------------------------------ #
    # Frames
    # ------------------------------------------------------------------ #

    def current_frame(
        self, max_width: int = 720, *, image_format: str = "png", max_age: float = 0.25
    ) -> dict:
        """Newest frame, capturing through the worker only when the cache is stale.

        The cache key includes both size and format: a JPEG frame cannot be served to
        the Tk preview box (Tk 8.6 decodes PNG and GIF only), so mixing them up would
        produce a blank box rather than an error.
        """

        with self.frame_lock:
            if self.frame_cache and time.time() - self.frame_cache[0] < max_age:
                cached = self.frame_cache[1]
                if cached.get("max_width") == max_width and cached.get("format") == image_format:
                    return cached
            response = self.desktop.request(
                "capture",
                {"max_width": max_width, "full": max_width is None, "format": image_format},
                timeout=90,
            )
            payload = {
                "frame": response.get("frame", ""),
                "meta": response.get("frame_meta", {}),
                "max_width": max_width,
                "format": image_format,
                "at": time.time(),
            }
            self.frame_cache = (time.time(), payload)
            return payload

    # ------------------------------------------------------------------ #
    # RPC used by the agent (CLI and MCP)
    # ------------------------------------------------------------------ #

    def rpc(self, op: str, args: dict | None = None, *, want_frame: bool = True) -> dict:
        args = dict(args or {})
        try:
            response = self.desktop.request(op, args, timeout=float(args.pop("timeout", 120)))
        except WorkerError as error:
            return {"ok": False, "error": {"code": error.code, "message": str(error)}}
        except WorkerGone as error:
            return {"ok": False, "error": {"code": "VD_WORKER_GONE", "message": str(error)}}
        except TimeoutError as error:
            return {"ok": False, "error": {"code": "VD_TIMEOUT", "message": str(error)}}

        result = {"ok": True, "result": response.get("result", {}), "desktop": self.desktop.name}
        if "frame_meta" in response:
            result["frame_meta"] = response["frame_meta"]
        if response.get("frame"):
            result["frame"] = response["frame"]
        if response.get("result", {}).get("op"):
            self.actions.append(response["result"])
        if op in ("click", "double_click", "type", "key", "hotkey", "scroll", "drag", "launch"):
            self.actions.append({"op": op, "args": {k: v for k, v in args.items() if k != "text"}})
            del self.actions[:-40]
        if want_frame and "frame" not in result:
            try:
                frame = self.current_frame(720)
                result["frame"] = frame["frame"]
                result["frame_meta"] = frame["meta"]
            except Exception:  # noqa: BLE001 - a missing frame must not fail the action
                pass
        return result

    def status(self) -> dict:
        cached = self.frame_cache[1]["meta"] if self.frame_cache else {}
        frame_age = (
            round(time.time() - self.frame_cache[0], 2) if self.frame_cache else None
        )
        return {
            "desktop": self.desktop.name,
            "mode": self.mode,
            "pid": os.getpid(),
            "worker_pid": self.desktop.worker_pid,
            "worker_alive": self.desktop.alive(),
            "active_input_desktop": desktop_module.win32.active_input_desktop_name(),
            "idle_seconds": round(self.desktop.idle_seconds(), 1),
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "frame_meta": cached,
            "frame_age_seconds": frame_age,
            "preview_pid": getattr(self.preview_process, "pid", None),
            "actions": self.actions[-20:],
            "url": self.url,
        }

    # ------------------------------------------------------------------ #
    # HTTP
    # ------------------------------------------------------------------ #

    def _start_http(self) -> None:
        session = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "vdcu/1.0"

            def log_message(self, *args):  # noqa: A003 - silence per-request logging
                pass

            # -- helpers ------------------------------------------------ #
            def _authorized(self, query: dict) -> bool:
                supplied = query.get("token", [""])[0] or self.headers.get("X-Vdcu-Token", "")
                return supplied == session.token

            def _send(self, code: int, body: bytes, content_type: str) -> None:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _json(self, code: int, payload: dict) -> None:
                self._send(code, json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                           "application/json; charset=utf-8")

            # -- routes ------------------------------------------------- #
            def do_GET(self):  # noqa: N802 - stdlib naming
                parsed = urlparse(self.path)
                query = parse_qs(parsed.query)
                if parsed.path in ("/", "/index.html"):
                    page = LIVE_PAGE.format(
                        desktop=session.desktop.name,
                        mode=session.mode,
                        base_width=720,
                        token_json=json.dumps(session.token),
                    )
                    self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
                    return
                if not self._authorized(query):
                    self._json(403, {"ok": False, "error": "bad token"})
                    return
                if parsed.path == "/health":
                    self._json(200, {"ok": True, "desktop": session.desktop.name})
                    return
                if parsed.path == "/status":
                    self._json(200, session.status())
                    return
                if parsed.path in ("/frame.png", "/frame.jpg", "/frame"):
                    width = int(query.get("w", ["720"])[0])
                    is_jpeg = parsed.path.endswith(".jpg") or query.get("format", [""])[0] == "jpeg"
                    try:
                        frame = session.current_frame(
                            width, image_format="jpeg" if is_jpeg else "png"
                        )
                    except Exception as error:  # noqa: BLE001
                        self._json(503, {"ok": False, "error": str(error)})
                        return
                    raw = base64.b64decode(frame["frame"]) if frame["frame"] else b""
                    if not raw:
                        self._json(503, {"ok": False, "error": "no frame"})
                        return
                    self._send(200, raw, "image/jpeg" if is_jpeg else "image/png")
                    return
                self._json(404, {"ok": False, "error": "not found"})

            def do_POST(self):  # noqa: N802 - stdlib naming
                parsed = urlparse(self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    request = json.loads(raw.decode("utf-8"))
                except ValueError:
                    self._json(400, {"ok": False, "error": "invalid json"})
                    return
                if request.get("token") != session.token:
                    self._json(403, {"ok": False, "error": "bad token"})
                    return
                if parsed.path == "/rpc":
                    op = request.get("op", "")
                    if op == "status":
                        self._json(200, {"ok": True, "result": session.status()})
                        return
                    if op == "stop":
                        # Teardown synchronously so the report is real, but leave the
                        # HTTP server up until after the response is written: the
                        # daemon exits the moment the session stops, and an exiting
                        # process takes in-flight responses with it.
                        report = session.stop(close_http=False)
                        self._json(200, {"ok": True, "result": report})
                        threading.Thread(
                            target=lambda: (time.sleep(0.4), session._close_http()),
                            daemon=True,
                        ).start()
                        return
                    result = session.rpc(op, request.get("args") or {})
                    self._json(200, result)
                    return
                self._json(404, {"ok": False, "error": "not found"})

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http.daemon_threads = True
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()

    def _watch_for_idle(self) -> None:
        while not self.stopping.wait(5.0):
            if self.desktop.idle_seconds() > self.idle_timeout:
                self.log(f"[session] idle for {self.idle_timeout:.0f}s: closing")
                self.stop()
                return
            if time.time() - self.started_at > self.max_seconds:
                self.log("[session] session ceiling reached: closing")
                self.stop()
                return


def load_state() -> dict | None:
    """Read the published session, or None when there is no live one."""

    try:
        with open(STATE_PATH, encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return None
    from . import win32

    if not win32.process_alive(int(state.get("pid", 0))):
        return None
    return state


def main(argv: list[str] | None = None) -> int:
    import argparse
    import signal

    parser = argparse.ArgumentParser(prog="vdcu daemon")
    parser.add_argument("--name", default=None, help="agent desktop name")
    parser.add_argument("--engine", default="shadow", choices=("shadow", "stealth"),
                        help="stealth: hidden Win32 desktop; shadow: the bundled runtime's shadow RDP session")
    parser.add_argument("--mode", default="stealth", choices=("stealth", "takeover"))
    parser.add_argument("--no-shell", action="store_true",
                        help="do not run explorer on the agent desktop (no wallpaper/taskbar)")
    parser.add_argument("--idle-timeout", type=float, default=1800.0)
    parser.add_argument("--max-seconds", type=float, default=14400.0)
    parser.add_argument("--no-preview", action="store_true")
    options = parser.parse_args(argv)
    desktop_module.win32.enable_dpi_awareness()

    session = Session(
        desktop_name=options.name,
        engine=options.engine,
        mode=options.mode,
        shell=not options.no_shell,
        idle_timeout=options.idle_timeout,
        max_seconds=options.max_seconds,
        preview=not options.no_preview,
    )
    try:
        session.start()
    except Exception as error:  # noqa: BLE001
        print(json.dumps({"ok": False, "error": repr(error)}, ensure_ascii=False))
        session.stop()
        return 1

    stopping = threading.Event()

    def handle_signal(*_args):
        stopping.set()

    for name in ("SIGINT", "SIGTERM"):
        try:
            signal.signal(getattr(signal, name), handle_signal)
        except (AttributeError, ValueError, OSError):
            pass

    try:
        while not stopping.wait(1.0):
            if session.stopping.is_set():
                break
    finally:
        session.stop()
        # The process exits as soon as the loop ends; give in-flight HTTP responses
        # a moment to flush before that happens (a stop response lost to process
        # exit looks like "session already gone" on the client).
        session.http_closed.wait(timeout=5.0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""MCP server: agent-desktop computer use for any MCP host.

Model Context Protocol over stdio, one JSON-RPC message per line.  Works with
Claude Code (``claude mcp add``), Kimi Code, and anything else that speaks MCP.

The behaviour that matches the bundled runtime's chat experience: every action returns the
frame it produced as MCP **image content**, so the host renders the agent desktop
inline, right next to the tool call, as the work happens.  A host that ignores image
content still gets the JSON summary, and the PNG is written to disk either way.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import traceback

from . import __version__
from . import client as client_module
from .client import Client, NoSession, SessionGone, save_frame

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "vdcu", "version": __version__}

TOOLS: list[dict] = [
    {
        "name": "vd_enter",
        "description": (
            "Create the agent's own virtual desktop and start the session. The user's "
            "desktop and input are untouched: the agent desktop is never switched to. "
            "A small always-on-top preview box shows what the agent sees."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "engine": {"type": "string", "enum": ["stealth", "shadow"],
                           "description": "shadow (default, isolated session) or stealth "
                                          "(the bundled runtime's own shadow RDP session; shows a password box)"},
                "mode": {"type": "string", "enum": ["stealth", "takeover"],
                         "description": "stealth (default, invisible) or takeover (visible switch)"},
            },
        },
    },
    {
        "name": "vd_status",
        "description": "State of the agent desktop: mode, windows, whether the user's desktop is untouched.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "vd_leave",
        "description": "Close the agent desktop and everything running on it.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "screenshot",
        "description": "Capture the agent desktop. Always do this before acting on coordinates.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "full": {"type": "boolean", "description": "native resolution (default)"},
                "max_width": {"type": "integer", "description": "downscale to this width"},
                "format": {"type": "string", "enum": ["jpeg", "png"],
                           "description": "jpeg (default, small) or png (lossless)"},
            },
        },
    },
    {
        "name": "click",
        "description": "Click at normalized 0-1000 coordinates of the agent desktop.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "number"}, "y": {"type": "number"},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "clicks": {"type": "integer", "default": 1},
            },
            "required": ["x", "y"],
        },
    },
    {
        "name": "double_click",
        "description": "Double-click at normalized 0-1000 coordinates.",
        "inputSchema": {
            "type": "object",
            "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
            "required": ["x", "y"],
        },
    },
    {
        "name": "move",
        "description": "Move the pointer to normalized 0-1000 coordinates without clicking.",
        "inputSchema": {
            "type": "object",
            "properties": {"x": {"type": "number"}, "y": {"type": "number"}},
            "required": ["x", "y"],
        },
    },
    {
        "name": "drag",
        "description": "Drag from one normalized point to another.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x1": {"type": "number"}, "y1": {"type": "number"},
                "x2": {"type": "number"}, "y2": {"type": "number"},
                "duration_ms": {"type": "integer", "default": 800},
            },
            "required": ["x1", "y1", "x2", "y2"],
        },
    },
    {
        "name": "scroll",
        "description": "Scroll at a point: delta is up/down/left/right or -10..10.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "x": {"type": "number"}, "y": {"type": "number"},
                "delta": {"type": ["string", "number"]},
            },
            "required": ["x", "y", "delta"],
        },
    },
    {
        "name": "type_text",
        "description": "Type text into the focused window of the agent desktop (Chinese works).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "submit": {"type": "boolean", "description": "press Enter afterwards"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "press_key",
        "description": "Press one key, e.g. enter, tab, esc, f5, delete.",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    {
        "name": "hotkey",
        "description": "Press a chord with keys passed separately, e.g. ['ctrl','s'].",
        "inputSchema": {
            "type": "object",
            "properties": {"keys": {"type": "array", "items": {"type": "string"}}},
            "required": ["keys"],
        },
    },
    {
        "name": "key_down",
        "description": "Hold a key down (pair with key_up) for modifier-style input.",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    {
        "name": "key_up",
        "description": "Release a key held with key_down.",
        "inputSchema": {
            "type": "object",
            "properties": {"key": {"type": "string"}},
            "required": ["key"],
        },
    },
    {
        "name": "copy_selection",
        "description": (
            "Post Ctrl+C to the focused window and return the clipboard text. Best "
            "effort in stealth mode: apps that test the global key state may ignore it."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "wait",
        "description": "Wait 0-180 seconds on the agent desktop (for slow dialogs to appear).",
        "inputSchema": {
            "type": "object",
            "properties": {"seconds": {"type": "number", "default": 3}},
        },
    },
    {
        "name": "list_apps",
        "description": "List launchable applications; pass an exact name to launch_app.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "filter": {"type": "string"}, "limit": {"type": "integer", "default": 40},
            },
        },
    },
    {
        "name": "launch_app",
        "description": "Launch an application on the agent desktop (exact name from list_apps).",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
        },
    },
    {
        "name": "list_windows",
        "description": "List windows on the agent desktop, in z-order.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_clipboard",
        "description": "Read the clipboard text (shared with the user's desktop).",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "set_clipboard",
        "description": "Set the clipboard text.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "open_preview",
        "description": "Give the URL of the live view page for the agent desktop.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]

_FRAME_TOOLS = {
    "screenshot", "click", "double_click", "move", "drag", "scroll",
    "type_text", "press_key", "hotkey", "key_down", "key_up", "copy_selection",
    "wait", "launch_app",
}


class Server:
    def __init__(self, stdin, stdout):
        self.stdin = stdin
        self.stdout = stdout
        self.write_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Transport
    # ------------------------------------------------------------------ #

    def send(self, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        with self.write_lock:
            self.stdout.write(data + b"\n")
            self.stdout.flush()

    def reply(self, request_id, result=None, error=None) -> None:
        payload = {"jsonrpc": "2.0", "id": request_id}
        if error is not None:
            payload["error"] = error
        else:
            payload["result"] = result
        self.send(payload)

    # ------------------------------------------------------------------ #
    # JSON-RPC
    # ------------------------------------------------------------------ #

    def handle(self, request: dict) -> None:
        method = request.get("method", "")
        request_id = request.get("id")
        params = request.get("params") or {}

        if method == "initialize":
            self.reply(request_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": SERVER_INFO,
            })
            return
        if method in ("notifications/initialized", "notifications/cancelled", "initialized"):
            return
        if method == "ping":
            self.reply(request_id, {})
            return
        if method == "tools/list":
            self.reply(request_id, {"tools": TOOLS})
            return
        if method == "tools/call":
            try:
                self.reply(request_id, self.call_tool(params.get("name", ""), params.get("arguments") or {}))
            except Exception as error:  # noqa: BLE001
                self.reply(request_id, error={
                    "code": -32603,
                    "message": f"{error!r}",
                    "data": traceback.format_exc(limit=4),
                })
            return
        if request_id is not None:
            self.reply(request_id, error={"code": -32601, "message": f"unknown method {method!r}"})

    # ------------------------------------------------------------------ #
    # Tools
    # ------------------------------------------------------------------ #

    def _client(self, *, autostart: bool = True) -> Client:
        try:
            return Client.current()
        except NoSession:
            if not autostart:
                raise
            client_module.start_session(preview=True)
            return Client.wait(90)

    def call_tool(self, name: str, arguments: dict) -> dict:
        if name == "vd_enter":
            state = client_module.start_session(
                engine=arguments.get("engine", "shadow"),
                mode=arguments.get("mode", "stealth"),
                preview=True,
            )
            client = Client.wait(90)
            status = client.status()
            text = (
                f"agent desktop '{client.desktop}' is up (engine={client.engine}, mode={client.mode}"
                f"{', already running' if state.get('already_running') else ''}). "
                f"User's active desktop: {status.get('active_input_desktop')} "
                f"(untouched: {status.get('active_input_desktop') != client.desktop})."
            )
            if client.engine == "shadow":
                text += (
                    " Windows authorization completed (empty credentials may be automatic). The session is a "
                    "real shadow (RDP) session: independent desktop, independent mouse and "
                    "keyboard, programs run inside it."
                )
            return self._content(text, self._fresh_frame(client))

        if name == "vd_status":
            client = self._client(autostart=False)
            status = client.status()
            # Shadow reports task time as 共用时 X 分 Y 秒 — do the same
            seconds = float(status.get("uptime") or 0)
            status["elapsed"] = f"{int(seconds // 60)} 分 {int(seconds % 60)} 秒"
            return {
                "content": [{"type": "text", "text": json.dumps(status, ensure_ascii=False, indent=2)}],
                "isError": False,
            }

        if name == "vd_leave":
            try:
                client = Client.current()
            except NoSession:
                return self._text("no agent desktop was running")
            desktop = client.desktop
            seconds = float(client.status().get("uptime") or 0)
            report = client.stop()
            summary = (
                f"agent desktop '{desktop}' closed · 共用时 "
                f"{int(seconds // 60)} 分 {int(seconds % 60)} 秒"
            )
            foreign = (report or {}).get("foreign_processes") or {}
            if foreign:
                images = sorted({entry.get("image", "?") for entry in foreign.values()})
                summary += (
                    f"; left alone (not ours): {', '.join(images)} — they hold the empty "
                    "desktop object until they exit, at logoff at the latest"
                )
            return self._text(summary)

        if name == "open_preview":
            client = self._client()
            url = f"{client.url}/?token={client.token}"
            return self._text(f"live view: {url}")

        client = self._client()
        op, args = self._map(name, arguments)
        response = client.rpc(op, args)

        if not response.get("ok"):
            error = response.get("error") or {}
            return {
                "content": [{"type": "text", "text": f"failed: {error.get('code')} {error.get('message')}"}],
                "isError": True,
            }

        summary = {
            "op": op,
            "desktop": client.desktop,
            "result": response.get("result", {}),
        }
        path = save_frame(response, tag=op)
        if path:
            summary["screenshot"] = path
        if "frame_meta" in response:
            summary["frame"] = response["frame_meta"]
        if name in ("list_apps",):
            apps = response.get("result", {}).get("apps", [])
            needle = str(arguments.get("filter", "")).casefold()
            if needle:
                apps = [
                    app for app in apps
                    if needle in (app.get("name") or "").casefold()
                    or needle in (app.get("key") or "").casefold()
                ]
            limit = int(arguments.get("limit", 40))
            summary["apps"] = apps[:limit]
            summary["returned"] = min(len(apps), limit)
            # how many matched overall, not how many are shown
            summary["count"] = response.get("result", {}).get("count", len(apps))
        return self._content(json.dumps(summary, ensure_ascii=False, indent=2), response)

    def _map(self, name: str, arguments: dict) -> tuple[str, dict]:
        if name == "screenshot":
            return "capture", {
                "max_width": None if arguments.get("full") else int(arguments.get("max_width", 0)) or None,
                "format": arguments.get("format", "jpeg"),
            }
        if name == "click":
            return "click", {
                "x": arguments["x"], "y": arguments["y"],
                "button": arguments.get("button", "left"),
                "clicks": int(arguments.get("clicks", 1)),
            }
        if name == "double_click":
            return "double_click", {"x": arguments["x"], "y": arguments["y"]}
        if name == "move":
            return "move", {"x": arguments["x"], "y": arguments["y"]}
        if name == "drag":
            return "drag", {
                "x1": arguments["x1"], "y1": arguments["y1"],
                "x2": arguments["x2"], "y2": arguments["y2"],
                "duration_ms": int(arguments.get("duration_ms", 800)),
            }
        if name == "scroll":
            return "scroll", {"x": arguments["x"], "y": arguments["y"], "delta": arguments["delta"]}
        if name == "type_text":
            text = str(arguments["text"]) + ("\n" if arguments.get("submit") else "")
            return "type", {"text": text}
        if name == "press_key":
            return "key", {"key": arguments["key"]}
        if name == "key_down":
            return "key_down", {"key": arguments["key"]}
        if name == "key_up":
            return "key_up", {"key": arguments["key"]}
        if name == "copy_selection":
            return "copy_selection", {}
        if name == "wait":
            return "wait", {"seconds": float(arguments.get("seconds", 3))}
        if name == "hotkey":
            return "hotkey", {"keys": arguments["keys"]}
        if name == "list_apps":
            return "list_apps", {
                "query": arguments.get("filter", ""),
                "limit": int(arguments.get("limit", 40)),
            }
        if name == "launch_app":
            return "launch", {"name": arguments["name"]}
        if name == "list_windows":
            return "windows", {}
        if name == "get_clipboard":
            return "clipboard_get", {}
        if name == "set_clipboard":
            return "clipboard_set", {"text": arguments["text"]}
        raise ValueError(f"unknown tool {name!r}")

    # ------------------------------------------------------------------ #
    # Result shaping
    # ------------------------------------------------------------------ #

    def _text(self, message: str) -> dict:
        return {"content": [{"type": "text", "text": message}], "isError": False}

    def _fresh_frame(self, client: Client) -> dict:
        try:
            return {"frame": base64.b64encode(client.frame_png(720)).decode("ascii")}
        except (SessionGone, OSError):
            return {}

    def _content(self, text: str, response: dict) -> dict:
        content = [{"type": "text", "text": text}]
        encoded = response.get("frame")
        if encoded:
            meta = response.get("frame_meta") or {}
            content.append(
                {
                    "type": "image",
                    "data": encoded,
                    "mimeType": "image/jpeg" if meta.get("format") == "jpeg" else "image/png",
                }
            )
        return {"content": content, "isError": False}


def main(argv: list[str] | None = None) -> int:
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    server = Server(stdin, stdout)
    for raw in stdin:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        try:
            server.handle(request)
        except Exception as error:  # noqa: BLE001
            if isinstance(request, dict) and request.get("id") is not None:
                server.reply(request["id"], error={"code": -32603, "message": repr(error)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

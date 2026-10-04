"""Expose the live isolated session through the local GAC protocol.

Adapts bundled computer-use clients to the SDK worker operations. It does not
connect to an external product installation or control the user's desktop.
"""

from __future__ import annotations

import hmac
import hashlib
import io
import json
import os
import socket
import struct
import sys
import threading
import time

def engine_packages() -> str:
    """Where the engine pack's ``python-packages`` live.

    Next to the skill when the pack is installed there; the installed
    location otherwise (a repo checkout has no pack of its own — its tests
    then exercise the installed one).  ``VDCU_ENGINE_PACKAGES`` overrides.
    """

    env = os.environ.get("VDCU_ENGINE_PACKAGES")
    if env and os.path.isdir(env):
        return env
    here = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "engine", "python-packages",
    )
    if os.path.isdir(here):
        return here
    return os.path.join(
        os.environ.get("LOCALAPPDATA", ""), "Programs", "vdcu",
        "engine", "python-packages",
    )


PACKAGES = engine_packages()
if os.path.isdir(PACKAGES):
    sys.path.insert(0, PACKAGES)

from gac_runtime.transport import chacha20poly1305 as crypto  # noqa: E402
from gac_runtime.transport import protocol as gac_protocol  # noqa: E402

# module/command ids from gac_runtime.modules.computer_use_windows
COMPUTER_USE_MODULE_ID = 2
COMMANDS = {
    1: "list_apps", 2: "launch_app", 3: "screenshot", 5: "click", 6: "hover",
    7: "drag", 8: "scroll", 9: "type", 10: "hotkey", 11: "wait",
    12: "get_clipboard", 13: "set_clipboard", 14: "press", 15: "key_down",
    16: "key_up", 17: "copy_selection",
}

RESPONSE_VERSION = 1

#: Single-character probes aggregated into the full catalogue (see Session._full_catalog).
CATALOG_PROBES = "abcdefghijklmnopqrstuvwxyz0123456789"


def _coded(code: str, message: str) -> RuntimeError:
    """A server-side error whose stable code reaches the agent verbatim
    (``seed_computer_use.<op>: [<code>] <message>`` — services/_errors.py)."""

    error = RuntimeError(message)
    error.code = code  # type: ignore[attr-defined]
    return error


def _read_u32(buf: io.BytesIO) -> int:
    return struct.unpack("<I", buf.read(4))[0]


def _write_str(out: io.BytesIO, text: str) -> None:
    data = text.encode("utf-8")
    out.write(struct.pack("<I", len(data)))
    out.write(data)


class Session:
    """The worker calls the server makes on the agent desktop session."""

    def __init__(self) -> None:
        from vdcu.client import Client

        self.client = Client.current()

    def call(self, name: str, params: dict) -> dict:
        if name == "list_apps":
            # wire ListApps takes no params: the contract is the COMPLETE
            # catalogue (services/app.py: "Return the complete application
            # catalog"), answered per the client's shape {"apps": [...]}.
            return {"apps": self._full_catalog()}
        if name == "launch_app":
            return self._launch(str(params.get("name", "")))
        mapping = {
            "screenshot": ("capture", {"format": "png", "max_width": 0}),
            "click": ("click", {"x": params.get("x", 0), "y": params.get("y", 0),
                               "button": params.get("button", "left"),
                               "clicks": params.get("clicks", 1)}),
            "hover": ("move", {"x": params.get("x", 0), "y": params.get("y", 0)}),
            "drag": ("drag", {"x1": params.get("x1"), "y1": params.get("y1"),
                              "x2": params.get("x2"), "y2": params.get("y2"),
                              "duration_ms": params.get("duration_ms", 1000)}),
            "scroll": ("scroll", {"x": params.get("x", 500), "y": params.get("y", 500),
                                  "delta": params.get("delta",
                                                      params.get("amount", 3)),
                                  "horizontal": bool(params.get("horizontal", False))}),
            "type": ("type", {"text": params.get("text", params.get("value", ""))}),
            "hotkey": ("hotkey", {"keys": params.get("keys", params.get("value", []))}),
            # the wire Wait request carries duration_ms (modules command 11);
            # cu.wait() itself sleeps client-side and never reaches here.
            "wait": ("wait", {"seconds": params.get("duration_ms", 1000) / 1000}),
            "get_clipboard": ("clipboard_get", {}),
            "set_clipboard": ("clipboard_set", {"text": params.get("text", "")}),
            "press": ("key", {"key": params.get("key", params.get("value", ""))}),
            "key_down": ("key_down", {"key": params.get("key", params.get("value", ""))}),
            "key_up": ("key_up", {"key": params.get("key", params.get("value", ""))}),
            "copy_selection": ("copy_selection", {}),
        }
        op, args = mapping[name]
        response = self.client.rpc(op, args, timeout=120)
        if not response.get("ok"):
            raise RuntimeError(json.dumps(response.get("error"), ensure_ascii=False))
        result = dict(response.get("result") or {})
        # the worker delivers frames at the top level of the response, not inside
        # the result dict (the result carries the metadata)
        if "frame" in response and "frame" not in result:
            result["frame"] = response["frame"]
        return result

    def _full_catalog(self) -> list[dict]:
        """The complete application catalogue, as the wire contract demands.

        The engine's ``QueryHybridPrograms`` refuses an empty query (measured),
        so the full listing is aggregated from single-character probes and
        deduplicated by app name — any installed app whose name holds at least
        one letter or digit is found, which in practice is all of them.
        """

        seen: dict[str, dict] = {}
        for probe in CATALOG_PROBES:
            response = self.client.rpc(
                "list_apps", {"query": probe, "limit": 200}, timeout=120)
            if not response.get("ok"):
                continue
            for app in (response.get("result") or {}).get("apps") or []:
                name = str(app.get("name") or "")
                if name and name not in seen:
                    seen[name] = app
        return list(seen.values())

    def _launch(self, name: str) -> dict:
        """Launch by catalogue name, answered in the client's launch-result
        shape (modules.computer_use_windows.launch_app): app/status/
        launch_method/window.  ``accepted_unverified`` + no window is the
        honest status: the engine launched the process but its SDK publishes
        no window enumeration, so a window can never be verified here."""

        response = self.client.rpc("launch", {"name": name}, timeout=120)
        if not response.get("ok"):
            raise RuntimeError(json.dumps(response.get("error"), ensure_ascii=False))
        result = dict(response.get("result") or {})
        if result.get("error"):
            if result["error"] == "ambiguous":
                candidates = ", ".join(
                    str(entry.get("name")) for entry in result.get("candidates") or []
                )
                raise _coded(
                    "CU_LAUNCH_AMBIGUOUS",
                    f"launch_app {name!r} is ambiguous; candidates: {candidates}",
                )
            raise _coded("CU_LAUNCH_FAILED", str(result["error"]))
        launched = result.get("launched") or {}
        target = str(result.get("target") or "")
        # an AUMID target went through the apps folder; a file path was resolved
        launch_method = (
            "apps_folder"
            if launched.get("unique_info") and launched.get("unique_info") == target
            else "resolved_target"
        )
        return {
            "app": {"name": str(launched.get("name") or name)},
            "status": "accepted_unverified",
            "launch_method": launch_method,
            "window": None,
        }


def handle_command(session: Session, module_id: int, command_id: int, payload: bytes) -> bytes:
    if module_id != COMPUTER_USE_MODULE_ID:
        # module 4 is the macOS AX plane (gac_runtime.modules.computer_use_ax);
        # the bundled runtime on Windows serves only module 2, so a foreign module must
        # be refused — never silently routed through the Windows command table.
        raise _coded(
            "CU_UNSUPPORTED_MODULE",
            f"Computer Use module {module_id} is not served on this Windows desktop",
        )
    name = COMMANDS.get(command_id)
    if not name:
        raise _coded("CU_UNKNOWN_COMMAND", f"unsupported command {command_id}")
    # the request param is write_str-encoded: u32 length + utf-8 JSON
    buf = io.BytesIO(payload)
    size = _read_u32(buf)
    params = json.loads(buf.read(size).decode("utf-8") or "{}")
    result = session.call(name, params)

    out = io.BytesIO()
    if name == "screenshot":
        import base64

        png = base64.b64decode(result.get("frame") or b"")
        width = int(result.get("width") or 0)
        height = int(result.get("height") or 0)
        meta = {"width": width, "height": height}
        _write_str(out, json.dumps(meta, separators=(",", ":")))
        out.write(struct.pack("<I", width))
        out.write(struct.pack("<I", height))
        out.write(struct.pack("<I", len(png)))
        out.write(png)
    elif name in ("list_apps", "launch_app", "get_clipboard", "copy_selection"):
        # these carry data back; copy_selection/get_clipboard are {"text": str},
        # list_apps is {"apps": [...]}, launch_app the launch-result object
        if name in ("get_clipboard", "copy_selection"):
            text = result.get("text", "")
            _write_str(out, json.dumps({"text": str(text)}, ensure_ascii=False, separators=(",", ":")))
        else:
            _write_str(out, json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    else:
        # void commands: WindowsComputerUseCommand._none requires exactly {}
        _write_str(out, "{}")
    return out.getvalue()


def serve(host: str = "127.0.0.1", port: int = 0, secret: bytes = b"") -> int:
    """Run the GAC server; returns the bound port (print it for the client env)."""

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((host, port))
    listener.listen(4)
    bound = listener.getsockname()[1]
    print(f"GAC server listening on {host}:{bound}", flush=True)
    print(f"export CUA_GAC_TRANSPORT=tcp CUA_GAC_SERVER_PORT={bound}", flush=True)

    def handle(conn: socket.socket) -> None:
        try:
            # handshake: client sends session:execution ids, we send a token,
            # client proves the secret, both derive the session key.
            _ids = gac_protocol.read_frame(conn)
            token = os.urandom(32)
            gac_protocol.write_frame(conn, token)
            digest = gac_protocol.read_frame(conn)
            expected = hmac.new(secret, token, hashlib.sha256).digest()
            if not hmac.compare_digest(digest, expected):
                gac_protocol.write_frame(conn, b"bad token")
                conn.close()
                return
            key = gac_protocol.hkdf(token, secret)
            transport = gac_protocol.EncryptedFrameTransport(conn, key)
            session = Session()
            while True:
                frame = transport.read_encrypted_frame()
                buf = io.BytesIO(frame)
                version = buf.read(1)[0]
                module_id = buf.read(1)[0]
                command_id = _read_u32(buf)
                size = _read_u32(buf)
                payload = buf.read(size)
                try:
                    body = handle_command(session, module_id, command_id, payload)
                    # response envelope: u8 status + u32-len + payload (wire._decode)
                    response = struct.pack("<B", 0) + struct.pack("<I", len(body)) + body
                except Exception as error:  # noqa: BLE001
                    # the error payload is a JSON string param {"code","message"}
                    # (modules._decode reads exactly that on the failure path).
                    # The stable code must reach the agent verbatim — the client
                    # re-raises it as seed_computer_use.<op>: [<code>] <message>.
                    code = getattr(error, "code", None)
                    message = str(error)
                    if not code:
                        # Session.call ferries worker errors as a JSON dict
                        try:
                            detail = json.loads(message)
                            if isinstance(detail, dict) and detail.get("code"):
                                code = str(detail["code"])
                                message = str(detail.get("message") or message)
                        except ValueError:
                            pass
                    print(f"command {command_id} failed: {error!r}", flush=True)
                    error_out = io.BytesIO()
                    _write_str(error_out, json.dumps(
                        {"code": code or "INTERNAL", "message": message[:500]},
                        ensure_ascii=False, separators=(",", ":"),
                    ))
                    message_out = error_out.getvalue()
                    response = struct.pack("<B", 1) + struct.pack("<I", len(message_out)) + message_out
                transport.write_encrypted_frame(bytearray(response))
        except (EOFError, OSError, AssertionError):
            pass
        finally:
            conn.close()

    threading.Thread(target=_session_watchdog, daemon=True).start()
    while True:
        connection, _address = listener.accept()
        threading.Thread(target=handle, args=(connection,), daemon=True).start()


def _session_watchdog() -> None:
    """Exit when the vdcu session this bridge serves is gone.

    The bridge exists to serve a live agent desktop; without one it is a zombie
    that would outlive ``vdcu leave``.  Poll the published session state (which
    itself checks the daemon pid) and stop the process when it disappears.
    """

    from vdcu.session import load_state

    while True:
        time.sleep(5)
        if not load_state():
            os._exit(0)


def main(argv: list[str] | None = None) -> int:
    import argparse
    import base64

    parser = argparse.ArgumentParser(prog="vdcu gac-server")
    parser.add_argument("--port", type=int, default=int(os.environ.get("CUA_GAC_SERVER_PORT", "0")))
    parser.add_argument("--secret", default=os.environ.get("CUA_GAC_SECRET", "vdcu-local"))
    options = parser.parse_args(argv)
    # the client decodes CUA_GAC_SECRET with urlsafe base64 (transport/config.py);
    # the server must use the same bytes or the HMAC challenge fails.
    raw = options.secret
    try:
        secret = base64.urlsafe_b64decode(raw + "==")
    except Exception:  # noqa: BLE001
        secret = raw.encode("utf-8")
    serve(port=options.port, secret=secret)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

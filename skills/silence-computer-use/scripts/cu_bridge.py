r"""KimiCU pipe bridge — talk to a KimiCU agent (cu.exe) from plain Python.

Protocol (measured from the kimi-cu-win plugin's trusted-service sources):
  - Named pipe, default ``\\.\pipe\kimi-cu-win-<username>``.
  - Frame: 8-byte little-endian uint64 length + UTF-8 JSON payload.
  - One request, one response per connection; the lifecycle lease is the one
    persistent connection (begin/end with a nonce, calls carry that nonce).

Request shapes:
  {"id", "session_id", "method": "__agent_ping", "params": {}}
  {"id", "session_id", "method": "__lifecycle_v1", "params": {"action": "begin"|"end", "nonce"}}
  {"id", "session_id", "method": "__computer_use_call_v1",
   "params": {"method": <api method>, "arguments": {...}},
   "telemetry_tool": <api method>, "lifecycle": {"nonce": nonce}}

API methods (schema.mjs): list_windows, get_window, list_apps,
list_available_apps, launch_app, get_window_state, click, press_key,
type_text, scroll, set_value, drag, drag_path, perform_secondary_action,
activate_window.

Usage:
  python cu_bridge.py ping
  python cu_bridge.py call list_windows
  python cu_bridge.py call get_window_state '{"window": {"app": "...", "id": 123}}'
  python cu_bridge.py serve            # NDJSON on stdin/stdout, one lease
"""

from __future__ import annotations

import base64
import json
import os
import struct
import sys
import time
import uuid

PIPE_NAME = os.environ.get(
    "KIMICU_PIPE",
    rf"\\.\pipe\kimi-cu-win-{os.environ.get('USERNAME', 'user')}",
)
MAX_FRAME = 128 * 1024 * 1024
SHOT_DIR = os.environ.get("KIMICU_SHOT_DIR", os.path.join(".", "cu_shots"))

ERROR_PIPE_BUSY = 231
ERROR_FILE_NOT_FOUND = 2


def _utf8_stdio() -> None:
    """Pin stdio to UTF-8 regardless of the console codepage.

    serve's NDJSON contract (and every command's JSON output) is UTF-8.  On a
    GBK-locale Windows host the default cp936 streams corrupt Chinese payloads
    both ways; lone surrogates from mangled input also kill a strict encoder
    ("surrogates not allowed").  Reconfiguring here frees callers from having
    to set PYTHONIOENCODING.
    """

    for stream, errors in (
        (sys.stdin, "replace"),
        (sys.stdout, "backslashreplace"),
        (sys.stderr, "backslashreplace"),
    ):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors=errors)
        except Exception:  # noqa: BLE001 - exotic stream wrappers; keep defaults
            pass


class BridgeError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _open_pipe(path: str, timeout: float = 10.0):
    """Open the named pipe as a binary file, waiting out ERROR_PIPE_BUSY."""

    import ctypes

    deadline = time.time() + timeout
    while True:
        try:
            return open(path, "r+b", buffering=0)
        except FileNotFoundError:
            raise BridgeError(
                "KIMICU_PIPE_NOT_FOUND",
                f"pipe {path} does not exist; is the KimiCU agent running "
                f"(cu.exe doctor)?",
            )
        except OSError as error:
            if error.winerror == ERROR_PIPE_BUSY and time.time() < deadline:
                ctypes.windll.kernel32.WaitNamedPipeW(path, 200)
                continue
            raise


def encode_frame(value: dict) -> bytes:
    payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
    return struct.pack("<Q", len(payload)) + payload


def decode_frame(pipe) -> dict:
    header = _read_exact(pipe, 8)
    (length,) = struct.unpack("<Q", header)
    if length > MAX_FRAME:
        raise BridgeError("KIMICU_FRAME_TOO_LARGE", f"frame length {length}")
    return json.loads(_read_exact(pipe, length).decode("utf-8"))


def _read_exact(pipe, count: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < count:
        part = pipe.read(count - len(chunks))
        if not part:
            raise BridgeError("KIMICU_TRUNCATED_FRAME", "agent closed the pipe mid-frame")
        chunks.extend(part)
    return bytes(chunks)


def exchange(request: dict, pipe_path: str = PIPE_NAME, timeout: float = 10.0) -> dict:
    """One-shot request/response on a fresh connection."""

    with _open_pipe(pipe_path, timeout) as pipe:
        pipe.write(encode_frame(request))
        response = decode_frame(pipe)
    if response.get("id") != request.get("id"):
        raise BridgeError(
            "KIMICU_RESPONSE_ID_MISMATCH",
            f"expected id {request.get('id')}, got {response.get('id')}",
        )
    return response


class Session:
    """A lease-holding KimiCU session: begin once, call many, end cleanly."""

    def __init__(self, pipe_path: str = PIPE_NAME, label: str = "silence-computer-use"):
        self.pipe_path = pipe_path
        self.nonce = uuid.uuid4().hex[:16]
        exec_id = uuid.uuid4().hex[:12]
        generation = "1"
        self.session_id = (
            f"node-repl/cell/{label}/{generation}/{exec_id}/{self.nonce}"
        )
        self._next_id = 1
        self._lease = None

    def _id(self) -> int:
        self._next_id += 1
        return self._next_id - 1

    def _request(self, method: str, params: dict) -> dict:
        return {
            "id": self._id(),
            "session_id": self.session_id,
            "method": method,
            "params": params,
        }

    def ping(self) -> dict:
        return exchange(self._request("__agent_ping", {}), self.pipe_path)

    def begin(self) -> dict:
        self._lease = _open_pipe(self.pipe_path)
        self._lease.write(
            encode_frame(self._request("__lifecycle_v1", {"action": "begin", "nonce": self.nonce}))
        )
        response = decode_frame(self._lease)
        if not response.get("ok"):
            raise BridgeError(
                response.get("error_code") or "KIMICU_LIFECYCLE_ERROR",
                response.get("error") or "agent rejected the lifecycle lease",
            )
        return response

    def end(self) -> None:
        if self._lease is None:
            return
        try:
            self._lease.write(
                encode_frame(self._request("__lifecycle_v1", {"action": "end", "nonce": self.nonce}))
            )
            decode_frame(self._lease)
        except Exception:  # noqa: BLE001 - closing is best effort
            pass
        finally:
            try:
                self._lease.close()
            except OSError:
                pass
            self._lease = None

    def call(self, method: str, arguments: dict | None = None) -> dict:
        request = self._request(
            "__computer_use_call_v1",
            {"method": method, "arguments": arguments or {}},
        )
        request["telemetry_tool"] = method
        request["lifecycle"] = {"nonce": self.nonce}
        response = exchange(request, self.pipe_path)
        if not response.get("ok"):
            raise BridgeError(
                response.get("error_code") or "KIMICU_AGENT_ERROR",
                response.get("error") or f"{method} failed",
            )
        return response.get("result")


def save_screenshots(result, shot_dir: str = SHOT_DIR):
    """Decode data-URL screenshots in a result to files; replace url with path."""

    if not isinstance(result, dict):
        return result
    shots = result.get("screenshots")
    if not isinstance(shots, list):
        return result
    os.makedirs(shot_dir, exist_ok=True)
    for shot in shots:
        url = (shot or {}).get("url") or ""
        if not url.startswith("data:"):
            continue
        try:
            header, _, data = url.partition(",")
            mime = header[5:].split(";")[0]
            ext = {"image/png": "png", "image/jpeg": "jpg"}.get(mime, "bin")
            raw = base64.b64decode(data)
            safe_id = "".join(
                ch if ch.isalnum() or ch in "-_" else "_" for ch in str(shot.get("id", "x"))
            )
            path = os.path.abspath(
                os.path.join(shot_dir, f"shot_{int(time.time() * 1000)}_{safe_id}.{ext}")
            )
            with open(path, "wb") as fh:
                fh.write(raw)
            shot["path"] = path
            shot["url"] = path
        except Exception:  # noqa: BLE001 - keep the data URL if saving fails
            pass
    return result


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)


def cmd_ping() -> int:
    session = Session()
    _print(session.ping())
    return 0


def cmd_call(method: str, args_json: str | None) -> int:
    arguments = json.loads(args_json) if args_json else {}
    session = Session()
    try:
        session.begin()
        result = session.call(method, arguments)
        _print({"ok": True, "result": save_screenshots(result)})
        return 0
    except BridgeError as error:
        _print({"ok": False, "error": {"code": error.code, "message": str(error)}})
        return 1
    finally:
        session.end()


def cmd_serve() -> int:
    """NDJSON loop: {"id": N, "method": "...", "arguments": {...}} per line."""

    session = Session()
    try:
        session.begin()
    except BridgeError as error:
        _print({"id": 0, "ok": False, "error": {"code": error.code, "message": str(error)}})
        return 1
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            request_id = request.get("id", 0)
            if request.get("method") in ("quit", "exit"):
                _print({"id": request_id, "ok": True, "result": {"quitting": True}})
                break
            result = session.call(request["method"], request.get("arguments") or {})
            _print({"id": request_id, "ok": True, "result": save_screenshots(result)})
        except BridgeError as error:
            _print({"id": locals().get("request_id", 0), "ok": False,
                    "error": {"code": error.code, "message": str(error)}})
        except Exception as error:  # noqa: BLE001
            _print({"id": locals().get("request_id", 0), "ok": False,
                    "error": {"code": "KIMICU_BRIDGE_ERROR", "message": repr(error)}})
    session.end()
    return 0


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    command, rest = args[0], args[1:]
    if command == "ping":
        return cmd_ping()
    if command == "call":
        if not rest:
            print("call needs a method name", file=sys.stderr)
            return 2
        return cmd_call(rest[0], rest[1] if len(rest) > 1 else None)
    if command == "serve":
        return cmd_serve()
    print(f"unknown command {command!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

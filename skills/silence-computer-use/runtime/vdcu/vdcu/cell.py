"""Execute Python cells against the live isolated session.

Bundled client packages connect to the local GAC transport. Public actions emit
compact transcripts; GUI results still require an observation to verify them.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import time

PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

from vdcu import win32 as _win32  # noqa: E402
from vdcu.gac_server import engine_packages  # noqa: E402
from vdcu.session import STATE_PATH, load_state  # noqa: E402

PACKAGES = engine_packages()

GAC_STATE = os.path.join(os.path.dirname(STATE_PATH), "gac.json")
GAC_LOG = os.path.join(os.path.dirname(STATE_PATH), "gac-server.log")


class NoSession(RuntimeError):
    """No live vdcu session; the agent should enter one first."""


# --------------------------------------------------------------------------- #
# GAC server lifecycle                                                         #
# --------------------------------------------------------------------------- #

def _read_gac_state() -> dict | None:
    try:
        with open(GAC_STATE, encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        return None
    if not _win32.process_alive(int(state.get("pid", 0))):
        return None
    try:
        with socket.create_connection(("127.0.0.1", int(state["port"])), timeout=1):
            pass
    except OSError:
        return None
    return state


def _spawn_gac_server() -> dict:
    secret_b64 = base64.urlsafe_b64encode(secrets.token_bytes(18)).decode()
    log = open(GAC_LOG, "ab", buffering=0)
    creation = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "vdcu.gac_server", "--port", "0",
         "--secret", secret_b64],
        cwd=PACKAGE_ROOT, stdout=log, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, creationflags=creation,
    )
    deadline = time.time() + 15
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(
                f"gac_server exited early (rc={proc.returncode}); see {GAC_LOG}"
            )
        try:
            with open(GAC_LOG, "r", encoding="utf-8", errors="replace") as handle:
                match = re.search(r"listening on [\d.]+:(\d+)", handle.read())
        except OSError:
            match = None
        if match:
            state = {"port": int(match.group(1)), "secret": secret_b64,
                     "pid": proc.pid}
            with open(GAC_STATE, "w", encoding="utf-8") as handle:
                json.dump(state, handle)
            return state
        time.sleep(0.2)
    raise RuntimeError(f"gac_server did not announce its port; see {GAC_LOG}")


def ensure_gac() -> dict:
    """A running GAC endpoint for the current session: reuse or spawn."""

    if not load_state():
        raise NoSession(
            "no live agent desktop; start one with 'vdcu enter' first "
            "(the GAC bridge serves the session, it does not replace it)"
        )
    state = _read_gac_state()
    if state:
        return state
    # A foreign engine pack may be absent: the bridge needs the pack's own
    # gac_runtime to speak the wire protocol.
    if not os.path.isdir(PACKAGES):
        raise RuntimeError(
            f"engine pack not found at {PACKAGES}; run 'vdcu engine-pack' once"
        )
    return _spawn_gac_server()


def cell_env(state: dict) -> dict:
    """The env Shadow's own GAC client dials from (transport/config.py)."""

    env = dict(os.environ)
    env.update({
        "CUA_GAC_TRANSPORT": "tcp",
        "CUA_GAC_SERVER_PORT": str(state["port"]),
        "CUA_GAC_SECRET": state["secret"],
        "CUA_GAC_SESSION_ID": "vdcu",
        "CUA_GAC_EXECUTION_ID": f"cell-{int(time.time())}",
    })
    return env


# --------------------------------------------------------------------------- #
# observation — the vendor's own cell-local sink (public/_instrumentation.py)  #
# --------------------------------------------------------------------------- #

_VERBS = {
    "click": "点击", "left_double": "双击", "triple_click": "三击",
    "right_single": "右键", "hover": "悬停", "drag": "拖动", "scroll": "滚动",
    "type": "输入", "hotkey": "按键", "press": "按键", "key_down": "按下",
    "key_up": "松开", "launch_app": "打开", "wait": "等待",
}


class CellObserver:
    """The executor Shadow's instrumentation audits into.

    The public API is already wrapped (``wrap_action`` / ``wrap_screenshot``):
    every action is reported as ``record(..., state=attempted|rejected|
    completed)`` with compact arguments (``type`` audits as ``chars=N`` — the
    text itself never reaches this sink, which is the vendor's own privacy
    rule), and every ``screenshot()`` frame arrives at ``emit_image``.  This
    observer turns those into the one-line echoes and the PNG file an agent
    needs — the same shape as Shadow's own transcript cards.
    """

    def __init__(self, shots_dir: str, *, echo: bool = True):
        self.shots_dir = shots_dir
        self.echo = echo
        self.count = 0
        os.makedirs(shots_dir, exist_ok=True)

    # -- the sink interface --------------------------------------------- #
    def record(self, plane: str, name: str, arguments: list, *, state: str) -> None:
        if not self.echo or plane != "cu":
            return
        if state == "completed":
            verb = _VERBS.get(name, name)
            detail = " ".join(str(a) for a in arguments)
            print(f"{verb} {detail}".rstrip(), flush=True)
        elif state == "rejected":
            print(f"{_VERBS.get(name, name)} 被拒绝", flush=True)

    def emit_image(self, data: bytes, *, tag: str | None, width: int,
                   height: int, scope: str) -> None:
        self.count += 1
        path = os.path.join(self.shots_dir, f"shot_{self.count:03d}.png")
        if data.startswith(b"\x89PNG"):
            with open(path, "wb") as handle:
                handle.write(data)
            if self.echo:
                print(f"已读取当前屏幕状态 → {path} ({width}×{height})", flush=True)
        elif self.echo:
            print("已读取当前屏幕状态（无图像数据）", flush=True)


# --------------------------------------------------------------------------- #
# cell execution                                                               #
# --------------------------------------------------------------------------- #

def run_cell(source: str, *, filename: str = "<cell>", shots_dir: str | None = None,
             echo: bool = True) -> int:
    """Exec one cell against the live session; returns a process-style rc."""

    if not os.environ.get("CUA_GAC_SERVER_PORT"):
        # Normal path: ensure the bridge for the live session and dial it.
        # An already-set port means the caller injected an endpoint (tests,
        # or --serve-only users running their own python).
        state = ensure_gac()
        os.environ.update(cell_env(state))
    if PACKAGES not in sys.path:
        sys.path.insert(0, PACKAGES)

    import seed_computer_use as cu  # the engine pack's own client
    from gac_runtime.computer_use._windows.public import _instrumentation

    observer = CellObserver(shots_dir or os.path.join(os.getcwd(), "cu_shots"),
                            echo=echo)
    previous = _instrumentation.install_executor(observer)
    namespace = {"__name__": "__cell__", "cu": cu}
    try:
        exec(compile(source, filename, "exec"), namespace)  # noqa: S102 - the
        # cell IS the user's agent program; that is the whole point here.
    except Exception as error:  # noqa: BLE001
        print(f"cell failed: {error}", file=sys.stderr, flush=True)
        return 1
    finally:
        _instrumentation.restore_executor(previous)
    return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="vdcu cu",
        description="run a Shadow-style Python cell against the live session",
    )
    parser.add_argument("file", nargs="?", help="cell file to run")
    parser.add_argument("-c", "--code", help="cell source given inline")
    parser.add_argument("--shots-dir", default=None,
                        help="where screenshot() saves PNGs (default ./cu_shots)")
    parser.add_argument("--no-echo", action="store_true",
                        help="do not print the one-line action echoes")
    parser.add_argument("--serve-only", action="store_true",
                        help="just ensure the GAC bridge is up and print its env")
    options = parser.parse_args(argv)

    if options.serve_only:
        state = ensure_gac()
        print(f"CUA_GAC_TRANSPORT=tcp CUA_GAC_SERVER_PORT={state['port']} "
              f"CUA_GAC_SECRET={state['secret']} CUA_GAC_SESSION_ID=vdcu "
              f"CUA_GAC_EXECUTION_ID=manual")
        return 0

    if options.code is not None:
        source, filename = options.code, "<-c>"
    elif options.file:
        with open(options.file, "r", encoding="utf-8") as handle:
            source, filename = handle.read(), options.file
    else:
        parser.error("give a cell file or -c CODE")
    return run_cell(source, filename=filename, shots_dir=options.shots_dir,
                    echo=not options.no_echo)


if __name__ == "__main__":
    raise SystemExit(main())

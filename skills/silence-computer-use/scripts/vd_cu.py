r"""vd_cu — operate a computer inside the shadow shadow session through KimiCU.

Stack (bottom to top):
  - vdcu (the skill's own runtime/vdcu engine pack, self-contained; falls back
    to %LOCALAPPDATA%\Programs\vdcu on old installations) provides the virtual
    space: a shadow-engine shadow session (own desktop, own input queue).
  - a KimiCU agent (runtime/cu.exe) launched *inside* that session provides
    the operation layer: screenshots, UIA, real mouse/keyboard — session-local.
  - this script bridges both: it talks to vdcu's daemon (session lifecycle,
    app catalogue launches) and to the in-session KimiCU pipe
    (default \\.\pipe\kimi-cu-win-vd, set via KIMI_CU_WIN_AGENT_PIPE at launch).

Commands:
  setup     repair the in-skill runtime/ from a legacy vendor/ bundle —
            offline, idempotent; a normal install needs no setup at all
  doctor    check prerequisites (Python, KimiCU runtime, vdcu pack) with
            remediation hints — the first thing to run on a new machine/host
  up        ensure the shadow session AND the in-session KimiCU agent are up
  status    session + agent health
  call M [JSON]   one KimiCU call (lease begin/end around it); screenshots are
                  saved to ./cu_shots/ and the result carries file paths
  tree WIN [KW]   print a window's UIA tree (WIN is a window id, a title
                  substring, or the list_windows JSON), optionally only lines
                  containing KW — how you find element_index values without
                  drowning in JSON
  serve     NDJSON loop on stdin/stdout: {"id":N,"method":"...","arguments":{...}}
                  optional per-request "tree_filter":"KW" shrinks big UIA trees
                  to matching lines; unfiltered long trees are truncated inline
                  with the full text saved to ./cu_trees/
  down      leave the shadow session (kills everything in it, agent included)

KimiCU methods (arguments schema mirrors @kimi/cu):
  list_windows | get_window {id} | list_apps | list_available_apps
  launch_app {app} | get_window_state {window, include_screenshot, include_text}
  click {window, x, y, screenshotId} or {window, element_index}
  press_key {window, key} | type_text {window, text}
  scroll {window, x, y, scrollX, scrollY} | drag {window, from_x, ...}
  drag_path {window, points} | set_value {window, element_index, value}
  perform_secondary_action {window, element_index, action}
  activate_window {window}

Coordinates are *screenshot pixels* of the window's own screenshot (top-left
origin) — always take a fresh get_window_state before clicking.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import cu_bridge  # noqa: E402


def _runtime_path(*parts: str) -> str:
    """A path inside the skill's self-contained runtime/ directory."""

    return os.path.normpath(os.path.join(HERE, os.pardir, "runtime", *parts))


def _default_vdcu_home() -> str:
    # The skill ships the vdcu engine pack in runtime/ — fully self-contained.
    # %LOCALAPPDATA%\Programs\vdcu remains as a fallback for old installations.
    in_skill = _runtime_path("vdcu")
    if os.path.exists(os.path.join(in_skill, "run_cli.py")):
        return in_skill
    return os.path.expandvars(r"%LOCALAPPDATA%\Programs\vdcu")


def _default_kimicu_exe() -> str:
    in_skill = _runtime_path("cu.exe")
    if os.path.exists(in_skill):
        return in_skill
    return os.path.expandvars(r"%LOCALAPPDATA%\KimiCU\kimi-cu.exe")


VDCU_HOME = os.environ.get("VDCU_HOME") or _default_vdcu_home()
KIMICU_EXE = os.environ.get("KIMICU_EXE") or _default_kimicu_exe()
VD_PIPE = os.environ.get("VD_KIMICU_PIPE", r"\\.\pipe\kimi-cu-win-vd")
AGENT_LOG = os.path.join(os.environ.get("TEMP", "."), "vd_kimicu_agent.log")
TREE_DIR = os.environ.get("VD_CU_TREE_DIR", "cu_trees")
TREE_INLINE_LIMIT = int(os.environ.get("VD_CU_TREE_INLINE_LIMIT", "3000"))


def _utf8_stdio() -> None:
    """Pin stdio to UTF-8 regardless of the console codepage.

    serve's NDJSON contract (and every command's JSON output) is UTF-8.  On a
    GBK-locale Windows host the default cp936 streams corrupt Chinese payloads
    both ways: reads turn UTF-8 pipe bytes into mojibake, writes crash a
    UTF-8-decoding consumer — and lone surrogates from mangled input kill a
    strict encoder outright ("surrogates not allowed").  Reconfiguring here
    frees callers from having to set PYTHONIOENCODING.
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


def _hide_owned_console() -> None:
    """Hide the console window when this process is its only owner.

    Double-clicking vd_cu.py (or launching it via Win+R / a shortcut /
    Start-Process) makes Windows allocate a fresh console for python.exe —
    the "black box" that pops up before the shadow password prompt.  When the
    console is shared with a parent shell (cmd / PowerShell / Git Bash / an
    agent host), leave it alone so this script's output stays visible.
    """

    if os.name != "nt":
        return
    import ctypes

    kernel32 = ctypes.windll.kernel32
    hwnd = kernel32.GetConsoleWindow()
    if not hwnd:
        return
    attached = (ctypes.c_ulong * 16)()
    count = kernel32.GetConsoleProcessList(attached, len(attached))
    if count == 1 and attached[0] == os.getpid():
        ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE


def _vendor_dirs() -> list[str]:
    """vendor/ sits next to the skill root; tolerate flat script layouts too."""

    return [
        os.path.normpath(os.path.join(HERE, os.pardir, "vendor")),
        os.path.join(HERE, "vendor"),
    ]


def _vendor_kimicu() -> str | None:
    """Bundled cu.exe, when the skill ships its runtime offline."""

    for directory in _vendor_dirs():
        for filename in ("cu.exe", "kimi-cu.exe"):
            path = os.path.join(directory, filename)
            if os.path.exists(path):
                return path
    return None


def _vendor_vdcu_zip() -> str | None:
    """Newest bundled vdcu-portable-*.zip, when the skill ships one."""

    for directory in _vendor_dirs():
        if not os.path.isdir(directory):
            continue
        candidates = sorted(
            (n for n in os.listdir(directory)
             if n.startswith("vdcu-portable-") and n.endswith(".zip")),
            reverse=True,
        )
        if candidates:
            return os.path.join(directory, candidates[0])
    return None


def _print(payload) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2), flush=True)


def _print_line(payload) -> None:
    """NDJSON: one compact response per line (serve mode's contract)."""

    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _filter_tree(tree: str, keyword: str) -> str:
    """Keep only lines containing the keyword (case-insensitive)."""

    needle = keyword.lower()
    return "\n".join(line for line in tree.split("\n") if needle in line.lower())


def _shrink_result(result, tree_filter: str | None = None):
    """Keep responses small enough that shells/chat UIs never truncate them.

    A full UIA tree can be tens of KB on one JSON line — past every consumer's
    limit (bash pipes, log viewers, chat renderers).  When the tree is big, the
    full text goes to ./cu_trees/tree_*.txt and the response carries a short
    preview plus the file path.  ``tree_filter`` additionally reduces the
    inline preview to matching lines (the file still gets the full tree).
    """

    if not isinstance(result, dict):
        return result
    acc = result.get("accessibility")
    tree = acc.get("tree") if isinstance(acc, dict) else None
    if not isinstance(tree, str):
        return result
    if tree_filter:
        acc["tree"] = _filter_tree(tree, tree_filter)
        acc["tree_filtered"] = tree_filter
    if len(tree) > TREE_INLINE_LIMIT:
        os.makedirs(TREE_DIR, exist_ok=True)
        path = os.path.abspath(os.path.join(TREE_DIR, f"tree_{int(time.time() * 1000)}.txt"))
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(tree)
        acc["tree_path"] = path
        if not tree_filter:
            acc["tree"] = (
                tree[:TREE_INLINE_LIMIT]
                + f"\n... [tree truncated inline: {len(tree)} chars total, full tree: {path}]"
            )
    return result


def _vdcu_client():
    sys.path.insert(0, VDCU_HOME)
    from vdcu import client  # noqa: PLC0415

    return client


def _session_status() -> dict | None:
    try:
        cu = _vdcu_client().Client.current()
        return cu.status()
    except Exception:  # noqa: BLE001 - no live session
        return None


def _agent_up() -> dict | None:
    try:
        return cu_bridge.Session(pipe_path=VD_PIPE, label="silence-computer-use").ping()["result"]
    except Exception:  # noqa: BLE001
        return None


def prerequisites() -> list[dict]:
    """Everything this stack needs, each with a remediation hint for any host."""

    checks = []
    py_ok = sys.version_info >= (3, 10)
    checks.append({
        "name": "python>=3.10", "ok": py_ok, "detail": sys.version.split()[0],
        "hint": "" if py_ok else "install Python 3.10+ (python.org) and rerun with it",
    })
    kimicu_ok = os.path.exists(KIMICU_EXE)
    kimicu_hint = (
        "run `vd_cu.py setup` to restore runtime/cu.exe from the bundled vendor/"
        if _vendor_kimicu() else
        "restore the skill's runtime/cu.exe (or install the KimiCU runtime: "
        "curl -fsSL https://cdn.kimi.com/kimi-computer-use-windows/latest/"
        "setup_windows.cmd -o setup_windows.cmd && setup_windows.cmd, then copy "
        "the downloaded executable into runtime/cu.exe or set KIMICU_EXE)"
    )
    checks.append({
        "name": "kimicu_runtime", "ok": kimicu_ok, "detail": KIMICU_EXE,
        "hint": "" if kimicu_ok else kimicu_hint,
    })
    vdcu_ok = os.path.exists(os.path.join(VDCU_HOME, "run_cli.py"))
    vdcu_hint = (
        "run `vd_cu.py setup` to restore runtime/vdcu from the bundled vendor/"
        if _vendor_vdcu_zip() else
        "restore the skill's runtime/vdcu directory (or set VDCU_HOME to a vdcu "
        "engine-pack installation)"
    )
    checks.append({
        "name": "vdcu", "ok": vdcu_ok, "detail": VDCU_HOME,
        "hint": "" if vdcu_ok else vdcu_hint,
    })
    if vdcu_ok:
        try:
            out = subprocess.run(
                [sys.executable, os.path.join(VDCU_HOME, "run_cli.py"), "doctor"],
                capture_output=True, timeout=60,
            ).stdout.decode("utf-8", "replace")
            self_contained = '"self_contained": true' in out
        except Exception:  # noqa: BLE001
            self_contained = False
        checks.append({
            "name": "vdcu_engine_self_contained", "ok": self_contained,
            "detail": "shadow engine binaries bundled",
            "hint": "" if self_contained else
            "restore the complete bundled runtime or use explicit engine-pack source paths",
        })
    return checks


def _cheap_prerequisites() -> list[dict]:
    """File-existence checks only, for `up`'s hot path.

    The full check list (including the vdcu engine self-containment probe, which
    costs a whole Python subprocess) stays in `doctor` — `up` runs every session,
    so it must not pay that price.
    """

    checks = []
    py_ok = sys.version_info >= (3, 10)
    checks.append({
        "name": "python>=3.10", "ok": py_ok, "detail": sys.version.split()[0],
        "hint": "" if py_ok else "install Python 3.10+ (python.org) and rerun with it",
    })
    kimicu_ok = os.path.exists(KIMICU_EXE)
    checks.append({
        "name": "kimicu_runtime", "ok": kimicu_ok, "detail": KIMICU_EXE,
        "hint": "" if kimicu_ok else "run `vd_cu.py doctor` for install steps",
    })
    vdcu_ok = os.path.exists(os.path.join(VDCU_HOME, "run_cli.py"))
    checks.append({
        "name": "vdcu", "ok": vdcu_ok, "detail": VDCU_HOME,
        "hint": "" if vdcu_ok else "run `vd_cu.py doctor` for install steps",
    })
    return checks


def _extract_runtime(zip_path: str, dest: str) -> int:
    """Extract only the runtime payload from a vdcu-portable zip into dest.

    The zip ships docs/research/tests/installers around the actual runtime;
    the skill's runtime/ keeps just what sessions need: run_cli.py,
    run_mcp.py, README.md, the vdcu/ package, the engine/ pack and docs/.
    """

    keep_tops = {"run_cli.py", "run_mcp.py", "README.md"}
    keep_dirs = ("vdcu/", "engine/", "docs/")
    os.makedirs(dest, exist_ok=True)
    count = 0
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            name = info.filename
            if name.endswith("/") or not (name in keep_tops or name.startswith(keep_dirs)):
                continue
            target = os.path.join(dest, name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with archive.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            count += 1
    return count


def cmd_setup() -> int:
    """Repair the skill's in-skill runtime/ — offline from a legacy vendor/ bundle.

    The skill is self-contained: scripts/ + runtime/ (cu.exe plus the
    vdcu engine pack), so a normal install needs no setup at all.  This
    command rebuilds a missing/incomplete runtime from vendor/ (cu.exe,
    vdcu-portable-*.zip) when one is present, else prints online hints.
    Nothing is installed outside the skill directory.
    """

    global VDCU_HOME, KIMICU_EXE
    steps = []
    failed = False

    # 1. KimiCU runtime: a single self-contained exe, copy is the whole install.
    #    identity.json/update-state.json are machine-local — never bundled.
    if os.path.exists(KIMICU_EXE):
        steps.append({"name": "kimicu_runtime", "ok": True,
                      "detail": f"ready: {KIMICU_EXE}"})
    elif _vendor_kimicu():
        target = _runtime_path("cu.exe")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(_vendor_kimicu(), target)
        KIMICU_EXE = target
        steps.append({"name": "kimicu_runtime", "ok": True,
                      "detail": f"restored from vendor/ to {target}"})
    else:
        failed = True
        steps.append({"name": "kimicu_runtime", "ok": False,
                      "detail": "runtime/cu.exe missing, no vendor/cu.exe or legacy vendor/kimi-cu.exe bundled",
                      "hint": "curl -fsSL https://cdn.kimi.com/kimi-computer-use-windows/"
                              "latest/setup_windows.cmd -o setup_windows.cmd && setup_windows.cmd"
                              " && copy %LOCALAPPDATA%\\KimiCU\\kimi-cu.exe runtime/cu.exe"})

    # 2. vdcu engine pack: extract only the runtime payload into runtime/vdcu.
    run_cli = os.path.join(VDCU_HOME, "run_cli.py")
    if os.path.exists(run_cli):
        steps.append({"name": "vdcu", "ok": True, "detail": f"ready: {VDCU_HOME}"})
    elif _vendor_vdcu_zip():
        target = _runtime_path("vdcu")
        print(f"extracting runtime from {os.path.basename(_vendor_vdcu_zip())} ...", flush=True)
        count = _extract_runtime(_vendor_vdcu_zip(), target)
        VDCU_HOME = target
        if os.path.exists(os.path.join(target, "run_cli.py")):
            steps.append({"name": "vdcu", "ok": True,
                          "detail": f"restored {count} files from vendor/ to {target}"})
        else:
            failed = True
            steps.append({"name": "vdcu", "ok": False,
                          "detail": f"extracted {count} files but run_cli.py is missing"})
    else:
        failed = True
        steps.append({"name": "vdcu", "ok": False,
                      "detail": "runtime/vdcu missing, no vendor/vdcu-portable-*.zip bundled",
                      "hint": "restore the skill's runtime/vdcu directory, or set VDCU_HOME"})

    # 3. Verify the whole stack, engine self-containment included.
    checks = prerequisites()
    ok = not failed and all(c["ok"] for c in checks)
    _print({"ok": ok, "steps": steps, "checks": checks,
            "next": None if ok else "fix the failed step above, then rerun setup"})
    return 0 if ok else 1


def cmd_doctor() -> int:
    checks = prerequisites()
    ok = all(c["ok"] for c in checks)
    _print({
        "ok": ok,
        "checks": checks,
        "session": bool(_session_status()),
        "agent": _agent_up(),
        "pipe": VD_PIPE,
    })
    return 0 if ok else 1


def cmd_up() -> int:
    missing = [c for c in _cheap_prerequisites() if not c["ok"]]
    if missing:
        _print({"ok": False, "error": "prerequisites missing",
                "checks": missing, "hint": "run `vd_cu.py doctor` for the full list"})
        return 1

    # Fast path: session and agent both alive — reuse them, start nothing.
    status = _session_status()
    agent = _agent_up() if status else None
    if status and agent:
        _print({"ok": True, "desktop": status.get("desktop"), "already_running": True,
                "agent_pid": agent.get("pid"), "agent_version": agent.get("version"),
                "pipe": VD_PIPE})
        return 0

    client = None
    started_session = False
    if not status:
        print(
            "no vdcu session; starting one with the shadow engine — the user's "
            "screen may show Windows authorization if a password is required or detection is inconclusive",
            flush=True,
        )
        # In-process start: no `run_cli.py enter` subprocess to boot and exit,
        # and we hold the Client the moment the session publishes itself.
        client_module = _vdcu_client()
        try:
            client_module.start_session(engine="shadow")
            client = client_module.Client.wait(60)
        except Exception as error:  # noqa: BLE001 - vdcu raises NoSession & friends
            _print({"ok": False, "error": f"vdcu enter failed: {error}"})
            return 1
        started_session = True
        status = client.status()
    engine = (status or {}).get("engine", "shadow")
    print(f"shadow session: {(status or {}).get('desktop')} (engine={engine})", flush=True)

    if not agent:
        # With runtime/vdcu/autolaunch.json present, the worker already fired
        # cu.exe the moment the session desktop existed — its cold start has
        # been overlapping the shell paint and the session publish, so the pipe
        # is usually up by now.  Never RPC-launch a second copy on top of that
        # (two agents would share the pipe name); only a session we did NOT just
        # start (autolaunch does not refire) takes the RPC path.
        autolaunched = started_session and os.path.exists(
            os.path.join(VDCU_HOME, "autolaunch.json"))
        if autolaunched:
            print("agent autolaunched with the session; waiting for its pipe...",
                  flush=True)
            deadline = time.time() + 30
            while time.time() < deadline and not agent:
                time.sleep(0.25)
                agent = _agent_up()
        else:
            # Fire the in-session launch the instant the session serves — cu.exe's
            # cold start then overlaps with everything the caller does next.
            print("starting KimiCU agent inside the shadow session...", flush=True)
            if client is None:
                client = _vdcu_client().Client.current()
            command_line = (
                f'/c "set KIMI_CU_WIN_AGENT_PIPE={VD_PIPE}&& "{KIMICU_EXE}" start-agent '
                f'> "{AGENT_LOG}" 2>&1"'
            )
            result = client.rpc("launch", {
                "application": r"C:\Windows\System32\cmd.exe",
                "command_line": command_line,
            })
            if not result.get("ok", True):
                _print({"ok": False, "error": "in-session agent launch failed", "detail": result})
                return 1
            deadline = time.time() + 30
            while time.time() < deadline and not agent:
                time.sleep(0.25)
                agent = _agent_up()
    if not agent:
        _print({"ok": False, "error": f"in-session agent pipe {VD_PIPE} never came up",
                  "log": AGENT_LOG})
        return 1
    _print({"ok": True, "desktop": (status or {}).get("desktop"),
            "agent_pid": agent.get("pid"), "agent_version": agent.get("version"),
            "pipe": VD_PIPE})
    return 0


def cmd_status() -> int:
    _print({
        "session": _session_status(),
        "agent": _agent_up(),
        "pipe": VD_PIPE,
    })
    return 0


def cmd_call(method: str, args_json: str | None) -> int:
    arguments = json.loads(args_json) if args_json else {}
    if not _agent_up():
        print("in-session agent is down; run up first", file=sys.stderr)
        return 1
    session = cu_bridge.Session(pipe_path=VD_PIPE, label="silence-computer-use")
    try:
        session.begin()
        result = session.call(method, arguments)
        result = _shrink_result(cu_bridge.save_screenshots(result))
        _print({"ok": True, "result": result})
        return 0
    except cu_bridge.BridgeError as error:
        _print({"ok": False, "error": {"code": error.code, "message": str(error)}})
        return 1
    finally:
        session.end()


def cmd_serve() -> int:
    if not _agent_up():
        _print_line({"id": 0, "ok": False, "error": {"code": "VD_AGENT_DOWN",
                                                "message": "run up first"}})
        return 1
    session = cu_bridge.Session(pipe_path=VD_PIPE, label="silence-computer-use")
    try:
        session.begin()
    except cu_bridge.BridgeError as error:
        _print_line({"id": 0, "ok": False, "error": {"code": error.code, "message": str(error)}})
        return 1
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        request_id = 0
        try:
            request = json.loads(line)
            request_id = request.get("id", 0)
            if request.get("method") in ("quit", "exit"):
                _print_line({"id": request_id, "ok": True, "result": {"quitting": True}})
                break
            result = session.call(request["method"], request.get("arguments") or {})
            result = cu_bridge.save_screenshots(result)
            result = _shrink_result(result, tree_filter=request.get("tree_filter"))
            _print_line({"id": request_id, "ok": True, "result": result})
        except cu_bridge.BridgeError as error:
            _print_line({"id": request_id, "ok": False,
                    "error": {"code": error.code, "message": str(error)}})
        except Exception as error:  # noqa: BLE001
            _print_line({"id": request_id, "ok": False,
                    "error": {"code": "VD_CU_ERROR", "message": repr(error)}})
    session.end()
    return 0


def cmd_tree(window_spec: str, keyword: str | None = None) -> int:
    """Print a window's UIA tree, optionally only lines matching a keyword.

    The go-to way to find element_index values without drowning in JSON:
      vd_cu.py tree 590700 按钮        # by window id from list_windows
      vd_cu.py tree 计算器 按钮         # by title substring
      vd_cu.py tree '{"app":"...","id":123,"title":"..."}' 按钮
    """

    if not _agent_up():
        print("in-session agent is down; run up first", file=sys.stderr)
        return 1
    session = cu_bridge.Session(pipe_path=VD_PIPE, label="silence-computer-use")
    try:
        session.begin()
        try:
            window = json.loads(window_spec)
            if not isinstance(window, dict):
                raise ValueError("not a window object")
        except (json.JSONDecodeError, ValueError):
            # Bare id or title substring — resolve via list_windows.  (Shells
            # mangle JSON backslashes; a bare number or name never breaks.)
            windows = session.call("list_windows", {})
            window = next(
                (w for w in windows
                 if str(w.get("id")) == window_spec or window_spec in (w.get("title") or "")),
                None,
            )
            if window is None:
                _print({"ok": False, "error": f"no window matches {window_spec!r}",
                        "windows": windows})
                return 1
        result = session.call("get_window_state", {
            "window": window,
            "include_text": True,
            "include_screenshot": False,
        })
    except cu_bridge.BridgeError as error:
        _print({"ok": False, "error": {"code": error.code, "message": str(error)}})
        return 1
    finally:
        session.end()
    tree = ((result.get("accessibility") or {}).get("tree")) or ""
    print(_filter_tree(tree, keyword) if keyword else tree)
    return 0


def cmd_down() -> int:
    try:
        cu = _vdcu_client().Client.current()
        _print(cu.stop())
    except Exception as error:  # noqa: BLE001
        _print({"ok": False, "error": repr(error)})
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    _utf8_stdio()
    _hide_owned_console()
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    command, rest = args[0], args[1:]
    if command == "setup":
        return cmd_setup()
    if command == "up":
        return cmd_up()
    if command == "doctor":
        return cmd_doctor()
    if command == "status":
        return cmd_status()
    if command == "call":
        if not rest:
            print("call needs a method name", file=sys.stderr)
            return 2
        return cmd_call(rest[0], rest[1] if len(rest) > 1 else None)
    if command == "tree":
        if not rest:
            print("tree needs a window JSON object", file=sys.stderr)
            return 2
        return cmd_tree(rest[0], rest[1] if len(rest) > 1 else None)
    if command == "serve":
        return cmd_serve()
    if command == "down":
        return cmd_down()
    print(f"unknown command {command!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

"""Command line interface — the path for agents that only have a shell tool.

Every action prints one JSON object on stdout and, when it changed the screen,
saves a PNG and names it in ``screenshot``.  That is deliberate: an agent with a
Bash tool and an image-reading tool (Claude Code, Kimi Code, Codex-style hosts)
needs a *file* it can open, while an MCP host gets the same frame inline as image
content from :mod:`vdcu.mcp`.

Examples
--------
    python -m vdcu enter                      # start the agent desktop + preview box
    python -m vdcu capture                    # save a frame, print its path
    python -m vdcu click 500 250 --shot after-click.png
    python -m vdcu type "hello" --enter
    python -m vdcu launch "记事本"
    python -m vdcu leave
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import webbrowser

from . import client as client_module
from .client import Client, NoSession, SessionGone, save_frame


def _emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _session(args) -> Client:
    try:
        return Client.current()
    except NoSession:
        if getattr(args, "autostart", False):
            client_module.start_session(mode=getattr(args, "mode", "stealth"), preview=True)
            return Client.current()
        raise


def _action(args, op: str, payload: dict, *, tag: str | None = None) -> int:
    client = _session(args)
    try:
        response = client.rpc(op, payload)
    except SessionGone as error:
        _emit({"ok": False, "error": str(error)})
        return 2
    if not response.get("ok"):
        _emit({"ok": False, "op": op, "error": response.get("error")})
        return 1
    result = {
        "ok": True,
        "op": op,
        "desktop": client.desktop,
        "mode": client.mode,
        "result": response.get("result", {}),
    }
    path = save_frame(response, tag=tag or op, explicit=getattr(args, "shot", None))
    if path:
        result["screenshot"] = path
    if "frame_meta" in response:
        result["frame"] = response["frame_meta"]
    _emit(result)
    return 0


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def cmd_enter(args) -> int:
    if args.engine == "shadow" and sys.stderr.isatty():
        print("正在启动 virtual desktop；空密码账户自动授权，其他账户可能需要本地 Windows 授权。",
              file=sys.stderr)
    state = client_module.start_session(
        engine=args.engine,
        mode=args.mode,
        shell=not args.no_shell,
        preview=not args.no_preview,
        idle_timeout=args.idle_timeout,
        max_seconds=args.max_seconds,
        name=args.name,
    )
    client = Client.wait(60)
    status = client.status()
    _emit(
        {
            "ok": True,
            "desktop": client.desktop,
            "engine": client.engine,
            "mode": client.mode,
            "already_running": state.get("already_running", False),
            "worker_pid": status.get("worker_pid"),
            "live_url": f"{client.url}/?token={client.token}",
            "preview_box": not args.no_preview,
            "user_desktop_untouched": status.get("active_input_desktop") != client.desktop,
        }
    )
    if args.browser:
        webbrowser.open(f"{client.url}/")
    return 0


def cmd_leave(args) -> int:
    try:
        client = Client.current()
    except NoSession:
        _emit({"ok": True, "note": "no session was running"})
        return 0
    desktop = client.desktop
    stop_report = client.stop()
    time.sleep(1)
    lingering = client_module.describe_desktop(desktop)
    foreign = stop_report.get("foreign_processes") if isinstance(stop_report, dict) else None
    lingering_note = None
    if lingering.get("desktop_exists"):
        images = sorted({entry.get("image", "?") for entry in (foreign or lingering.get("held_by", {})).values()})
        lingering_note = (
            f"the desktop object is still held by {', '.join(images)} "
            "(not ours, so not killed); it disappears when they exit, at logoff at the latest"
        )
    _emit({
        "ok": True,
        "stopped": desktop,
        "teardown": {k: v for k, v in stop_report.items() if k != "foreign_processes"}
                    if isinstance(stop_report, dict) else {},
        "foreign_processes": foreign or lingering.get("held_by", {}),
        **{k: v for k, v in lingering.items() if k != "held_by"},
        # In the JSON, not on stderr: an agent running through a shell wrapper sees
        # stderr as a failure and would report a successful leave as an error.
        "note": lingering_note,
    })
    if lingering_note and sys.stderr.isatty():
        print(f"note: {lingering_note}", file=sys.stderr)
    return 0


def cmd_status(args) -> int:
    client = _session(args)
    _emit({"ok": True, **client.status()})
    return 0


def cmd_capture(args) -> int:
    client = _session(args)
    max_width = 0 if args.full else args.max_width
    image_format = args.format
    response = client.rpc(
        "capture", {"max_width": max_width or None, "format": image_format}
    )
    if not response.get("ok"):
        _emit({"ok": False, "error": response.get("error")})
        return 1
    path = save_frame(response, tag="capture", explicit=args.shot, image_format=image_format)
    _emit(
        {
            "ok": True,
            "screenshot": path,
            "result": response.get("result", {}),
            "frame": response.get("frame_meta", {}),
        }
    )
    return 0


def cmd_windows(args) -> int:
    client = _session(args)
    response = client.rpc("windows", {"include_untitled": args.all})
    if not response.get("ok"):
        _emit({"ok": False, "op": "windows", "error": response.get("error")})
        return 1
    _emit({"ok": True, "windows": response.get("result", {}).get("windows", [])})
    return 0


def cmd_apps(args) -> int:
    client = _session(args)
    response = client.rpc("list_apps", {"query": args.filter or "", "limit": args.limit})
    if not response.get("ok"):
        _emit({"ok": False, "op": "list_apps", "error": response.get("error")})
        return 1
    result = response.get("result", {})
    apps = result.get("apps", [])
    if args.filter:
        needle = args.filter.casefold()
        apps = [
            app for app in apps
            if needle in (app.get("name") or "").casefold()
            or needle in (app.get("key") or "").casefold()
        ]
    # ``count`` is how many matched before the limit; ``returned`` is what is shown.
    _emit({
        "ok": True,
        "count": result.get("count", len(apps)),
        "returned": len(apps[: args.limit]),
        "apps": apps[: args.limit],
    })
    return 0


def cmd_view(args) -> int:
    client = _session(args)
    url = f"{client.url}/"
    _emit({"ok": True, "live_url": url, "note": "在浏览器里打开可以看到实时画面"})
    webbrowser.open(url)
    return 0


def cmd_preview(args) -> int:
    from . import preview as preview_module

    # The daemon starts this process itself and passes the endpoint explicitly; a
    # user running the command by hand gets the live session's endpoint.
    style = getattr(args, "style", "card")
    width = args.width or (480 if style == "pip" else preview_module.DEFAULT_WIDTH)
    if args.url and args.token:
        return preview_module.PreviewBox(
            args.url, args.token, args.title or "vdcu", width=width, style=style,
        ).run()

    client = _session(args)
    return preview_module.PreviewBox(
        client.url, client.token, client.desktop, width=width, style=style,
    ).run()


def cmd_mcp(args) -> int:
    from . import mcp as mcp_module

    return mcp_module.main([])


def cmd_verify(args) -> int:
    """Run the bundled offline authentication regressions without GUI side effects."""
    import subprocess
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    result = subprocess.run([sys.executable, "-B", "-m", "unittest", "discover",
                             "-s", "tests", "-v"], cwd=root)
    _emit({"ok": result.returncode == 0, "suite": "authorization-regressions"})
    return result.returncode


def cmd_engine_pack(args) -> int:
    """Copy the vendor engine into a self-contained pack (no the bundled runtime needed later).

    Run this once on a machine that has the bundled runtime installed; afterwards ``--engine
    shadow`` works from the pack alone.  The files are byte-identical copies of the
    vendor's signed binaries — nothing is patched, so their signature allow-list
    stays satisfied.
    """

    from . import engine_pack

    try:
        report = engine_pack.build_pack(
            app_dir=args.from_dir or None,
            interpreter_dir=args.interpreter or None,
            target_root=args.to or None,
        )
    except engine_pack.PackError as error:
        _emit({"ok": False, "error": str(error)})
        return 1
    _emit(report)
    return 0


def cmd_doctor(args) -> int:
    """Report the facts that decide whether this host can run the package."""

    from . import win32

    report = {
        "ok": True,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "active_input_desktop": win32.active_input_desktop_name(),
        "desktops": win32.enum_desktops(),
        "virtual_screen": list(win32.virtual_screen()),
        "state_dir": client_module.desktop_module.STATE_DIR,
        "frame_dir": client_module.desktop_module.FRAME_DIR,
    }
    try:
        client = Client.current()
        report["session"] = client.status()
    except NoSession:
        report["session"] = None

    # Can this machine run the shadow engine at all?  Both pieces have to exist:
    # the vendor's signed interpreter (the only caller its signature allow-list
    # accepts) and the engine binaries themselves.
    try:
        from . import shadow_engine, engine_pack

        app_dir = shadow_engine.find_app_dir()
        pack = engine_pack.verify_pack()
        report["shadow_engine"] = {
            "available": True,
            "app_dir": app_dir,
            "source": "bundled pack" if pack["pack_root"] in app_dir else "explicit override",
            "self_contained": pack["ok"],
            "pip_sdk": shadow_engine.find_pip_dll(),
            "sandbox_python": shadow_engine.find_sandbox_python(),
        }
    except Exception as error:  # noqa: BLE001
        report["shadow_engine"] = {"available": False, "reason": str(error)}
    _emit(report)
    return 0


def cmd_cleanup(args) -> int:
    """Remove orphaned agent desktops left behind by a hard kill or an aborted run."""

    from . import cleanup as cleanup_module

    report = cleanup_module.reap(include=tuple(args.include or ()), dry_run=args.dry_run)
    _emit({"ok": True, **report})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vdcu",
        description="Virtual-desktop computer use for any agent host (Windows).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(target, *, autostart_default=True):
        target.add_argument("--shot", help="where to save the frame PNG")
        target.add_argument(
            "--autostart", action="store_true", default=autostart_default,
            help="start a session automatically when none is running (default: on)",
        )
        target.add_argument("--no-autostart", dest="autostart", action="store_false")
        target.add_argument("--mode", default="stealth", choices=("stealth", "takeover"))

    enter = sub.add_parser("enter", help="start the agent desktop")
    enter.add_argument("--engine", default="shadow", choices=("shadow", "stealth"),
                       help="shadow (default): isolated session; stealth: hidden Win32 desktop")
    enter.add_argument("--mode", default="stealth", choices=("stealth", "takeover"))
    enter.add_argument("--no-shell", action="store_true",
                       help="skip explorer on the agent desktop (no wallpaper or taskbar)")
    enter.add_argument("--no-preview", action="store_true")
    enter.add_argument("--browser", action="store_true", help="also open the live page")
    enter.add_argument("--idle-timeout", type=float, default=1800.0)
    enter.add_argument("--max-seconds", type=float, default=14400.0)
    enter.add_argument("--name", default=None)
    enter.set_defaults(func=cmd_enter)

    leave = sub.add_parser("leave", help="stop the agent desktop")
    leave.set_defaults(func=cmd_leave)

    status = sub.add_parser("status", help="session status")
    add_common(status, autostart_default=False)
    status.set_defaults(func=cmd_status)

    capture = sub.add_parser("capture", help="save a frame of the agent desktop")
    capture.add_argument("--max-width", type=int, default=0,
                         help="downscale to this width; 0 (default) keeps native resolution")
    capture.add_argument("--format", default="jpeg", choices=("jpeg", "png"),
                         help="jpeg (default, ~0.9 MB at native resolution) or png (lossless, ~3 MB)")
    capture.add_argument("--full", action="store_true")
    add_common(capture)
    capture.set_defaults(func=cmd_capture)

    windows = sub.add_parser("windows", help="list windows on the agent desktop")
    windows.add_argument("--all", action="store_true", help="include untitled windows")
    add_common(windows)
    windows.set_defaults(func=cmd_windows)

    apps = sub.add_parser("apps", help="list launchable applications")
    apps.add_argument("--filter", default=None)
    apps.add_argument("--limit", type=int, default=40)
    add_common(apps)
    apps.set_defaults(func=cmd_apps)

    click = sub.add_parser("click", help="click at normalized 0-1000 coordinates")
    click.add_argument("x", type=float)
    click.add_argument("y", type=float)
    click.add_argument("--button", default="left", choices=("left", "right", "middle"))
    click.add_argument("--clicks", type=int, default=1)
    add_common(click)
    click.set_defaults(func=lambda args: _action(
        args, "click", {"x": args.x, "y": args.y, "button": args.button, "clicks": args.clicks}
    ))

    for name, op in (("double-click", "double_click"), ("right-click", None)):
        node = sub.add_parser(name, help=f"{name} at normalized coordinates")
        node.add_argument("x", type=float)
        node.add_argument("y", type=float)
        add_common(node)
        if op:
            node.set_defaults(func=lambda args, op=op: _action(args, op, {"x": args.x, "y": args.y}))
        else:
            node.set_defaults(func=lambda args: _action(
                args, "click", {"x": args.x, "y": args.y, "button": "right"}
            ))

    move = sub.add_parser("move", help="move the pointer")
    move.add_argument("x", type=float)
    move.add_argument("y", type=float)
    add_common(move)
    move.set_defaults(func=lambda args: _action(args, "move", {"x": args.x, "y": args.y}))

    drag = sub.add_parser("drag", help="drag between two points")
    for label in ("x1", "y1", "x2", "y2"):
        drag.add_argument(label, type=float)
    drag.add_argument("--duration-ms", type=int, default=800)
    add_common(drag)
    drag.set_defaults(func=lambda args: _action(args, "drag", {
        "x1": args.x1, "y1": args.y1, "x2": args.x2, "y2": args.y2,
        "duration_ms": args.duration_ms,
    }))

    scroll = sub.add_parser("scroll", help="scroll at a point")
    scroll.add_argument("x", type=float)
    scroll.add_argument("y", type=float)
    scroll.add_argument("delta", help="up/down/left/right or -10..10")
    add_common(scroll)
    scroll.set_defaults(func=lambda args: _action(
        args, "scroll", {"x": args.x, "y": args.y, "delta": args.delta}
    ))

    type_node = sub.add_parser("type", help="type text into the focused window")
    type_node.add_argument("text")
    type_node.add_argument("--enter", action="store_true", help="press Enter afterwards")
    add_common(type_node)
    type_node.set_defaults(func=lambda args: _action(
        args, "type", {"text": args.text + ("\n" if args.enter else "")}
    ))

    key = sub.add_parser("key", help="press one key")
    key.add_argument("name")
    add_common(key)
    key.set_defaults(func=lambda args: _action(args, "key", {"key": args.name}))

    hotkey = sub.add_parser("hotkey", help="press a chord, keys passed separately")
    hotkey.add_argument("keys", nargs="+")
    add_common(hotkey)
    hotkey.set_defaults(func=lambda args: _action(args, "hotkey", {"keys": args.keys}))

    key_down = sub.add_parser("key-down", help="hold a key down until key-up")
    key_down.add_argument("name")
    add_common(key_down)
    key_down.set_defaults(func=lambda args: _action(args, "key_down", {"key": args.name}))

    key_up = sub.add_parser("key-up", help="release a held key")
    key_up.add_argument("name")
    add_common(key_up)
    key_up.set_defaults(func=lambda args: _action(args, "key_up", {"key": args.name}))

    copy = sub.add_parser("copy", help="post Ctrl+C and return the clipboard text")
    add_common(copy)
    copy.set_defaults(func=lambda args: _action(args, "copy_selection", {}))

    wait = sub.add_parser("wait", help="wait 0-180 seconds on the agent desktop")
    wait.add_argument("seconds", type=float)
    add_common(wait)
    wait.set_defaults(func=lambda args: _action(args, "wait", {"seconds": args.seconds}))

    children = sub.add_parser("children", help="list child windows of one window")
    children.add_argument("hwnd", type=int)
    add_common(children)
    children.set_defaults(func=lambda args: _action(args, "children", {"hwnd": args.hwnd}))

    clipboard = sub.add_parser("clipboard", help="read or write the clipboard")
    clipboard.add_argument("action", choices=("get", "set"))
    clipboard.add_argument("text", nargs="?")
    add_common(clipboard)
    clipboard.set_defaults(func=lambda args: _action(
        args,
        "clipboard_get" if args.action == "get" else "clipboard_set",
        {} if args.action == "get" else {"text": args.text or ""},
    ))

    launch = sub.add_parser("launch", help="launch an app on the agent desktop")
    launch.add_argument("name")
    add_common(launch)
    launch.set_defaults(func=lambda args: _action(args, "launch", {"name": args.name}))

    view = sub.add_parser("view", help="open the live page in a browser")
    add_common(view, autostart_default=False)
    view.set_defaults(func=cmd_view)

    preview = sub.add_parser("preview", help="open the always-on-top preview box")
    preview.add_argument("--width", type=int, default=None)
    preview.add_argument("--style", choices=("card", "pip"), default="card")
    preview.add_argument("--url", default=None, help="session endpoint (set by the daemon)")
    preview.add_argument("--token", default=None, help="session token (set by the daemon)")
    preview.add_argument("--title", default=None, help="window title suffix")
    add_common(preview, autostart_default=False)
    preview.set_defaults(func=cmd_preview)

    mcp = sub.add_parser("mcp", help="run the MCP server on stdio (for Claude Code, Kimi Code, ...)")
    mcp.set_defaults(func=cmd_mcp)

    doctor = sub.add_parser("doctor", help="report host capabilities and session state")
    doctor.set_defaults(func=cmd_doctor)

    pack = sub.add_parser("engine-pack", help="copy signed engine components from explicit local sources")
    pack.add_argument("--from", dest="from_dir", default="", help="engine app directory to copy from")
    pack.add_argument("--interpreter", default="", help="the sandbox python directory to copy")
    pack.add_argument("--to", default="", help="pack destination (default: <install>/engine)")
    pack.set_defaults(func=cmd_engine_pack)

    verify = sub.add_parser("verify", help="run bundled offline authentication regressions")
    verify.set_defaults(func=cmd_verify)

    cleanup = sub.add_parser("cleanup", help="remove orphaned agent desktops and stray workers")
    cleanup.add_argument("--dry-run", action="store_true")
    cleanup.add_argument("--include", nargs="*", default=[])
    cleanup.set_defaults(func=cmd_cleanup)

    return parser


INTERNAL = ("daemon", "worker", "watchdog")


def _internal(name: str, rest: list[str]) -> int:
    """Forward the daemon/worker/watchdog subcommands straight to their modules.

    These are process entry points (the session daemon, the worker that lives on
    the agent desktop, the detached guardian), not user commands, and their flags
    are parsed by their own argument parsers.  Routing them through the CLI parser
    would make argparse reject those flags — as it did with ``--no-preview`` — so
    they are intercepted before it runs.
    """

    from . import session as session_module
    from . import watchdog as watchdog_module
    from . import worker as worker_module

    module = {"daemon": session_module, "worker": worker_module, "watchdog": watchdog_module}[name]
    return module.main(rest)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    from . import win32

    win32.enable_dpi_awareness()  # keep reported metrics in the same space as captures
    if argv and argv[0] in INTERNAL:
        return _internal(argv[0], argv[1:])

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except NoSession as error:
        _emit({"ok": False, "error": str(error), "hint": "run 'vdcu enter' first"})
        return 3
    except SessionGone as error:
        _emit({"ok": False, "error": str(error)})
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

"""Reap orphaned agent desktops, their processes and stray workers.

A desktop object is destroyed only when nothing is attached to it any more.  If a
daemon is killed hard — or a test aborts — the desktop, the worker on it and the
apps it started can all survive, invisible but real.  That is litter the user never
asked for, and this module removes it.

Safety rules, because this deletes things:

* only desktops whose name carries the agent prefix are touched (``vdcu_``), plus any
  explicitly named legacy desktop the caller passes in;
* the desktop of the *live* session is never touched, and neither is its worker;
* the user's own desktop (``Default``) is never a candidate — the prefix rule makes
  that structurally impossible rather than a matter of care.
"""

from __future__ import annotations

import os
import subprocess
import time

from . import win32

AGENT_PREFIX = "vdcu_"
_HEADLESS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def agent_desktops(*, include: tuple[str, ...] = ()) -> list[str]:
    """Every desktop that looks like ours, excluding the live session's."""

    live = ""
    try:
        from .session import load_state

        state = load_state()
        live = (state or {}).get("desktop", "")
    except Exception:  # noqa: BLE001
        live = ""

    names = []
    for name in win32.enum_desktops():
        if name == "Default" or name == live:
            continue
        if name.startswith(AGENT_PREFIX) or name in include:
            names.append(name)
    return names


def _pids_on_desktop(name: str) -> list[int]:
    from ctypes import wintypes

    try:
        hdesk = win32.open_desktop(name, win32.DESKTOP_ENUMERATE)
    except win32.Win32Error:
        return []

    pids: set[int] = set()

    def callback(hwnd, _lparam):
        owner = wintypes.DWORD()
        win32.user32.GetWindowThreadProcessId(hwnd, __import__("ctypes").byref(owner))
        if owner.value:
            pids.add(int(owner.value))
        return True

    try:
        win32.user32.EnumDesktopWindows(hdesk, win32.WNDENUMPROC(callback), 0)
    finally:
        win32.close_desktop(hdesk)
    return sorted(pid for pid in pids if pid != os.getpid())


def _kill(pid: int) -> bool:
    handle = win32.kernel32.OpenProcess(win32.PROCESS_TERMINATE | win32.SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        return bool(win32.kernel32.TerminateProcess(handle, 1))
    finally:
        win32.kernel32.CloseHandle(handle)


def _vdcu_processes() -> list[dict]:
    """Processes that are part of this package, from their command line."""

    script = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*-m vdcu *' } |"
        " ForEach-Object { '{0}`t{1}' -f $_.ProcessId, $_.CommandLine }"
    )
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=30, check=False, creationflags=_HEADLESS,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in completed.stdout.decode("utf-8", errors="replace").splitlines():
        if "\t" not in line:
            continue
        pid_text, _, command = line.partition("\t")
        try:
            found.append({"pid": int(pid_text.strip()), "command": command.strip()})
        except ValueError:
            continue
    return found


def reap(*, include: tuple[str, ...] = (), dry_run: bool = False) -> dict:
    """Kill everything attached to orphaned agent desktops and report what was done."""

    from .session import load_state

    state = load_state() or {}
    live_worker = int(state.get("worker_pid") or 0)
    live_pid = int(state.get("pid") or 0)

    report: dict = {
        "dry_run": dry_run,
        "desktops": [],
        "killed": [],
        "workers": [],
        "skipped_live": {"pid": live_pid, "worker_pid": live_worker},
        "preview_cards": [],
    }

    # orphan preview cards: windows of ours titled virtual desktop whose session is
    # gone.  They belong to no desktop, so without this they would sit on the
    # user's desktop forever — which annoyed them.
    owned_pids = {p["pid"] for p in _vdcu_processes()}
    for window in win32.desktop_windows():
        title = (window.get("title") or "")
        if not title.startswith("virtual desktop"):
            continue
        pid = int(window.get("pid") or 0)
        if pid not in owned_pids:
            continue
        image = (win32.process_image_name(pid) or "").rsplit("\\", 1)[-1].casefold()
        if image not in ("python.exe", "pythonw.exe"):
            continue
        if pid == live_pid or pid == live_worker:
            continue
        entry = {"hwnd": int(window["hwnd"]), "pid": pid, "title": title, "killed": False}
        if not dry_run and _kill(pid):
            entry["killed"] = True
            report["killed"].append(pid)
        report["preview_cards"].append(entry)

    for name in agent_desktops(include=include):
        pids = _pids_on_desktop(name)
        threads = win32.threads_on_desktop(name)
        thread_pids = sorted({item["pid"] for item in threads})
        entry = {
            "desktop": name,
            "pids": pids,
            "images": {
                pid: (win32.process_image_name(pid) or "?").rsplit("\\", 1)[-1]
                for pid in pids
            },
            # Windowless holders: invisible to window enumeration, which is exactly
            # how an "empty" desktop survives every cleanup that only looks at
            # windows.  Reported always, killed only for our own desktops.
            "thread_holders": threads,
            "killed": [],
        }
        if not dry_run:
            is_ours = name.startswith(AGENT_PREFIX)
            for pid in pids:
                if pid in (live_pid, live_worker):
                    continue
                if _kill(pid):
                    entry["killed"].append(pid)
                    report["killed"].append(pid)
            if is_ours:
                # Windowless leftovers on *our* desktops are ours to reap; anything
                # on a foreign desktop (the --include case) is reported only.
                for pid in thread_pids:
                    if pid in (live_pid, live_worker) or pid in pids:
                        continue
                    if _kill(pid):
                        entry["killed"].append(pid)
                        report["killed"].append(pid)
            time.sleep(0.3)
            try:
                handle = win32.open_desktop(name, win32.DESKTOP_READOBJECTS)
            except win32.Win32Error:
                handle = None
            if handle:
                win32.close_desktop(handle)
        report["desktops"].append(entry)

    # Workers whose daemon is gone: they hold a handle to their desktop, which is
    # exactly what keeps an orphan alive.
    alive_desktops = set(win32.enum_desktops())
    for process in _vdcu_processes():
        if process["pid"] in (live_pid, live_worker):
            continue
        command = process["command"]
        if "-m vdcu worker" not in command:
            continue
        desktop = ""
        if "--desktop" in command:
            desktop = command.split("--desktop", 1)[1].strip().split()[0]
        stale = bool(desktop) and desktop not in alive_desktops
        report["workers"].append({"pid": process["pid"], "desktop": desktop, "stale": stale})
        if stale and not dry_run and _kill(process["pid"]):
            report["killed"].append(process["pid"])

    if not dry_run:
        report["desktops_remaining"] = [
            name for name in win32.enum_desktops() if name.startswith(AGENT_PREFIX)
        ]
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="vdcu cleanup", description="remove orphaned agent desktops")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--include", nargs="*", default=[], help="extra desktop names to treat as ours")
    parser.add_argument("--if-needed", action="store_true", help="print nothing when there is nothing to do")
    options = parser.parse_args(argv)

    report = reap(include=tuple(options.include), dry_run=options.dry_run)
    if options.if_needed and not report["desktops"] and not report["workers"]:
        return 0
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

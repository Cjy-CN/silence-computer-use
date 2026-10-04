"""Detached guardian: cleans up if the daemon dies without cleaning up itself.

The daemon restores and releases everything in its own ``finally``.  This process
exists for the case where it cannot: a kill, a power event, a hung thread.  It is
started detached, holds no window, and does the minimum that makes an abandoned
session harmless:

* in ``takeover`` mode, put the user's desktop back on screen (this is the failure
  that would actually be visible — a machine left showing an empty desktop);
* stop the worker;
* stop any shell that was started on the agent desktop;
* release the desktop handle, which destroys the desktop object.

It also enforces a hard ceiling on session length, so a forgotten session does not
live forever.
"""

from __future__ import annotations

import ctypes
import sys
import time

from ctypes import wintypes

from . import win32


def _desktop_shell_pids(desktop_name: str) -> list[int]:
    """PIDs of shell windows on the agent desktop (Progman / tray)."""

    try:
        hdesk = win32.open_desktop(desktop_name, win32.DESKTOP_ENUMERATE)
    except win32.Win32Error:
        return []

    pids: list[int] = []

    def callback(hwnd, _lparam):
        buffer = ctypes.create_unicode_buffer(256)
        win32.user32.GetClassNameW(hwnd, buffer, 256)
        if buffer.value in ("Progman", "Shell_TrayWnd", "WorkerW"):
            pid = wintypes.DWORD()
            win32.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                pids.append(int(pid.value))
        return True

    try:
        win32.user32.EnumDesktopWindows(hdesk, win32.WNDENUMPROC(callback), 0)
    finally:
        win32.close_desktop(hdesk)
    return sorted(set(pids))


def _kill(pid: int) -> None:
    if pid <= 0:
        return
    handle = win32.kernel32.OpenProcess(
        win32.PROCESS_TERMINATE | win32.SYNCHRONIZE, False, pid
    )
    if handle:
        win32.kernel32.TerminateProcess(handle, 1)
        win32.kernel32.CloseHandle(handle)


def clean_up(desktop_name: str, mode: str, worker_pid: int) -> dict:
    report = {"restored": False, "killed": [], "shell_pids": []}

    if mode == "takeover" and win32.active_input_desktop_name() == desktop_name:
        try:
            handle = win32.open_desktop("Default", win32.DESKTOP_SWITCHDESKTOP)
        except win32.Win32Error:
            handle = None
        if handle:
            report["restored"] = bool(win32.switch_desktop(handle))
            win32.close_desktop(handle)

    report["shell_pids"] = _desktop_shell_pids(desktop_name)
    for pid in [worker_pid, *report["shell_pids"]]:
        if pid and win32.process_alive(pid):
            _kill(pid)
            report["killed"].append(pid)

    # Opening and closing the desktop is not a no-op: it is what lets the last
    # handle drop once the daemon is gone, which is what destroys the desktop.
    try:
        handle = win32.open_desktop(desktop_name, win32.DESKTOP_READOBJECTS)
    except win32.Win32Error:
        handle = None
    if handle:
        win32.close_desktop(handle)
    return report


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    parser = argparse.ArgumentParser(prog="vdcu watchdog")
    parser.add_argument("--pid", type=int, required=True, help="daemon process id")
    parser.add_argument("--desktop", required=True)
    parser.add_argument("--mode", default="stealth", choices=("stealth", "takeover"))
    parser.add_argument("--worker", type=int, default=0)
    parser.add_argument("--parent-seconds", type=float, default=900.0)
    parser.add_argument("--poll", type=float, default=1.0)
    options = parser.parse_args(argv)

    deadline = time.time() + max(options.parent_seconds, 30.0)
    while time.time() < deadline:
        if not win32.process_alive(options.pid):
            break
        time.sleep(options.poll)
    else:
        # Hard ceiling reached: the daemon overstayed, so tear the session down.
        pass

    report = clean_up(options.desktop, options.mode, options.worker)
    report["reason"] = "daemon exited or session ceiling reached"
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())

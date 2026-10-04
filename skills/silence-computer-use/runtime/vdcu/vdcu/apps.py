"""Application catalogue and launching *onto a chosen desktop*.

``os.startfile`` is useless here: it hands the request to the shell, which lives
on the user's desktop.  Everything therefore resolves the Start-menu entry to a
real command line and calls ``CreateProcessW`` with ``STARTUPINFO.lpDesktop``, so
the new process is born on the hidden desktop and never appears on the user's.

The rule that matters most (learned the hard way, on this machine)
------------------------------------------------------------------
**Packaged apps are never activated through the shell.**  ``explorer.exe
shell:AppsFolder\\<AUMID>`` looks desktop-safe — the caller can even run on the
agent desktop — but the activation is dispatched to the *shell broker of the
session*, which places the app on the user's desktop.  That was observed live: a
Notepad appeared on the user's desktop and the screen flickered.  So an AUMID is
resolved to the package's real executable and started with ``CreateProcessW``
instead, and every launch is watched by :func:`_guard_against_leak`, which kills
the new process the moment a window of it shows up on the user's desktop.

Resolution rules
----------------
``.lnk``   parsed through ``WScript.Shell`` (one short PowerShell call per launch).
``.url``   an INI file; the ``URL=`` line is the target.
``.exe``   launched directly.
AUMID      resolved to the package's install-location executable.
"""

from __future__ import annotations

import os
import subprocess
import time

from . import win32

_HEADLESS = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_JOB = None


def session_job():
    """The job that every launched app joins, so the session can end them all.

    Created once per worker process.  Closing this handle (which happens when the
    worker exits, crashes or is killed) terminates every process assigned to it,
    descendants included — the reason it exists is that killing only the processes
    that own windows left a WinUI background process holding the agent desktop open.
    """

    global _JOB
    if _JOB is None:
        try:
            _JOB = win32.create_kill_on_close_job()
        except win32.Win32Error:
            _JOB = 0
    return _JOB or None


class AppError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _powershell(script: str, timeout: float = 30.0) -> str:
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, timeout=timeout, check=False, creationflags=_HEADLESS,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise AppError("CU_APP_LAUNCH_FAILED", f"powershell unavailable: {error}") from error
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()[:200]
        raise AppError("CU_APP_LAUNCH_FAILED", detail or "powershell failed")
    return completed.stdout.decode("utf-8", errors="replace")


def resolve_shortcut(path: str) -> dict:
    """Return ``{target, arguments, working_directory}`` for a ``.lnk`` file."""

    script = (
        "$ErrorActionPreference='Stop';"
        f"$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{path}');"
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Write-Output $s.TargetPath; Write-Output $s.Arguments; Write-Output $s.WorkingDirectory"
    )
    lines = _powershell(script).splitlines()
    while len(lines) < 3:
        lines.append("")
    return {"target": lines[0].strip(), "arguments": lines[1].strip(), "cwd": lines[2].strip()}


def resolve_url(path: str) -> str:
    for encoding in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            with open(path, "r", encoding=encoding) as handle:
                for line in handle:
                    if line.strip().lower().startswith("url="):
                        return line.split("=", 1)[1].strip()
        except (OSError, UnicodeError):
            continue
    raise AppError("CU_APP_LAUNCH_FAILED", f"{path!r} has no URL= entry")


def resolve_packaged_executable(app: dict) -> str:
    """Find the real ``.exe`` behind a packaged (AUMID) entry.

    Two traps, both hit while building this:

    * ``Get-AppxPackage -Name`` expects the bare package name, but a Start-menu
      AppID carries the *family* name (``Microsoft.WindowsNotepad_8wekyb3d8bbwe``),
      so the lookup matches on ``PackageFamilyName``;
    * the install directory cannot be enumerated — ``os.listdir`` under
      ``C:\\Program Files\\WindowsApps`` is denied for a normal user — so the
      executable name is read from the package manifest and the path is checked
      directly instead of being discovered by listing.

    Deliberately does *not* activate the app through the shell: see the module
    docstring.  A failure here is reported rather than worked around, because the
    only workaround is the activation path that leaks onto the user's screen.
    """

    app_id = str(app.get("target", ""))
    family = app_id.split("!")[0]
    if not family:
        raise AppError("CU_APP_UNKNOWN", f"{app.get('name')!r} has no package family name")

    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        f"Get-AppxPackage | Where-Object {{ $_.PackageFamilyName -eq '{family}' }} |"
        " ForEach-Object {"
        "   Write-Output ('DIR=' + $_.InstallLocation);"
        "   $m = Get-AppxPackageManifest -Package $_.PackageFullName;"
        "   foreach ($a in $m.Package.Applications.Application) {"
        "     Write-Output ('EXE=' + $a.Executable)"
        "   }"
        " }"
    )
    output = _powershell(script, timeout=90)

    location = ""
    executables: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("DIR="):
            location = line[4:].strip()
        elif line.startswith("EXE="):
            executables.append(line[4:].strip())

    if not location:
        raise AppError(
            "CU_APP_UNKNOWN",
            f"package {family!r} is not installed for this user, so {app.get('name')!r} "
            "cannot be started on the agent desktop",
        )

    tried: list[str] = []
    for executable in executables or []:
        candidate = os.path.join(location, executable.replace("/", os.sep))
        tried.append(candidate)
        if os.path.isfile(candidate):
            return candidate

    raise AppError(
        "CU_APP_LAUNCH_FAILED",
        f"the executable of {app.get('name')!r} could not be read from {location!r} "
        f"(tried {tried or ['no manifest entry']}); packaged apps whose files are not "
        "readable cannot be started on a hidden desktop",
    )


def command_for(app: dict) -> str:
    """Build the command line that starts ``app`` on a desktop."""

    kind, target = app.get("kind"), app.get("target", "")
    if kind == "aumid":
        return f'"{resolve_packaged_executable(app)}"'
    if kind == "shortcut" and target.lower().endswith(".lnk"):
        resolved = resolve_shortcut(target)
        if not resolved["target"]:
            raise AppError("CU_APP_LAUNCH_FAILED", f"{target!r} has no target")
        parts = [f'"{resolved["target"]}"']
        if resolved["arguments"]:
            parts.append(resolved["arguments"])
        return " ".join(parts)
    if kind == "shortcut" and target.lower().endswith(".url"):
        return f'rundll32.exe url.dll,FileProtocolHandler "{resolve_url(target)}"'
    return f'"{target}"'


#: Executables that only host something else (protocol handlers, interpreters).
#: Matching the reap rule on these would put unrelated system processes at risk,
#: so a launch that resolves to one of them reports no image instead.
_LAUNCHER_SHIMS = {
    "rundll32.exe", "cmd.exe", "powershell.exe", "pwsh.exe",
    "wscript.exe", "cscript.exe", "mshta.exe", "regsvr32.exe", "control.exe",
}


def _image_name(app: dict, command: str) -> str:
    """Basename of the executable this launch will run (empty when unknown)."""

    target = app.get("target", "")
    if app.get("kind") == "aumid":
        try:
            image = os.path.basename(resolve_packaged_executable(app))
        except AppError:
            return ""
        return "" if image.casefold() in _LAUNCHER_SHIMS else image
    if target.lower().endswith((".lnk", ".url")):
        # Resolved inside command_for; the command's first exe token is the image.
        for token in command.strip().split('"'):
            if token.lower().endswith(".exe"):
                image = os.path.basename(token)
                return "" if image.casefold() in _LAUNCHER_SHIMS else image
        return ""
    image = os.path.basename(target)
    return "" if image.casefold() in _LAUNCHER_SHIMS else image


def _desktop_windows_detail(desktop: str) -> dict[int, dict]:
    """``{pid: {title, visible, class}}`` for windows on ``desktop``."""

    return win32.desktop_windows_detail(desktop)


def _desktop_window_pids(desktop: str) -> dict[int, str]:
    """``{pid: window title}`` for windows on ``desktop``.

    Includes untitled and hidden windows: this is the raw sensor.  The guard below
    decides what a given sighting *means*; it must not decide it here.
    """

    return {
        pid: info["title"]
        for pid, info in _desktop_windows_detail(desktop).items()
    }


def _kill(pid: int) -> None:
    handle = win32.kernel32.OpenProcess(win32.PROCESS_TERMINATE | win32.SYNCHRONIZE, False, pid)
    if handle:
        win32.kernel32.TerminateProcess(handle, 1)
        win32.kernel32.CloseHandle(handle)


def launch(app: dict, desktop: str, *, hdesk=None, wait_window: float = 10.0):
    """Start ``app`` on ``desktop``, with a guard against leaking to the user.

    Returns ``{status, pid, window, method, command}``; ``status`` is ``ready`` when a
    new window appeared on the agent desktop, otherwise ``accepted_unverified``
    (not an error — some apps take longer than the window we wait).

    The guard, and why it is this narrow
    ------------------------------------
    An earlier version treated *any* process that newly appeared on the user's
    desktop during the wait as a leak and killed it.  That was wrong and dangerous:
    the sensor sees every top-level window, including hidden ones, so ordinary
    background activity — a tray window, an IME helper, anything at all — could be
    killed.  It did happen on this machine: a launch reported ``CU_APP_LEAKED`` and
    terminated three processes while the app itself was running correctly on the
    agent desktop.

    A process is now treated as *ours* only when it is the process we started or a
    descendant of it (parent chain), **and** it owns a visible, titled window on the
    user's desktop.  Everything else is reported as ``unrelated`` and left alone.
    """

    command = command_for(app)
    image = _image_name(app, command)
    before_agent = {(window["title"], window["hwnd"]) for window in win32.desktop_windows(hdesk)}
    before_user = set(_desktop_windows_detail("Default").keys())

    handle, pid = win32.launch_on_desktop(
        command, f"winsta0\\{desktop}", flags=win32.CREATE_NEW_PROCESS_GROUP, hide_window=False
    )
    # Join the session job before the handle is dropped, so the app and everything it
    # spawns are reaped with the session.
    win32.assign_to_job(session_job(), handle)
    win32.kernel32.CloseHandle(handle)

    stem = os.path.splitext(os.path.basename(app.get("target", "")))[0].casefold()
    unrelated: dict[int, str] = {}
    deadline = time.monotonic() + wait_window
    while time.monotonic() < deadline:
        for candidate, info in _desktop_windows_detail("Default").items():
            if candidate in before_user or candidate == os.getpid():
                continue
            if not info["visible"] or not info["title"]:
                continue  # hidden or untitled: ordinary background noise, not a window
            if not win32.is_descendant_of(candidate, pid):
                unrelated[candidate] = info["title"]
                continue
            _kill(candidate)
            raise AppError(
                "CU_APP_LEAKED",
                f"{app.get('name')!r} opened a window on the user's desktop "
                f"(pid {candidate}, {info['title']!r}); it was terminated immediately. "
                "Use another app, or takeover mode if this one cannot run on a hidden "
                "desktop.",
            )

        for window in win32.desktop_windows(hdesk):
            if (window["title"], window["hwnd"]) in before_agent:
                continue
            title = window["title"].casefold()
            if not stem or stem in title or str(window["pid"]) == str(pid):
                return {
                    "status": "ready",
                    "pid": pid,
                    "image": image,
                    "method": "create_process_on_desktop",
                    "command": command,
                    "window": {"title": window["title"], "hwnd": window["hwnd"], "pid": window["pid"]},
                    "unrelated_user_windows": unrelated,
                }
        time.sleep(0.25)

    return {
        "status": "accepted_unverified",
        "pid": pid,
        "image": image,
        "method": "create_process_on_desktop",
        "command": command,
        "window": None,
        "unrelated_user_windows": unrelated,
    }


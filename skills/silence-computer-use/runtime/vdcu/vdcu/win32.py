"""Win32 bindings for the virtual-desktop computer-use backend.

Everything here is :mod:`ctypes` against the standard library only: no pywin32,
no comtypes, no Pillow.

Two rules in this file are load-bearing on 64-bit Windows and are the reason it
exists as a single module rather than scattered ``ctypes.windll`` calls:

1. **Every handle-returning entry point declares ``restype``.**  The ctypes
   default is ``c_int``, which silently truncates a 64-bit HANDLE to 32 bits.
   A truncated handle dereferences as garbage: in the reference implementation
   that produced an access violation with no traceback at all.
2. **Every prototype that takes structured input declares ``argtypes``.**  A
   missing ``argtypes`` lets ctypes pass a Python int as a 32-bit value where
   the callee reads 64 bits.

Desktop-scoped DC rule (measured on Windows 11, 2026-10-01)
---------------------------------------------------------
``GetDC(NULL)`` returns the screen DC **of the desktop owned by the calling
thread**.  ``BitBlt`` between DCs of two different desktops fails, which is why
capture and input must run inside a process that lives on the target desktop —
see :mod:`vdcu.worker`.  This is not a documentation guess; the failure was
reproduced: a thread on ``Default`` could not BitBlt while a freshly created
desktop was the active one.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
from ctypes import wintypes

IS_WINDOWS = sys.platform == "win32"

# --------------------------------------------------------------------------- #
# Access masks and constants
# --------------------------------------------------------------------------- #

GENERIC_ALL = 0x10000000
DESKTOP_READOBJECTS = 0x0001
DESKTOP_CREATEWINDOW = 0x0002
DESKTOP_ENUMERATE = 0x0040
DESKTOP_WRITEOBJECTS = 0x0080
DESKTOP_SWITCHDESKTOP = 0x0100
DESKTOP_ALL_ACCESS = 0x01FF

WINSTA_ALL_ACCESS = 0x0000037F

UOI_NAME = 2
UOI_TYPE = 3

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79

SRCCOPY = 0x00CC0020
DIB_RGB_COLORS = 0
BI_RGB = 0

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_HWHEEL = 0x0100
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

CREATE_NO_WINDOW = 0x08000000
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_UNICODE_ENVIRONMENT = 0x00000400
STARTF_USESTDHANDLES = 0x00000100

SYNCHRONIZE = 0x00100000
PROCESS_TERMINATE = 0x0001
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
STILL_ACTIVE = 259
INFINITE = 0xFFFFFFFF

# A desktop that was created but never switched to composites to solid black.
# This threshold is used to tell "captured a real desktop" from "captured
# nothing" — see :func:`vdcu.png.non_blank_ratio`.
BLANK_RATIO_THRESHOLD = 0.02

# --------------------------------------------------------------------------- #
# Structures
# --------------------------------------------------------------------------- #


class MOUSEINPUT(ctypes.Structure):
    _fields_ = (
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.WPARAM),
    )


class KEYBDINPUT(ctypes.Structure):
    _fields_ = (
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", wintypes.WPARAM),
    )


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = (
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    )


class _INPUTUNION(ctypes.Union):
    _fields_ = (("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT))


class INPUT(ctypes.Structure):
    _fields_ = (("type", wintypes.DWORD), ("union", _INPUTUNION))


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = (
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    )


class BITMAPINFO(ctypes.Structure):
    _fields_ = (("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3))


class STARTUPINFOW(ctypes.Structure):
    _fields_ = (
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    )


class PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = (
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    )


# --------------------------------------------------------------------------- #
# Error plumbing
# --------------------------------------------------------------------------- #


class Win32Error(OSError):
    """An OSError carrying the failing call's name and its Win32 error code."""

    def __init__(self, call: str, code: int | None = None, detail: str = ""):
        self.call = call
        self.code = ctypes.get_last_error() if code is None else code
        message = ctypes.FormatError(self.code).strip() if self.code else "no error code"
        if detail:
            message = f"{message} ({detail})"
        super().__init__(f"{call} failed: [{self.code}] {message}")


def check(result, call: str, *, detail: str = ""):
    """Raise :class:`Win32Error` when a BOOL-returning Win32 call reports failure."""

    if not result:
        raise Win32Error(call, detail=detail)
    return result


if not IS_WINDOWS:  # pragma: no cover - the package is Windows-only by design
    raise ImportError(
        "vdcu.win32 is Windows-only: it drives Win32 desktop objects, GDI capture "
        "and SendInput."
    )

# --------------------------------------------------------------------------- #
# DLL handles and prototypes
# --------------------------------------------------------------------------- #

user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

HDESK = wintypes.HANDLE
HWINSTA = wintypes.HANDLE
HDC = wintypes.HDC
HBITMAP = wintypes.HBITMAP
HGLOBAL = wintypes.HGLOBAL

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
DESKTOPENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.LPWSTR, wintypes.LPARAM)

# --- desktop objects -------------------------------------------------------- #
user32.CreateDesktopW.restype = HDESK
user32.CreateDesktopW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_void_p,
    wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
]
user32.OpenDesktopW.restype = HDESK
user32.OpenDesktopW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD,
]
user32.OpenInputDesktop.restype = HDESK
user32.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
user32.CloseDesktop.restype = wintypes.BOOL
user32.CloseDesktop.argtypes = [HDESK]
user32.SwitchDesktop.restype = wintypes.BOOL
user32.SwitchDesktop.argtypes = [HDESK]
user32.GetThreadDesktop.restype = HDESK
user32.GetThreadDesktop.argtypes = [wintypes.DWORD]
user32.SetThreadDesktop.restype = wintypes.BOOL
user32.SetThreadDesktop.argtypes = [HDESK]
user32.GetProcessWindowStation.restype = HWINSTA
user32.GetProcessWindowStation.argtypes = []
user32.EnumDesktopsW.restype = wintypes.BOOL
user32.EnumDesktopsW.argtypes = [HWINSTA, DESKTOPENUMPROC, wintypes.LPARAM]
user32.GetUserObjectInformationW.restype = wintypes.BOOL
user32.GetUserObjectInformationW.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
]
user32.EnumDesktopWindows.restype = wintypes.BOOL
user32.EnumDesktopWindows.argtypes = [HDESK, WNDENUMPROC, wintypes.LPARAM]
user32.EnumWindows.restype = wintypes.BOOL
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
user32.EnumChildWindows.restype = wintypes.BOOL
user32.EnumChildWindows.argtypes = [wintypes.HWND, WNDENUMPROC, wintypes.LPARAM]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]

# Window inspection and message plumbing, declared here as well as in the stealth
# executor: a caller that never imports the stealth module (the takeover executor)
# would otherwise hit 64-bit truncation through the default ``c_int`` restype.
user32.GetWindow.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetTopWindow.restype = wintypes.HWND
user32.GetTopWindow.argtypes = [wintypes.HWND]
user32.WindowFromPoint.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [ctypes.c_void_p]
user32.ChildWindowFromPointEx.restype = wintypes.HWND
user32.ChildWindowFromPointEx.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.UINT]
user32.ScreenToClient.restype = wintypes.BOOL
user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]

# --- screen capture --------------------------------------------------------- #
user32.GetSystemMetrics.restype = ctypes.c_int
user32.GetSystemMetrics.argtypes = [ctypes.c_int]
user32.GetDC.restype = HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.restype = ctypes.c_int
user32.ReleaseDC.argtypes = [wintypes.HWND, HDC]
gdi32.CreateCompatibleDC.restype = HDC
gdi32.CreateCompatibleDC.argtypes = [HDC]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [HDC]
gdi32.CreateCompatibleBitmap.restype = HBITMAP
gdi32.CreateCompatibleBitmap.argtypes = [HDC, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.BitBlt.restype = wintypes.BOOL
gdi32.BitBlt.argtypes = [
    HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD,
]
gdi32.GetDIBits.restype = ctypes.c_int
gdi32.GetDIBits.argtypes = [
    HDC, HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
    ctypes.POINTER(BITMAPINFO), wintypes.UINT,
]

# --- input ------------------------------------------------------------------ #
user32.SendInput.restype = wintypes.UINT
user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
user32.VkKeyScanW.restype = ctypes.c_short
user32.VkKeyScanW.argtypes = [ctypes.c_wchar]
user32.GetKeyState.restype = ctypes.c_short
user32.GetKeyState.argtypes = [ctypes.c_int]

# --- windows ---------------------------------------------------------------- #
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetForegroundWindow.argtypes = []

# --- clipboard -------------------------------------------------------------- #
user32.OpenClipboard.restype = wintypes.BOOL
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.CloseClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.GetClipboardData.restype = wintypes.HANDLE
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]

# --- kernel / processes ----------------------------------------------------- #
kernel32.GetCurrentProcessId.restype = wintypes.DWORD
kernel32.GetCurrentProcessId.argtypes = []
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.GetCurrentThreadId.argtypes = []
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.WaitForSingleObject.restype = wintypes.DWORD
kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
kernel32.GetExitCodeProcess.restype = wintypes.BOOL
kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel32.TerminateProcess.restype = wintypes.BOOL
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel32.CreateProcessW.restype = wintypes.BOOL
kernel32.CreateProcessW.argtypes = [
    wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
    wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
    ctypes.c_void_p, ctypes.c_void_p,
]
kernel32.GlobalAlloc.restype = HGLOBAL
kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalLock.argtypes = [HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalUnlock.argtypes = [HGLOBAL]
kernel32.GlobalFree.restype = HGLOBAL
kernel32.GlobalFree.argtypes = [HGLOBAL]


# --------------------------------------------------------------------------- #
# Desktop objects
# --------------------------------------------------------------------------- #


def active_input_desktop_name() -> str:
    """Return the name of the desktop currently receiving user input."""

    hdesk = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if not hdesk:
        return ""
    try:
        return object_name(hdesk)
    finally:
        user32.CloseDesktop(hdesk)


def object_name(handle) -> str:
    """Return the Win32 name of a desktop or window-station handle."""

    needed = wintypes.DWORD()
    user32.GetUserObjectInformationW(handle, UOI_NAME, None, 0, ctypes.byref(needed))
    if needed.value <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(max(needed.value, 2))
    ok = user32.GetUserObjectInformationW(
        handle, UOI_NAME, buffer, ctypes.sizeof(buffer), ctypes.byref(needed)
    )
    return buffer.value if ok else ""


def enum_desktops() -> list[str]:
    """List every desktop in this process's window station."""

    names: list[str] = []

    def callback(name, _lparam):
        if name:
            names.append(name)
        return True

    station = user32.GetProcessWindowStation()
    user32.EnumDesktopsW(station, DESKTOPENUMPROC(callback), 0)
    return names


def open_desktop(name: str, access: int = DESKTOP_ALL_ACCESS):
    """Open an existing desktop, or raise :class:`Win32Error`."""

    handle = user32.OpenDesktopW(name, 0, False, access)
    if not handle:
        raise Win32Error(f"OpenDesktopW({name!r})")
    return handle


def create_desktop(name: str, access: int = GENERIC_ALL):
    """Create a new desktop object and return its handle."""

    handle = user32.CreateDesktopW(name, None, None, 0, access, None)
    if not handle:
        raise Win32Error(f"CreateDesktopW({name!r})")
    return handle


def close_desktop(handle) -> bool:
    if not handle:
        return False
    return bool(user32.CloseDesktop(handle))


def switch_desktop(handle) -> bool:
    """Make ``handle`` the visible, input-receiving desktop."""

    return bool(user32.SwitchDesktop(handle))


def thread_desktop() -> object:
    return user32.GetThreadDesktop(kernel32.GetCurrentThreadId())


def set_thread_desktop(handle) -> bool:
    return bool(user32.SetThreadDesktop(handle))


# --------------------------------------------------------------------------- #
# Screen geometry and capture
# --------------------------------------------------------------------------- #


def virtual_screen() -> tuple[int, int, int, int]:
    """Return ``(left, top, width, height)`` of the calling desktop's screen."""

    left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    width = user32.GetSystemMetrics(SM_CXVIRTUALSCREEN)
    height = user32.GetSystemMetrics(SM_CYVIRTUALSCREEN)
    if width <= 0 or height <= 0:
        width = user32.GetSystemMetrics(0) or 1920
        height = user32.GetSystemMetrics(1) or 1080
        left, top = 0, 0
    return left, top, width, height


def capture_bgra() -> tuple[bytes, int, int]:
    """BitBlt the calling thread's desktop into a BGRA buffer.

    Raises :class:`Win32Error` when the DC belongs to a desktop that is not the
    active one — the measured cross-desktop failure described in the module
    docstring.  Callers turn that into a ``VD_DESKTOP_NOT_ACTIVE`` error rather
    than a crash, because it is a recoverable session state.
    """

    left, top, width, height = virtual_screen()
    screen_dc = user32.GetDC(None)
    if not screen_dc:
        raise Win32Error("GetDC(NULL)")

    memory_dc = gdi32.CreateCompatibleDC(screen_dc)
    if not memory_dc:
        user32.ReleaseDC(None, screen_dc)
        raise Win32Error("CreateCompatibleDC")

    bitmap = gdi32.CreateCompatibleBitmap(screen_dc, width, height)
    if not bitmap:
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(None, screen_dc)
        raise Win32Error("CreateCompatibleBitmap")

    try:
        if not gdi32.SelectObject(memory_dc, bitmap):
            raise Win32Error("SelectObject")
        if not gdi32.BitBlt(memory_dc, 0, 0, width, height, screen_dc, left, top, SRCCOPY):
            raise Win32Error(
                "BitBlt",
                detail=(
                    "the calling thread's desktop is not the active desktop; "
                    "capture must run inside a process launched onto the target desktop"
                ),
            )

        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # top-down rows, matching PNG order
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB

        buffer = ctypes.create_string_buffer(width * 4 * height)
        copied = gdi32.GetDIBits(
            memory_dc, bitmap, 0, height, buffer, ctypes.byref(info), DIB_RGB_COLORS
        )
        if copied != height:
            raise Win32Error("GetDIBits", detail=f"{copied} of {height} scanlines")
        return buffer.raw, width, height
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(None, screen_dc)


# --------------------------------------------------------------------------- #
# Input synthesis
# --------------------------------------------------------------------------- #


def mouse_input(flags: int, dx: int = 0, dy: int = 0, data: int = 0) -> INPUT:
    return INPUT(
        type=INPUT_MOUSE,
        union=_INPUTUNION(
            mi=MOUSEINPUT(dx=dx, dy=dy, mouseData=data, dwFlags=flags, time=0, dwExtraInfo=0)
        ),
    )


def key_input(vk: int, up: bool = False, unicode_unit: int | None = None, extended: bool = False) -> INPUT:
    flags = KEYEVENTF_KEYUP if up else 0
    if unicode_unit is not None:
        return INPUT(
            type=INPUT_KEYBOARD,
            union=_INPUTUNION(
                ki=KEYBDINPUT(
                    wVk=0, wScan=unicode_unit, dwFlags=flags | KEYEVENTF_UNICODE,
                    time=0, dwExtraInfo=0,
                )
            ),
        )
    if extended:
        flags |= KEYEVENTF_EXTENDEDKEY
    return INPUT(
        type=INPUT_KEYBOARD,
        union=_INPUTUNION(
            ki=KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags, time=0, dwExtraInfo=0)
        ),
    )


def send_inputs(inputs: list[INPUT]) -> int:
    """Deliver a batch of synthesized events; returns how many were accepted."""

    if not inputs:
        return 0
    array = (INPUT * len(inputs))(*inputs)
    return int(user32.SendInput(len(inputs), array, ctypes.sizeof(INPUT)))


# --------------------------------------------------------------------------- #
# Clipboard (per window station: shared by every desktop in winsta0)
# --------------------------------------------------------------------------- #

_CLIPBOARD_RETRIES = 12
_CLIPBOARD_RETRY_DELAY = 0.05


def open_clipboard() -> None:
    for _ in range(_CLIPBOARD_RETRIES):
        if user32.OpenClipboard(None):
            return
        time.sleep(_CLIPBOARD_RETRY_DELAY)
    raise Win32Error("OpenClipboard")


def close_clipboard() -> None:
    user32.CloseClipboard()


def clipboard_text() -> str:
    open_clipboard()
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return ""
        try:
            return ctypes.c_wchar_p(pointer).value or ""
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        close_clipboard()


def set_clipboard_text(text: str) -> None:
    payload = text.encode("utf-16-le") + b"\x00\x00"
    open_clipboard()
    try:
        if not user32.EmptyClipboard():
            raise Win32Error("EmptyClipboard")
        handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(payload))
        if not handle:
            raise Win32Error("GlobalAlloc")
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            kernel32.GlobalFree(handle)
            raise Win32Error("GlobalLock")
        try:
            ctypes.memmove(pointer, payload, len(payload))
        finally:
            kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(CF_UNICODETEXT, handle):
            kernel32.GlobalFree(handle)
            raise Win32Error("SetClipboardData")
    finally:
        close_clipboard()


# --------------------------------------------------------------------------- #
# Windows on a desktop
# --------------------------------------------------------------------------- #


def desktop_windows(hdesk=None) -> list[dict]:
    """Return visible, titled top-level windows of ``hdesk`` (this desktop if None)."""

    windows: list[dict] = []

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        if not buffer.value:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        windows.append({"title": buffer.value, "pid": int(pid.value), "hwnd": int(hwnd)})
        return True

    if hdesk:
        user32.EnumDesktopWindows(hdesk, WNDENUMPROC(callback), 0)
    else:
        user32.EnumWindows(WNDENUMPROC(callback), 0)
    return windows


def foreground_window_title() -> str:
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    length = user32.GetWindowTextLengthW(hwnd)
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buffer, length + 1)
    return buffer.value


# --------------------------------------------------------------------------- #
# Processes (launching onto a named desktop)
# --------------------------------------------------------------------------- #


def launch_on_desktop(
    command: str,
    desktop: str | None = None,
    *,
    flags: int = 0,
    env: dict | None = None,
    cwd: str | None = None,
    hide_window: bool = True,
    stdin=None,
    stdout=None,
    stderr=None,
) -> tuple[object, int]:
    """Create a process, optionally bound to ``desktop`` (``"winsta0\\Name"``).

    Returns ``(hProcess, pid)``.  The caller owns the process handle.
    """

    si = STARTUPINFOW()
    si.cb = ctypes.sizeof(STARTUPINFOW)
    if desktop:
        si.lpDesktop = desktop
    si.dwFlags |= STARTF_USESTDHANDLES
    si.hStdInput = _as_handle(stdin)
    si.hStdOutput = _as_handle(stdout)
    si.hStdError = _as_handle(stderr)
    if hide_window:
        si.dwFlags |= 0x00000001  # STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE

    environment = None
    if env is not None:
        blocks = []
        for key, value in env.items():
            blocks.append(f"{key}={value}")
        environment = ctypes.create_unicode_buffer("\x00".join(blocks) + "\x00\x00")
        flags |= CREATE_UNICODE_ENVIRONMENT

    pi = PROCESS_INFORMATION()
    ok = kernel32.CreateProcessW(
        None,
        ctypes.create_unicode_buffer(command),
        None,
        None,
        bool(stdin or stdout or stderr),
        flags,
        ctypes.cast(environment, ctypes.c_void_p) if environment else None,
        cwd,
        ctypes.byref(si),
        ctypes.byref(pi),
    )
    if not ok:
        raise Win32Error(f"CreateProcessW({command!r})")
    kernel32.CloseHandle(pi.hThread)
    return pi.hProcess, int(pi.dwProcessId)


def _as_handle(stream):
    if stream is None:
        return None
    if isinstance(stream, int):
        return wintypes.HANDLE(stream)
    try:
        import msvcrt

        return wintypes.HANDLE(msvcrt.get_osfhandle(stream.fileno()))
    except Exception:  # noqa: BLE001 - fall back to no inherited handle
        return None


def process_alive(pid: int) -> bool:
    """True while ``pid`` is still running."""

    handle = kernel32.OpenProcess(SYNCHRONIZE | PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        code = wintypes.DWORD()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return False
    finally:
        kernel32.CloseHandle(handle)


def terminate_process(handle) -> None:
    if handle:
        kernel32.TerminateProcess(handle, 1)
        kernel32.CloseHandle(handle)


# --------------------------------------------------------------------------- #
# Process identity: used by the leak guard, which must never kill a process it
# cannot prove belongs to the app it just launched
# --------------------------------------------------------------------------- #

kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
]

_ntdll = ctypes.WinDLL("ntdll")


class PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = (
        ("Reserved1", ctypes.c_void_p),
        ("PebBaseAddress", ctypes.c_void_p),
        ("Reserved2", ctypes.c_void_p * 2),
        ("UniqueProcessId", ctypes.c_void_p),
        ("InheritedFromUniqueProcessId", ctypes.c_void_p),
    )


_ntdll.NtQueryInformationProcess.restype = ctypes.c_long
_ntdll.NtQueryInformationProcess.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG, ctypes.POINTER(wintypes.ULONG),
]


def process_image_name(pid: int) -> str:
    """Full image path of a running process, or "" when it cannot be read."""

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(32768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
        return ""
    finally:
        kernel32.CloseHandle(handle)


def process_parent(pid: int) -> int:
    """Parent process id, or 0 when it cannot be read."""

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return 0
    try:
        info = PROCESS_BASIC_INFORMATION()
        needed = wintypes.ULONG()
        status = _ntdll.NtQueryInformationProcess(
            handle, 0, ctypes.byref(info), ctypes.sizeof(info), ctypes.byref(needed)
        )
        if status != 0:
            return 0
        return int(info.InheritedFromUniqueProcessId or 0)
    finally:
        kernel32.CloseHandle(handle)


def is_descendant_of(pid: int, ancestor: int, *, max_depth: int = 8) -> bool:
    """True when ``pid`` is ``ancestor`` itself or a descendant of it."""

    if not pid or not ancestor:
        return False
    seen: set[int] = set()
    current = int(pid)
    for _ in range(max_depth):
        if current == int(ancestor):
            return True
        if current in seen:
            return False
        seen.add(current)
        current = process_parent(current)
        if not current:
            return False
    return False


def z_order_handles() -> list[int]:
    """Top-level windows of the calling thread's desktop, topmost first.

    The composite painter needs the real z-order: ``EnumDesktopWindows`` enumerates
    the desktop's windows but does **not** promise z-order, and painting in its order
    let the wallpaper window cover the applications and the taskbar (observed: a
    capture showing only the desktop with the launched app missing).  ``GetTopWindow``
    + ``GW_HWNDNEXT`` is the documented z-order walk.
    """

    GW_HWNDNEXT = 2
    handles: list[int] = []
    seen: set[int] = set()
    hwnd = user32.GetTopWindow(None)
    while hwnd:
        value = int(hwnd)
        if value in seen:
            break
        seen.add(value)
        handles.append(value)
        hwnd = user32.GetWindow(hwnd, GW_HWNDNEXT)
    return handles


def focused_window() -> int:
    """The focused control of the calling thread's desktop, or 0.

    ``GetGUIThreadInfo`` works even on a hidden desktop (unlike ``WindowFromPoint``),
    and it is how message-level copy finds the control to send ``WM_COPY`` to.
    """

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = (
            ("cbSize", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("hwndActive", wintypes.HWND),
            ("hwndFocus", wintypes.HWND),
            ("hwndCapture", wintypes.HWND),
            ("hwndMenuOwner", wintypes.HWND),
            ("hwndMoveSize", wintypes.HWND),
            ("hwndCaret", wintypes.HWND),
            ("rcCaret", wintypes.RECT),
        )

    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(GUITHREADINFO)
    if not user32.GetGUIThreadInfo(0, ctypes.byref(info)):
        return 0
    return int(info.hwndFocus or info.hwndActive or 0)


user32.GetGUIThreadInfo.restype = wintypes.BOOL
user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.c_void_p]


def descendants(hwnd: int) -> list[int]:
    """Every descendant window of ``hwnd`` (``EnumChildWindows`` covers the subtree).

    The ``GetWindow``-chain walk in the stealth executor lists one level at a time;
    this is the enumeration the worker's ``children`` op uses, so control lookups
    and window listings agree on what is inside a window.
    """

    found: list[int] = []

    def callback(child, _lparam):
        found.append(int(child))
        return True

    user32.EnumChildWindows(wintypes.HWND(hwnd), WNDENUMPROC(callback), 0)
    return found


def desktop_windows_detail(desktop: str) -> dict[int, dict]:
    """``{pid: {title, visible, class}}`` for every window on ``desktop``.

    Used to tell "a window of ours" from "somebody else's window that happens to be
    on the agent desktop".  Third-party tray software really does put windows there —
    a FortiTray helper appeared on every agent desktop on this machine — so the
    distinction decides what may be killed and what must be left alone.

    A process that owns no window is invisible to this: that is how an empty desktop
    object survives every window-based cleanup.  :func:`threads_on_desktop` finds
    those.
    """

    try:
        hdesk = open_desktop(desktop, DESKTOP_ENUMERATE)
    except Win32Error:
        return {}

    found: dict[int, dict] = {}

    def callback(hwnd, _lparam):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if not owner.value:
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length > 0:
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            title = buffer.value
        class_buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buffer, 256)
        entry = found.setdefault(
            int(owner.value), {"title": "", "visible": False, "class": class_buffer.value}
        )
        if title and not entry["title"]:
            entry["title"] = title
        entry["visible"] = entry["visible"] or bool(user32.IsWindowVisible(hwnd))
        return True

    try:
        user32.EnumDesktopWindows(hdesk, WNDENUMPROC(callback), 0)
    finally:
        close_desktop(hdesk)
    return found


# --- thread-level desktop attachment --------------------------------------- #

TH32CS_SNAPTHREAD = 0x00000004


class THREADENTRY32(ctypes.Structure):
    _fields_ = (
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ThreadID", wintypes.DWORD),
        ("th32OwnerProcessID", wintypes.DWORD),
        ("tpBasePri", ctypes.c_long),
        ("tpDeltaPri", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
    )


kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.Thread32First.restype = wintypes.BOOL
kernel32.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]
kernel32.Thread32Next.restype = wintypes.BOOL
kernel32.Thread32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(THREADENTRY32)]


def threads_on_desktop(desktop: str) -> list[dict]:
    """Every thread attached to ``desktop``, with its owning process.

    The answer to "what is holding this desktop alive?" when window enumeration says
    nothing: ``GetThreadDesktop`` reports the real attachment, so a windowless
    process can no longer hide.  Read-only; the desktop handles returned here are
    owned by the system and must not be closed.
    """

    snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return []
    found: list[dict] = []
    try:
        entry = THREADENTRY32()
        entry.dwSize = ctypes.sizeof(THREADENTRY32)
        ok = bool(kernel32.Thread32First(snapshot, ctypes.byref(entry)))
        seen: set[tuple[int, int]] = set()
        while ok:
            tid = int(entry.th32ThreadID)
            pid = int(entry.th32OwnerProcessID)
            ok = bool(kernel32.Thread32Next(snapshot, ctypes.byref(entry)))
            if (tid, pid) in seen:
                continue
            seen.add((tid, pid))
            try:
                hdesk = user32.GetThreadDesktop(tid)
            except Exception:  # noqa: BLE001
                continue
            if not hdesk:
                continue
            if object_name(hdesk) == desktop:
                found.append(
                    {
                        "tid": tid,
                        "pid": pid,
                        "image": os.path.basename(process_image_name(pid) or "?"),
                    }
                )
    finally:
        kernel32.CloseHandle(snapshot)
    return found


def python_executable() -> str:
    """The interpreter that should host worker/guard processes."""

    executable = sys.executable or "python"
    if os.path.basename(executable).lower() == "pythonw.exe":
        return executable
    candidate = os.path.join(os.path.dirname(executable), "pythonw.exe")
    return candidate if os.path.exists(candidate) else executable


# --------------------------------------------------------------------------- #
# DPI awareness
# --------------------------------------------------------------------------- #

DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
PROCESS_PER_MONITOR_DPI_AWARE = 2


def enable_dpi_awareness() -> str:
    """Make this process per-monitor DPI aware; returns which API worked.

    Without this, Windows virtualizes the process: on a 1920×1080 display at 125%
    scaling, ``GetSystemMetrics`` reports 1536×864 and ``PrintWindow`` renders at that
    size, so every capture is a resampled, softer image than the screen really is —
    which is exactly how a desktop capture came out looking "low resolution and
    blurry" next to a native one.
    """

    try:
        user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(
            ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
        ):
            return "per-monitor-v2"
    except (AttributeError, OSError):
        pass

    try:
        shcore = ctypes.WinDLL("shcore")
        shcore.SetProcessDpiAwareness.restype = ctypes.c_long
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        if shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE) == 0:
            return "per-monitor"
    except (AttributeError, OSError):
        pass

    try:
        if user32.SetProcessDPIAware():
            return "system"
    except (AttributeError, OSError):
        pass
    return "none"


# --------------------------------------------------------------------------- #
# Job objects: kill everything a session started, including grandchildren
# --------------------------------------------------------------------------- #

JobObjectExtendedLimitInformation = 9
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000


class IO_COUNTERS(ctypes.Structure):
    _fields_ = (
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    )


class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = (
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.POINTER(ctypes.c_ulong)),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    )


class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = (
        ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    )


kernel32.CreateJobObjectW.restype = wintypes.HANDLE
kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
kernel32.SetInformationJobObject.restype = wintypes.BOOL
kernel32.SetInformationJobObject.argtypes = [
    wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
]
kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]


def create_kill_on_close_job():
    """A job object whose processes die when the job handle closes.

    This is the only reliable way to end a session cleanly: a WinUI or packaged app
    leaves background processes with no window of their own, and a desktop object
    survives as long as *any* process is attached to it.  Killing window owners was
    not enough — a leftover app kept the agent desktop alive after the session ended.
    """

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        raise Win32Error("CreateJobObjectW")
    info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not kernel32.SetInformationJobObject(
        job, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
    ):
        kernel32.CloseHandle(job)
        raise Win32Error("SetInformationJobObject")
    return job


def assign_to_job(job, process_handle) -> bool:
    """Best-effort: a process already in a non-nestable job cannot be moved."""

    if not job or not process_handle:
        return False
    return bool(kernel32.AssignProcessToJobObject(job, process_handle))

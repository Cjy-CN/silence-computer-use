"""Shadow-session host and worker-protocol server.

Uses the bundled signed interpreter and SDK without modifying binaries. A
message-pumping UI thread owns the render surface. Windows verifies a local
empty credential before deciding whether an authorization card is necessary.
Passwords are passed only in memory, never persisted by this host.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
import sys
import threading
import time
from ctypes import wintypes

HERE = os.path.dirname(os.path.abspath(__file__))
PACKAGE_ROOT = os.path.dirname(HERE)
sys.path.insert(0, PACKAGE_ROOT)

from vdcu.authorization import authorize_session, probe_empty_credential  # noqa: E402
from vdcu import png  # noqa: E402
from vdcu import win32 as win32mod  # noqa: E402
from vdcu.shadow_engine import (  # noqa: E402
    PipSdk,
    find_app_dir,
    find_cursor_path,
    runtime_dirs,
)

ole32 = ctypes.WinDLL("ole32")
user32 = ctypes.WinDLL("user32")

gdi32 = ctypes.WinDLL("gdi32")

# GDI bindings for the styled authorization card; declared explicitly because the
# default ctypes prototypes truncate 64-bit handles.
gdi32.CreateFontW.restype = wintypes.HANDLE
gdi32.CreateFontW.argtypes = [
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPCWSTR,
]
gdi32.CreateRoundRectRgn.restype = wintypes.HRGN
gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
user32.SetWindowRgn.restype = wintypes.BOOL
user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]

gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
gdi32.CreateSolidBrush.argtypes = [wintypes.COLORREF]
gdi32.SetTextColor.restype = wintypes.COLORREF
gdi32.SetTextColor.argtypes = [wintypes.HDC, wintypes.COLORREF]
gdi32.SetBkMode.restype = ctypes.c_int
gdi32.SetBkMode.argtypes = [wintypes.HDC, ctypes.c_int]
user32.SetWindowPos.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
LRESULT = ctypes.c_ssize_t
# wintypes.WPARAM/LPARAM are 32-bit even on Win64 — pointers (HDC/HWND) arriving
# in wparam/lparam get truncated; use the true 64-bit sizes everywhere here.
WPARAM64 = ctypes.c_size_t
LPARAM64 = ctypes.c_ssize_t

user32.SetFocus.restype = wintypes.HWND
user32.SetFocus.argtypes = [wintypes.HWND]
user32.GetWindowTextW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.SetWindowTextW.restype = wintypes.BOOL
user32.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
user32.SendMessageW.restype = ctypes.c_ssize_t
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, WPARAM64, LPARAM64]
kernel32 = ctypes.WinDLL("kernel32")

COINIT_APARTMENTTHREADED = 0x2
ole32.CoInitializeEx.restype = ctypes.c_long
ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]

WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, WPARAM64, LPARAM64)
WNDENUMPROC_CHILD = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

# input shield (pip form is view-only, like the floating preview): SDK children
# are subclassed to answer WM_NCHITTEST with HTCAPTION so clicks drag the window
# instead of ever reaching the session.  original procs kept for restore.
_SHIELD_WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t)
_SHIELD_PROCS: dict = {}  # hwnd -> (original proc pointer, shield callback)


# --- window styles & constants (restored after a cleanup script over-deleted) ---
WS_BORDER, ES_PASSWORD, ES_AUTOHSCROLL, BS_DEFPUSHBUTTON, SS_LEFT = (
    0x00800000, 0x0020, 0x0080, 0x00000001, 0)
WS_EX_TOPMOST = 0x8
WS_EX_TOOLWINDOW = 0x00000080
WS_OVERLAPPED, WS_OVERLAPPEDWINDOW = 0x00000000, 0x00CF0000
WS_VISIBLE, WS_CHILD, WS_TABSTOP = 0x10000000, 0x40000000, 0x00010000
WS_POPUP = 0x80000000
SW_SHOW = 5
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SWP_NOSIZE, SWP_NOMOVE, SWP_SHOWWINDOW, SWP_FRAMECHANGED = 0x0001, 0x0002, 0x0040, 0x0020
WM_COMMAND, WM_KEYDOWN, VK_RETURN = 0x0111, 0x0100, 0x0D
WM_NCHITTEST = 0x0084
ID_OK, ID_EDIT = 101, 102

# WndProcs must outlive their windows (DispatchMessageW into freed memory is how
# an earlier probe crashed); this list is the lifetime.
_KEEPALIVE: list = []

# The container window has TWO forms of the SAME SDK-rendered surface, exactly
# The render surface has two views:
#   pip — small borderless view-only window, bottom-right (not topmost: the user
#         wants no forced topmost; capsule buttons ⤢ / ✕)
#   big — standard titled window 「virtual desktop」 with the view inset on a canvas
PIP = (480, 270)                    # logical client size in pip form (16:9 exact)
BIG_CLIENT = (1008, 567)            # logical client size in big form (16:9 exact)
BIG_VIEW = (44, 70, 920, 517)       # SDK view inset inside the big client
DISPLAY = (1920, 1080)
CANVAS = 0x00FAFAFA                 # COLORREF #FAFAFA — the big form's margin canvas
PIP_CAPSULE = 0x00404040            # COLORREF #404040 — the capsule buttons (dark, measured from Shadow's pip)
GWL_STYLE, GWL_EXSTYLE = -16, -20
GWLP_WNDPROC = -4


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.UINT), ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HICON),
    )


for _name, _restype, _argtypes in (
    ("CreateWindowExW", wintypes.HWND, [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]),
    ("DefWindowProcW", LRESULT, [wintypes.HWND, wintypes.UINT, WPARAM64, LPARAM64]),
    ("PeekMessageW", wintypes.BOOL, [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]),
    ("TranslateMessage", wintypes.BOOL, [ctypes.c_void_p]),
    ("DispatchMessageW", LRESULT, [ctypes.c_void_p]),
    ("ShowWindow", wintypes.BOOL, [wintypes.HWND, ctypes.c_int]),
    ("DestroyWindow", wintypes.BOOL, [wintypes.HWND]),
    ("GetWindowTextW", ctypes.c_int, [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]),
    ("SetFocus", wintypes.HWND, [wintypes.HWND]),
    ("GetSysColorBrush", wintypes.HBRUSH, [ctypes.c_int]),
    ("SetWindowPos", wintypes.BOOL, [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                     ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]),
):
    fn = getattr(user32, _name)
    fn.restype = _restype
    fn.argtypes = _argtypes


def cursor_centroid(before: bytes, after: bytes) -> tuple[int, int] | None:
    """Where the engine's virtual cursor moved, as a pixel position in the frame.

    The engine composites a virtual cursor onto every snapshot ("overlay virtual
    cursor onto snapshot png"), so the centroid of the pixels that changed between
    two frames is where the pointer went.  This is how the mouse coordinate scale
    gets *measured* instead of assumed.
    """

    try:
        wa, ha, rgb_a = png.decode(before)
        wb, hb, rgb_b = png.decode(after)
    except Exception:  # noqa: BLE001
        return None
    if (wa, ha) != (wb, hb):
        return None
    total_x = total_y = count = 0
    row = wa * 3
    for y in range(0, ha, 2):
        base = y * row
        for x in range(0, row, 6):
            if rgb_a[base + x : base + x + 3] != rgb_b[base + x : base + x + 3]:
                total_x += x // 3
                total_y += y
                count += 1
    if count < 4:
        return None
    return total_x // count, total_y // count


def _register_and_window(class_name: str, title: str, exstyle: int, style: int,
                         x: int, y: int, width: int, height: int, proc,
                         bg_brush: int | None = None, class_style: int = 0,
                         show: bool = True) -> int:
    instance = kernel32.GetModuleHandleW(None)
    wproc = WNDPROC(proc)
    _KEEPALIVE.append(wproc)
    wc = WNDCLASSEXW()
    wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
    wc.style = class_style
    wc.lpfnWndProc = wproc
    wc.hInstance = instance
    wc.lpszClassName = class_name
    wc.hbrBackground = bg_brush or user32.GetSysColorBrush(1)
    user32.RegisterClassExW(ctypes.byref(wc))
    # outer size from the wanted *client* size (the view must land at its inset)
    wanted = wintypes.RECT(0, 0, width, height)
    user32.AdjustWindowRectEx(ctypes.byref(wanted), style & ~WS_VISIBLE, False, exstyle)
    hwnd = user32.CreateWindowExW(
        exstyle, class_name, title, style, x, y,
        wanted.right - wanted.left, wanted.bottom - wanted.top,
        None, None, instance, None,
    )
    if show:
        user32.ShowWindow(hwnd, SW_SHOW)
    return int(hwnd)


WM_CTLCOLORSTATIC = 0x0138
WM_CTLCOLOREDIT = 0x0133
WM_ERASEBKGND = 0x0014
WM_SETFONT = 0x0030
WM_COMMAND = 0x0111
STN_CLICKED = 0
SS_NOTIFY = 0x0100
SS_CENTER = 0x00000001
FW_NORMAL = 400
FW_SEMIBOLD = 600
ANTIALIASED_QUALITY = 5

# Neutral authorization-card palette.
ACCENT = 0x00F07010          # COLORREF 0x00BBGGRR: #1070f0
ACCENT_DEEP = 0x00C9570B     # #0b57c9 hover
HINT_GREY = 0x008A8A8A
TEXT_BLACK = 0x00000000


def _make_font(height: int, weight: int = FW_NORMAL):
    return gdi32.CreateFontW(
        -height, 0, 0, 0, weight, 0, 0, 0, 0, 0, 0, ANTIALIASED_QUALITY, 0, "Segoe UI"
    )


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = (
        ("hdc", wintypes.HDC), ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT), ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL), ("rgbReserved", ctypes.c_byte * 32),
    )


class DRAWITEMSTRUCT(ctypes.Structure):
    _fields_ = (
        ("CtlType", wintypes.UINT), ("CtlID", wintypes.UINT),
        ("itemID", wintypes.UINT), ("itemAction", wintypes.UINT),
        ("itemState", wintypes.UINT), ("hwndItem", wintypes.HWND),
        ("hDC", wintypes.HDC), ("rcItem", wintypes.RECT),
        ("itemData", ctypes.c_size_t),
    )


WM_DRAWITEM = 0x002B
WM_PAINT = 0x000F
WM_CTLCOLOREDIT = WM_CTLCOLOREDIT  # already defined above; keep the name local


def _dpi_scale() -> float:
    """Physical pixels per logical unit after DPI awareness is enabled."""
    user32.GetDpiForSystem.restype = wintypes.UINT
    return max(1.0, user32.GetDpiForSystem() / 96.0)


for _name, _restype, _argtypes in (
    ("BeginPaint", wintypes.HDC, [wintypes.HWND, ctypes.c_void_p]),
    ("EndPaint", wintypes.BOOL, [wintypes.HWND, ctypes.c_void_p]),
    ("DrawTextW", ctypes.c_int, [wintypes.HDC, wintypes.LPCWSTR, ctypes.c_int, ctypes.c_void_p, wintypes.UINT]),
    ("GetFocus", wintypes.HWND, []),
    ("GetClientRect", wintypes.BOOL, [wintypes.HWND, ctypes.c_void_p]),
    ("FillRect", ctypes.c_int, [wintypes.HDC, ctypes.c_void_p, wintypes.HBRUSH]),
    ("GetCursorPos", wintypes.BOOL, [ctypes.c_void_p]),
    ("SetTimer", ctypes.c_size_t, [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]),
    ("KillTimer", wintypes.BOOL, [wintypes.HWND, ctypes.c_size_t]),
    ("CallWindowProcW", ctypes.c_ssize_t,
     [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t]),
    ("GetWindowLongPtrW", ctypes.c_ssize_t, [wintypes.HWND, ctypes.c_int]),
    ("SetWindowLongPtrW", ctypes.c_ssize_t,
     [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]),
):
    fn = getattr(user32, _name)
    fn.restype = _restype
    fn.argtypes = _argtypes

for _name, _restype, _argtypes in (
    ("RoundRect", wintypes.BOOL, [wintypes.HDC] + [ctypes.c_int] * 6),
    ("CreatePen", wintypes.HPEN, [ctypes.c_int, ctypes.c_int, wintypes.COLORREF]),
    ("GetStockObject", wintypes.HGDIOBJ, [ctypes.c_int]),
    ("SelectObject", wintypes.HGDIOBJ, [wintypes.HDC, wintypes.HGDIOBJ]),
    ("DeleteObject", wintypes.BOOL, [wintypes.HGDIOBJ]),
    ("SetTextColor", wintypes.COLORREF, [wintypes.HDC, wintypes.COLORREF]),
    ("SetBkMode", ctypes.c_int, [wintypes.HDC, ctypes.c_int]),
    ("CreateSolidBrush", wintypes.HBRUSH, [wintypes.COLORREF]),
):
    fn = getattr(gdi32, _name)
    fn.restype = _restype
    fn.argtypes = _argtypes


class PasswordDialog:
    """DPI-aware local authorization card with a password field and cancel control."""

    CARD_W, CARD_H, CARD_R = 520, 268, 16

    def __init__(self):
        self.scale = _dpi_scale()
        s = self.scale
        self.hwnd = 0
        self.edit = 0
        self.label = 0
        self.hint = 0
        self.button = 0
        self.close_btn = 0
        self.submitted = False
        self.cancelled = False
        self.brush = gdi32.CreateSolidBrush(0x00FCFCFC)   # Shadow modal card bg
        self.brush_white = gdi32.CreateSolidBrush(0x00FFFFFF)
        self.brush_accent = gdi32.CreateSolidBrush(ACCENT)
        self.brush_accent_deep = gdi32.CreateSolidBrush(ACCENT_DEEP)
        self.font_title = _make_font(round(20 * s), FW_SEMIBOLD)
        self.font_body = _make_font(round(13 * s))
        self.font_small = _make_font(round(11 * s))
        self.font_edit = _make_font(round(14 * s))

    # geometry helpers (logical → physical)
    def _s(self, v: float) -> int:
        return round(v * self.scale)

    def _wndproc(self, hwnd, message, wparam, lparam):
        if message == WM_COMMAND and (wparam & 0xFFFF) in (ID_OK, 205):
            self.submitted = True
            return 0
        if message == WM_COMMAND and (wparam & 0xFFFF) == 206:
            self.cancelled = True  # the ✕ — abort the enter cleanly
            return 0
        if message == WM_KEYDOWN and wparam == VK_RETURN:
            self.submitted = True
            return 0
        if message == WM_DRAWITEM:
            self._draw_button(lparam)
            return 1
        if message == WM_PAINT:
            self._paint_card(hwnd)
            return 0
        if message == WM_NCHITTEST:
            # borderless card: drag it by anywhere that is not the ✕
            point = int(lparam)
            x, y = point & 0xFFFF, (point >> 16) & 0xFFFF
            if x >= 0x8000:
                x -= 0x10000
            if y >= 0x8000:
                y -= 0x10000
            rect = wintypes.RECT()
            user32.GetWindowRect(wintypes.HWND(self.hwnd), ctypes.byref(rect))
            if rect.left + self._s(self.CARD_W - 56) <= x <= rect.left + self._s(self.CARD_W - 8) \
                    and rect.top + self._s(6) <= y <= rect.top + self._s(52):
                return 1  # HTCLIENT: let the ✕ take its own click
            return 2      # HTCAPTION: drag the card
        if message == WM_ERASEBKGND:
            # fill the card ourselves — returning 1 without painting leaves the
            # background black in off-screen renders (PrintWindow) and undefined
            # on screen
            rect = wintypes.RECT()
            user32.GetClientRect(wintypes.HWND(self.hwnd), ctypes.byref(rect))
            user32.FillRect(wintypes.HDC(wparam), ctypes.byref(rect), self.brush)
            return 1
        if message in (WM_CTLCOLORSTATIC, WM_CTLCOLOREDIT):
            control = int(lparam)  # LPARAM64: arrives as a plain int now
            gdi32.SetBkMode(wparam, 1)  # TRANSPARENT
            if self.hint and control == self.hint:
                gdi32.SetTextColor(wparam, HINT_GREY)
                return self.brush
            if self.close_btn and control == self.close_btn:
                gdi32.SetTextColor(wparam, HINT_GREY)
                return self.brush
            if message == WM_CTLCOLOREDIT:
                return self.brush_white
            gdi32.SetTextColor(wparam, TEXT_BLACK)
            return self.brush
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    def _paint_card(self, hwnd) -> None:
        """Rounded outline behind the (borderless) edit — Shadow's input field."""
        ps = PAINTSTRUCT()
        hdc = user32.BeginPaint(wintypes.HWND(self.hwnd), ctypes.byref(ps))
        if not hdc:
            return
        pen = gdi32.CreatePen(0, max(1, self._s(1)), 0x00D9D9D9)
        old_pen = gdi32.SelectObject(hdc, pen)
        old_brush = gdi32.SelectObject(hdc, self.brush_white)
        r = self._s(self.CARD_R // 2 + 2)
        gdi32.RoundRect(hdc, self._s(28), self._s(130),
                        self._s(28 + 464), self._s(130 + 40), r, r)
        gdi32.SelectObject(hdc, old_brush)
        gdi32.SelectObject(hdc, old_pen)
        gdi32.DeleteObject(pen)
        user32.EndPaint(wintypes.HWND(self.hwnd), ctypes.byref(ps))

    def _draw_button(self, lparam) -> None:
        """Owner-drawn accent button: rounded, blue, white centred text."""
        dis = DRAWITEMSTRUCT.from_address(lparam)
        if dis.CtlType != 4:  # ODT_BUTTON
            return
        hdc = dis.hDC
        pressed = bool(dis.itemState & 0x0001)  # ODS_SELECTED
        gdi32.SelectObject(hdc, self.brush_accent_deep if pressed else self.brush_accent)
        gdi32.SelectObject(hdc, gdi32.GetStockObject(8))  # NULL_PEN — no outline
        r = self._s(10)
        gdi32.RoundRect(hdc, dis.rcItem.left, dis.rcItem.top,
                        dis.rcItem.right, dis.rcItem.bottom, r, r)
        gdi32.SetBkMode(hdc, 1)
        gdi32.SetTextColor(hdc, 0x00FFFFFF)
        gdi32.SelectObject(hdc, self.font_body)
        user32.DrawTextW(hdc, "进入虚拟桌面", -1, ctypes.byref(dis.rcItem), 0x25)

    def create(self) -> int:
        s = self._s
        self.hwnd = _register_and_window(
            "VdcuPwdHost", "virtual desktop", WS_EX_TOPMOST,
            # WS_POPUP = no system chrome at all; CS_DROPSHADOW floats the card
            # on the soft shadow Shadow modals have
            WS_POPUP | WS_VISIBLE, 520, 260, s(self.CARD_W), s(self.CARD_H),
            self._wndproc, bg_brush=self.brush, class_style=0x00020000,
        )
        # rounded corners: the window region is the card shape (Shadow cards r≈16)
        gdi32.CreateRoundRectRgn.restype = wintypes.HRGN
        gdi32.CreateRoundRectRgn.argtypes = [ctypes.c_int] * 6
        user32.SetWindowRgn.restype = wintypes.BOOL
        user32.SetWindowRgn.argtypes = [wintypes.HWND, wintypes.HRGN, wintypes.BOOL]
        region = gdi32.CreateRoundRectRgn(0, 0, s(self.CARD_W), s(self.CARD_H),
                                          s(self.CARD_R * 2), s(self.CARD_R * 2))
        user32.SetWindowRgn(wintypes.HWND(self.hwnd), region, True)

        instance = kernel32.GetModuleHandleW(None)
        hwnd = wintypes.HWND(self.hwnd)

        def static(text, x, y, w, h, font, control_id, *, center=False, notify=False):
            style = WS_CHILD | WS_VISIBLE | (SS_CENTER if center else SS_LEFT)
            if notify:
                style |= SS_NOTIFY
            handle = int(user32.CreateWindowExW(
                0, "STATIC", text, style,
                s(x), s(y), s(w), s(h), hwnd, wintypes.HMENU(control_id),
                wintypes.HINSTANCE(instance), None))
            user32.SendMessageW(wintypes.HANDLE(handle), WM_SETFONT, wintypes.WPARAM(font), 1)
            return handle

        # ✕ close, top-right — every Shadow modal has it
        self.close_btn = static("✕", self.CARD_W - 46, 12, 32, 32,
                                self.font_body, 206, center=True, notify=True)
        static("virtual desktop", 28, 20, 400, 30, self.font_title, 201)
        static("Windows 授权 · 在独立桌面中运行应用",
               28, 58, 464, 22, self.font_body, 202)
        self.label = static("输入 Windows 账户密码（空密码可直接回车）：", 28, 102, 464, 22, self.font_body, 103)

        # borderless edit over the rounded field drawn in WM_PAINT
        self.edit = int(user32.CreateWindowExW(
            0, "EDIT", "", WS_CHILD | WS_VISIBLE | WS_TABSTOP | ES_PASSWORD | ES_AUTOHSCROLL,
            s(40), s(138), s(440), s(24), hwnd, wintypes.HMENU(ID_EDIT),
            wintypes.HINSTANCE(instance), None))
        user32.SendMessageW(wintypes.HANDLE(self.edit), WM_SETFONT, wintypes.WPARAM(self.font_edit), 1)

        # owner-drawn rounded accent button (BS_OWNERDRAW → WM_DRAWITEM)
        self.button = int(user32.CreateWindowExW(
            0, "BUTTON", "", WS_CHILD | WS_VISIBLE | WS_TABSTOP | 0x000B,
            s(28), s(186), s(464), s(42), hwnd, wintypes.HMENU(205),
            wintypes.HINSTANCE(instance), None))

        self.hint = static("密码仅用于本次 Windows 授权，不保存、不写入日志",
                           28, 240, 464, 20, self.font_small, 204, center=True)

        user32.SetWindowPos(hwnd, wintypes.HWND(HWND_TOPMOST), 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW)
        user32.SetFocus(wintypes.HWND(self.edit))
        return self.hwnd

    def set_message(self, text: str) -> None:
        """Update the instruction line — how a wrong password becomes visible.

        Before this, a rejected password changed nothing on screen: the box sat
        there as if it had not received the input at all.
        """

        if self.label:
            user32.SetWindowTextW(wintypes.HWND(self.label), text)

    def clear_input(self) -> None:
        if self.edit:
            user32.SetWindowTextW(wintypes.HWND(self.edit), "")
            user32.SetFocus(wintypes.HWND(self.edit))
        self.submitted = False

    def read_password(self) -> str:
        buffer = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(wintypes.HWND(self.edit), buffer, 512)
        return buffer.value

    def close(self) -> None:
        if self.hwnd:
            user32.DestroyWindow(wintypes.HWND(self.hwnd))
            self.hwnd = 0


class UiThread(threading.Thread):
    """STA thread owning both windows; pumps until told to stop."""

    def __init__(self, *, show_authorization: bool = True):
        super().__init__(daemon=True)
        self.show_authorization = show_authorization
        self.ready = threading.Event()
        self.stop_pumping = threading.Event()
        self.dialog = PasswordDialog()
        self.host_hwnd = 0
        self.view_mode = "pip"          # "pip" (floating) | "big" (titled window)
        self.view_resize_hook = None    # set by ShadowHost: re-fit the SDK view
        self._btn_expand = 0
        self._btn_hide = 0
        self._tracking_hover = False
        self._buttons_visible = False
        self._capsule_brush = 0

    def run(self) -> None:
        ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
        if self.show_authorization:
            self.dialog.create()
        self.canvas_brush = gdi32.CreateSolidBrush(CANVAS)
        self._capsule_brush = gdi32.CreateSolidBrush(PIP_CAPSULE)
        s = self.dialog.scale
        pw, ph = round(PIP[0] * s), round(PIP[1] * s)
        user32.GetSystemMetrics.restype = ctypes.c_int
        user32.GetSystemMetrics.argtypes = [ctypes.c_int]
        screen_w, screen_h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        self.host_hwnd = _register_and_window(
            "VdcuShadowView", "virtual desktop",
            # no WS_EX_TOPMOST: the pip's stacking follows the agent tool window
            # (user requirement), which the watcher in _host_proc maintains
            WS_EX_TOOLWINDOW,
            # The floating preview: a small borderless pure-content view parked
            # bottom-right — and it IS the SDK's render surface (not a polling
            # screenshot, which is what made the old small window laggy and blurry).
            # Hidden until the session exists; the big form is the same window grown.
            WS_POPUP, screen_w - pw - round(24 * s), screen_h - ph - round(64 * s),
            pw, ph, self._host_proc, bg_brush=self.canvas_brush, show=False,
        )
        # rounded corners: the region clips the SDK's child windows too
        region = gdi32.CreateRoundRectRgn(0, 0, pw, ph, round(16 * s), round(16 * s))
        user32.SetWindowRgn(wintypes.HWND(self.host_hwnd), region, True)
        self.ready.set()

        class MSG(ctypes.Structure):
            _fields_ = (
                ("hwnd", wintypes.HWND),
                ("message", wintypes.UINT),
                ("wParam", WPARAM64),
                ("lParam", LPARAM64),
                ("time", wintypes.DWORD),
                ("pt", wintypes.POINT),
            )

        user32.IsDialogMessageW.restype = wintypes.BOOL
        user32.IsDialogMessageW.argtypes = [wintypes.HWND, ctypes.c_void_p]

        while not self.stop_pumping.is_set():
            message = MSG()
            while user32.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                if getattr(self, "_pump_trace", False):
                    print(f"ui| pump msg=0x{int(message.message):04x} w={int(message.wParam)}", flush=True)
                # IsDialogMessage turns Enter into the default button's click.  Without
                # it the EDIT control swallows VK_RETURN and the password box appeared
                # to ignore someone pressing Enter after typing — measured, that is how
                # a real password entry once went nowhere.
                if (self.dialog.hwnd and message.message == WM_KEYDOWN
                        and message.wParam == VK_RETURN):
                    focused = user32.GetFocus()
                    if focused and int(focused) == self.dialog.edit:
                        self.dialog.submitted = True
                        continue
                if self.dialog.hwnd and user32.IsDialogMessageW(
                    wintypes.HWND(self.dialog.hwnd), ctypes.byref(message)
                ):
                    continue
                user32.TranslateMessage(ctypes.byref(message))
                user32.DispatchMessageW(ctypes.byref(message))
            time.sleep(0.02)

    # Cross-thread window management.  DestroyWindow/CreateWindowEx from the main
    # thread target windows owned by *this* UI thread: DestroyWindow simply fails
    # (that is why the password card stayed up after the password was in), and a
    # retry-card created off-thread would never get its messages pumped.  Route the
    # operations through the host window's proc, which runs on this thread.
    WM_VDCU_CLOSE = 0x8000 + 1   # close the password card
    WM_VDCU_SHOW = 0x8000 + 2    # (re)create it for a retry, with pending_message
    WM_VDCU_DESTROY = 0x8000 + 3 # shutdown: close card + destroy the host window
    WM_VDCU_SHOW_HOST = 0x8000 + 4  # session is up: reveal the preview window
    WM_CLOSE = 0x0010

    def _host_proc(self, hwnd, message, wparam, lparam):
        if message in (0x0111, self.WM_CLOSE, self.WM_VDCU_SHOW_HOST, self.WM_VDCU_DESTROY):
            print(f"ui| msg=0x{message:04x} w={int(wparam)} mode={self.view_mode}", flush=True)
        if message == self.WM_CLOSE:
            # Measured: destroying this window kills the SDK's RDP view with it
            # (every snapshot afterwards fails — a zombie session).  Shadow's big
            # window closes back to the floating one; the floating one closes to
            # hidden (the session keeps running; reopen via show_view / MCP).
            if self.view_mode == "big":
                self._set_mode("pip")
            else:
                user32.ShowWindow(wintypes.HWND(self.host_hwnd), 0)  # SW_HIDE
            return 0
        if message == WM_COMMAND and (wparam & 0xFFFF) == 210:  # ⤢
            self._set_mode("big" if self.view_mode == "pip" else "pip")
            return 0
        if message == WM_COMMAND and (wparam & 0xFFFF) == 211:  # ✕
            # inline (we are already on the UI thread) — posting a message to a
            # window of this same thread proved lossy for no clear reason
            if self.view_mode == "big":
                self._set_mode("pip")
            else:
                user32.ShowWindow(wintypes.HWND(self.host_hwnd), 0)  # SW_HIDE
            return 0
        if message == WM_NCHITTEST and self.view_mode == "pip":
            # borderless: drag anywhere except the capsule buttons;
            # a few px from the edges resizes (user asked for a resizable pip)
            point = int(lparam)
            x, y = point & 0xFFFF, (point >> 16) & 0xFFFF
            if x >= 0x8000:
                x -= 0x10000
            if y >= 0x8000:
                y -= 0x10000
            if self._point_on_button(x, y):
                return 1  # HTCLIENT
            rect = wintypes.RECT()
            user32.GetWindowRect(wintypes.HWND(self.host_hwnd), ctypes.byref(rect))
            s = self.dialog.scale
            edge = round(6 * s)
            near_left = x - rect.left < edge
            near_right = rect.right - x < edge
            near_top = y - rect.top < edge
            near_bottom = rect.bottom - y < edge
            if near_left and near_top:
                return 13   # HTTOPLEFT
            if near_right and near_top:
                return 14   # HTTOPRIGHT
            if near_left and near_bottom:
                return 16   # HTBOTTOMLEFT
            if near_right and near_bottom:
                return 17   # HTBOTTOMRIGHT
            if near_left:
                return 10   # HTLEFT
            if near_right:
                return 11   # HTRIGHT
            if near_top:
                return 12   # HTTOP
            if near_bottom:
                return 15   # HTBOTTOM
            return 2        # HTCAPTION — drag
        if message == 0x0214 and self.host_hwnd:
            # WM_SIZING: lock the aspect to 16:9 (user: 不许拉伸、不许白边)
            rect = ctypes.cast(lparam, ctypes.POINTER(wintypes.RECT)).contents
            frame_w, frame_h = self._frame_overhead()
            client_w = rect.right - rect.left - frame_w
            client_h = rect.bottom - rect.top - frame_h
            if client_w * 9 > client_h * 16:
                client_w = client_h * 16 // 9
            else:
                client_h = client_w * 9 // 16
            rect.right = rect.left + client_w + frame_w
            rect.bottom = rect.top + client_h + frame_h
            return 1  # TRUE: we adjusted the rect
        if message == 0x0005:  # WM_SIZE — keep the SDK view fitted (live resize)
            if self.view_mode == "pip":
                self._apply_input_shield(True)  # late SDK children get shielded too
                self._reposition_buttons()
            if self.view_resize_hook:
                try:
                    self.view_resize_hook()
                except Exception:  # noqa: BLE001
                    pass
            return user32.DefWindowProcW(hwnd, message, wparam, lparam)
        if message in (WM_CTLCOLORSTATIC,) and self.view_mode == "pip":
            control = int(lparam)
            if control in (self._btn_expand, self._btn_hide):
                gdi32.SetBkMode(wparam, 1)
                gdi32.SetTextColor(wparam, 0x00FFFFFF)  # white on the dark capsule
                return self._capsule_brush
        if message == self.WM_VDCU_CLOSE:
            print(f"ui| closing password card (hwnd={self.dialog.hwnd})", flush=True)
            self.dialog.close()
            print(f"ui| card closed (hwnd={self.dialog.hwnd})", flush=True)
            return 0
        if message == self.WM_VDCU_SHOW:
            print("ui| recreating card for retry", flush=True)
            self.dialog.create()
            # fresh card = fresh state: the previous submit flag must not make the
            # main loop read an empty password box immediately
            self.dialog.clear_input()
            if getattr(self, "pending_message", ""):
                self.dialog.set_message(self.pending_message)
            return 0
        if message == self.WM_VDCU_DESTROY:
            self.dialog.close()
            if self.host_hwnd:
                user32.DestroyWindow(wintypes.HWND(self.host_hwnd))
                self.host_hwnd = 0
            return 0
        if message == self.WM_VDCU_SHOW_HOST:
            if self.host_hwnd:
                if self.view_mode == "big":
                    self._round_sdk_view()
                else:
                    # pip = 纯观看（不能操作，用户要求与运行时一致）：SDK 子窗全部
                    # 屏蔽输入；胶囊按钮常驻（用户：必须一直能放大/关闭）
                    self._apply_input_shield(True)
                    self._set_buttons_visible(True)
                user32.ShowWindow(wintypes.HWND(self.host_hwnd), SW_SHOW)
            return 0
        return user32.DefWindowProcW(hwnd, message, wparam, lparam)

    # -- the two forms of the same view ---------------------------------- #

    def client_view_rect(self) -> tuple[int, int, int, int]:
        """Where the SDK should render: the full client area.  The window is
        aspect-locked to 16:9 by WM_SIZING, so the content always fills it
        exactly — no stretch, no letterbox bars."""
        rect = wintypes.RECT()
        user32.GetClientRect(wintypes.HWND(self.host_hwnd), ctypes.byref(rect))
        return (0, 0, rect.right, rect.bottom)

    def _set_mode(self, mode: str) -> None:
        if mode == self.view_mode or not self.host_hwnd:
            return
        t0 = time.time()
        def _tick(what):
            print(f"ui| mode->{mode} {what} +{time.time() - t0:.2f}s", flush=True)
        hwnd = wintypes.HWND(self.host_hwnd)
        s = self.dialog.scale
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
        if mode == "big":
            user32.SetWindowLongPtrW(hwnd, GWL_STYLE, WS_OVERLAPPEDWINDOW | WS_VISIBLE)
            _tick("style")
            ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
            user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE,
                                     (ex & ~WS_EX_TOOLWINDOW) & ~WS_EX_TOPMOST)
            user32.SetWindowRgn(hwnd, None, True)
            wanted = wintypes.RECT(0, 0, round(BIG_CLIENT[0] * s), round(BIG_CLIENT[1] * s))
            user32.AdjustWindowRectEx(ctypes.byref(wanted), WS_OVERLAPPEDWINDOW, False, 0)
            user32.SetWindowPos(hwnd, wintypes.HWND(HWND_NOTOPMOST), 60, 40,
                                wanted.right - wanted.left, wanted.bottom - wanted.top,
                                SWP_FRAMECHANGED | SWP_SHOWWINDOW)
            _tick("frame")
            self.view_mode = "big"
            self._set_buttons_visible(False)
            _tick("buttons")
            self._apply_input_shield(False)  # big form is operable again
            _tick("shield-off")
            self._round_sdk_view()
            _tick("round")
        else:
            pw, ph = round(PIP[0] * s), round(PIP[1] * s)
            user32.SetWindowLongPtrW(hwnd, GWL_STYLE, WS_POPUP | WS_VISIBLE)
            ex = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
            # no topmost: the pip's stacking follows the agent tool window
            user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE,
                                     (ex | WS_EX_TOOLWINDOW) & ~WS_EX_TOPMOST)
            screen_w, screen_h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
            region = gdi32.CreateRoundRectRgn(0, 0, pw, ph, round(16 * s), round(16 * s))
            user32.SetWindowRgn(hwnd, region, True)
            user32.SetWindowPos(hwnd, wintypes.HWND(0),
                                screen_w - pw - round(24 * s), screen_h - ph - round(64 * s),
                                pw, ph, SWP_FRAMECHANGED | SWP_SHOWWINDOW | 0x0010)
            _tick("frame")
            self.view_mode = "pip"
            self._reposition_buttons()
            self._apply_input_shield(True)   # pip form is view-only
            _tick("shield-on")
            self._set_buttons_visible(True)  # capsule stays on (always usable)
            _tick("buttons")
        if self.view_resize_hook:
            try:
                self.view_resize_hook()
            except Exception:  # noqa: BLE001
                pass

    def _frame_overhead(self) -> tuple[int, int]:
        """(width, height) the window frame adds beyond the client area."""
        if self.view_mode != "big":
            return (0, 0)  # pip is borderless — window == client
        wr = wintypes.RECT()
        cr = wintypes.RECT()
        user32.GetWindowRect(wintypes.HWND(self.host_hwnd), ctypes.byref(wr))
        user32.GetClientRect(wintypes.HWND(self.host_hwnd), ctypes.byref(cr))
        return (wr.right - wr.left - cr.right, wr.bottom - wr.top - cr.bottom)

    # -- hover capsule (⧉ / ✕), Shadow's measured pip controls ------------ #

    def _ensure_buttons(self) -> None:
        if self._btn_expand or not self.host_hwnd:
            return
        instance = kernel32.GetModuleHandleW(None)
        hwnd = wintypes.HWND(self.host_hwnd)
        s = self.dialog.scale
        font = _make_font(round(11 * s))
        for label, control_id in (("⧉", 210), ("✕", 211)):
            handle = int(user32.CreateWindowExW(
                0, "STATIC", label, WS_CHILD | SS_CENTER | SS_NOTIFY,
                0, 0, round(22 * s), round(16 * s), hwnd, wintypes.HMENU(control_id),
                wintypes.HINSTANCE(instance), None))
            user32.SendMessageW(wintypes.HANDLE(handle), WM_SETFONT, wintypes.WPARAM(font), 1)
            # 压过 SDK 渲染面（它在子窗 z 序最上面，按钮必须再上一层）
            user32.SetWindowPos(wintypes.HWND(handle), wintypes.HWND(0), 0, 0, 0, 0,
                                0x0001 | 0x0002)  # HWND_TOP + NOSIZE+NOMOVE
            if control_id == 210:
                self._btn_expand = handle
            else:
                self._btn_hide = handle
        self._reposition_buttons()

    def _reposition_buttons(self) -> None:
        if not (self._btn_expand and self.host_hwnd):
            return
        rect = wintypes.RECT()
        user32.GetClientRect(wintypes.HWND(self.host_hwnd), ctypes.byref(rect))
        s = self.dialog.scale
        # 移到右上角 + 压到最上层（SDK 渲染面会盖住它们）
        flags = 0x0001 | 0x0040  # SWP_NOSIZE | SWP_SHOWWINDOW
        user32.SetWindowPos(wintypes.HWND(self._btn_hide), wintypes.HWND(0),
                            rect.right - round(28 * s), round(8 * s), 0, 0, flags)
        user32.SetWindowPos(wintypes.HWND(self._btn_expand), wintypes.HWND(0),
                            rect.right - round(54 * s), round(8 * s), 0, 0, flags)

    def _set_buttons_visible(self, visible: bool) -> None:
        if visible == self._buttons_visible:
            return
        if visible and self.view_mode == "pip":
            self._ensure_buttons()
        self._buttons_visible = visible
        command = 5 if visible else 0  # SW_SHOW / SW_HIDE
        for handle in (self._btn_expand, self._btn_hide):
            if handle:
                user32.ShowWindow(wintypes.HWND(handle), command)

    def _point_on_button(self, x: int, y: int) -> bool:
        for handle in (self._btn_expand, self._btn_hide):
            if not handle:
                continue
            rect = wintypes.RECT()
            user32.GetWindowRect(wintypes.HWND(handle), ctypes.byref(rect))
            if rect.left <= x <= rect.right and rect.top <= y <= rect.bottom:
                return True
        return False

    def _apply_input_shield(self, on: bool) -> None:
        """pip = 纯观看（用户要求与运行时一致，不能操作）：SDK 的渲染子窗全部
        子类化为 WM_NCHITTEST→HTCAPTION，点击变成拖动、永远不会进到会话里；
        切回大窗时还原原窗口过程（大窗可操作）。"""
        hwnd = wintypes.HWND(self.host_hwnd)
        ours = {self._btn_expand, self._btn_hide}
        children = []

        def cb(h, p):
            value = int(h)
            if value not in ours and value:
                children.append(value)
            return True

        user32.EnumChildWindows(hwnd, WNDENUMPROC_CHILD(cb), 0)
        for h in children:
            if on and h not in _SHIELD_PROCS:
                orig = user32.GetWindowLongPtrW(wintypes.HWND(h), GWLP_WNDPROC)

                def make(orig_proc):
                    def proc(h_, m, w, l):
                        if m == 0x0084:  # WM_NCHITTEST → caption: drag, never input
                            return 2
                        return user32.CallWindowProcW(
                            orig_proc, wintypes.HWND(int(h_)), m, w, l)
                    return proc

                shield = _SHIELD_WNDPROC(make(orig))
                _SHIELD_PROCS[h] = (orig, shield)  # keep the callback alive
                user32.SetWindowLongPtrW(
                    wintypes.HWND(h), GWLP_WNDPROC,
                    ctypes.cast(shield, ctypes.c_void_p).value)
            elif not on and h in _SHIELD_PROCS:
                orig, shield = _SHIELD_PROCS.pop(h)
                user32.SetWindowLongPtrW(wintypes.HWND(h), GWLP_WNDPROC, orig)
                del shield

    def _round_sdk_view(self) -> None:
        """Round the SDK's view child (Shadow's content card has r≈8 corners).

        The SDK creates its own child window inside our container to render the
        session into; a window region on that child gives the rounded card look.
        If it paints directly into our client area instead, there is nothing to
        round — logged, not guessed.
        """
        children = []
        def cb(h, p):
            rect = wintypes.RECT()
            user32.GetWindowRect(h, ctypes.byref(rect))
            cls = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(h, cls, 128)
            children.append((h, cls.value, rect.right - rect.left, rect.bottom - rect.top))
            return True
        user32.EnumChildWindows(wintypes.HWND(self.host_hwnd), WNDENUMPROC_CHILD(cb), 0)
        if not children:
            print("ui| no SDK child window to round (SDK paints into the client area)", flush=True)
            return
        biggest = max(children, key=lambda c: c[2] * c[3])
        print(f"ui| SDK view children: {[(c[1], c[2], c[3]) for c in children]}", flush=True)
        region = gdi32.CreateRoundRectRgn(0, 0, biggest[2], biggest[3], 16, 16)
        user32.SetWindowRgn(wintypes.HWND(biggest[0]), region, True)

    def close_dialog(self) -> None:
        """Close the password card *on the UI thread* (SendMessage = synchronous)."""
        if self.host_hwnd and not self.stop_pumping.is_set():
            user32.SendMessageW(wintypes.HWND(self.host_hwnd), self.WM_VDCU_CLOSE, 0, 0)

    def show_dialog(self, message: str) -> None:
        """Recreate the password card for a retry, on the UI thread."""
        self.pending_message = message
        if self.host_hwnd and not self.stop_pumping.is_set():
            user32.SendMessageW(wintypes.HWND(self.host_hwnd), self.WM_VDCU_SHOW, 0, 0)

    def show_host(self) -> None:
        """Reveal the preview window once the session exists (Shadow's order:
        authorization first, live view after)."""
        if self.host_hwnd and not self.stop_pumping.is_set():
            user32.SendMessageW(wintypes.HWND(self.host_hwnd), self.WM_VDCU_SHOW_HOST, 0, 0)

    def shutdown(self) -> None:
        if self.host_hwnd and not self.stop_pumping.is_set():
            # synchronous: the window must be gone when this returns, otherwise
            # the pump stopping first can take the destroy message with it
            user32.SendMessageW(wintypes.HWND(self.host_hwnd), self.WM_VDCU_DESTROY, 0, 0)
        self.stop_pumping.set()
        time.sleep(0.1)


class ShadowHost:
    """Worker-protocol server backed by the vendor engine."""

    def __init__(self, ui: UiThread, sdk: PipSdk, runtime: dict, display: tuple[int, int]):
        self.ui = ui
        self.sdk = sdk
        self.runtime = runtime
        self.display = display
        self.session_guid = ""
        self.started = time.time()
        self.lock = threading.Lock()
        # "pixels": 0-1000 maps onto the session resolution (1920x1080);
        # "unit":   0-1000 maps onto a 0.0-1.0 range.  Measured at session start.
        # The engine's mouse coordinates are normalized 0-1 doubles.  That is not an
        # assumption: the SDK's own validator rejects pixel values and accepts
        # fractions — observed in its log, same run, same call:
        #     SendMouseMoveEvent(x=0.3, y=0.3)  → success
        #     SendMouseMoveEvent(x=576, y=324) → "invalid coord: x=576, y=324"
        #     SendMouseClickEvent(x=960, y=540) → "invalid coord: x=960, y=540"
        # ``calibrate()`` below verifies the mapping end to end; it no longer decides it.
        self.coord_scale = "unit"
        self.calibration: dict = {}
        self.actions: list[dict] = []
        self.guard = ObserveGuard()
        # window resize / pip<->big switch re-fits the SDK render area
        self.ui.view_resize_hook = self.fit_view

    # -- coordinate scale ---------------------------------------------- #

    def wait_for_render(self, timeout: float = 45.0) -> dict:
        """Wait until the session actually paints something.

        Observed: snapshots right after creation are an empty small frame at the
        view-rect size (1067x600 in the vendor's client, 600x338 in our pip
        window), and once the RDP session is up they become the painted desktop
        at display resolution (1920x1080).  Calibrating or clicking against the
        empty frame measures nothing — one real run calibrated on a 600x338
        blank frame and reported frame_size accordingly — so the host waits for
        a frame that must be the painted desktop: non-blank AND at least 1280 px
        wide (the pre-paint frames are <= 1067 px, the painted one is 1920).
        """

        import time as _time

        from . import png as png_module

        started = _time.time()
        last_size = None
        while _time.time() - started < timeout:
            if self.ui.dialog and self.ui.dialog.cancelled:
                # ✕ during the wait must abort promptly, not after the timeout
                return {"rendered": False, "cancelled": True,
                        "waited": round(_time.time() - started, 2)}
            status, data, layout = self.sdk.take_snapshot()
            if data.startswith(b"\x89PNG"):
                dims = png_module.dimensions(data)
                non_blank = png_module.non_blank_ratio(data, step=24)
                last_size = {"size": dims, "non_blank": round(non_blank, 3),
                             "waited": round(_time.time() - started, 2)}
                if non_blank > 0.02 and dims[0] >= 1280:
                    self.guard.mark_observed()
                    return {"rendered": True, **last_size}
            _time.sleep(1.0)
        return {"rendered": False, **(last_size or {}), "waited": round(_time.time() - started, 2)}

    def calibrate(self) -> dict:
        """Verify the mouse coordinate mapping end to end (the mapping itself is known).

        ``SendMouse*`` takes 0-1 doubles (the SDK's validator proves it — see the note
        in ``__init__``).  This verifies the mapping *relatively* with three probes
        (0.2 / 0.5 / 0.8).  ``cursor_centroid`` diffs a before/after pair, so each
        reading is the **midpoint** of the cursor's old and new positions — the two
        blobs in the diff are "cursor left here" and "cursor arrived here".  That is
        why the old two-probe version kept reporting ``unexpected_mapping``: its span
        was ``(P2 - P0)/2`` — half the expected value whenever the cursor started near
        the first probe — a false alarm that sent one real session chasing a
        coordinate drift that never existed (pip_sdk log 2026-10-03 13:29).

        Three probes cancel the unknown start position: with readings
        c1=(P0+P1)/2, c2=(P1+P2)/2, c3=(P2+P3)/2 the last pair gives
        ``c3 - c2 = (P3 - P1)/2`` = 0.3 frame spans exactly, and
        ``mid(c2, c3) = (P1 + 2*P2 + P3)/4`` = the frame centre — both independent
        of P0.
        """

        def probe(x_value: float, y_value: float):
            base = self.sdk.take_snapshot()[1]
            self.sdk.send_mouse_move(x_value, y_value)
            time.sleep(0.6)
            moved = self.sdk.take_snapshot()[1]
            if base and moved:
                return cursor_centroid(base, moved), png.dimensions(moved)
            return None, None

        first, dims = probe(0.2, 0.2)
        second, _ = probe(0.5, 0.5)
        third, _dims = probe(0.8, 0.8)
        self.guard.mark_observed()  # calibration moves the pointer and reads frames

        verdict = "no_cursor_visible"
        span = None
        centre = None
        if second and third and dims:
            # Only the last pair judges: c1 is None whenever the cursor was
            # already parked at the first probe (nothing moves, nothing diffs),
            # which is common — the judgement must not depend on it.
            span = (third[0] - second[0], third[1] - second[1])
            centre = ((second[0] + third[0]) // 2, (second[1] + third[1]) // 2)
            expected_span = (0.3 * dims[0], 0.3 * dims[1])
            close_x = abs(span[0] - expected_span[0]) < max(80, expected_span[0] * 0.5)
            close_y = abs(span[1] - expected_span[1]) < max(80, expected_span[1] * 0.5)
            centred = (abs(centre[0] - 0.5 * dims[0]) < max(80, 0.1 * dims[0])
                       and abs(centre[1] - 0.5 * dims[1]) < max(80, 0.1 * dims[1]))
            if close_x and close_y and centred:
                verdict = "unit_scale_confirmed"
            else:
                verdict = "unexpected_mapping"
        self.calibration = {
            "scale": self.coord_scale,
            "frame_size": dims,
            "cursor_at_0.2": first,
            "cursor_at_0.5": second,
            "cursor_at_0.8": third,
            "span_0.5_to_0.8": span,
            "centre_check": centre,
            "verdict": verdict,
        }
        return self.calibration

    def _to_display(self, x_1000: float, y_1000: float) -> tuple[float, float]:
        """0-1000 (the package's coordinate space) into the engine's doubles."""

        if self.coord_scale == "unit":
            return (x_1000 / 1000.0, y_1000 / 1000.0)
        return (x_1000 / 1000.0 * self.display[0], y_1000 / 1000.0 * self.display[1])

    # -- lifecycle ------------------------------------------------------ #

    def create_session(self, password: str) -> dict:
        # the view rect follows the window's current form (pip = full client,
        # big = the inset content card) — measured physical pixels
        x, y, width, height = self.ui.client_view_rect()
        status = self.sdk.create_shadow_session(
            self.ui.host_hwnd, rect=(x, y, width, height), display=self.display,
            visible=1, password=password, prompt=0,
        )
        self.session_guid = self._session_guid_from_log()
        return {"status": status, "session_guid": self.session_guid}

    def fit_view(self) -> None:
        """Re-fit the SDK render area to the window's current form (on resize /
        mode switch) — the same SetShadowSessionRect call Shadow's client makes
        when its live window is resized (seen in its own log)."""
        if not self.session_guid:
            return
        x, y, width, height = self.ui.client_view_rect()
        rc = self.sdk.set_shadow_session_display_settings(
            x, y, width, height, self.display[0], self.display[1])
        print(f"ui| fit_view ({x},{y},{width},{height}) rc={rc}", flush=True)

    def _session_guid_from_log(self) -> str:
        import re

        for line in reversed(self.sdk.read_log_tail(self.runtime["log"], lines=30)):
            match = re.search(r"session_guid=([0-9a-fA-F-]{36})", line)
            if match:
                return match.group(1)
        return ""

    def destroy_session(self) -> dict:
        status = self.sdk.destroy_shadow_session()
        self.session_guid = ""
        return {"status": status}

    # -- operations ----------------------------------------------------- #

    def capture(self, *, max_width: int | None = None, quality: int = 88) -> dict:
        status, data, layout = self.sdk.take_snapshot()
        if not data or not data.startswith(b"\x89PNG"):
            return {"error": f"snapshot unavailable (rc={status}, {len(data)} bytes, {layout})"}
        self.guard.mark_observed()
        width, height = png.dimensions(data)
        if max_width and width > max_width:
            data = png.downscale(data, max_width)
            width, height = png.dimensions(data)
        return {"frame": base64.b64encode(data).decode("ascii"), "width": width, "height": height,
                "layout": layout}

    def _sdk_verdict(self, api: str) -> bool | None:
        """What the engine itself said about the most recent ``api`` call.

        The C ABI returns an opaque int; the ground truth is the SDK's own log,
        which writes ``PipSdk_<api>() exit with result {... success=true|false}``
        before the blocking call returns.  ``None`` means no verdict found.
        """

        needle = f"PipSdk_{api}() exit with result"
        for line in self.sdk.read_log_tail(self.runtime["log"], lines=6):
            if needle in line:
                return "success=true" in line
        return None

    def click(self, x, y, button: str = "left", clicks: int = 1) -> dict:
        """A click the way the vendor's own client sends it (its pip_sdk log,
        same machine, same day: a successful WorkBuddy check-in at 13:40)::

            SendMouseMoveEvent(x, y)
            SendMouseClickEvent(button, 0, action=1, x, y)     # single click
            SendMouseClickEvent(button, 0, action=2, x, y)     # double click

        ``action`` is a *gesture* enum, not down/up: 1 = click, 2 = double click.
        The old down(action=1)+up(action=0) pair failed every time — the engine
        refuses action=0 (0/12 across two days of logs, IPC "AsyncRequest
        failed"), which left the button logically held and turned every later
        click into a drag.  That — not a coordinate drift — is what made clicks
        land on the wrong controls.
        """

        px, py = self._to_display(x, y)
        button_id = {"left": 1, "right": 2, "middle": 3}.get(button, 1)
        action = 2 if int(clicks) == 2 else 1
        # triple click: the gesture enum is verified only for 1 (click) and 2
        # (double) in the vendor's logs — no action=3 sample exists.  Three
        # rapid action=1 gestures land inside Windows' multi-click window and
        # register as a triple click (best-effort until a real session proves
        # the enum extends).
        repeats = 3 if int(clicks) >= 3 else 1
        self.sdk.send_mouse_move(px, py)
        for round_ in range(repeats):
            self.sdk.send_mouse_click(button_id, 0, action, px, py)
            if round_ < repeats - 1:
                time.sleep(0.05)  # well inside the 500 ms multi-click window
        verdict = self._sdk_verdict("SendMouseClickEvent")
        result = {"display_point": [px, py], "button": button, "clicks": clicks,
                  "gesture": f"move+click(action={action})x{repeats}"}
        if verdict is False:
            result["ok"] = False
            result["error"] = ("the engine refused the click (SDK log: "
                               "success=false) — see the pip_sdk log tail")
        else:
            result["ok"] = True
        return result

    def move(self, x, y) -> dict:
        px, py = self._to_display(x, y)
        return {"status": self.sdk.send_mouse_move(px, py), "display_point": [px, py]}

    def scroll(self, x, y, notches: int, *, horizontal: bool = False) -> dict:
        px, py = self._to_display(x, y)
        # wheel_type 0 = vertical, 1 = horizontal — the seed wire protocol's
        # ``horizontal`` flag; same convention as dist/computer-use/backend.
        return {"status": self.sdk.send_mouse_wheel(
                    1 if horizontal else 0, 0, int(notches) * 120, px, py),
                "horizontal": bool(horizontal)}

    def type_text(self, text: str) -> dict:
        return {"status": self.sdk.send_text(str(text)), "length": len(str(text))}

    def key(self, virtual_key: int, is_down: int = 1) -> dict:
        """A full press: down then up.  The vendor's own client sends pairs back
        to back and they all succeed (its log: 0x11/0x43 down+up within 12 ms);
        sending only the down half strands the key held (two 0x1B presses in the
        2026-10-03 13:31 session were never released)."""

        down = self.sdk.send_keyboard_event(int(virtual_key), 1)
        up = self.sdk.send_keyboard_event(int(virtual_key), 0)
        return {"sent": [down, up]}

    def key_down(self, virtual_key: int) -> dict:
        return {"status": self.sdk.send_keyboard_event(int(virtual_key), 1)}

    def key_up(self, virtual_key: int) -> dict:
        return {"status": self.sdk.send_keyboard_event(int(virtual_key), 0)}

    def hotkey(self, virtual_keys: list[int]) -> dict:
        """A chord — exactly what the vendor's own ``SendKeyStroke(size=2)`` call is."""

        return {"status": self.sdk.send_key_stroke([int(key) for key in virtual_keys])}

    def copy_selection(self, *, wait: float = 0.4) -> dict:
        """Ctrl+C then read the clipboard back through the engine's own API."""

        ctrl, c = 0x11, 0x43
        self.sdk.send_keyboard_event(ctrl, 1)
        self.sdk.send_keyboard_event(c, 1)
        self.sdk.send_keyboard_event(c, 0)
        self.sdk.send_keyboard_event(ctrl, 0)
        time.sleep(wait)
        status, text = self.sdk.get_clipboard()
        return {"status": status, "text": text}

    def drag(self, x1, y1, x2, y2, *, steps: int = 8, duration_ms: int = 800) -> dict:
        """A true hold-and-drag is **not offered** by this engine.

        The vendor's mouse vocabulary — read out of its own pip_sdk log — is
        move / click (action=1) / double click (action=2) / wheel.  The
        press-release pair this used to send fails at the release: the engine
        refuses ``action=0`` (measured 0/12 over two days; every release
        returned IPC "AsyncRequest failed"), which strands the button in the
        held state and silently converts later input into drags.  Refusing
        loudly beats a half-drag with a stuck button.  Scrollable UIs work via
        ``scroll`` (wheel); moving windows is not possible on this engine.
        """

        error = RuntimeError(
            "vdcu.drag: [VD_DRAG_UNSUPPORTED] the shadow engine has no working "
            "button-hold verb (SendMouseClickEvent action=0 is refused, 0/12 "
            "measured) — use scroll for lists, or click + type instead."
        )
        error.code = "VD_DRAG_UNSUPPORTED"  # type: ignore[attr-defined]
        raise error

    def clipboard_get(self) -> dict:
        status, text = self.sdk.get_clipboard()
        return {"status": status, "text": text}

    def clipboard_set(self, text: str) -> dict:
        return {"status": self.sdk.set_clipboard(str(text))}

    def launch(self, application: str, command_line: str | None = None) -> dict:
        return {"status": self.sdk.create_process(application, command_line)}

    def query_programs(self, query: str | None, max_count: int = 20) -> dict:
        status, text = self.sdk.query_hybrid_programs(query, max_count=max_count)
        return {"status": status, "json": text}

    def dump_programs(self) -> dict:
        """The session's running programs (the SDK's nearest thing to window listing)."""

        status, text = self.sdk.dump_hybrid_programs()
        return {"status": status, "json": text}

    def set_view_rect(self, x: int, y: int, width: int, height: int,
                      display_width: int | None = None, display_height: int | None = None) -> dict:
        """Resize the session view — the vendor's own 实况框-zoom call.

        Its log shows exactly this signature being driven by the chat preview being
        resized: ``SetShadowSessionRect(0, 0, 426, 276)`` etc.
        """

        display_width = self.display[0] if display_width is None else display_width
        display_height = self.display[1] if display_height is None else display_height
        rect_rc = self.sdk.set_shadow_session_rect(int(x), int(y), int(width), int(height))
        full_rc = self.sdk.set_shadow_session_display_settings(
            int(x), int(y), int(width), int(height), int(display_width), int(display_height)
        )
        return {"rect_status": rect_rc, "display_status": full_rc}

    def list_apps(self, query: str, limit: int = 20) -> dict:
        """The engine's program catalogue, searched by name.

        ``QueryHybridPrograms`` needs a non-empty query (measured), so this is a
        search rather than a full listing: ``{file_path, name, score, unique_info}``
        with ``unique_info`` holding the AUMID for packaged apps.
        """

        status, text = self.sdk.query_hybrid_programs(query, max_count=max(limit, 1))
        import json as _json

        try:
            entries = _json.loads(text) if text else []
        except ValueError:
            entries = []
        return {"status": status, "query": query, "count": len(entries), "apps": entries[:limit]}

    def launch_by_name(self, name: str) -> dict:
        """Search the catalogue and start the best match.

        Ambiguity is refused rather than guessed: several strong hits come back as
        candidates for the caller to disambiguate.
        """

        import json as _json

        status, text = self.sdk.query_hybrid_programs(name, max_count=6)
        try:
            entries = _json.loads(text) if text else []
        except ValueError:
            entries = []
        if not entries:
            return {"status": status, "error": f"no match for {name!r}"}
        strong = [entry for entry in entries if entry.get("score", 0) >= 100]
        if len(strong) > 1 and strong[0].get("score") == strong[1].get("score"):
            return {"status": status, "error": "ambiguous", "candidates": strong}
        chosen = entries[0]
        target = chosen.get("unique_info") or chosen.get("file_path")
        rc = self.sdk.create_process(target)
        return {"status": rc, "launched": chosen, "target": target}

    def status(self) -> dict:
        return {
            "engine": "shadow",
            "session_guid": self.session_guid,
            "display": list(self.display),
            "sdk_version": self.sdk.version(),
            "coord_scale": self.coord_scale,
            "calibration": self.calibration,
            "uptime": round(time.time() - self.started, 2),
        }


def serve(socket_path: tuple[str, int], token: str, host: ShadowHost) -> int:
    import socket

    deadline = time.time() + 20
    while True:
        try:
            sock = socket.create_connection(socket_path, timeout=5)
            break
        except OSError:
            if time.time() > deadline:
                raise
            time.sleep(0.2)
    sock.settimeout(None)
    reader = sock.makefile("rb")
    writer = sock.makefile("wb")
    writer.write(json.dumps({"hello": "worker", "token": token, "desktop": "shadow-shadow"}).encode() + b"\n")
    writer.flush()

    def send(payload: dict) -> None:
        writer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
        writer.flush()

    for raw in reader:
        line = raw.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError as error:
            send({"id": 0, "ok": False, "error": {"code": "VD_BAD_REQUEST", "message": str(error)}})
            continue

        request_id = request.get("id", 0)
        op = request.get("op", "")
        args = request.get("args") or {}
        with host.lock:
            try:
                result = dispatch(host, op, args)
                if op in _ACTION_OPS:
                    # The action log is diagnostic; keep typed text out of it (the
                    # same rule the stealth worker follows).
                    host.actions.append(
                        {"op": op, "args": {k: v for k, v in args.items() if k != "text"}}
                    )
                    del host.actions[:-40]
                response = {"id": request_id, "ok": True, "result": result}
                if op == "capture" and "frame" in result:
                    # Serve the capture's own frame at its own resolution.  A second
                    # grab at a hard-coded width once replaced a native 1920x1080
                    # capture with a 533x300 preview-grade image — which is exactly
                    # how "the screenshots are blurry" happens.
                    response["frame"] = result["frame"]
                    response["frame_meta"] = {
                        "width": result.get("width"), "height": result.get("height"),
                        "format": result.get("format", "png"),
                        "image_width": result.get("image_width"),
                        "image_height": result.get("image_height"),
                        "non_blank": result.get("non_blank", 1.0),
                    }
                elif op in ("click", "move", "scroll", "type", "key", "launch") or args.get("with_frame"):
                    frame = host.capture(max_width=args.get("frame_max_width") or args.get("max_width") or 720)
                    if "frame" in frame:
                        response["frame"] = frame["frame"]
                        response["frame_meta"] = {
                            "width": frame["width"], "height": frame["height"],
                            "format": "png", "non_blank": 1.0,
                        }
            except Exception as error:  # noqa: BLE001
                response = {
                    "id": request_id, "ok": False,
                    "error": {"code": getattr(error, "code", "VD_OPERATION_FAILED"),
                              "message": str(error), "type": type(error).__name__},
                }
            try:
                send(response)
            except OSError:
                break
        if op == "quit":
            break
    try:
        sock.close()
    except OSError:
        pass
    return 0


# The worker protocol this engine answers, kept as explicit sets so the offline
# selftest can compare them against the stealth worker's operations.  Two engines
# behind one CLI/MCP surface drift apart quietly otherwise — this is the guard.
SUPPORTED_OPS = frozenset(
    {
        "ping", "status", "calibrate", "capture", "click", "double_click", "move",
        "scroll", "type", "key", "key_down", "key_up", "hotkey", "copy_selection",
        "wait", "drag", "clipboard_get", "clipboard_set", "launch", "list_apps",
        "query_programs", "dump_programs", "set_view_rect", "actions", "paste_into",
        "tracked_pids", "quit", "show_view",
    }
)
#: Operations the stealth worker has but this engine cannot: the shadow-session
#: SDK publishes no window enumeration (its 31 exports cover session, input,
#: clipboard and program launching — not HWNDs).  Declared rather than missing.
UNSUPPORTED_OPS = frozenset(
    {
        # Window-oriented: the shadow-session SDK publishes no window enumeration,
        # and hit-testing is the stealth engine's workaround for exactly that gap
        # on hidden desktops.  Declared as unsupported, not missing.
        "windows", "children", "window_text", "close_window", "hit_test",
    }
)

#: Mutating operations recorded in the action log (mirrors the stealth worker).
_ACTION_OPS = frozenset(
    {"click", "double_click", "move", "drag", "scroll", "type", "key", "key_down",
     "key_up", "hotkey", "copy_selection", "launch"}
)

#: Pointer operations that the contract refuses before any capture —
#: "Take a screenshot before sending pointer input", the same rule the bundled runtime's own
#: client enforces (``CU_SCREENSHOT_REQUIRED``).
_POINTER_OPS = frozenset({"click", "double_click", "move", "drag", "scroll"})


class ObserveGuard:
    """The observe-before-acting rule, enforced on the engine's behalf."""

    def __init__(self) -> None:
        self.observed = False

    def mark_observed(self) -> None:
        self.observed = True

    def require(self, operation: str) -> None:
        if self.observed:
            return
        error = RuntimeError(
            f"vdcu.{operation}: [CU_SCREENSHOT_REQUIRED] Take a screenshot before "
            "sending pointer input, and derive coordinates from that frame."
        )
        error.code = "CU_SCREENSHOT_REQUIRED"  # type: ignore[attr-defined]
        raise error


def _vk(key) -> int:
    """Key name to virtual-key code — the same mapping the stealth worker uses.

    Reusing ``primitives._vk_for`` keeps ``key enter`` meaning the same thing on
    both engines instead of drifting into two conventions.
    """

    from vdcu import primitives

    return primitives._vk_for(str(key))


def dispatch(host: ShadowHost, op: str, args: dict) -> dict:
    if op in _POINTER_OPS:
        # Shadow's contract: blind pointer input is refused ("Take a screenshot
        # before sending pointer input").  capture()/calibrate() satisfy it.
        host.guard.require(op)
    if op == "ping":
        return {"pong": True, **host.status()}
    if op == "status":
        return host.status()
    if op == "calibrate":
        return host.calibrate()
    if op == "capture":
        return host.capture(max_width=args.get("max_width"), quality=int(args.get("quality", 88)))
    if op == "click":
        return host.click(args["x"], args["y"], args.get("button", "left"), int(args.get("clicks", 1)))
    if op in ("double_click",):
        return host.click(args["x"], args["y"], "left", 2)
    if op == "move":
        return host.move(args["x"], args["y"])
    if op == "scroll":
        return host.scroll(args["x"], args["y"], args.get("delta", 1),
                           horizontal=bool(args.get("horizontal", False)))
    if op == "type":
        return host.type_text(args.get("text", ""))
    if op == "key":
        return host.key(_vk(args.get("key", "")), 1)
    if op == "key_down":
        return host.key_down(_vk(args.get("key", "")))
    if op == "key_up":
        return host.key_up(_vk(args.get("key", "")))
    if op == "hotkey":
        return host.hotkey([_vk(item) for item in args.get("keys", [])])
    if op == "copy_selection":
        return host.copy_selection()
    if op == "wait":
        time.sleep(float(args.get("seconds", 1)))
        return {"waited": args.get("seconds", 1)}
    if op == "drag":
        return host.drag(
            args["x1"], args["y1"], args["x2"], args["y2"],
            duration_ms=int(args.get("duration_ms", 1000)),  # the wire's default
        )
    if op == "clipboard_get":
        return host.clipboard_get()
    if op == "clipboard_set":
        return host.clipboard_set(args.get("text", ""))
    if op == "paste_into":
        text = str(args.get("text", ""))
        if args.get("use_clipboard"):
            host.clipboard_set(text)
            host.hotkey([0x11, 0x56])  # Ctrl+V — a real chord through SendKeyStroke
            return {"method": "clipboard+chord", "characters": len(text)}
        result = host.type_text(text)
        return {"method": "typed", **result}
    if op == "launch":
        # One op name for both engines: a name is resolved through the catalogue
        # search, an explicit application path is started as-is.
        if args.get("name"):
            return host.launch_by_name(args["name"])
        return host.launch(args.get("application", ""), args.get("command_line"))
    if op == "list_apps":
        return host.list_apps(args.get("query", ""), int(args.get("limit", 20)))
    if op == "query_programs":
        return host.query_programs(args.get("query"), int(args.get("max_count", 20)))
    if op in ("windows", "children", "window_text", "close_window"):
        # Honest gap: the shadow-session SDK exposes no window-enumeration API
        # (its 31 exports cover the session, input, clipboard and program
        # launching — not HWNDs).  Pretending otherwise would fail silently later.
        raise NotImplementedError(
            f"{op} is not available on the shadow engine: the shadow-session SDK has no "
            "window-enumeration interface. Use capture to see the session, dump_programs "
            "to list what is running in it, or the stealth engine which enumerates windows."
        )
    if op == "dump_programs":
        return host.dump_programs()
    if op == "actions":
        limit = int(args.get("limit", 20))
        return {"actions": host.actions[-limit:]}
    if op == "set_view_rect":
        return host.set_view_rect(
            int(args.get("x", 0)), int(args.get("y", 0)),
            int(args.get("width", 0)), int(args.get("height", 0)),
            args.get("display_width"), args.get("display_height"),
        )
    if op == "show_view":
        # re-show the 「virtual desktop」 window (its ✕ hides rather than destroys —
        # destroying it kills the SDK's RDP view, measured)
        host.ui.show_host()
        return {"shown": True}
    if op == "tracked_pids":
        return {"pids": [kernel32.GetCurrentProcessId()]}
    if op == "quit":
        return {"quitting": True}
    raise ValueError(f"unknown op {op!r}")


def _shadow_session_in_use() -> str:
    """Is another shadow session already occupying the engine?  Description or "".

    Measured behaviour (2026-10-03, user report): with one shadow session alive,
    CreateShadowSession accepts the *correct* password, issues a guid, and the
    RDP logon then fails asynchronously — surfacing as an endless password retry
    loop.  The engine effectively runs one shadow session at a time, so check
    *before* asking for the password at all.
    """
    # 1) live preview windows of a running session (Shadow's own or a parallel one)
    hits = []

    def cb(h, p):
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(h, cls, 256)
        name = cls.value
        if name.startswith("ShadowPreview") or name == "ComputerUsePipHostWindow":
            hits.append(name)
        return True

    user32.EnumWindows(WNDENUMPROC_CHILD(cb), 0)
    if hits:
        return f"窗口 {sorted(set(hits))}"
    # 2) the engine backend itself already running (someone else's pip_main)
    try:
        import subprocess
        # This host itself runs without a console (spawn_detached); a bare
        # subprocess.run would allocate a visible console window for tasklist
        # and flash a cmd box on the user's desktop right before the password
        # prompt.  CREATE_NO_WINDOW keeps it headless like every other spawn
        # in this package.
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq pip_main.exe", "/NH"],
                             capture_output=True, text=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        if "pip_main.exe" in out:
            return "引擎后端 pip_main.exe 已在运行"
    except Exception:  # noqa: BLE001
        pass
    return ""


def _autolaunch(host) -> None:
    """Fire configured processes inside the fresh shadow session, before serve().

    ``<vdcu root>/autolaunch.json`` lists ``{"application", "command_line"}``
    pairs launched through the engine the moment the session is authorized and
    rendering — a slow-booting helper (the KimiCU agent) then overlaps its cold
    start with calibration, the control connect-back and the client's session
    wait, instead of being launched by an RPC after everything else.  ``{root}``
    expands to the vdcu root, ``{kimicu}`` to ``<root>/../cu.exe``, and
    %-env vars expand in both fields.  Failures are reported, never fatal.
    """

    config = os.path.join(PACKAGE_ROOT, "autolaunch.json")
    try:
        with open(config, encoding="utf-8") as handle:
            commands = json.load(handle).get("commands", [])
    except (OSError, ValueError):
        return
    kimicu = os.path.normpath(os.path.join(PACKAGE_ROOT, os.pardir, "cu.exe"))
    for command in commands:
        try:
            application = os.path.expandvars(
                str(command["application"]).replace("{root}", PACKAGE_ROOT).replace("{kimicu}", kimicu)
            )
            command_line = command.get("command_line")
            if command_line is not None:
                command_line = os.path.expandvars(
                    str(command_line).replace("{root}", PACKAGE_ROOT).replace("{kimicu}", kimicu)
                )
            result = host.launch(application, command_line)
            print(f"autolaunch      : {os.path.basename(application)} -> "
                  f"{json.dumps(result, ensure_ascii=False)[:200]}", flush=True)
        except Exception as error:  # noqa: BLE001 - autolaunch must never break startup
            print(f"autolaunch      : FAILED {command}: {error}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vdcu shadow-host")
    parser.add_argument("--connect", required=True, help="host:port of the daemon control socket")
    parser.add_argument("--token", default="")
    parser.add_argument("--password-timeout", type=float, default=600.0,
                        help="how long to wait for the password box (people need time to find it)")
    parser.add_argument("--no-ui", action="store_true", help="debug: skip the windows (no password box)")
    options = parser.parse_args(argv)

    from .win32 import enable_dpi_awareness
    enable_dpi_awareness()  # geometry below is designed at 96 DPI

    app_dir = find_app_dir()
    cursor = find_cursor_path()
    runtime = runtime_dirs(os.path.join(os.environ.get("TEMP", "."), "vdcu-shadow"))

    # One shadow session at a time: if another one (the bundled runtime's own CU, a
    # parallel experiment, a leftover) is alive, creating ours would accept the
    # password and then fail asynchronously — an endless retry loop.  Fail fast
    # with the actual reason instead.  Recheck once: a just-destroyed backend
    # takes a moment to exit.
    busy = _shadow_session_in_use()
    if busy:
        time.sleep(3)
        busy = _shadow_session_in_use()
    if busy:
        print("ERROR: 已有一个影子会话在运行，引擎同时只能承载一个（实测：此时输入正确密码", flush=True)
        print("       也会被异步拒绝，表现为反复要求输入密码）。", flush=True)
        print(f"       占用来源：{busy}", flush=True)
        print("       请先结束占用引擎的会话，确认归属后再清理残留。", flush=True)
        print("       然后重新启动；不要结束无关的应用。", flush=True)
        return 5

    credential = probe_empty_credential()
    auto_empty = credential.try_without_card
    print(f"authorization   : {credential.status} (winerror={credential.winerror})", flush=True)
    ui = UiThread(show_authorization=not auto_empty)
    if not options.no_ui:
        ui.start()
        if not ui.ready.wait(timeout=30):
            ui.shutdown()
            print("ERROR: render UI did not initialize", flush=True)
            return 3
    ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)

    # the host's stdout lands in a log file with the locale codec (GBK here):
    # printing ✕/⚠ once crashed main() mid-cancel with UnicodeEncodeError
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

    sdk = PipSdk()
    sdk.initialize(
        pip_module_dir=app_dir, log_path_base=runtime["log"],
        dump_path_base=runtime["dump"], vm_cache_dir=runtime["cache"],
        auto_destroy_on_exit=1,
    )
    if cursor:
        sdk.set_virtual_cursor_path(cursor)
    sdk.is_system_environment_initialized()

    if auto_empty:
        print(">>> 无弹框尝试空密码创建 virtual desktop；以实际渲染结果验证授权。", flush=True)
    else:
        print(">>> 请在本地 virtual desktop 授权框输入 Windows 账户密码；空密码可直接回车。", flush=True)
    host = ShadowHost(ui, sdk, runtime, DISPLAY)
    try:
        authorization = authorize_session(
            host, ui, auto_empty=auto_empty, timeout=options.password_timeout)
    except Exception:
        host.destroy_session()
        sdk.uninitialize()
        ui.shutdown()
        # Do not log an exception that might contain a submitted credential.
        print("ERROR: session authorization failed unexpectedly", flush=True)
        return 3
    print(f"authorization_result: {json.dumps(authorization, ensure_ascii=False)}", flush=True)
    if not authorization.get("ok"):
        host.destroy_session()
        sdk.uninitialize()
        ui.shutdown()
        return authorization.get("exit_code", 3)

    _autolaunch(host)  # in-session helpers (KimiCU agent) start booting NOW
    ui.show_host()  # authorization done and pixels are live — reveal the preview window
    calibration = host.calibrate()
    print(f"calibration     : {json.dumps(calibration, ensure_ascii=False)}", flush=True)

    host_address = options.connect.rsplit(":", 1)
    try:
        serve((host_address[0], int(host_address[1])), options.token, host)
    finally:
        host.destroy_session()
        sdk.uninitialize()
        ui.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

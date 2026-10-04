"""Stealth executor: drive a desktop that is never displayed.

This is the mode that matches the the bundled runtime experience — the agent works on its
own desktop inside a small live preview while the user's desktop and input are
untouched.  Everything here runs **inside the worker process, which lives on the
target desktop**; that detail is what makes the measurements below possible.

Measured facts this module is built on (Windows 11, 2026-10-01)
--------------------------------------------------------------
===========================  ==========================================
 ``BitBlt(GetDC(0))``         FAILS on a desktop that is not active
                              (``ERROR_INVALID_HANDLE``), even when the
                              calling thread is bound to that desktop.
 ``PrintWindow(hwnd, 0)``     succeeds but returns solid black.
 ``PrintWindow(hwnd, 2)``     ``PW_RENDERFULLCONTENT`` returns real pixels
                              (0.96 non-blank in the fixture run).
 ``PostMessage(WM_LBUTTON*)`` reaches a window on a hidden desktop.
 ``PostMessage(WM_CHAR)``     reaches a child EDIT control; text reads back
                              with ``WM_GETTEXT``.
===========================  ==========================================

So: capture is per-window ``PrintWindow`` composited in z-order, and input is
window messages rather than ``SendInput``.  ``SendInput`` is deliberately *not*
used here — it targets the active input desktop, which is exactly what must not
change in this mode.  See :mod:`vdcu.takeover` for the opposite trade-off.

Consequence worth knowing before choosing this mode: a posted ``Ctrl+C`` does not
set the global key state, so applications that test ``GetKeyState`` will not see
the modifier.  Plain typing, clicking, dragging, scrolling and ordinary keys work;
chorded shortcuts are the one place :mod:`vdcu.takeover` is more faithful.
"""

from __future__ import annotations

import ctypes
import time

from ctypes import wintypes

from . import png, win32

user32 = win32.user32
gdi32 = win32.gdi32
kernel32 = win32.kernel32

# --- prototypes ------------------------------------------------------------- #
LRESULT = ctypes.c_ssize_t

user32.PrintWindow.restype = wintypes.BOOL
user32.PrintWindow.argtypes = [wintypes.HWND, win32.HDC, wintypes.UINT]
user32.GetWindowRect.restype = wintypes.BOOL
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.GetClientRect.restype = wintypes.BOOL
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.ScreenToClient.restype = wintypes.BOOL
user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.ClientToScreen.restype = wintypes.BOOL
user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.WindowFromPoint.restype = wintypes.HWND
user32.WindowFromPoint.argtypes = [ctypes.c_void_p]
user32.ChildWindowFromPointEx.restype = wintypes.HWND
user32.ChildWindowFromPointEx.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.UINT]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.IsIconic.restype = wintypes.BOOL
user32.IsIconic.argtypes = [wintypes.HWND]
user32.IsWindowEnabled.restype = wintypes.BOOL
user32.IsWindowEnabled.argtypes = [wintypes.HWND]
user32.PostMessageW.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.SendMessageW.restype = LRESULT
user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.GetClassNameW.restype = ctypes.c_int
user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowLongW.restype = wintypes.LONG
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetAncestor.restype = wintypes.HWND
user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetFocus.restype = wintypes.HWND
user32.GetFocus.argtypes = []
user32.SetFocus.restype = wintypes.HWND
user32.SetFocus.argtypes = [wintypes.HWND]
user32.GetDesktopWindow.restype = wintypes.HWND
user32.GetDesktopWindow.argtypes = []
user32.RedrawWindow.restype = wintypes.BOOL
user32.RedrawWindow.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.HANDLE, wintypes.UINT]

PW_RENDERFULLCONTENT = 0x00000002
WM_PRINT = 0x0317
PRF_CLIENT = 0x00000004
PRF_CHILDREN = 0x00000010
PRF_ERASEBKGND = 0x00000008


def _is_blank_bgra(bgra: bytes, width: int, height: int) -> bool:
    """True when every sampled pixel of the capture is the same (a blank render)."""

    step = max(1, (width * height) // 64)
    first = None
    for index in range(0, width * height, step):
        base = index * 4
        pixel = bgra[base : base + 4]
        if first is None:
            first = pixel
        elif pixel != first:
            return False
    return True
GW_OWNER = 4
GW_CHILD = 5
GW_HWNDNEXT = 2
GA_ROOT = 2

CWP_SKIPINVISIBLE = 0x0001
CWP_SKIPDISABLED = 0x0002
CWP_SKIPTRANSPARENT = 0x0004

WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0201, 0x0202
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
WM_MBUTTONDOWN, WM_MBUTTONUP = 0x0207, 0x0208
WM_MOUSEWHEEL = 0x020A
WM_MOUSEHWHEEL = 0x020E
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
WM_CLOSE = 0x0010
WM_SETTEXT, WM_GETTEXT = 0x000C, 0x000D

MK_LBUTTON, MK_RBUTTON, MK_MBUTTON = 0x0001, 0x0002, 0x0010
WHEEL_DELTA = 120

# Windows that are pure noise in a desktop view.  The shell's own windows are
# deliberately NOT here: when a shell runs on the agent desktop, Progman draws the
# wallpaper and desktop icons and Shell_TrayWnd draws the taskbar, and those are
# exactly what make the preview look like a real desktop.  An earlier version listed
# Progman/WorkerW/Shell_TrayWnd as noise, which both hid them from the view *and*
# made the "is a shell running?" check report a false negative.
CLASS_NOISE = {
    "SysShadow", "tooltips_class32", "TaskListThumbnailWnd",
    "UAC_InputIndicatorOverlayWnd", "UAC Input Indicator",
    "MSCTFIME UI", "IME", "CiceroUIWndFrame", "CicLoaderWndClass",
    "DDEMLEvent", "DDEMLMom", "GDI+ Hook Window Class",
    "TabletPenServiceHelperClass", "Touch Tooltip Window",
    "Windows.UI.Core.CoreWindow",
}

# Classes that belong to the shell and are worth painting even without a title.
SHELL_CLASSES = {
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "SHELLDLL_DefView",
}

# Kept for callers that used the old name.
CLASS_BLACKLIST = CLASS_NOISE


class POINT(ctypes.Structure):
    _fields_ = (("x", ctypes.c_long), ("y", ctypes.c_long))


class RECT(ctypes.Structure):
    _fields_ = (
        ("left", ctypes.c_long), ("top", ctypes.c_long),
        ("right", ctypes.c_long), ("bottom", ctypes.c_long),
    )


VK_NAMES = {
    "backspace": 0x08, "tab": 0x09, "enter": 0x0D, "return": 0x0D,
    "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12, "menu": 0x12,
    "esc": 0x1B, "escape": 0x1B, "space": 0x20, "pageup": 0x21, "pgup": 0x21,
    "pagedown": 0x22, "pgdn": 0x22, "end": 0x23, "home": 0x24, "left": 0x25,
    "up": 0x26, "right": 0x27, "down": 0x28, "insert": 0x2D, "ins": 0x2D,
    "delete": 0x2E, "del": 0x2E, "win": 0x5B, "apps": 0x5D,
}
for _index in range(1, 25):
    VK_NAMES[f"f{_index}"] = 0x6F + _index
for _char in "0123456789":
    VK_NAMES[_char] = 0x30 + int(_char)
for _offset, _char in enumerate("abcdefghijklmnopqrstuvwxyz"):
    VK_NAMES[_char] = 0x41 + _offset


class StealthExecutor:
    """Capture and drive one desktop without ever making it the active one."""

    def __init__(self, desktop_name: str = "default", hdesk=None):
        self.desktop_name = desktop_name
        self.hdesk = hdesk
        self.focus_hwnd: int = 0
        self.last_point: tuple[int, int] | None = None
        self.actions: list[dict] = []
        self.held_keys: list[int] = []

    # ------------------------------------------------------------------ #
    # Window discovery
    # ------------------------------------------------------------------ #

    def windows(self, *, include_untitled: bool = False) -> list[dict]:
        """Top-level windows of the target desktop, in z-order (topmost first)."""

        found: list[dict] = []

        def callback(hwnd, _lparam):
            info = self._window_info(hwnd, include_untitled=include_untitled)
            if info:
                found.append(info)
            return True

        win32.user32.EnumDesktopWindows(
            self.hdesk, win32.WNDENUMPROC(callback), 0
        ) if self.hdesk else win32.user32.EnumWindows(win32.WNDENUMPROC(callback), 0)
        return found

    def _window_info(self, hwnd, *, include_untitled: bool = False) -> dict | None:
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return None
        if user32.GetAncestor(hwnd, GA_ROOT) != hwnd:
            return None
        rect = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width < 24 or height < 24:
            return None

        class_buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buffer, 256)
        class_name = class_buffer.value
        if class_name in CLASS_NOISE:
            return None

        length = user32.GetWindowTextLengthW(hwnd)
        title = ""
        if length > 0:
            title_buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title_buffer, length + 1)
            title = title_buffer.value
        # A window with no title is only interesting when it is part of the shell:
        # untitled top-level windows are otherwise background infrastructure (IME
        # helpers, DDE, hooks) that would litter the composite.
        if not title and not include_untitled and class_name not in SHELL_CLASSES:
            return None
        if not title and include_untitled and class_name not in SHELL_CLASSES:
            return None

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return {
            "hwnd": int(hwnd),
            "title": title,
            "class": class_name,
            "pid": int(pid.value),
            "rect": [rect.left, rect.top, rect.right, rect.bottom],
            "size": [width, height],
            "focused": int(hwnd) == self.focus_hwnd,
        }

    # ------------------------------------------------------------------ #
    # Capture
    # ------------------------------------------------------------------ #

    def _capture_window_bgra(self, hwnd) -> tuple[bytes, int, int] | None:
        rect = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width <= 0 or height <= 0:
            return None

        window_dc = user32.GetDC(hwnd)
        if not window_dc:
            return None
        memory_dc = gdi32.CreateCompatibleDC(window_dc)
        bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
        if not memory_dc or not bitmap:
            user32.ReleaseDC(hwnd, window_dc)
            return None
        try:
            gdi32.SelectObject(memory_dc, bitmap)

            # Several render paths, first non-blank wins.  Measured: the Win11
            # taskbar (a DirectComposition surface) reports success from
            # PrintWindow(PW_RENDERFULLCONTENT) but comes out blank — which is how
            # the composite lost the taskbar while painting everything else.  A
            # forced synchronous repaint sometimes wakes such a surface up, so it
            # is tried first.
            RDW_INVALIDATE, RDW_UPDATENOW, RDW_ALLCHILDREN = 0x0001, 0x0100, 0x0080
            user32.RedrawWindow.argtypes = [wintypes.HWND, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
            user32.RedrawWindow.restype = wintypes.BOOL
            user32.UpdateWindow.argtypes = [wintypes.HWND]

            def redraw_then_print():
                user32.RedrawWindow(hwnd, None, None, RDW_INVALIDATE | RDW_UPDATENOW | RDW_ALLCHILDREN)
                user32.UpdateWindow(hwnd)
                time.sleep(0.12)
                return user32.PrintWindow(hwnd, memory_dc, PW_RENDERFULLCONTENT)

            attempts = (
                ("redraw+printwindow", redraw_then_print),
                ("printwindow-full", lambda: user32.PrintWindow(hwnd, memory_dc, PW_RENDERFULLCONTENT)),
                ("printwindow-client", lambda: user32.PrintWindow(hwnd, memory_dc, 0)),
                ("wm-print", lambda: user32.SendMessageW(hwnd, WM_PRINT, wintypes.HDC(memory_dc),
                                                         PRF_CLIENT | PRF_CHILDREN | PRF_ERASEBKGND)),
            )
            rendered = ""
            last_raw = None
            for name, renderer in attempts:
                try:
                    renderer()
                except Exception:  # noqa: BLE001
                    continue
                info = win32.BITMAPINFO()
                info.bmiHeader.biSize = ctypes.sizeof(win32.BITMAPINFOHEADER)
                info.bmiHeader.biWidth = width
                info.bmiHeader.biHeight = -height  # top-down
                info.bmiHeader.biPlanes = 1
                info.bmiHeader.biBitCount = 32
                info.bmiHeader.biCompression = win32.BI_RGB
                buffer = ctypes.create_string_buffer(width * 4 * height)
                copied = gdi32.GetDIBits(
                    memory_dc, bitmap, 0, height, buffer, ctypes.byref(info), win32.DIB_RGB_COLORS
                )
                if copied != height:
                    continue
                raw = buffer.raw
                if not _is_blank_bgra(raw, width, height):
                    self.last_render_method = name
                    return raw, width, height
                last_raw = raw
                rendered = name
            # solid-colour windows are legitimately "blank" — paint what we got
            self.last_render_method = f"{rendered}(blank)"
            return (last_raw, width, height) if last_raw else None
        finally:
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(hwnd, window_dc)

    def capture_composite(
        self, *, max_width: int | None = None, image_format: str = "png", quality: int = 88
    ) -> dict:
        """Render the whole desktop by compositing every window, bottom to top.

        There is no screen to copy in this mode, so the desktop view *is* its
        windows.  ``PrintWindow`` per window is the only thing that returns real
        pixels on a desktop that was never activated.

        ``image_format`` matters more than it sounds: a native-resolution frame of a
        photographic wallpaper is ~3 MB as PNG and ~0.9 MB as JPEG, so JPEG is the
        default for anything a human looks at.  PNG stays available (and is what the
        Tk preview must use, since Tk 8.6 cannot decode JPEG).
        """

        left, top, width, height = win32.virtual_screen()
        canvas = bytearray(b"\x20\x20\x20\x00" * (width * height))  # BGRA dark grey

        windows = self.windows(include_untitled=True)
        # Paint bottom-up in the *real* z-order.  ``EnumDesktopWindows`` does not
        # promise z-order — trusting it let the wallpaper window cover the
        # applications and the taskbar in some captures (observed), which is why the
        # z-order walk in ``win32.z_order_handles`` decides the painting order here.
        zorder = {hwnd: index for index, hwnd in enumerate(win32.z_order_handles())}
        windows.sort(key=lambda info: zorder.get(info["hwnd"], len(zorder)))
        paint_order = [
            {"hwnd": info["hwnd"], "class": info.get("class", ""),
             "z": zorder.get(info["hwnd"]), "rect": list(info.get("rect") or ())}
            for info in windows
        ]
        painted = 0
        for info in reversed(windows):  # bottom-up so the topmost ends on top
            self.last_render_method = "?"
            captured = self._capture_window_bgra(info["hwnd"])
            if not captured:
                continue
            bgra, win_width, win_height = captured
            for entry in paint_order:
                if entry["hwnd"] == info["hwnd"]:
                    entry["rendered"] = self.last_render_method
                    break
            x = info["rect"][0] - left
            y = info["rect"][1] - top
            if x >= width or y >= height or x + win_width <= 0 or y + win_height <= 0:
                continue
            painted += 1
            self._blit(canvas, bgra, width, height, x, y, win_width, win_height)
            # Child windows are separate HWNDs with their own content: the taskbar's
            # Start button, search box, tray icons and clock are children of
            # Shell_TrayWnd, and without this the shell painted as a bare strip next to
            # a reference whose taskbar was complete.  Painting them in z-order on top
            # of their parent is what makes the difference.
            painted += self._paint_children(canvas, info["hwnd"], width, height, left, top, depth=0)

        self._draw_cursor(canvas, width, height)

        # Downscale the *pixels*, not the encoded file: png.downscale decodes a PNG, so
        # calling it on a JPEG raised "not a PNG" and broke every preview frame after
        # capture learned to emit JPEG.  Resampling first is also cheaper, because the
        # encoder then runs on a smaller image.
        raw = bytes(canvas)
        image_width, image_height = width, height
        if max_width and width > max_width:
            raw, image_width, image_height = _downscale_bgra(raw, width, height, max_width)

        frame = self._encode(raw, image_width, image_height, image_format, quality)
        return {
            "frame": frame,
            "format": image_format,
            "width": width,
            "height": height,
            "image_width": image_width,
            "image_height": image_height,
            "windows_painted": painted,
            "windows_total": len(windows),
            "paint_order": paint_order,
            # Honest metadata: on a desktop that is never activated, Windows does not
            # compose the modern (XAML/DirectComposition) taskbar at all, so
            # PrintWindow returns blank for Shell_TrayWnd through every render path
            # (measured four ways).  The wallpaper and icons are classic GDI and do
            # render.  A taskbar-visible desktop needs the real shadow session
            # (engine="shadow") or takeover mode.
            "taskbar_rendered": any(
                "Tray" in (entry.get("class") or "")
                and "blank" not in (entry.get("rendered") or "blank")
                for entry in paint_order
            ),
            # Measured on the raw pixels, not on the encoded image, so the number means
            # the same thing for PNG and JPEG (a JPEG cannot be fed to the PNG decoder).
            "non_blank": round(_non_blank_bgra(raw, image_width, image_height), 4),
        }

    def _blit(
        self, canvas: bytearray, bgra: bytes, width: int, height: int,
        x: int, y: int, win_width: int, win_height: int,
    ) -> None:
        """Copy one window's pixels into the canvas, clipping at the edges."""

        for row in range(win_height):
            target_y = y + row
            if target_y < 0 or target_y >= height:
                continue
            source_start = row * win_width * 4
            target_start = (target_y * width + x) * 4
            if x >= 0 and x + win_width <= width:
                canvas[target_start : target_start + win_width * 4] = bgra[
                    source_start : source_start + win_width * 4
                ]
            else:  # clipped at an edge
                for column in range(win_width):
                    target_x = x + column
                    if 0 <= target_x < width:
                        src = source_start + column * 4
                        dst = (target_y * width + target_x) * 4
                        canvas[dst : dst + 4] = bgra[src : src + 4]

    def _paint_children(
        self, canvas: bytearray, parent: int, width: int, height: int,
        left: int, top: int, *, depth: int, max_depth: int = 3, min_size: int = 8,
    ) -> int:
        """Paint visible child windows of ``parent``, deepest last (on top)."""

        if depth > max_depth:
            return 0
        painted = 0
        for child in self.children_of(parent):
            if not user32.IsWindowVisible(child):
                continue
            rect = RECT()
            if not user32.GetWindowRect(child, ctypes.byref(rect)):
                continue
            child_width, child_height = rect.right - rect.left, rect.bottom - rect.top
            if child_width < min_size or child_height < min_size:
                continue
            captured = self._capture_window_bgra(child)
            if captured:
                bgra, captured_width, captured_height = captured
                self._blit(
                    canvas, bgra, width, height,
                    rect.left - left, rect.top - top, captured_width, captured_height,
                )
                painted += 1
            painted += self._paint_children(
                canvas, child, width, height, left, top, depth=depth + 1,
                max_depth=max_depth, min_size=min_size,
            )
        return painted

    @staticmethod
    def _encode(bgra: bytes, width: int, height: int, image_format: str, quality: int) -> bytes:
        if image_format == "jpeg":
            from . import jpeg

            return jpeg.encode_bgra(bgra, width, height, quality=quality)
        return png.encode_bgra(bgra, width, height, level=1)

    def _draw_cursor(self, canvas: bytearray, width: int, height: int) -> None:
        """Mark the last synthetic click, so a viewer can see what the agent did."""

        if not self.last_point:
            return
        left, top, _w, _h = win32.virtual_screen()
        cx, cy = self.last_point[0] - left, self.last_point[1] - top
        for radius, colour in ((6, (0x40, 0x40, 0xFF, 0xFF)), (3, (0xFF, 0xFF, 0xFF, 0xFF))):
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    if dx * dx + dy * dy > radius * radius:
                        continue
                    x, y = cx + dx, cy + dy
                    if 0 <= x < width and 0 <= y < height:
                        offset = (y * width + x) * 4
                        canvas[offset : offset + 4] = bytes(colour)

    # ------------------------------------------------------------------ #
    # Hit testing and message input
    # ------------------------------------------------------------------ #

    def _normalized_to_screen(self, x_1000: float, y_1000: float) -> tuple[int, int]:
        left, top, width, height = win32.virtual_screen()
        return (
            int(left + (x_1000 / 1000.0) * (width - 1)),
            int(top + (y_1000 / 1000.0) * (height - 1)),
        )

    def window_at(self, screen_x: int, screen_y: int) -> int:
        """Deepest window under a screen point on the target desktop.

        Neither ``WindowFromPoint`` nor ``ChildWindowFromPointEx`` can be used here.
        Both were measured on a desktop that is never activated: ``WindowFromPoint``
        returns NULL, and ``ChildWindowFromPointEx`` returns 0 even when a child
        covers the whole client area.  So the z-order is walked by hand and the
        descent uses child rectangles, which are reliable there.
        """

        for info in self.windows(include_untitled=True):
            left, top, right, bottom = info["rect"]
            if left <= screen_x < right and top <= screen_y < bottom:
                return self._descend_to_deepest(info["hwnd"], screen_x, screen_y)
        return 0

    def children_of(self, hwnd) -> list[int]:
        """Immediate child windows, topmost first (``GetWindow`` walks z-order)."""

        children: list[int] = []
        child = user32.GetWindow(hwnd, GW_CHILD)
        while child:
            children.append(int(child))
            child = user32.GetWindow(child, GW_HWNDNEXT)
        return children

    def _descend_to_deepest(self, hwnd: int, screen_x: int, screen_y: int) -> int:
        """Deepest visible, enabled child whose rectangle contains the point."""

        current = hwnd
        for _ in range(8):  # bounded: UI trees are not deeper than this in practice
            chosen = 0
            for child in self.children_of(current):
                if not user32.IsWindowVisible(child) or not user32.IsWindowEnabled(child):
                    continue
                rect = RECT()
                if not user32.GetWindowRect(child, ctypes.byref(rect)):
                    continue
                if rect.left <= screen_x < rect.right and rect.top <= screen_y < rect.bottom:
                    chosen = child
                    break
            if not chosen:
                break
            current = chosen
        return current

    def _client_point(self, hwnd, screen_x: int, screen_y: int) -> tuple[int, int]:
        point = POINT(screen_x, screen_y)
        user32.ScreenToClient(hwnd, ctypes.byref(point))
        return point.x, point.y

    def _pack(self, x: int, y: int) -> int:
        return ((y & 0xFFFF) << 16) | (x & 0xFFFF)

    def _post(self, hwnd, message: int, wparam: int = 0, lparam: int = 0) -> bool:
        return bool(user32.PostMessageW(hwnd, message, wparam, lparam))

    def click(self, x_1000, y_1000, *, button: str = "left", clicks: int = 1) -> dict:
        screen_x, screen_y = self._normalized_to_screen(x_1000, y_1000)
        target = self.window_at(screen_x, screen_y)
        if not target:
            raise RuntimeError(
                f"no window under ({screen_x}, {screen_y}) on desktop "
                f"{self.desktop_name!r}; the desktop may be empty"
            )
        client_x, client_y = self._client_point(target, screen_x, screen_y)
        point = self._pack(client_x, client_y)
        down, up, flag = {
            "left": (WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON),
            "right": (WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON),
            "middle": (WM_MBUTTONDOWN, WM_MBUTTONUP, MK_MBUTTON),
        }[button]

        self._post(target, WM_MOUSEMOVE, 0, point)
        for _ in range(clicks):
            self._post(target, down, flag, point)
            self._post(target, up, 0, point)
        self.focus_hwnd = target
        self.last_point = (screen_x, screen_y)
        record = {
            "op": "click",
            "x": x_1000,
            "y": y_1000,
            "target": target,
            "target_class": self.class_name(target),
        }
        self.actions.append(record)
        return {
            "target": target,
            "target_class": record["target_class"],
            "screen": [screen_x, screen_y],
            "client": [client_x, client_y],
            "clicks": clicks,
        }

    def hit_test_trace(self, x_1000, y_1000) -> dict:
        """Walk the same path :meth:`window_at` walks, reporting every step.

        Kept because hit-testing on a hidden desktop is the least obvious part of
        this module: ``WindowFromPoint`` returns NULL there, so the descent is done
        by hand and a wrong answer is otherwise invisible.
        """

        screen_x, screen_y = self._normalized_to_screen(x_1000, y_1000)
        steps: list[dict] = []
        result = 0
        for info in self.windows(include_untitled=True):
            left, top, right, bottom = info["rect"]
            inside = left <= screen_x < right and top <= screen_y < bottom
            steps.append(
                {
                    "hwnd": info["hwnd"],
                    "class": info["class"],
                    "title": info.get("title", ""),
                    "rect": info["rect"],
                    "contains_point": inside,
                }
            )
            if not inside:
                continue
            hwnd = info["hwnd"]
            while True:
                rect = RECT()
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                point = POINT(screen_x, screen_y)
                ok = user32.ScreenToClient(hwnd, ctypes.byref(point))
                children = [
                    {"hwnd": child, "class": self.class_name(child),
                     "visible": bool(user32.IsWindowVisible(child)),
                     "enabled": bool(user32.IsWindowEnabled(child))}
                    for child in self.children_of(hwnd)
                ]
                deeper = self._descend_to_deepest(hwnd, screen_x, screen_y)
                steps.append(
                    {
                        "descent": True,
                        "parent": hwnd,
                        "parent_class": self.class_name(hwnd),
                        "screen_to_client_ok": bool(ok),
                        "client": [point.x, point.y],
                        "children": children,
                        "descended_to": deeper,
                        "descended_class": self.class_name(deeper) if deeper else "",
                    }
                )
                if not deeper or deeper == hwnd:
                    result = hwnd
                    break
                hwnd = deeper
            break
        return {"screen": [screen_x, screen_y], "steps": steps, "resolved": result,
                "resolved_class": self.class_name(result) if result else ""}

    def class_name(self, hwnd) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buffer, 256)
        return buffer.value

    def move(self, x_1000, y_1000) -> dict:
        screen_x, screen_y = self._normalized_to_screen(x_1000, y_1000)
        target = self.window_at(screen_x, screen_y)
        if target:
            client_x, client_y = self._client_point(target, screen_x, screen_y)
            self._post(target, WM_MOUSEMOVE, 0, self._pack(client_x, client_y))
        self.last_point = (screen_x, screen_y)
        return {"target": target, "screen": [screen_x, screen_y]}

    def drag(self, x1, y1, x2, y2, *, duration_ms: int = 800) -> dict:
        start_x, start_y = self._normalized_to_screen(x1, y1)
        end_x, end_y = self._normalized_to_screen(x2, y2)
        target = self.window_at(start_x, start_y)
        if not target:
            raise RuntimeError("no window at the drag origin")
        client_x, client_y = self._client_point(target, start_x, start_y)
        self._post(target, WM_MOUSEMOVE, 0, self._pack(client_x, client_y))
        self._post(target, WM_LBUTTONDOWN, MK_LBUTTON, self._pack(client_x, client_y))
        steps = max(4, min(20, int(duration_ms / 40) or 4))
        for index in range(1, steps + 1):
            ratio = index / steps
            mixed_x = int(start_x + (end_x - start_x) * ratio)
            mixed_y = int(start_y + (end_y - start_y) * ratio)
            cx, cy = self._client_point(target, mixed_x, mixed_y)
            self._post(target, WM_MOUSEMOVE, MK_LBUTTON, self._pack(cx, cy))
            time.sleep(duration_ms / 1000.0 / steps)
        cx, cy = self._client_point(target, end_x, end_y)
        self._post(target, WM_LBUTTONUP, 0, self._pack(cx, cy))
        self.last_point = (end_x, end_y)
        return {"target": target, "end": [end_x, end_y]}

    def scroll(self, x_1000, y_1000, notches: int, *, horizontal: bool = False) -> dict:
        screen_x, screen_y = self._normalized_to_screen(x_1000, y_1000)
        target = self.window_at(screen_x, screen_y) or self.focus_hwnd
        if not target:
            raise RuntimeError("no window to scroll")
        client_x, client_y = self._client_point(target, screen_x, screen_y)
        message = WM_MOUSEHWHEEL if horizontal else WM_MOUSEWHEEL
        # the wheel messages take *screen* coordinates in lParam, unlike the
        # other mouse messages, so it is packed from the screen point directly.
        self._post(
            target, message, (notches * WHEEL_DELTA) << 16, self._pack(screen_x, screen_y)
        )
        return {"target": target, "notches": notches, "horizontal": bool(horizontal)}

    def type_text(self, text: str) -> dict:
        target = self.focus_hwnd or self._topmost_window()
        if not target:
            raise RuntimeError("nothing to type into: no window on this desktop")
        sent = 0
        for char in text:
            code = ord(char)
            if char == "\n":
                self._post(target, WM_KEYDOWN, 0x0D, 0x001C0001)
                self._post(target, WM_KEYUP, 0x0D, 0xC01C0001)
            elif char == "\t":
                self._post(target, WM_KEYDOWN, 0x09, 0x000F0001)
                self._post(target, WM_KEYUP, 0x09, 0xC00F0001)
            else:
                self._post(target, WM_CHAR, code, 1)
            sent += 1
            if sent % 32 == 0:
                time.sleep(0.01)
        self.actions.append({"op": "type", "target": target, "length": len(text)})
        return {"target": target, "characters": sent}

    def key(self, vk: int, *, extended: bool = False) -> dict:
        target = self.focus_hwnd or self._topmost_window()
        if not target:
            raise RuntimeError("nothing to send the key to: no window on this desktop")
        scan = _scan_code(vk)
        lparam_down = 1 | (scan << 16) | (0x01000000 if extended else 0)
        lparam_up = lparam_down | 0xC0000000
        self._post(target, WM_KEYDOWN, vk, lparam_down)
        self._post(target, WM_KEYUP, vk, lparam_up)
        return {"target": target, "vk": vk}

    def key_down(self, vk: int) -> dict:
        """Hold a key down without releasing it (pair with :meth:`key_up`)."""

        target = self.focus_hwnd or self._topmost_window()
        if not target:
            raise RuntimeError("nothing to send the key to: no window on this desktop")
        self._post(target, WM_KEYDOWN, vk, 1 | (_scan_code(vk) << 16))
        self.held_keys.append(vk)
        return {"target": target, "vk": vk, "held": list(self.held_keys)}

    def key_up(self, vk: int) -> dict:
        target = self.focus_hwnd or self._topmost_window()
        if not target:
            raise RuntimeError("nothing to send the key to: no window on this desktop")
        self._post(target, WM_KEYUP, vk, 0xC0000000 | 1 | (_scan_code(vk) << 16))
        if vk in self.held_keys:
            self.held_keys.remove(vk)
        return {"target": target, "vk": vk, "held": list(self.held_keys)}

    def copy_selection(self, *, wait: float = 0.35) -> dict:
        """Copy the selection to the clipboard, message-level first.

        ``WM_COPY`` is the reliable route in this mode: it needs no key state at
        all, while a posted Ctrl+C fails in applications that test ``GetKeyState``
        (a posted modifier does not update the global state — observed: Notepad
        ignored the posted chord and the clipboard stayed empty).  The key chord is
        still sent as a fallback for windows that have no edit control semantics,
        and the clipboard is read either way so the caller can tell what happened.
        """

        target = self.focus_hwnd or win32.focused_window() or self._topmost_window()
        if not target:
            raise RuntimeError("nothing to copy from: no window on this desktop")
        # Resolve the edit control: WM_COPY to a frame window does nothing, while
        # every edit control implements it (observed: the clipboard kept its old
        # content until the message went to the document control itself).
        resolved = self._edit_control(target)
        edit = resolved or target

        WM_COPY = 0x0301
        user32.SendMessageW(edit, WM_COPY, 0, 0)
        if not resolved:
            # Key fallback only when no edit control was found.  When one was, the
            # posted keys would carry no modifier state and type their letters —
            # measured: the "c" of Ctrl+C replaced the entire selection it was
            # supposed to copy, leaving a one-character document.
            for vk in (0x11, 0x43):  # Ctrl, C
                self._post(target, WM_KEYDOWN, vk, 1 | (_scan_code(vk) << 16))
            for vk in (0x43, 0x11):
                self._post(target, WM_KEYUP, vk, 0xC0000000 | 1 | (_scan_code(vk) << 16))
        time.sleep(wait)

        # The selection itself, sliced precisely: EM_GETSEL gives the range and
        # WM_GETTEXT the content.  This is what makes copy work on Win11 Notepad's
        # DirectWrite control, which ignores WM_COPY entirely (measured: the
        # clipboard kept its previous content while this returns the right text).
        selection = ""
        method = "clipboard"
        start = wintypes.DWORD()
        end = wintypes.DWORD()
        # EM_GETSEL takes two pointers in its LPARAM/WPARAM slots; SendMessageW's
        # integer-typed parameters want the addresses as plain values.
        user32.SendMessageW(edit, 0x00B0, ctypes.addressof(start), ctypes.addressof(end))
        full = self.window_text(edit, 16384)
        if end.value > start.value and full:
            selection = full[start.value:end.value]
            method = "EM_GETSEL"
        elif full:
            # Some controls (Win11 Notepad's DirectWrite control, measured) keep the
            # selection to themselves and report none.  Hand back the document text
            # and say so — the caller gets the content either way and the method
            # field tells it which path produced the answer.
            selection = full
            method = "window_text_fallback"
        text = selection or self.clipboard()
        return {"target": target, "edit": edit, "edit_class": self.class_name(edit),
                "method": method,
                "selection": [int(start.value), int(end.value)], "text": text,
                "full_text_len": len(full or "")}

    def hotkey(self, vks: list[int]) -> dict:
        """Chord, with the classic edit chords translated to semantic messages.

        Posted modifiers do not update the *global* key state, so an application
        that asks ``GetKeyState(VK_CONTROL)`` will not see Ctrl held — measured:
        Notepad ignored a posted Ctrl+A and nothing was selected.  For the four
        chords that edit controls implement as messages, send the message too
        (``EM_SETSEL`` / ``WM_COPY`` / ``WM_PASTE`` / ``WM_CUT``): those need no key
        state at all and work in every edit control.

        Everything else stays best effort: a posted chord reaches applications that
        act on the message alone (most dialogs and classic controls).
        """

        EDIT_CHORDS = {
            (0x11, 0x41): "select_all",  # Ctrl+A
            (0x11, 0x43): "copy",        # Ctrl+C
            (0x11, 0x56): "paste",       # Ctrl+V
            (0x11, 0x58): "cut",         # Ctrl+X
        }
        key = tuple(sorted(set(vks)))
        semantic = EDIT_CHORDS.get(key)
        target = self.focus_hwnd or win32.focused_window() or self._topmost_window()

        semantic_sent = None
        if semantic and target:
            edit = self._edit_control(target)
            if edit:
                if semantic == "select_all":
                    user32.SendMessageW(edit, 0x00B1, 0, -1)  # EM_SETSEL: all
                else:
                    user32.SendMessageW(edit, {"copy": 0x0301, "paste": 0x0302,
                                               "cut": 0x0300}[semantic], 0, 0)
                semantic_sent = semantic

        if not target:
            raise RuntimeError("nothing to send the chord to: no window on this desktop")
        if not semantic_sent:
            # Only fall back to posted keys when no semantic message applied: the
            # key messages carry no real modifier state, so the control treats the
            # letters as literal typing (measured: Ctrl+A and Ctrl+C left "ac" in
            # the document and destroyed the selection they were meant to make).
            for vk in vks:
                self._post(target, WM_KEYDOWN, vk, 1 | (_scan_code(vk) << 16))
            for vk in reversed(vks):
                self._post(target, WM_KEYUP, vk, 0xC0000000 | 1 | (_scan_code(vk) << 16))
        self.actions.append({"op": "hotkey", "vks": list(vks), "semantic": semantic_sent})
        return {"target": target, "vks": list(vks), "semantic": semantic_sent,
                "best_effort": not bool(semantic_sent)}

    def _descendants(self, hwnd: int) -> list[int]:
        """Every descendant window of ``hwnd`` (same enumeration as ``children``)."""

        return win32.descendants(hwnd)

    def _edit_control(self, hwnd: int) -> int:
        """The edit-like control that shares ``hwnd``'s window tree.

        Walking only downwards from ``hwnd`` misses the document control when the
        click landed on a sibling (measured on Win11 Notepad: the hit target was
        ``InputSiteWindowClass`` and the text control sat beside it, so the
        semantic Ctrl+A reached nothing).  Climb to the root first, then search the
        whole subtree.
        """

        user32.GetParent.restype = wintypes.HWND
        user32.GetParent.argtypes = [wintypes.HWND]
        root = hwnd
        for _ in range(16):
            parent = int(user32.GetParent(root) or 0)
            if not parent:
                break
            root = parent

        matches = []
        for candidate in [root, *self._descendants(root)]:
            name = self.class_name(candidate).casefold()
            if any(tag in name for tag in ("edit", "richedit")):
                matches.append(candidate)
        return matches[-1] if matches else 0

    def _topmost_window(self) -> int:
        for info in self.windows():
            return info["hwnd"]
        return 0

    def close_window(self, hwnd: int) -> dict:
        ok = self._post(hwnd, WM_CLOSE)
        return {"hwnd": hwnd, "posted": ok}

    def window_text(self, hwnd: int, limit: int = 4096) -> str:
        buffer = ctypes.create_unicode_buffer(limit + 1)
        user32.SendMessageW(hwnd, WM_GETTEXT, limit, ctypes.cast(buffer, ctypes.c_void_p).value)
        return buffer.value

    def set_window_text(self, hwnd: int, text: str) -> dict:
        user32.SendMessageW(hwnd, WM_SETTEXT, 0, ctypes.cast(ctypes.c_wchar_p(text), ctypes.c_void_p).value)
        return {"hwnd": hwnd, "set": True}

    # ------------------------------------------------------------------ #
    # Clipboard
    # ------------------------------------------------------------------ #

    def clipboard(self) -> str:
        return win32.clipboard_text()

    def set_clipboard(self, text: str) -> None:
        win32.set_clipboard_text(text)

    def paste_into(self, target: int, text: str, *, use_clipboard: bool = False) -> dict:
        """Put text into a specific window.

        Default is the clipboard-free path: type it, so the user's clipboard is
        never disturbed.  ``use_clipboard=True`` goes clipboard → ``WM_PASTE``, which
        is faster and needs no key state at all (the same reason copy uses
        ``WM_COPY``) — for edit controls it is the reliable route.
        """

        if use_clipboard:
            self.set_clipboard(text)
            WM_PASTE = 0x0302
            edit = self._edit_control(target) or target
            user32.SendMessageW(edit, WM_PASTE, 0, 0)
            time.sleep(0.2)
            return {"target": target, "edit": edit, "method": "WM_PASTE", "characters": len(text)}
        previous, self.focus_hwnd = self.focus_hwnd, target
        try:
            result = self.type_text(text)
            return {"target": target, "method": "typed", **result}
        finally:
            self.focus_hwnd = previous

    def status(self) -> dict:
        windows = self.windows()
        return {
            "mode": "stealth",
            "desktop": self.desktop_name,
            "active_input_desktop": win32.active_input_desktop_name(),
            "windows": len(windows),
            "focus_hwnd": self.focus_hwnd,
            "last_point": self.last_point,
            "screen": list(win32.virtual_screen()),
        }


_SCAN_CODES = {
    0x08: 0x0E, 0x09: 0x0F, 0x0D: 0x1C, 0x10: 0x2A, 0x11: 0x1D, 0x12: 0x38,
    0x1B: 0x01, 0x20: 0x39, 0x21: 0x49, 0x22: 0x51, 0x23: 0x4F, 0x24: 0x47,
    0x25: 0x4B, 0x26: 0x48, 0x27: 0x4D, 0x28: 0x50, 0x2D: 0x52, 0x2E: 0x53,
}


def _downscale_bgra(bgra: bytes, width: int, height: int, max_width: int) -> tuple[bytes, int, int]:
    """Nearest-neighbour downscale of a BGRA buffer, before encoding."""

    factor = max(1, (width + max_width - 1) // max_width)
    if factor <= 1:
        return bgra, width, height
    new_width = max(1, width // factor)
    new_height = max(1, height // factor)
    rows = bytearray()
    for y in range(new_height):
        source_row = (y * factor) * width * 4
        for x in range(new_width):
            offset = source_row + (x * factor) * 4
            rows.extend(bgra[offset : offset + 4])
    return bytes(rows), new_width, new_height


def _non_blank_bgra(bgra: bytes, width: int, height: int, *, threshold: int = 12, step: int = 16) -> float:
    """Fraction of sampled pixels that are not near-black, measured on raw BGRA."""

    lit = total = 0
    stride = width * 4
    for y in range(0, height, step):
        base = y * stride
        for x in range(0, stride, 4 * step):
            total += 1
            if (
                bgra[base + x] > threshold
                or bgra[base + x + 1] > threshold
                or bgra[base + x + 2] > threshold
            ):
                lit += 1
    return (lit / total) if total else 0.0


def _scan_code(vk: int) -> int:
    """``MapVirtualKeyW(vk, MAPVK_VK_TO_VSC)`` with a small built-in table.

    Posted key messages carry the scan code in their lParam; a wrong scan code makes
    applications type the wrong character.
    """

    if vk in _SCAN_CODES:
        return _SCAN_CODES[vk]
    return _user32_map(vk)


def _user32_map(vk: int) -> int:
    mapped = win32.user32.MapVirtualKeyW(vk, 0) if hasattr(win32.user32, "MapVirtualKeyW") else 0
    return mapped or 0


try:  # declared lazily so the module still imports if the symbol is missing
    win32.user32.MapVirtualKeyW.restype = wintypes.UINT
    win32.user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
except AttributeError:  # pragma: no cover
    pass

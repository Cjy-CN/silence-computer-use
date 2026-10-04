"""Takeover executor: real ``SendInput`` input, for the mode that switches the screen.

``stealth`` mode (the default) drives a hidden desktop with window messages, which
is what keeps the user's desktop untouched.  ``takeover`` mode switches the display
to the agent desktop and may therefore use the real input stack — that is the whole
point of accepting the intrusion.  Routing that mode through the message-based
executor would make the documentation a lie, so this adapter exists: same executor
interface as :class:`vdcu.stealth.StealthExecutor`, backed by
:mod:`vdcu.primitives` (SendInput + BitBlt), which is the code the offline self-test
and the earlier live probes already cover.

The mode is opt-in and needs the user's consent: it takes the screen away from them.
"""

from __future__ import annotations

import time

from . import primitives


class TakeoverExecutor:
    """Executor whose input is real hardware input (``SendInput``)."""

    def __init__(self, desktop_name: str = "default", hdesk=None):
        self.desktop_name = desktop_name
        self.hdesk = hdesk
        self.actions: list[dict] = []
        self.held_keys: list[int] = []
        primitives.use_desktop_context(desktop_name, hdesk)
        primitives.allow_input_without_screenshot()  # the host captures per action

    # -- capture ------------------------------------------------------- #

    def capture_composite(
        self, *, max_width: int | None = None, image_format: str = "png", quality: int = 88
    ) -> dict:
        """BitBlt of the real screen — the desktop is active in this mode."""

        frame = primitives.screenshot()
        data = bytes(frame)
        result = {
            "frame": data,
            "format": "png",
            "width": frame.width,
            "height": frame.height,
            "image_width": frame.width,
            "image_height": frame.height,
            "windows_painted": 0,
            "windows_total": len(primitives.windows()),
            "non_blank": 1.0,
        }
        if image_format == "jpeg":
            from . import jpeg

            result["frame"] = jpeg.encode_bgra(
                self._png_to_bgra(data), frame.width, frame.height, quality=quality
            )
            result["format"] = "jpeg"
        return result

    @staticmethod
    def _png_to_bgra(png_bytes: bytes) -> bytes:
        from . import png as png_module

        width, height, rgb = png_module.decode(png_bytes)
        bgra = bytearray(width * height * 4)
        bgra[0::4] = rgb[2::3]
        bgra[1::4] = rgb[1::3]
        bgra[2::4] = rgb[0::3]
        bgra[3::4] = b"\xff" * (width * height)
        return bytes(bgra)

    # -- pointer ------------------------------------------------------- #

    def click(self, x_1000, y_1000, *, button: str = "left", clicks: int = 1) -> dict:
        primitives.click(x_1000, y_1000, button=button, clicks=clicks)
        self.actions.append({"op": "click", "x": x_1000, "y": y_1000, "mode": "takeover"})
        return {"display_point": [x_1000, y_1000], "button": button, "clicks": clicks,
                "injected": "SendInput"}

    def move(self, x_1000, y_1000) -> dict:
        primitives.hover(x_1000, y_1000)
        return {"display_point": [x_1000, y_1000], "injected": "SendInput"}

    def drag(self, x1, y1, x2, y2, *, duration_ms: int = 800) -> dict:
        primitives.drag(x1, y1, x2, y2, duration_ms=duration_ms)
        return {"from": [x1, y1], "to": [x2, y2], "injected": "SendInput"}

    def scroll(self, x_1000, y_1000, notches: int, *, horizontal: bool = False) -> dict:
        primitives.scroll(x_1000, y_1000, notches, horizontal=horizontal)
        return {"notches": notches, "horizontal": bool(horizontal),
                "injected": "SendInput"}

    # -- keyboard ------------------------------------------------------ #

    def type_text(self, text: str) -> dict:
        primitives.type_text(str(text))
        self.actions.append({"op": "type", "length": len(str(text)), "mode": "takeover"})
        return {"characters": len(str(text)), "injected": "SendInput"}

    def key(self, virtual_key: int, *, extended: bool = False) -> dict:
        # ``primitives`` speaks key *names*; the caller already resolved the VK, so
        # send it through the raw input path directly.
        from . import win32

        win32.send_inputs(
            [win32.key_input(virtual_key), win32.key_input(virtual_key, up=True)]
        )
        return {"vk": virtual_key, "injected": "SendInput"}

    def key_down(self, virtual_key: int) -> dict:
        from . import win32

        win32.send_inputs([win32.key_input(virtual_key)])
        self.held_keys.append(virtual_key)
        return {"vk": virtual_key, "held": list(self.held_keys), "injected": "SendInput"}

    def key_up(self, virtual_key: int) -> dict:
        from . import win32

        win32.send_inputs([win32.key_input(virtual_key, up=True)])
        if virtual_key in self.held_keys:
            self.held_keys.remove(virtual_key)
        return {"vk": virtual_key, "held": list(self.held_keys), "injected": "SendInput"}

    def hotkey(self, virtual_keys: list[int]) -> dict:
        from . import win32

        events = [win32.key_input(vk) for vk in virtual_keys]
        events += [win32.key_input(vk, up=True) for vk in reversed(virtual_keys)]
        win32.send_inputs(events)
        return {"vks": list(virtual_keys), "injected": "SendInput"}

    def copy_selection(self, *, wait: float = 0.35) -> dict:
        primitives.hotkey("ctrl", "c")
        time.sleep(wait)
        return {"text": primitives.get_clipboard()}

    # -- clipboard / windows ------------------------------------------- #

    def clipboard(self) -> str:
        return primitives.get_clipboard()

    def set_clipboard(self, text: str) -> None:
        primitives.set_clipboard(text)

    def windows(self, *, include_untitled: bool = False) -> list[dict]:
        return primitives.windows()

    def hit_test_trace(self, x_1000, y_1000) -> dict:
        """Interface parity with the stealth executor.

        Hit-testing exists there because ``WindowFromPoint`` fails on a hidden
        desktop; the real input stack does not need it, so this just reports where
        the point lands among the windows it can see.
        """

        screen_x, screen_y = self._to_screen(x_1000, y_1000)
        for window in self.windows(include_untitled=True):
            left, top, right, bottom = window["rect"]
            if left <= screen_x < right and top <= screen_y < bottom:
                return {"screen": [screen_x, screen_y], "resolved": window["hwnd"],
                        "resolved_class": window["class"], "mode": "takeover"}
        return {"screen": [screen_x, screen_y], "resolved": 0, "resolved_class": "", "mode": "takeover"}

    @staticmethod
    def _to_screen(x_1000: float, y_1000: float) -> tuple[int, int]:
        from . import win32

        left, top, width, height = win32.virtual_screen()
        return (
            int(left + (x_1000 / 1000.0) * (width - 1)),
            int(top + (y_1000 / 1000.0) * (height - 1)),
        )

    def close_window(self, hwnd: int) -> dict:
        # A window message is the polite way to ask; SendInput cannot address one.
        from . import win32

        WM_CLOSE = 0x0010
        ok = bool(win32.user32.PostMessageW(hwnd, WM_CLOSE, 0, 0))
        return {"hwnd": hwnd, "posted": ok}

    # -- window inspection (interface parity) --------------------------- #
    # The stealth executor implements these by hand because ``WindowFromPoint``
    # fails on a hidden desktop.  Here the desktop is active and the real input
    # stack is available, so the native hit-testing works; the rest is the same
    # thin Win32 wrapper.

    def class_name(self, hwnd) -> str:
        import ctypes

        from . import win32

        buffer = ctypes.create_unicode_buffer(256)
        win32.user32.GetClassNameW(hwnd, buffer, 256)
        return buffer.value

    def children_of(self, hwnd) -> list[int]:
        from . import win32

        GW_CHILD, GW_HWNDNEXT = 5, 2
        children: list[int] = []
        child = win32.user32.GetWindow(hwnd, GW_CHILD)
        while child:
            children.append(int(child))
            child = win32.user32.GetWindow(child, GW_HWNDNEXT)
        return children

    def window_at(self, screen_x: int, screen_y: int) -> int:
        """Deepest window under a screen point — the native way, which works here."""

        import ctypes

        from . import win32

        class POINT(ctypes.Structure):
            _fields_ = (("x", ctypes.c_long), ("y", ctypes.c_long))

        point = POINT(screen_x, screen_y)
        hwnd = win32.user32.WindowFromPoint(ctypes.byref(point))
        if not hwnd:
            return 0
        while True:
            client = POINT(screen_x, screen_y)
            win32.user32.ScreenToClient(hwnd, ctypes.byref(client))
            child = win32.user32.ChildWindowFromPointEx(
                hwnd, ctypes.byref(client), 0x0001 | 0x0002  # skip invisible, disabled
            )
            if not child or int(child) == int(hwnd):
                return int(hwnd)
            hwnd = child

    def window_text(self, hwnd: int, limit: int = 4096) -> str:
        import ctypes

        from . import win32

        buffer = ctypes.create_unicode_buffer(limit + 1)
        WM_GETTEXT = 0x000D
        win32.user32.SendMessageW(
            hwnd, WM_GETTEXT, limit, ctypes.cast(buffer, ctypes.c_void_p).value
        )
        return buffer.value

    def set_window_text(self, hwnd: int, text: str) -> dict:
        import ctypes

        from . import win32

        WM_SETTEXT = 0x000C
        win32.user32.SendMessageW(
            hwnd, WM_SETTEXT, 0, ctypes.cast(ctypes.c_wchar_p(text), ctypes.c_void_p).value
        )
        return {"hwnd": hwnd, "set": True}

    def paste_into(self, target: int, text: str, *, use_clipboard: bool = False) -> dict:
        # Real input is available in this mode, so typing is the reliable route;
        # the keyword exists for interface parity with the stealth executor.
        previous = getattr(self, "_focus_hwnd", 0)
        self._focus_hwnd = target
        try:
            return self.type_text(text)
        finally:
            self._focus_hwnd = previous

    def status(self) -> dict:
        return {
            "mode": "takeover",
            "desktop": self.desktop_name,
            "active_input_desktop": win32_active_input_desktop(),
            "injection": "SendInput",
        }


def win32_active_input_desktop() -> str:
    from . import win32

    return win32.active_input_desktop_name()

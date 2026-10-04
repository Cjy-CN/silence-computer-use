"""Live, window-scoped session preview with neutral virtual desktop naming.

The pip view is read-only; hiding it keeps the session running. Its frame source
is the isolated session, not a capture of the user's active desktop.
"""

from __future__ import annotations

import base64
import io
import json
import queue
import threading
import time
import tkinter as tk
import urllib.error
import urllib.request

POLL_SECONDS = 0.4
# 16:9 layout — a wrong aspect clipped the bottom of the frame (the taskbar)
ASPECT = 1080 / 1920  # 0.5625
# the desktop is captured at exactly the display width (the daemon resamples
# BGRA *before* encoding, so a 1280-wide window gets a crisp 1280 frame);
# 原始 fetches the native 1920×1080 frame pixel-for-pixel
NATIVE_WIDTH = 1920
DEFAULT_WIDTH = 1280

# Neutral preview palette.
ACCENT = "#1070f0"
ACCENT_DEEP = "#0b57c9"
CARD_BG = "#ffffff"
CARD_EDGE = "#ebebeb"
APP_BG = "#f9f9f9"
PILL_BG = "#f0f0f0"
PILL_BG_HOVER = "#e6e6e6"
PILL_TEXT = "#111111"
TEXT = "#000000"
TEXT_MUTED = "#8a8a8a"
FRAME_BG = "#0f1115"
TRANSPARENT_KEY = "#ff00fe"  # corner-cut colour key
FONT = "Microsoft YaHei UI"
CORNER = 16


PIP_CAPSULE_BG = "#404040"   # dark capsule like Shadow's pip
PIP_CAPSULE_TEXT = "#ffffff"


class FrameFetcher(threading.Thread):
    """Pull frames on a timer and hand them to the UI thread."""

    def __init__(self, url: str, token: str, out: queue.Queue):
        super().__init__(daemon=True)
        self.url = url.rstrip("/")
        self.token = token
        self.out = out
        self.width = 720
        self.paused = threading.Event()
        self.stopped = threading.Event()
        self.failures = 0

    def set_width(self, width: int) -> None:
        # fetch at the display width (native 1920 at most): capture-time
        # resampling keeps text sharp; Tk's integer subsample would blur
        self.width = max(320, min(NATIVE_WIDTH, int(width)))

    def run(self) -> None:
        while not self.stopped.is_set():
            if self.paused.is_set():
                time.sleep(0.2)
                continue
            started = time.time()
            try:
                request = urllib.request.Request(
                    f"{self.url}/frame.png?w={self.width}&token={self.token}&t={time.time()}"
                )
                with urllib.request.urlopen(request, timeout=10) as response:
                    data = response.read()
                self.failures = 0
                self.out.put(("frame", data))
            except (urllib.error.URLError, OSError, TimeoutError) as error:
                self.failures += 1
                self.out.put(("error", str(error)))
                if self.failures > 12:
                    self.out.put(("dead", "session appears to be gone"))
                    return
            delay = max(0.05, POLL_SECONDS - (time.time() - started))
            time.sleep(delay)


class PreviewBox:
    def __init__(self, url: str, token: str, title: str, *, width: int = DEFAULT_WIDTH,
                 style: str = "card"):
        self.url = url.rstrip("/")
        self.token = token
        self.width = width
        self.style = style  # "card" (header + pills) | "pip" (Shadow's floating view)
        self.events: queue.Queue = queue.Queue()
        self.fetcher = FrameFetcher(url, token, self.events)

        self.root = tk.Tk()
        if style == "pip":
            # 运行时的悬浮预览是纯浮层：无任务栏按钮、标题干净不带会话 ID
            self.root.title("virtual desktop")
            self.root.attributes("-toolwindow", True)
        else:
            self.root.title("virtual desktop")
        self.root.overrideredirect(True)          # the card draws its own chrome
        self.root.attributes("-topmost", True)
        # Deliberately NO ``-transparentcolor``: a colour-keyed layered window on this
        # Tk build draws the canvas shapes but silently drops canvas *images*, so the
        # card showed an empty dark rectangle with the desktop nowhere in it.  Square
        # corners are the price of a preview that actually shows the screen.
        self.root.configure(bg=CARD_BG)
        self.root.minsize(200, 120)

        self.canvas = tk.Canvas(self.root, bg=CARD_BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        self.image: tk.PhotoImage | None = None
        self.scaled: tk.PhotoImage | None = None
        self.display_factor = 1
        self._resize_job: str | None = None
        self._drag_origin: tuple[int, int] | None = None
        self._hover: str | None = None
        self._mouse_inside = False

        self.root.bind("<Configure>", self._on_configure)
        self.root.bind("<Enter>", lambda _e: self._set_mouse_inside(True))
        self.root.bind("<Leave>", lambda _e: self._set_mouse_inside(False))
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<Motion>", self._move)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.root.bind("<Control-plus>", lambda _e: self.zoom(1.25))
        self.root.bind("<Control-equal>", lambda _e: self.zoom(1.25))
        self.root.bind("<Control-minus>", lambda _e: self.zoom(1 / 1.25))
        self.root.bind("<Control-Key-0>", lambda _e: self.show_native())
        self.root.bind("<Escape>", lambda _e: self.root.withdraw())
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

    def _set_mouse_inside(self, inside: bool) -> None:
        if inside != self._mouse_inside:
            self._mouse_inside = inside
            if self.style == "pip":
                self._redraw()

    # ------------------------------------------------------------------ #
    # Drawing: the card, the header, the pill buttons
    # ------------------------------------------------------------------ #

    def _redraw(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        width = max(canvas.winfo_width(), 2)
        height = max(canvas.winfo_height(), 2)

        if self.style == "pip":
            # 运行时画中画：内容铺满整个窗口，右上角半透明深色胶囊按钮（常驻）
            canvas.create_rectangle(0, 0, width, height, fill=CARD_BG, width=0)
            self._round_rect(2, 2, width - 2, height - 2, CORNER, fill=CARD_BG, outline=CARD_EDGE)
            if self.image is not None:
                image = self.image
                factor = max(1, -(-image.width() // max(width, 1)),
                             -(-image.height() // max(height, 1)))
                if factor > 1:
                    if self.display_factor != factor or self.scaled is None:
                        self.scaled = image.subsample(factor, factor)
                        self.display_factor = factor
                    image = self.scaled
                else:
                    self.scaled = None
                    self.display_factor = 1
                self.canvas.create_image(width // 2, height // 2, image=image)
            else:
                canvas.create_text(width // 2, height // 2, text="连接中…",
                                   fill=TEXT_MUTED, font=(FONT, 9))

            # 胶囊按钮：半透明深色底 + 白图标，压在内容右上角
            x = width - 8
            for name in ("✕", "⧉"):
                x = self._pill(x - 4, 8, name, primary=False, capsule=True)
            return

        # rounded body
        canvas.create_rectangle(0, 0, width, height, fill=CARD_BG, width=0)
        self._round_rect(2, 2, width - 2, height - 2, CORNER, fill=CARD_BG, outline=CARD_EDGE)

        # header: accent mark + title + status + pills
        self._round_rect(14, 12, 30, 28, 8, fill=ACCENT, outline="")
        canvas.create_text(38, 20, anchor="w", text="虚拟桌面实况",
                           fill=TEXT, font=(FONT, 10, "bold"))
        canvas.create_text(38, 36, anchor="w", text=self._status_text(),
                           fill=TEXT_MUTED, font=(FONT, 8))

        x = width - 12
        for name in ("退出", "暂停", "原始", "＋", "－"):
            x = self._pill(x - 6, 12, name, primary=(name == "退出"))

        # live frame area — sized to the image's 16:9 so nothing is clipped
        top = 52
        self._round_rect(10, top, width - 10, height - 10, 10, fill=FRAME_BG, outline="")
        if self.image is not None:
            area_w, area_h = width - 20, height - 10 - top
            image = self.image
            factor = max(
                1,
                -(-image.width() // max(area_w, 1)),
                -(-image.height() // max(area_h, 1)),
            )
            if factor > 1:
                # keep the subsampled copy referenced on self: a locally-created
                # PhotoImage is garbage-collected and the canvas then draws nothing
                if self.display_factor != factor or self.scaled is None:
                    self.scaled = image.subsample(factor, factor)
                    self.display_factor = factor
                image = self.scaled
            else:
                self.scaled = None
                self.display_factor = 1
            self.canvas.create_image(width // 2, (top + height - 10) // 2, image=image)
        else:
            canvas.create_text(width // 2, (top + height) // 2, text="连接中…",
                               fill=TEXT_MUTED, font=(FONT, 9))

    def _round_rect(self, x1: int, y1: int, x2: int, y2: int, radius: int, **style) -> int:
        points = [
            x1 + radius, y1,
            x2 - radius, y1, x2, y1, x2, y1 + radius,
            x2, y2 - radius, x2, y2, x2 - radius, y2,
            x1 + radius, y2, x1, y2, x1, y2 - radius,
            x1, y1 + radius, x1, y1, x1 + radius, y1,
        ]
        return self.canvas.create_polygon(points, smooth=True, **style)

    def _pill(self, right_edge: int, y: int, label: str, *, primary: bool,
              capsule: bool = False) -> int:
        font = (FONT, 8, "bold" if primary else "normal")
        text_width = max(self._text_width(label, font), 30)
        height = 24
        left = right_edge - text_width - 16
        hovered = self._hover == label
        if capsule:
            fill = PIP_CAPSULE_BG
            text_fill = PIP_CAPSULE_TEXT
        elif primary:
            fill = ACCENT_DEEP if hovered else ACCENT
            text_fill = "#ffffff"
        else:
            fill = PILL_BG_HOVER if hovered else PILL_BG
            text_fill = PILL_TEXT
        self._round_rect(left, y, right_edge, y + height, height // 2,
                         fill=fill, outline="")
        self.canvas.create_text((left + right_edge) / 2, y + height / 2, text=label,
                                fill=text_fill, font=font)
        return left

    def _text_width(self, text: str, font) -> int:
        try:
            return int(self.canvas.font_measure(font, text))
        except Exception:  # noqa: BLE001 - fall back to an estimate
            return 9 * len(text)

    def _status_text(self) -> str:
        if self.fetcher.paused.is_set():
            return "已暂停"
        if self.image is not None:
            return f"{self.image.width()}×{self.image.height()} · {time.strftime('%H:%M:%S')}"
        return "连接中…"

    # ------------------------------------------------------------------ #
    # Interaction: drag by the header, pills by click
    # ------------------------------------------------------------------ #

    def _pill_at(self, x: int, y: int) -> str | None:
        width = max(self.canvas.winfo_width(), 2)
        if self.style == "pip":
            # capsule buttons are always visible in pip mode (user requirement)
            cursor = width - 8
            for name in ("✕", "⧉"):
                font = (FONT, 8)
                text_width = max(self._text_width(name, font), 30)
                left = cursor - 4 - text_width - 16
                right = cursor - 4
                if left <= x <= right and 8 <= y <= 32:
                    return name
                cursor = left + 4
            return None
        if not 8 <= y <= 42:
            return None
        cursor = width - 12
        for name in ("退出", "暂停", "原始", "＋", "－"):
            font = (FONT, 8, "bold" if name == "退出" else "normal")
            text_width = max(self._text_width(name, font), 30)
            left = cursor - 6 - text_width - 16
            right = cursor - 6
            if left <= x <= right:
                return name
            cursor = left + 6
        return None

    def _press(self, event) -> None:
        hit = self._pill_at(event.x, event.y)
        if hit:
            self._activate(hit)
            return
        self._drag_origin = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _activate(self, label: str) -> None:
        actions = {
            "退出": self.stop_session,
            "暂停": self.toggle_pause,
            "原始": self.show_native,
            "＋": lambda: self.zoom(1.25),
            "－": lambda: self.zoom(1 / 1.25),
            # pip 形态（对齐运行时悬浮窗）：✕ 只是收起小窗（会话继续），⤢ 打开大视图
            "✕": self.root.withdraw,
            "⧉": self.open_live_page,
        }
        actions[label]()

    def open_live_page(self) -> None:
        """⤢: re-show the SDK's big 「virtual desktop」 window (Shadow's pop-out),
        falling back to the browser live page when the session can't."""
        try:
            body = b'{"token":"' + self.token.encode() + b'","op":"show_view"}'
            request = urllib.request.Request(
                f"{self.url}/rpc", data=body, headers={"Content-Type": "application/json"}
            )
            response = json.loads(urllib.request.urlopen(request, timeout=8).read())
            if response.get("ok"):
                return
        except Exception:  # noqa: BLE001 - fall back to the browser page
            pass
        import webbrowser
        webbrowser.open(f"{self.url}/?token={self.token}")

    def _move(self, event) -> None:
        hover = self._pill_at(event.x, event.y)
        if hover != self._hover:
            self._hover = hover
            self._redraw()

    def _release(self, _event) -> None:
        self._drag_origin = None

    def _drag(self, event) -> None:
        if not self._drag_origin:
            return
        self.root.geometry(f"+{event.x_root - self._drag_origin[0]}+{event.y_root - self._drag_origin[1]}")

    def _on_configure(self, event) -> None:
        if event.widget is not self.root:
            return
        self._redraw()
        self.set_width(max(200, event.width - 20), debounce=True)

    # ------------------------------------------------------------------ #
    # Behaviour
    # ------------------------------------------------------------------ #

    def set_width(self, width: int, *, debounce: bool = False) -> None:
        self.width = max(320, min(NATIVE_WIDTH, int(width)))
        self.fetcher.set_width(self.width)
        if debounce:
            if self._resize_job:
                self.root.after_cancel(self._resize_job)
            self._resize_job = self.root.after(220, lambda: self.fetcher.set_width(self.width))

    def show_native(self) -> None:
        """Pixel-for-pixel 1080p: fetch the native frame, grow the card to fit."""
        limit = self.root.winfo_screenwidth() - 40
        width = min(NATIVE_WIDTH, limit)
        self.set_width(width)
        extra = 4 if self.style == "pip" else 64
        self.root.geometry(f"{width}x{int(width * ASPECT) + extra}")
        self._redraw()

    def zoom(self, factor: float) -> None:
        new_width = int((self.root.winfo_width() or 960) * factor)
        extra = 4 if self.style == "pip" else 64
        height = int(new_width * ASPECT) + extra
        self.root.geometry(f"{new_width}x{height}")
        self._redraw()

    def toggle_pause(self) -> None:
        if self.fetcher.paused.is_set():
            self.fetcher.paused.clear()
        else:
            self.fetcher.paused.set()
        self._redraw()

    def stop_session(self) -> None:
        """Ask the daemon to shut the session down — the card is the user's off switch."""

        try:
            body = b'{"token":"' + self.token.encode() + b'","op":"stop"}'
            request = urllib.request.Request(
                f"{self.url}/rpc", data=body, headers={"Content-Type": "application/json"}
            )
            urllib.request.urlopen(request, timeout=8).read()
        except Exception:  # noqa: BLE001 - the session may already be gone
            pass
        self.quit()

    def quit(self) -> None:
        self.fetcher.stopped.set()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #

    def _drain(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "frame":
                    self._show(payload)
                elif kind == "dead":
                    # the session is gone: close instead of lingering on the user's
                    # desktop (a preview card that outlived its session annoyed them)
                    self.quit()
                    return
        except queue.Empty:
            pass
        self.root.after(120, self._drain)

    def _show(self, png_bytes: bytes) -> None:
        try:
            self.image = tk.PhotoImage(data=base64.b64encode(png_bytes))
        except tk.TclError:
            return
        self._redraw()

    def run(self) -> int:
        self.fetcher.start()
        self.root.after(120, self._drain)
        self.root.after(60, self._redraw)
        if self.style == "pip":
            def place_pip():
                width = self.width
                # 16:9 content + top band (buttons) + margins — Shadow's pip card
                height = int(width * ASPECT) + 52
                screen_w = self.root.winfo_screenwidth()
                screen_h = self.root.winfo_screenheight()
                self.root.geometry(f"{width}x{height}+{screen_w - width - 24}+{screen_h - height - 64}")
            self.root.after(150, place_pip)
        else:
            self.root.after(150, lambda: self.root.geometry(f"{self.width}x{int(self.width * ASPECT) + 64}"))
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            self.fetcher.stopped.set()
        return 0


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="vdcu preview", description="live preview card")
    parser.add_argument("--url", required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--title", default="vdcu")
    parser.add_argument("--style", choices=("card", "pip"), default="card",
                        help="card = header + pills; pip = Shadow-style floating view")
    parser.add_argument("--width", type=int, default=None,
                        help="window width; frames are captured at this width (原始 = native 1080p)")
    options = parser.parse_args(argv)
    width = options.width
    if width is None:
        width = 480 if options.style == "pip" else DEFAULT_WIDTH
    return PreviewBox(options.url, options.token, options.title,
                      width=width, style=options.style).run()


if __name__ == "__main__":
    raise SystemExit(main())

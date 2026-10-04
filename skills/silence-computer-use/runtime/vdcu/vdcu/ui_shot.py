"""Capture the current session's preview window and report its palette.

Window-scoped capture does not include the user's desktop behind the preview.
"""

from __future__ import annotations

import argparse
import collections
import ctypes
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vdcu import png, win32  # noqa: E402

win32.enable_dpi_awareness()

WM_PRINT = 0x0317
WM_PRINTCLIENT = 0x0318
PRF_CLIENT = 0x00000004
PRF_CHILDREN = 0x00000010
PW_RENDERFULLCONTENT = 2

def find_preview_windows() -> list[dict]:
    """Capture only the current skill session's preview, never unrelated apps."""
    from .session import load_state
    state = load_state() or {}
    pid = int(state.get("worker_pid") or 0)
    if not pid:
        return []
    return [w for w in win32.desktop_windows()
            if int(w.get("pid") or 0) == pid and w.get("title") == "virtual desktop"]


def capture_window(hwnd: int) -> tuple[int, int, bytes] | None:
    """Window pixels as top-down BGRA (window-scoped; the desktop is not read)."""

    rect = win32.user32.GetWindowRect
    class RECT(ctypes.Structure):
        _fields_ = (("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long))
    box = RECT()
    if not rect(wintypes_hwnd(hwnd), ctypes.byref(box)):
        return None
    width, height = int(box.right - box.left), int(box.bottom - box.top)
    if width <= 0 or height <= 0:
        return None

    user32 = win32.user32
    gdi32 = _gdi32()
    _declare()
    hdc = user32.GetWindowDC(wintypes_hwnd(hwnd))
    memdc = gdi32.CreateCompatibleDC(hdc)
    bitmap = gdi32.CreateCompatibleBitmap(hdc, width, height)
    previous = gdi32.SelectObject(memdc, bitmap)
    # PrintWindow with PW_RENDERFULLCONTENT renders modern (DirectComposition)
    # windows that BitBlt misses — the same reason the stealth compositor uses it.
    user32.PrintWindow(wintypes_hwnd(hwnd), memdc, PW_RENDERFULLCONTENT)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = (("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long), ("biPlanes", ctypes.c_uint16),
                    ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                    ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", ctypes.c_uint32),
                    ("biClrImportant", ctypes.c_uint32))
    header = BITMAPINFOHEADER()
    header.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    header.biWidth = width
    header.biHeight = -height  # top-down
    header.biPlanes = 1
    header.biBitCount = 32
    buffer = ctypes.create_string_buffer(width * height * 4)
    # 32bpp DIBs carry no colour table, so the header is the whole BITMAPINFO;
    # declare the prototype loosely because ctypes wants LP_BITMAPINFO otherwise.
    gdi32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                                ctypes.c_uint, ctypes.c_void_p, ctypes.c_void_p,
                                ctypes.c_uint]
    gdi32.GetDIBits(memdc, bitmap, 0, height, buffer, ctypes.byref(header), 0)

    gdi32.SelectObject(memdc, previous)
    gdi32.DeleteObject(bitmap)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(wintypes_hwnd(hwnd), hdc)
    return width, height, buffer.raw


def wintypes_hwnd(value: int):
    from ctypes import wintypes

    return wintypes.HWND(value)


def _declare() -> None:
    """64-bit-safe prototypes: the defaults truncate handles to c_int."""

    from ctypes import wintypes

    user32, gdi32 = win32.user32, _gdi32()
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowDC.argtypes = [wintypes.HWND]
    user32.GetWindowDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL


def _gdi32():
    import ctypes as _ctypes

    return getattr(win32, "gdi32", None) or _ctypes.WinDLL("gdi32")


def palette(bgra: bytes, *, samples: int = 4096) -> list[list[int]]:
    """Dominant colours of the capture (quantised to 16 levels per channel)."""

    counter: collections.Counter = collections.Counter()
    step = max(1, len(bgra) // 4 // samples)
    for index in range(0, len(bgra) // 4, step):
        base = index * 4
        b, g, r = bgra[base], bgra[base + 1], bgra[base + 2]
        key = (r // 16 * 16, g // 16 * 16, b // 16 * 16)
        counter[key] += 1
    total = sum(counter.values()) or 1
    return [[r, g, b, round(count / total, 4)] for (r, g, b), count in counter.most_common(12)]


def accent_from(palette_rows: list[list[int]]) -> list[int]:
    """The saturated colour that appears most — the brand accent."""

    best, best_score = [0, 0, 0], -1.0
    for r, g, b, share in palette_rows:
        saturation = max(r, g, b) - min(r, g, b)
        score = saturation * share
        if score > best_score and saturation > 40:
            best, best_score = [r, g, b], score
    return best


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vdcu ui-shot", description="capture the live session preview")
    parser.add_argument("--out", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ui_reference"))
    parser.add_argument("--hwnd", type=int, default=0, help="capture this window instead of searching")
    options = parser.parse_args(argv)

    windows = find_preview_windows()
    if options.hwnd:
        windows = [{"pid": 0, "title": f"hwnd={options.hwnd}", "hwnd": options.hwnd, "class": ""}]
    if not windows:
        print(json.dumps({
            "ok": False,
            "error": "no current session preview found; start the session first",
        }, ensure_ascii=False, indent=2))
        return 1

    os.makedirs(options.out, exist_ok=True)
    reports = []
    for entry in windows:
        hwnd = int(entry.get("hwnd") or 0)
        capture = capture_window(hwnd) if hwnd else None
        if not capture:
            reports.append({**entry, "ok": False, "error": "window capture failed"})
            continue
        width, height, bgra = capture
        rows = png.bgra_to_rgb_rows(bgra, width, height)
        image_path = os.path.join(options.out, f"reference_{hwnd}.png")
        with open(image_path, "wb") as handle:
            handle.write(png.encode_rgb(rows, width, height))
        colors = palette(bgra)
        tokens = {
            "accent": accent_from(colors),
            "palette": colors[:8],
            "size": [width, height],
        }
        with open(os.path.join(options.out, f"tokens_{hwnd}.json"), "w", encoding="utf-8") as handle:
            json.dump(tokens, handle, ensure_ascii=False, indent=2)
        reports.append({**entry, "ok": True, "image": image_path,
                        "size": [width, height], "tokens": tokens})
    print(json.dumps({"ok": True, "windows": reports, "out": options.out},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

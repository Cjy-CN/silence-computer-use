"""Computer-use primitives, implemented against the Win32 layer.

This module is the action vocabulary the agent drives: capture, pointer, keyboard,
clipboard and application launching.  It is a from-scratch implementation, but it
deliberately keeps the *contract* that the bundled runtime's own computer-use library
exposes to its agent, because that contract is what makes a skill portable:

* pointer coordinates are normalized to **0-1000** over the desktop, not pixels;
* a screenshot is required before pointer input (``CU_SCREENSHOT_REQUIRED``);
* ``list_apps()`` must precede ``launch_app()`` and names are matched exactly;
* ``scroll`` accepts ``-10..10`` or the aliases ``up/down/left/right``;
* failures carry a stable ``.code`` instead of only a message.

Only ``launch_app`` and ``windows()`` need to know which desktop they act on, so
they read :func:`use_desktop_context` — set once by the worker process that lives
on the virtual desktop.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import threading
import time
import unicodedata

from . import png as png_module
from . import win32

__all__ = [
    "CodedError",
    "App",
    "Frame",
    "Record",
    "click",
    "copy_selection",
    "drag",
    "get_clipboard",
    "hotkey",
    "hover",
    "key_down",
    "key_up",
    "launch_app",
    "left_double",
    "list_apps",
    "press",
    "right_single",
    "screenshot",
    "scroll",
    "set_clipboard",
    "triple_click",
    "type_text",
    "wait",
    "windows",
]

MAX_COORDINATE = 1000
TYPABLE_LIMIT = 200

_HEADLESS_POWERSHELL = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# --------------------------------------------------------------------------- #
# Errors and result containers
# --------------------------------------------------------------------------- #


class CodedError(Exception):
    """An error carrying a stable machine-readable ``.code``."""

    def __init__(self, operation: str, code: str, detail: str):
        self.operation = operation
        self.code = code
        self.detail = detail
        super().__init__(f"vdcu.{operation}: [{code}] {detail}")


class Record(dict):
    """Dictionary whose keys are also readable as attributes."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError as error:
            raise AttributeError(name) from error


class App(Record):
    def __repr__(self) -> str:
        return f"App(name={self.get('name')!r})"


class AppCatalog(list):
    def __repr__(self) -> str:
        preview = list.__repr__(self[:20])
        if len(self) <= 20:
            return preview
        return f"{preview[:-1]}, ... {len(self) - 20} more]"


class Frame(bytes):
    """PNG bytes carrying the captured desktop dimensions."""

    def __new__(cls, data: bytes, width: int, height: int):
        frame = super().__new__(cls, data)
        frame.width = int(width)
        frame.height = int(height)
        return frame

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height

    def save(self, target) -> None:
        if hasattr(target, "write"):
            target.write(bytes(self))
            return
        with open(target, "wb") as output:
            output.write(bytes(self))

    def __repr__(self) -> str:
        return f"<Frame {self.width}x{self.height}, {len(self)} bytes PNG>"


# --------------------------------------------------------------------------- #
# Session state: the observe-before-acting guard, and the desktop context
# --------------------------------------------------------------------------- #

_STATE = threading.local()
_NO_GUARD = os.environ.get("VDCU_NO_GUARD") == "1"

_DESKTOP_CONTEXT: dict = {}


def use_desktop_context(name: str, handle=None) -> None:
    """Bind the desktop that ``launch_app`` and ``windows()`` target."""

    _DESKTOP_CONTEXT["name"] = name
    _DESKTOP_CONTEXT["handle"] = handle


def desktop_context() -> dict:
    return dict(_DESKTOP_CONTEXT)


def _state() -> dict:
    state = getattr(_STATE, "value", None)
    if state is None:
        state = {"observed": False, "frame": None, "catalog": None}
        _STATE.value = state
    return state


def reset_session() -> None:
    _STATE.value = {"observed": False, "frame": None, "catalog": None}


def allow_input_without_screenshot() -> None:
    _state()["observed"] = True


def _require_observation(operation: str) -> None:
    if _NO_GUARD:
        return
    if not _state()["observed"]:
        raise CodedError(
            operation,
            "CU_SCREENSHOT_REQUIRED",
            "Take a screenshot before sending pointer input, and derive coordinates "
            "from that frame.",
        )


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


def _coordinate(value, operation: str, parameter: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CodedError(
            operation, "CU_INVALID_ACTION", f"{parameter} must be a finite number in [0, 1000]"
        )
    if not 0 <= value <= MAX_COORDINATE:
        raise CodedError(
            operation, "CU_INVALID_ACTION", f"{parameter} must be in [0, 1000], got {value}"
        )
    return float(value)


_SCROLL_ALIASES = {
    "up": (5, False),
    "down": (-5, False),
    "right": (5, True),
    "left": (-5, True),
}


def _scroll_delta(value) -> tuple[int, bool]:
    if isinstance(value, str):
        try:
            return _SCROLL_ALIASES[value.strip().lower()]
        except KeyError as error:
            raise CodedError(
                "scroll",
                "CU_SCROLL_DELTA",
                "delta string must be one of 'up', 'down', 'right', 'left'",
            ) from error
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise CodedError(
            "scroll", "CU_SCROLL_DELTA", "delta must be a finite non-zero number or alias"
        )
    if value == 0:
        raise CodedError("scroll", "CU_SCROLL_DELTA", "delta must not be zero")
    if not -10 <= value <= 10:
        raise CodedError("scroll", "CU_SCROLL_DELTA", "delta must be in [-10, 10]")
    normalized = round(value)
    if normalized == 0:
        normalized = 1 if value > 0 else -1
    return normalized, False


def _duration_milliseconds(seconds) -> int:
    if isinstance(seconds, bool):
        raise CodedError("wait", "CU_WAIT_SECONDS", "seconds must be a number in [0, 180]")
    try:
        value = float(seconds)
    except (TypeError, ValueError) as error:
        raise CodedError(
            "wait", "CU_WAIT_SECONDS", "seconds must be a number in [0, 180]"
        ) from error
    if not math.isfinite(value) or not 0 <= value <= 180:
        raise CodedError(
            "wait",
            "CU_WAIT_SECONDS",
            "seconds must be in [0, 180]; millisecond values must be divided by 1000",
        )
    return round(value * 1000)


# --------------------------------------------------------------------------- #
# Capture
# --------------------------------------------------------------------------- #


def screenshot(*, tag: str | None = None) -> Frame:
    """Capture the calling desktop and return it as a PNG :class:`Frame`."""

    try:
        bgra, width, height = win32.capture_bgra()
    except win32.Win32Error as error:
        code = (
            "VD_DESKTOP_NOT_ACTIVE"
            if error.call == "BitBlt"
            else "CU_SCREENSHOT_FAILED"
        )
        raise CodedError("screenshot", code, str(error)) from error

    data = png_module.encode_bgra(bgra, width, height)
    frame = Frame(data, width, height)
    state = _state()
    state["observed"] = True
    state["frame"] = frame
    return frame


# --------------------------------------------------------------------------- #
# Pointer
# --------------------------------------------------------------------------- #


def _to_absolute(x_1000: float, y_1000: float) -> tuple[int, int]:
    left, top, width, height = win32.virtual_screen()
    px = left + (x_1000 / MAX_COORDINATE) * (width - 1)
    py = top + (y_1000 / MAX_COORDINATE) * (height - 1)
    dx = int(round(px * 65535 / max(width - 1, 1)))
    dy = int(round(py * 65535 / max(height - 1, 1)))
    return dx, dy


def _send(inputs: list[win32.INPUT], operation: str) -> None:
    if not inputs:
        return
    sent = win32.send_inputs(inputs)
    if sent != len(inputs):
        raise CodedError(
            operation,
            "CU_INPUT_FAILED",
            f"SendInput accepted {sent} of {len(inputs)} events "
            f"(is the virtual desktop the active desktop?)",
        )


def _move_to(x_1000: float, y_1000: float) -> None:
    dx, dy = _to_absolute(x_1000, y_1000)
    _send(
        [
            win32.mouse_input(
                win32.MOUSEEVENTF_MOVE
                | win32.MOUSEEVENTF_ABSOLUTE
                | win32.MOUSEEVENTF_VIRTUALDESK,
                dx,
                dy,
            )
        ],
        "move",
    )


_BUTTON_FLAGS = {
    "left": (win32.MOUSEEVENTF_LEFTDOWN, win32.MOUSEEVENTF_LEFTUP),
    "right": (win32.MOUSEEVENTF_RIGHTDOWN, win32.MOUSEEVENTF_RIGHTUP),
    "middle": (win32.MOUSEEVENTF_MIDDLEDOWN, win32.MOUSEEVENTF_MIDDLEUP),
}


def click(x, y, button: str = "left", clicks: int = 1) -> None:
    """Click a mouse button at normalized 0-1000 desktop coordinates."""

    _require_observation("click")
    position = (_coordinate(x, "click", "x"), _coordinate(y, "click", "y"))
    name = str(button or "left").lower()
    if name not in _BUTTON_FLAGS:
        raise CodedError(
            "click", "CU_INVALID_ACTION", "button must be 'left', 'right' or 'middle'"
        )
    if isinstance(clicks, bool) or not isinstance(clicks, int) or clicks < 1:
        raise CodedError("click", "CU_INVALID_ACTION", "clicks must be a positive integer")
    down, up = _BUTTON_FLAGS[name]
    _move_to(*position)
    events: list[win32.INPUT] = []
    for _ in range(clicks):
        events.append(win32.mouse_input(down))
        events.append(win32.mouse_input(up))
    _send(events, "click")


def left_double(x, y) -> None:
    click(x, y, "left", 2)


def triple_click(x, y) -> None:
    click(x, y, "left", 3)


def right_single(x, y) -> None:
    click(x, y, "right", 1)


def hover(x, y) -> None:
    """Move the pointer without clicking."""

    _require_observation("hover")
    _move_to(_coordinate(x, "hover", "x"), _coordinate(y, "hover", "y"))


move = hover
move_mouse = hover


def drag(x1, y1, x2, y2, duration_ms: int = 1000) -> None:
    """Press at the first point, glide to the second, release."""

    _require_observation("drag")
    start = (_coordinate(x1, "drag", "x1"), _coordinate(y1, "drag", "y1"))
    end = (_coordinate(x2, "drag", "x2"), _coordinate(y2, "drag", "y2"))
    steps = max(8, min(60, int(duration_ms / 20) or 8))
    delay = (duration_ms / 1000.0) / steps
    _move_to(*start)
    time.sleep(0.05)
    _send([win32.mouse_input(win32.MOUSEEVENTF_LEFTDOWN)], "drag")
    try:
        for index in range(1, steps + 1):
            ratio = index / steps
            _move_to(
                start[0] + (end[0] - start[0]) * ratio,
                start[1] + (end[1] - start[1]) * ratio,
            )
            time.sleep(delay)
    finally:
        _send([win32.mouse_input(win32.MOUSEEVENTF_LEFTUP)], "drag")


def scroll(x, y, delta, *, horizontal: bool = False) -> None:
    """Scroll by a signed wheel delta at normalized coordinates.

    ``horizontal`` is the explicit flag (the seed wire field); a direction
    alias inside ``delta`` ("left"/"right") implies it too.
    """

    _require_observation("scroll")
    position = (_coordinate(x, "scroll", "x"), _coordinate(y, "scroll", "y"))
    normalized, alias_horizontal = _scroll_delta(delta)
    _move_to(*position)
    sideways = bool(horizontal) or alias_horizontal
    flag = win32.MOUSEEVENTF_HWHEEL if sideways else win32.MOUSEEVENTF_WHEEL
    _send([win32.mouse_input(flag, data=int(normalized * 120))], "scroll")


# --------------------------------------------------------------------------- #
# Keyboard
# --------------------------------------------------------------------------- #

_VK_NAMES: dict[str, int] = {
    "backspace": 0x08, "tab": 0x09, "clear": 0x0C, "enter": 0x0D, "return": 0x0D,
    "shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12, "menu": 0x12,
    "pause": 0x13, "capslock": 0x14, "esc": 0x1B, "escape": 0x1B, "space": 0x20,
    "pageup": 0x21, "pgup": 0x21, "pagedown": 0x22, "pgdn": 0x22, "end": 0x23,
    "home": 0x24, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "printscreen": 0x2C, "insert": 0x2D, "ins": 0x2D, "delete": 0x2E, "del": 0x2E,
    "win": 0x5B, "lwin": 0x5B, "rwin": 0x5C, "meta": 0x5B, "cmd": 0x5B, "apps": 0x5D,
    "numpad0": 0x60, "numpad1": 0x61, "numpad2": 0x62, "numpad3": 0x63,
    "numpad4": 0x64, "numpad5": 0x65, "numpad6": 0x66, "numpad7": 0x67,
    "numpad8": 0x68, "numpad9": 0x69, "multiply": 0x6A, "add": 0x6B,
    "subtract": 0x6D, "decimal": 0x6E, "divide": 0x6F, "numlock": 0x90,
    "scrolllock": 0x91, "volumemute": 0xAD, "volumedown": 0xAE, "volumeup": 0xAF,
}
for _index in range(1, 25):
    _VK_NAMES[f"f{_index}"] = 0x6F + _index
for _char in "0123456789":
    _VK_NAMES[_char] = 0x30 + int(_char)
for _offset, _char in enumerate("abcdefghijklmnopqrstuvwxyz"):
    _VK_NAMES[_char] = 0x41 + _offset

_EXTENDED_KEYS = {
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D, 0x6F,
}

_KEY_ALIASES = {
    "arrowleft": "left", "arrowright": "right", "arrowup": "up", "arrowdown": "down",
}


def _key(value) -> str:
    normalized = str(value).lower()
    return _KEY_ALIASES.get(normalized, normalized)


def _vk_for(key: str) -> int:
    name = _key(key)
    if name in _VK_NAMES:
        return _VK_NAMES[name]
    if len(name) == 1:
        code = win32.user32.VkKeyScanW(name)
        if code != -1:
            return code & 0xFF
    raise CodedError(
        "key",
        "CU_INVALID_ACTION",
        f"unknown key {key!r}; use a name such as 'enter', 'ctrl', 'f5', or one character",
    )


def _events_for(vk: int, up: bool = False) -> win32.INPUT:
    return win32.key_input(vk, up=up, extended=vk in _EXTENDED_KEYS)


def press(key) -> None:
    value = _key(key)
    if not value:
        return None
    vk = _vk_for(value)
    _send([_events_for(vk), _events_for(vk, up=True)], "press")
    return None


def key_down(key) -> None:
    _send([_events_for(_vk_for(key))], "key_down")
    return None


def key_up(key) -> None:
    _send([_events_for(_vk_for(key), up=True)], "key_up")
    return None


def hotkey(*keys) -> None:
    """Press a combination whose keys are passed separately: ``hotkey('ctrl','s')``."""

    normalized = tuple(_key(value) for value in keys)
    if len(normalized) == 1 and normalized[0] != "+" and "+" in normalized[0]:
        raise CodedError(
            "hotkey",
            "CU_BAD_HOTKEY",
            "pass each key separately, for example hotkey('ctrl', 's')",
        )
    if not normalized:
        return None
    vks = [_vk_for(value) for value in normalized]
    events = [_events_for(vk) for vk in vks]
    events.extend(_events_for(vk, up=True) for vk in reversed(vks))
    _send(events, "hotkey")
    return None


def _is_typable(text: str) -> bool:
    if len(text) > TYPABLE_LIMIT:
        return False
    for char in text:
        if char in "\r\n\t":
            continue
        if ord(char) > 0x7E or ord(char) < 0x20:
            return False
        if win32.user32.VkKeyScanW(char) == -1:
            return False
    return True


def _type_typable(text: str) -> None:
    events: list[win32.INPUT] = []
    for char in text:
        if char == "\n":
            events.extend([_events_for(0x0D), _events_for(0x0D, up=True)])
            continue
        if char == "\t":
            events.extend([_events_for(0x09), _events_for(0x09, up=True)])
            continue
        code = win32.user32.VkKeyScanW(char)
        vk = code & 0xFF
        shifted = bool(code >> 8 & 1)
        if shifted:
            events.append(_events_for(0x10))
        events.append(_events_for(vk))
        events.append(_events_for(vk, up=True))
        if shifted:
            events.append(_events_for(0x10, up=True))
    _send(events, "type")


def _type_unicode(text: str) -> None:
    """Send text as KEYEVENTF_UNICODE so the keyboard layout cannot mangle it."""

    events: list[win32.INPUT] = []
    units = text.encode("utf-16-le")
    for index in range(0, len(units), 2):
        unit = int.from_bytes(units[index : index + 2], "little")
        events.append(win32.key_input(0, unicode_unit=unit))
        events.append(win32.key_input(0, up=True, unicode_unit=unit))
    _send(events, "type")


def _type_via_clipboard(text: str) -> None:
    try:
        previous = get_clipboard()
    except Exception:  # noqa: BLE001 - a non-text clipboard is not an error here
        previous = None
    set_clipboard(text)
    time.sleep(0.05)
    hotkey("ctrl", "v")
    time.sleep(0.35)
    if previous is not None:
        try:
            set_clipboard(previous)
        except Exception:  # noqa: BLE001
            pass


def type_text(text) -> None:
    """Type into the focused control.  A trailing newline presses Enter.

    Non-ASCII text and text too long to type key by key go through the clipboard,
    which is why Chinese input needs no IME.
    """

    value = "" if text is None else str(text)
    if not value:
        return None
    trailing_enter = value.endswith("\n")
    body = value[:-1] if trailing_enter else value
    if body:
        if _is_typable(body):
            _type_typable(body)
        else:
            try:
                _type_unicode(body)
            except Exception:  # noqa: BLE001 - fall back to paste
                _type_via_clipboard(body)
    if trailing_enter:
        press("enter")
    return None


# --------------------------------------------------------------------------- #
# Clipboard
# --------------------------------------------------------------------------- #


def get_clipboard() -> str:
    try:
        return win32.clipboard_text()
    except win32.Win32Error as error:
        raise CodedError("get_clipboard", "CU_CLIPBOARD_ERROR", str(error)) from error


def set_clipboard(text) -> None:
    try:
        win32.set_clipboard_text("" if text is None else str(text))
    except win32.Win32Error as error:
        raise CodedError("set_clipboard", "CU_CLIPBOARD_ERROR", str(error)) from error
    return None


def copy_selection() -> str:
    """Press Ctrl+C and return what landed on the clipboard."""

    hotkey("ctrl", "c")
    time.sleep(0.15)
    return get_clipboard()


# --------------------------------------------------------------------------- #
# Application catalog and launching
# --------------------------------------------------------------------------- #

_START_MENU_ROOTS = (
    os.path.join(
        os.environ.get("ProgramData", r"C:\ProgramData"),
        "Microsoft", "Windows", "Start Menu", "Programs",
    ),
    os.path.join(
        os.environ.get("APPDATA", ""),
        "Microsoft", "Windows", "Start Menu", "Programs",
    ),
)
_LAUNCHABLE_SUFFIXES = (".lnk", ".url", ".appref-ms")


def _enumerate_shortcuts() -> list[dict]:
    found: list[dict] = []
    for root in _START_MENU_ROOTS:
        if not root or not os.path.isdir(root):
            continue
        for directory, _dirs, files in os.walk(root):
            for filename in files:
                if filename.lower().endswith(_LAUNCHABLE_SUFFIXES):
                    found.append(
                        {
                            "name": os.path.splitext(filename)[0],
                            "target": os.path.join(directory, filename),
                            "kind": "shortcut",
                        }
                    )
    return found


def _enumerate_packaged() -> list[dict]:
    """Read the packaged-app namespace through ``Get-StartApps``."""

    command = [
        "powershell", "-NoProfile", "-NonInteractive", "-Command",
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
        "Get-StartApps | ForEach-Object { $_.Name + \"`t\" + $_.AppID }",
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, timeout=30, check=False,
            creationflags=_HEADLESS_POWERSHELL,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    apps: list[dict] = []
    for line in completed.stdout.decode("utf-8", errors="replace").splitlines():
        if "\t" not in line:
            continue
        name, _, app_id = line.partition("\t")
        if name.strip() and app_id.strip():
            apps.append({"name": name.strip(), "target": app_id.strip(), "kind": "aumid"})
    return apps


def _slug(app: dict) -> str:
    """An ASCII-only handle for an app, so a host that mangles non-ASCII command
    line arguments (some Windows shells do) can still address it."""

    base = os.path.splitext(os.path.basename(app.get("target", "")))[0]
    if app.get("kind") == "aumid":
        base = base.split("!")[-1].split("_")[-1] or base
    ascii_only = unicodedata.normalize("NFKD", base).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "-", ascii_only).strip("-").lower()
    return slug or "app"


def _qualify(entries: list[dict]) -> AppCatalog:
    """Build a catalog whose names are unique ignoring case.

    Windows really does hold several Start-menu entries under one name, so
    duplicates are qualified (``Calculator (Store)``) instead of dropped; the
    invariant ``launch_app`` depends on is uniqueness, not the label wording.
    Each entry also carries an ASCII ``key``.
    """

    grouped: dict[str, list[dict]] = {}
    for entry in entries:
        grouped.setdefault(entry["name"].casefold(), []).append(entry)

    catalog = AppCatalog()
    for key in sorted(grouped, key=lambda value: grouped[value][0]["name"].casefold()):
        group = grouped[key]
        if len(group) == 1:
            only = group[0]
            catalog.append(
                App(name=only["name"], target=only["target"], kind=only["kind"], key=_slug(only))
            )
            continue
        seen: dict[str, int] = {}
        for item in sorted(group, key=lambda entry: (entry["kind"] != "aumid", entry["target"].casefold())):
            base = "Store" if item["kind"] == "aumid" else "Desktop"
            seen[base] = seen.get(base, 0) + 1
            label = base if seen[base] == 1 else f"{base} {seen[base]}"
            catalog.append(
                App(
                    name=f"{item['name']} ({label})",
                    target=item["target"],
                    kind=item["kind"],
                    key=f"{_slug(item)}-{label.split()[0].lower()}",
                )
            )

    folded = [app.name.casefold() for app in catalog]
    if len(folded) != len(set(folded)):
        duplicates = sorted({name for name in folded if folded.count(name) > 1})
        raise CodedError(
            "list_apps",
            "CU_APP_CATALOG_CONFLICT",
            f"generated catalog has non-unique names: {duplicates[:5]}",
        )

    # Keys must be unique too: they are the fallback address.
    used: dict[str, int] = {}
    for app in catalog:
        base = app["key"]
        used[base] = used.get(base, 0) + 1
        if used[base] > 1:
            app["key"] = f"{base}-{used[base]}"
    return catalog


def list_apps() -> AppCatalog:
    """Return the application catalog; ``launch_app`` matches these names exactly."""

    catalog = _qualify([*_enumerate_shortcuts(), *_enumerate_packaged()])
    _state()["catalog"] = {app.name: app for app in catalog}
    return catalog


def _resolve(name: str) -> dict:
    """Resolve a launch argument to one catalog entry.

    Exact ``name`` first (the contract).  Then the ASCII ``key``, then a unique
    substring — both exist because agents reach this through shells and tool
    wrappers that sometimes hand over a name that has been through an ANSI
    round-trip, and failing to launch a visible app is a bad failure mode.  An
    ambiguous substring is refused with the candidates, never guessed.
    """

    state = _state()
    if not state.get("catalog"):
        list_apps()  # implicit listing; the exact-name rule itself is unchanged
    catalog = state["catalog"]

    entry = catalog.get(name)
    if entry is not None:
        return dict(entry)

    folded = str(name).casefold()
    by_key = [item for item in catalog.values() if item["key"].casefold() == folded]
    if len(by_key) == 1:
        return dict(by_key[0])

    matches = [
        item for item in catalog.values()
        if folded in item["name"].casefold() or folded in item["key"].casefold()
    ]
    if len(matches) == 1:
        return dict(matches[0])
    if matches:
        preview = ", ".join(sorted(item["name"] for item in matches)[:8])
        raise CodedError(
            "launch_app",
            "CU_APP_AMBIGUOUS",
            f"{name!r} matches {len(matches)} entries: {preview}. Pass an exact name.",
        )
    raise CodedError(
        "launch_app",
        "CU_APP_UNKNOWN",
        f"{name!r} is not in the application catalog; call list_apps() and use an exact name",
    )


def launch_app(name, *, verify_seconds: float = 10.0) -> Record:
    """Launch one exact ``App.name`` *into the bound desktop*.

    Returns ``status="ready"`` with matching window evidence, or
    ``status="accepted_unverified"`` when the process started but no window has
    been confirmed yet.  ``accepted_unverified`` is not an error.
    """

    if not isinstance(name, str):
        raise CodedError("launch_app", "CU_INVALID_ACTION", "name must be a string")
    app = _resolve(name)
    context = desktop_context()
    desktop = context.get("name")
    hdesk = context.get("handle")
    target_desktop = f"winsta0\\{desktop}" if desktop else None

    before = {(window["title"], window["pid"]) for window in win32.desktop_windows(hdesk)}

    try:
        method = _start(app, target_desktop)
    except win32.Win32Error as error:
        raise CodedError(
            "launch_app", "CU_APP_LAUNCH_FAILED", f"could not start {name!r}: {error}"
        ) from error

    stem = os.path.splitext(os.path.basename(app["target"]))[0].casefold()
    deadline = time.monotonic() + verify_seconds
    while time.monotonic() < deadline:
        for window in win32.desktop_windows(hdesk):
            if (window["title"], window["pid"]) in before:
                continue
            title = window["title"].casefold()
            if stem and stem in title:
                return Record(
                    app=App(**app),
                    status="ready",
                    launch_method=method,
                    desktop=desktop or "default",
                    window=Record(title=window["title"], pid=window["pid"]),
                )
        time.sleep(0.25)

    return Record(
        app=App(**app),
        status="accepted_unverified",
        launch_method=method,
        desktop=desktop or "default",
        window=None,
    )


def _start(app: dict, target_desktop: str | None) -> str:
    if app["kind"] == "aumid":
        # Never activate a packaged app through the shell: the request is dispatched
        # by the session's shell broker and the app lands on the *user's* desktop.
        # vdcu.apps resolves the package's real executable instead.
        raise CodedError(
            "launch_app",
            "CU_APP_NEEDS_RESOLUTION",
            f"packaged app {app['name']!r} must be launched through vdcu.apps, which "
            "resolves its executable; shell activation would open it on the user's desktop",
        )
    target = app["target"]
    if target_desktop:
        win32.launch_on_desktop(f'"{target}"', target_desktop, hide_window=False)
        return "resolved_target_on_desktop"
    os.startfile(target)  # noqa: S606
    return "resolved_target"


def windows() -> list[dict]:
    """Visible titled windows of the bound desktop."""

    context = desktop_context()
    return win32.desktop_windows(context.get("handle"))


# --------------------------------------------------------------------------- #
# Waiting
# --------------------------------------------------------------------------- #


def wait(seconds=3) -> None:
    """Sleep locally for 0-180 **seconds**."""

    time.sleep(_duration_milliseconds(seconds) / 1000)
    return None


# ``type`` is the historical public name; bound last, and nothing below it calls
# the builtin, so the shadowing that bit the reference implementation cannot recur.
type = type_text  # noqa: A001

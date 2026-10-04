"""Low-level CDP keyboard primitives."""

import sys

from .transport import cdp


_KEY_TABLE = {
    "backspace": (0x08, "Backspace", "Backspace"),
    "tab": (0x09, "Tab", "Tab"),
    "enter": (0x0D, "Enter", "Enter"),
    "shift": (0x10, "Shift", "ShiftLeft"),
    "shiftleft": (0x10, "Shift", "ShiftLeft"),
    "shiftright": (0x10, "Shift", "ShiftRight"),
    "control": (0x11, "Control", "ControlLeft"),
    "controlleft": (0x11, "Control", "ControlLeft"),
    "controlright": (0x11, "Control", "ControlRight"),
    "alt": (0x12, "Alt", "AltLeft"),
    "altleft": (0x12, "Alt", "AltLeft"),
    "altright": (0x12, "Alt", "AltRight"),
    "pause": (0x13, "Pause", "Pause"),
    "capslock": (0x14, "CapsLock", "CapsLock"),
    "escape": (0x1B, "Escape", "Escape"),
    "space": (0x20, " ", "Space"),
    "pageup": (0x21, "PageUp", "PageUp"),
    "pagedown": (0x22, "PageDown", "PageDown"),
    "end": (0x23, "End", "End"),
    "home": (0x24, "Home", "Home"),
    "arrowleft": (0x25, "ArrowLeft", "ArrowLeft"),
    "arrowup": (0x26, "ArrowUp", "ArrowUp"),
    "arrowright": (0x27, "ArrowRight", "ArrowRight"),
    "arrowdown": (0x28, "ArrowDown", "ArrowDown"),
    "printscreen": (0x2C, "PrintScreen", "PrintScreen"),
    "insert": (0x2D, "Insert", "Insert"),
    "delete": (0x2E, "Delete", "Delete"),
    "meta": (0x5B, "Meta", "MetaLeft"),
    "metaleft": (0x5B, "Meta", "MetaLeft"),
    "metaright": (0x5C, "Meta", "MetaRight"),
    "contextmenu": (0x5D, "ContextMenu", "ContextMenu"),
    "numlock": (0x90, "NumLock", "NumLock"),
    "scrolllock": (0x91, "ScrollLock", "ScrollLock"),
    "semicolon": (0xBA, ";", "Semicolon"),
    "equal": (0xBB, "=", "Equal"),
    "comma": (0xBC, ",", "Comma"),
    "minus": (0xBD, "-", "Minus"),
    "period": (0xBE, ".", "Period"),
    "slash": (0xBF, "/", "Slash"),
    "backquote": (0xC0, "`", "Backquote"),
    "bracketleft": (0xDB, "[", "BracketLeft"),
    "backslash": (0xDC, "\\", "Backslash"),
    "bracketright": (0xDD, "]", "BracketRight"),
    "quote": (0xDE, "'", "Quote"),
}
_KEY_TABLE.update(
    {
        f"digit{digit}": (0x30 + digit, str(digit), f"Digit{digit}")
        for digit in range(10)
    }
)
_KEY_TABLE.update(
    {
        f"key{letter.lower()}": (
            ord(letter),
            letter.lower(),
            f"Key{letter}",
        )
        for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    }
)
_KEY_TABLE.update(
    {
        f"f{number}": (0x6F + number, f"F{number}", f"F{number}")
        for number in range(1, 13)
    }
)

_CHAR_TABLE = {
    " ": (0x20, "Space"),
    ";": (0xBA, "Semicolon"),
    "=": (0xBB, "Equal"),
    ",": (0xBC, "Comma"),
    "-": (0xBD, "Minus"),
    ".": (0xBE, "Period"),
    "/": (0xBF, "Slash"),
    "`": (0xC0, "Backquote"),
    "[": (0xDB, "BracketLeft"),
    "\\": (0xDC, "Backslash"),
    "]": (0xDD, "BracketRight"),
    "'": (0xDE, "Quote"),
}
_CHAR_TABLE.update(
    {
        str(digit): (0x30 + digit, f"Digit{digit}")
        for digit in range(10)
    }
)
_CHAR_TABLE.update(
    {
        letter: (ord(letter.upper()), f"Key{letter.upper()}")
        for letter in "abcdefghijklmnopqrstuvwxyz"
    }
)

_MODIFIER_KEYS = (
    (1, "alt"),
    (2, "control"),
    (4, "meta"),
    (8, "shift"),
)
_KEY_LOCATIONS = {
    "AltLeft": 1,
    "MetaLeft": 1,
    "ShiftLeft": 1,
    "ControlLeft": 1,
    "AltRight": 2,
    "MetaRight": 2,
    "ShiftRight": 2,
    "ControlRight": 2,
}

_COMMON_INPUT_COMMANDS = {
    (0x00, "ArrowLeft"): ["moveLeft"],
    (0x00, "ArrowRight"): ["moveRight"],
    (0x00, "ArrowUp"): ["moveUp"],
    (0x00, "ArrowDown"): ["moveDown"],
    (0x00, "Home"): ["moveToBeginningOfLine"],
    (0x00, "End"): ["moveToEndOfLine"],
    (0x00, "Backspace"): ["deleteBackward"],
    (0x00, "Delete"): ["deleteForward"],
    (0x08, "Enter"): ["insertLineBreak"],
    (0x00, "Enter"): ["insertParagraph"],
    (0x00, "Tab"): ["insertTab"],
    (0x00, "PageUp"): ["scrollPageUp"],
    (0x00, "PageDown"): ["scrollPageDown"],
    (0x00, "Escape"): ["cancel"],
}
if sys.platform == "darwin":
    _INPUT_COMMANDS = {
        **_COMMON_INPUT_COMMANDS,
        (0x04, "KeyA"): ["selectAll"],
        (0x01, "ArrowLeft"): ["moveWordLeft"],
        (0x01, "ArrowRight"): ["moveWordRight"],
        (0x04, "ArrowLeft"): ["moveToBeginningOfLine"],
        (0x02, "KeyA"): ["moveToBeginningOfLine"],
        (0x04, "ArrowRight"): ["moveToEndOfLine"],
        (0x02, "KeyE"): ["moveToEndOfLine"],
        (0x04, "ArrowUp"): [
            "moveToBeginningOfDocument",
            "scrollToBeginningOfDocument",
        ],
        (0x04, "ArrowDown"): [
            "moveToEndOfDocument",
            "scrollToEndOfDocument",
        ],
        (0x02, "KeyD"): ["deleteForward"],
        (0x01, "Backspace"): ["deleteWordBackward"],
        (0x03, "KeyD"): ["deleteWordForward"],
        (0x04, "KeyX"): ["cut"],
        (0x04, "KeyC"): ["copy"],
        (0x04, "KeyV"): ["paste"],
        (0x0C, "KeyV"): ["pasteAndMatchStyle"],
        (0x04, "KeyZ"): ["undo"],
        (0x0C, "KeyZ"): ["redo"],
    }
else:
    _INPUT_COMMANDS = {
        **_COMMON_INPUT_COMMANDS,
        (0x02, "KeyA"): ["selectAll"],
        (0x02, "ArrowLeft"): ["moveWordLeft"],
        (0x02, "ArrowRight"): ["moveWordRight"],
        (0x02, "Home"): [
            "moveToBeginningOfDocument",
            "scrollToBeginningOfDocument",
        ],
        (0x02, "End"): [
            "moveToEndOfDocument",
            "scrollToEndOfDocument",
        ],
        (0x02, "Backspace"): ["deleteWordBackward"],
        (0x02, "Delete"): ["deleteWordForward"],
        (0x02, "KeyX"): ["cut"],
        (0x02, "KeyC"): ["copy"],
        (0x02, "KeyV"): ["paste"],
        (0x0A, "KeyV"): ["pasteAndMatchStyle"],
        (0x02, "KeyZ"): ["undo"],
        (0x02, "KeyY"): ["redo"],
        (0x0A, "KeyZ"): ["redo"],
    }


def _resolve_key(key):
    text = str(key)
    if len(text) == 1:
        normalized = text.lower() if text.isalpha() else text
        entry = _CHAR_TABLE.get(normalized)
        if entry is not None:
            virtual_key, code = entry
            return virtual_key, normalized, code
    entry = _KEY_TABLE.get(text.lower())
    if entry is None:
        raise ValueError(f"unsupported key: {key!r}")
    return entry


def _fire_key_event(key, modifiers, is_down):
    virtual_key, key_value, code = _resolve_key(key)
    cdp(
        "Input.dispatchKeyEvent",
        type="rawKeyDown" if is_down else "keyUp",
        modifiers=modifiers,
        code=code,
        key=key_value,
        windowsVirtualKeyCode=virtual_key,
        nativeVirtualKeyCode=virtual_key,
        location=_KEY_LOCATIONS.get(code, 0),
        commands=_INPUT_COMMANDS.get((modifiers, code), []) if is_down else [],
    )


def press_key(key, modifiers=0):
    """Dispatch one key; modifiers use Alt=1, Ctrl=2, Meta=4, Shift=8."""
    text = str(key)
    if len(text) == 1 and not modifiers:
        cdp("Input.insertText", text=text)
        return

    active_modifiers = 0
    pressed_modifiers = []
    try:
        for bit, modifier_key in _MODIFIER_KEYS:
            if not modifiers & bit:
                continue
            active_modifiers |= bit
            _fire_key_event(modifier_key, active_modifiers, True)
            pressed_modifiers.append((bit, modifier_key))
        _fire_key_event(text, active_modifiers, True)
        _fire_key_event(text, active_modifiers, False)
    finally:
        for bit, modifier_key in reversed(pressed_modifiers):
            active_modifiers &= ~bit
            _fire_key_event(modifier_key, active_modifiers, False)

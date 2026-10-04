"""Public keyboard action policies."""

from ..domain.core import *


_HOTKEY_ALIASES = {
    "arrowleft": "ArrowLeft",
    "arrowright": "ArrowRight",
    "arrowup": "ArrowUp",
    "arrowdown": "ArrowDown",
    "left": "ArrowLeft",
    "right": "ArrowRight",
    "up": "ArrowUp",
    "down": "ArrowDown",
    "esc": "Escape",
    "escape": "Escape",
    "enter": "Enter",
    "return": "Enter",
    "tab": "Tab",
    "backspace": "Backspace",
    "delete": "Delete",
    "space": " ",
    "del": "Delete",
}
_MODIFIER_BITS = {
    "alt": 1,
    "ctrl": 2,
    "control": 2,
    "meta": 4,
    "cmd": 4,
    "command": 4,
    "win": 4,
    "shift": 8,
}


def hotkey(*keys):
    """Press a key combination such as hotkey("ctrl", "a")."""
    modifiers = 0
    main = None
    for key in keys:
        normalized = str(key).lower()
        if normalized in _MODIFIER_BITS:
            modifiers |= _MODIFIER_BITS[normalized]
        else:
            main = _HOTKEY_ALIASES.get(
                normalized,
                key if len(str(key)) == 1 else str(key),
            )
    if main is not None:
        with _dispatch("hotkey", ["+".join(str(key) for key in keys)]):
            _bh.press_key(main, modifiers=modifiers)


def press_key(key):
    """Press one special or printable key."""
    normalized = _HOTKEY_ALIASES.get(str(key).lower(), key)
    with _dispatch("press_key", [_audit_value(key)]):
        _bh.press_key(normalized)

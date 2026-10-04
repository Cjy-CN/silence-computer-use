from ....modules.computer_use_windows import WindowsComputerUseCommand
from ._errors import execute_command
from ._validation import hotkey_keys
from ._validation import key as normalize_key
from ._validation import text as normalize_text


def type(  # noqa: A001
    text,
):
    """Type text into the focused control."""
    value = normalize_text(text)
    if not value:
        return None
    return execute_command("type", WindowsComputerUseCommand.type, value)


def hotkey(
    *keys,
):
    """Press a key combination whose keys are passed separately."""
    values = hotkey_keys(keys)
    if not values:
        return None
    return execute_command(
        "hotkey",
        WindowsComputerUseCommand.hotkey,
        *values,
    )


def press(key):
    """Press and release one key."""
    value = normalize_key(key)
    if not value:
        return None
    return execute_command("press", WindowsComputerUseCommand.press, value)


def key_down(key):
    """Hold one key until a matching ``key_up`` call."""
    value = normalize_key(key)
    return execute_command(
        "key_down",
        WindowsComputerUseCommand.key_down,
        value,
    )


def key_up(key):
    """Release one key."""
    value = normalize_key(key)
    return execute_command(
        "key_up",
        WindowsComputerUseCommand.key_up,
        value,
    )

from ....modules.computer_use_windows import WindowsComputerUseCommand
from ._errors import execute_command
from ._validation import text as normalize_text


def get_clipboard():
    """Return the host clipboard's text content."""
    return execute_command(
        "get_clipboard",
        WindowsComputerUseCommand.get_clipboard,
    )


def set_clipboard(text):
    """Replace the host clipboard's text content."""
    return execute_command(
        "set_clipboard",
        WindowsComputerUseCommand.set_clipboard,
        normalize_text(text),
    )


def copy_selection():
    """Copy the focused selection and return clipboard text."""
    return execute_command(
        "copy_selection",
        WindowsComputerUseCommand.copy_selection,
    )

from ....modules.computer_use_windows import WindowsComputerUseCommand
from ._errors import execute_command
from ._validation import string


def list_apps():
    """Return the complete application catalog for later app selection."""
    return execute_command("list_apps", WindowsComputerUseCommand.list_apps)


def launch_app(name):
    """Launch an application by an exact name returned by ``list_apps``."""
    return execute_command(
        "launch_app",
        WindowsComputerUseCommand.launch_app,
        string(name, "launch_app", "name"),
    )

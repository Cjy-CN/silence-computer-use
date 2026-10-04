from ....modules.computer_use_windows import WindowsComputerUseCommand
from ._errors import execute_command


def screenshot(*, tag=None, show=True):
    """Capture the complete Windows virtual desktop as PNG bytes."""
    normalized_tag = None if tag is None else str(tag)
    return execute_command(
        "screenshot",
        WindowsComputerUseCommand.screenshot,
        normalized_tag,
    )

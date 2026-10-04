from ....modules.computer_use_windows import WindowsComputerUseCommand
from ._errors import execute_command
from ._validation import coordinate
from ._validation import scroll_delta


def left_double(
    x,
    y,
):
    """Double-click the left mouse button at normalized desktop coordinates."""
    return _click("left_double", x, y, "left", 2)


def click(
    x,
    y,
):
    """Click the left mouse button once at normalized coordinates."""
    return _click("click", x, y, "left", 1)


def triple_click(
    x,
    y,
):
    """Triple-click the left mouse button at normalized desktop coordinates."""
    return _click("triple_click", x, y, "left", 3)


def right_single(
    x,
    y,
):
    """Click the right mouse button once at normalized desktop coordinates."""
    return _click("right_single", x, y, "right", 1)


def hover(
    x,
    y,
):
    """Move the pointer to normalized desktop coordinates without clicking."""
    return execute_command(
        "hover",
        WindowsComputerUseCommand.hover,
        coordinate(x, "hover", "x"),
        coordinate(y, "hover", "y"),
    )


def drag(
    x1,
    y1,
    x2,
    y2,
):
    """Drag between two normalized desktop coordinates."""
    return execute_command(
        "drag",
        WindowsComputerUseCommand.drag,
        coordinate(x1, "drag", "x1"),
        coordinate(y1, "drag", "y1"),
        coordinate(x2, "drag", "x2"),
        coordinate(y2, "drag", "y2"),
    )


def scroll(
    x,
    y,
    delta,
):
    """Scroll by a signed wheel-notch delta at normalized coordinates."""
    normalized_delta, horizontal = scroll_delta(delta)
    return execute_command(
        "scroll",
        WindowsComputerUseCommand.scroll,
        coordinate(x, "scroll", "x"),
        coordinate(y, "scroll", "y"),
        normalized_delta,
        horizontal=horizontal,
    )


def _click(operation, x, y, button, clicks):
    return execute_command(
        operation,
        WindowsComputerUseCommand.click,
        coordinate(x, operation, "x"),
        coordinate(y, operation, "y"),
        button,
        clicks,
    )

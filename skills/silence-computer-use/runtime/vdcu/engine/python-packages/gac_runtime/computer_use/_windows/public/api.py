"""Public Windows Computer Use API assembled from native services."""

from ..services._errors import coded_error as _coded_error
from ..services.app import launch_app
from ..services.app import list_apps
from ..services.clipboard import copy_selection
from ..services.clipboard import get_clipboard
from ..services.clipboard import set_clipboard
from ..services.keyboard import hotkey
from ..services.keyboard import key_down
from ..services.keyboard import key_up
from ..services.keyboard import press
from ..services.keyboard import type
from ..services.mouse import click
from ..services.mouse import drag
from ..services.mouse import hover
from ..services.mouse import left_double
from ..services.mouse import right_single
from ..services.mouse import scroll
from ..services.mouse import triple_click
from ..services.screenshot import screenshot
from ..services.wait import wait
from ._instrumentation import wrap_action
from ._instrumentation import wrap_public_call
from ._instrumentation import wrap_screenshot


screenshot = wrap_screenshot(screenshot)
for _call_name in (
    "list_apps",
    "wait",
    "get_clipboard",
    "set_clipboard",
):
    globals()[_call_name] = wrap_public_call(
        _call_name,
        globals()[_call_name],
    )
for _action_name in (
    "launch_app",
    "click",
    "left_double",
    "triple_click",
    "right_single",
    "hover",
    "drag",
    "scroll",
    "type",
    "hotkey",
    "press",
    "key_down",
    "key_up",
    "copy_selection",
):
    globals()[_action_name] = wrap_action(
        _action_name,
        globals()[_action_name],
    )


__all__ = [
    "screenshot",
    "list_apps",
    "launch_app",
    "click",
    "left_double",
    "triple_click",
    "right_single",
    "hover",
    "drag",
    "scroll",
    "type",
    "hotkey",
    "press",
    "key_down",
    "key_up",
    "wait",
    "get_clipboard",
    "set_clipboard",
    "copy_selection",
]


def __getattr__(name):
    """Fail unknown public API calls with a stable code."""

    if str(name).startswith("__"):
        raise AttributeError(name)
    raise _coded_error(
        AttributeError,
        str(name),
        "CU_UNKNOWN_API",
        f"unknown public API cu.{name}.",
    )

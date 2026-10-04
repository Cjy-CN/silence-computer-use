"""Public macOS Accessibility Tree Computer Use API."""

from ..services.api import AppState
from ..services.api import Record
from ..services.api import click
from ..services.api import drag
from ..services.api import get_app_state
from ..services.api import list_apps
from ..services.api import perform_secondary_action
from ..services.api import press_key
from ..services.api import scroll
from ..services.api import select_text
from ..services.api import set_value
from ..services.api import type_text
from ..services.api import wait
from ._instrumentation import wrap_action
from ._instrumentation import wrap_app_state


get_app_state = wrap_app_state(get_app_state)
for _action_name in (
    "click",
    "type_text",
    "press_key",
    "set_value",
    "scroll",
    "select_text",
    "drag",
    "perform_secondary_action",
):
    globals()[_action_name] = wrap_action(
        _action_name,
        globals()[_action_name],
    )


__all__ = [
    "AppState",
    "Record",
    "click",
    "drag",
    "get_app_state",
    "list_apps",
    "perform_secondary_action",
    "press_key",
    "scroll",
    "select_text",
    "set_value",
    "type_text",
    "wait",
]

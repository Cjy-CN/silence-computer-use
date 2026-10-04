"""Complete Browser Use API backed exclusively by the Native AhaBUA task backend."""

from __future__ import annotations

import difflib
import inspect

from gac_runtime.gac_profile import bind_gac_profile_exports

from ._api import *
from ._api import __all__
from ._profile import AHA_GAC_PROFILE, AHA_SNAPSHOT_DEFAULTS, bind_snapshot_defaults


snapshot = bind_snapshot_defaults(snapshot, AHA_SNAPSHOT_DEFAULTS)
bind_gac_profile_exports(AHA_GAC_PROFILE, globals(), __all__)


_API_SUGGESTIONS = {
    "get_url": ('bu.page_info()["url"]', 'bu.current_tab()["url"]'),
    "get_current_url": ('bu.page_info()["url"]', 'bu.current_tab()["url"]'),
    "get_title": ('bu.page_info()["title"]',),
    "get_tabs": ("bu.list_tabs()",),
    "evaluate": ("bu.js(code)",),
    "open_new_tab": ("bu.new_tab(url)",),
    "press": ("bu.press_key(key)",),
}


def __getattr__(name):
    if str(name).startswith("__"):
        raise AttributeError(name)
    suggestions = list(_API_SUGGESTIONS.get(str(name), ()))
    if not suggestions:
        candidates = [item for item in __all__ if inspect.isroutine(globals().get(item))]
        suggestions = [
            f"bu.{item}{inspect.signature(globals()[item])}"
            for item in difflib.get_close_matches(str(name), candidates, n=3, cutoff=0.5)
        ]
    message = f"aha_browser_use: unknown public API bu.{name}."
    if suggestions:
        message += " Did you mean: " + ", ".join(suggestions) + "?"
    raise AttributeError(message)

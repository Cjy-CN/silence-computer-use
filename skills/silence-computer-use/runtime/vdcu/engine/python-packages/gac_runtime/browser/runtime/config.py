"""Runtime configuration without filesystem side effects."""

import os


INTERNAL = (
    "chrome://",
    "chrome-untrusted://",
    "devtools://",
    "chrome-extension://",
    "about:",
)
MARK_TABS = os.environ.get("CUA_GAC_MARK_TABS", "0") == "1"

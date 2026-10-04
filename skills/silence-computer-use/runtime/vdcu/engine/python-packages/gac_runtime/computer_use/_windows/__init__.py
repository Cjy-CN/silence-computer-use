"""Windows-native Computer Use implementation.

This subpackage holds the complete Windows behaviour and is intentionally kept
separate from the macOS code in :mod:`gac_runtime.computer_use._darwin`.
Platform selection happens in :mod:`gac_runtime.computer_use`; nothing outside
``computer_use`` should import this subpackage directly.

The public contract follows the Windows v2 API and uses JSON command payloads.
It intentionally differs from the macOS v1 app-state and window-targeting API.
"""

from .public.api import *
from .public.api import __all__
from .public.api import __getattr__

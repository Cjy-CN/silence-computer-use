"""Public GAC Runtime API.

Implementation lives in focused modules under :mod:`gac_runtime.browser`. This
module intentionally contains no connection or browser state initialization.
"""

import platform as _platform

from .browser import *
from .browser import __all__ as _browser_all
from .modules import BrowserUseCommand
from .wire import Runtime

__all__ = [
    *_browser_all,
    "BrowserUseCommand",
    "Runtime",
]

if _platform.system() == "Windows":
    from .modules import ComputerUseCommand

    __all__.append("ComputerUseCommand")

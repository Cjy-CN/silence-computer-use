"""Browser backend implementations."""

from .base import BrowserBackend
from .gac import *
from .gac import __all__ as _gac_all

__all__ = ["BrowserBackend", *_gac_all]

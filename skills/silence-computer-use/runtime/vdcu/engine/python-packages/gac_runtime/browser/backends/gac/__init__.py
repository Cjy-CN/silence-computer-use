"""Default GAC backend lifecycle."""

import os
import tempfile
import threading

from pathlib import Path
from typing import Any

from ..base import BrowserBackend
from .client import GacBackend
from .constants import GAC_ENV


TMP_DIR = Path(tempfile.gettempdir()) / "gac-runtime"
_backend: BrowserBackend | None = None
_backend_lock = threading.Lock()


def is_configured() -> bool:
    return all(os.environ.get(name) for name in GAC_ENV)


def request(payload: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
    global _backend
    if _backend is None:
        with _backend_lock:
            if _backend is None:
                _backend = GacBackend()
    return _backend.request(payload, timeout)


def install(backend: BrowserBackend) -> None:
    global _backend
    _backend = backend


def clear() -> None:
    global _backend
    _backend = None


def description() -> str:
    return "gac"


GACTransportAdapter = GacBackend

__all__ = [
    "GacBackend",
    "GACTransportAdapter",
    "TMP_DIR",
    "clear",
    "description",
    "install",
    "is_configured",
    "request",
]

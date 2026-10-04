"""Explicit direct delegation to seed_browser_use for stateless helpers."""

from __future__ import annotations

import importlib
from typing import Any

from ._routes import ROUTES, Route


def _seed_module():
    return importlib.import_module("seed_browser_use")


def _call_seed(seed_name: str, *args: Any, **kwargs: Any) -> Any:
    function = getattr(_seed_module(), seed_name)
    # Reuse seed's implementation without re-selecting the seed GAC profile.
    implementation = getattr(function, "__wrapped__", function)
    return implementation(*args, **kwargs)


def call_direct(tool: str, *args: Any, **kwargs: Any) -> Any:
    """Call a seed tool that is explicitly approved as SEED_DIRECT."""
    spec = ROUTES.get(tool)
    if spec is None or spec.route != Route.SEED_DIRECT:
        raise AssertionError(f"aha_browser_use.{tool} is not a SEED_DIRECT tool")
    return _call_seed(spec.seed_tool or tool, *args, **kwargs)


def call(tool: str, *args: Any, page_id: str | None = None, **kwargs: Any) -> Any:
    """Compatibility entry for seed-backed calls; only SEED_DIRECT is allowed."""
    if page_id is not None:
        raise AssertionError(f"aha_browser_use.{tool} does not accept page_id")
    return call_direct(tool, *args, **kwargs)


__all__ = ["call", "call_direct"]

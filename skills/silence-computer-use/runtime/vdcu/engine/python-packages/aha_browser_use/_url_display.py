"""URL compaction shared by model-facing observation renderers."""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit


_DEFAULT_URL_PORTS = {"http": 80, "https": 443}


def same_origin_relative_url(url: str, current_url: str) -> str | None:
    """Return a root-relative display URL when both URLs share an origin."""
    try:
        target = urlsplit(url)
        current = urlsplit(current_url)
        target_scheme = target.scheme.casefold()
        current_scheme = current.scheme.casefold()
        if target_scheme not in _DEFAULT_URL_PORTS or not target.hostname:
            return None
        if current_scheme not in _DEFAULT_URL_PORTS or not current.hostname:
            return None
        target_origin = (
            target_scheme,
            target.hostname.casefold(),
            target.port or _DEFAULT_URL_PORTS[target_scheme],
        )
        current_origin = (
            current_scheme,
            current.hostname.casefold(),
            current.port or _DEFAULT_URL_PORTS[current_scheme],
        )
        if target_origin != current_origin:
            return None
        return urlunsplit(
            ("", "", target.path or "/", target.query, target.fragment)
        )
    except ValueError:
        return None

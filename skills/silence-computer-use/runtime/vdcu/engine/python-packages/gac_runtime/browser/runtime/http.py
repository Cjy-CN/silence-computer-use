"""HTTP reads and authenticated direct-download helpers."""

import gzip
import os
import urllib.request

from urllib.parse import urlparse

from .transport import download_url as native_download_url


def http_get_bytes(url, headers=None, timeout=20.0):
    """Pure HTTP GET returning bytes, including binary/download responses."""
    if os.environ.get("BROWSER_USE_API_KEY"):
        try:
            from fetch_use import fetch_sync
            response = fetch_sync(url, headers=headers, timeout_ms=int(timeout * 1000))
            return response.content
        except ImportError:
            pass
    h = {"User-Agent": "Mozilla/5.0", "Accept-Encoding": "gzip"}
    if headers: h.update(headers)
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip": data = gzip.decompress(data)
        return data


def http_get(url, headers=None, timeout=20.0):
    """Pure HTTP GET returning text; undecodable binary bytes are replaced safely."""
    return http_get_bytes(url, headers=headers, timeout=timeout).decode(
        "utf-8", errors="replace"
    )


def download_url(url, filename=None, timeout=30.0):
    """Download an HTTP(S) URL through the active browser session."""
    target_url = str(url or "").strip()
    if urlparse(target_url).scheme.lower() not in ("http", "https"):
        raise ValueError("download_url requires an absolute http:// or https:// URL")
    return native_download_url(
        target_url,
        filename=filename,
        timeout=timeout,
    )

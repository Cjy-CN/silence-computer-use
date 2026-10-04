"""HTTP, download, and screenshot diagnostics."""

import os as _os

from urllib.parse import urlsplit as _urlsplit

from ..domain.core import *
from .actions import _element_clip_css


def http_get(url, headers=None, timeout=20.0):
    """Pure HTTP GET (read-only data, no UI)."""
    return _bh.http_get(url, headers=headers, timeout=timeout)


def _validated_download_timeout(timeout, operation):
    if isinstance(timeout, bool):
        raise _coded_error(
            TypeError,
            ERR_DOWNLOAD_TIMEOUT,
            "gac_runtime.%s: timeout must be seconds in [0, 180], not a "
            "boolean." % operation,
        )
    try:
        value = float(timeout)
    except (TypeError, ValueError) as exc:
        raise _coded_error(
            TypeError,
            ERR_DOWNLOAD_TIMEOUT,
            "gac_runtime.%s: timeout must be seconds in [0, 180]."
            % operation,
        ) from exc
    if not _math.isfinite(value) or not 0 <= value <= _MAX_WAIT_SECONDS:
        raise _coded_error(
            ValueError,
            ERR_DOWNLOAD_TIMEOUT,
            "gac_runtime.%s: timeout must be seconds in [0, 180]."
            % operation,
        )
    return value


def _validated_download_url(value):
    if not isinstance(value, str):
        raise BrowserUseError(
            "gac_runtime.download: target must be a snapshot ref or an "
            "absolute HTTP(S) URL. Nothing was downloaded.",
            code=ERR_DOWNLOAD_TARGET,
        )
    target_url = value.strip()
    try:
        parsed = _urlsplit(target_url)
        parsed.port
        valid = (
            parsed.scheme.lower() in ("http", "https")
            and bool(parsed.netloc)
            and bool(parsed.hostname)
            and not any(char.isspace() or ord(char) < 32 for char in target_url)
        )
    except ValueError:
        valid = False
    if not valid:
        raise BrowserUseError(
            "gac_runtime.download: target must resolve to an absolute HTTP(S) "
            "URL. Nothing was downloaded.",
            code=ERR_DOWNLOAD_TARGET,
        )
    return target_url


def _download_backend_call(operation, callback):
    try:
        return callback()
    except BrowserUseError:
        raise
    except Exception as exc:
        if ERR_SESSION_STALE in str(exc):
            raise
        raise BrowserUseError(
            "gac_runtime.%s: the browser download request failed. Check the "
            "page and browser session before retrying; no file is available."
            % operation,
            code=ERR_DOWNLOAD_BLOCKED,
        ) from exc


def _completed_download_record(record, operation):
    if not isinstance(record, dict):
        raise BrowserUseError(
            "gac_runtime.%s: the browser returned an invalid download result. "
            "No completed file is available." % operation,
            code=ERR_DOWNLOAD_BLOCKED,
        )

    state = str(record.get("state") or "unknown")
    if state != "completed":
        raise BrowserUseError(
            "gac_runtime.%s: Chrome canceled, blocked, or did not complete "
            "the download (state=%s). The file is incomplete and no path may "
            "be used." % (operation, state),
            code=ERR_DOWNLOAD_BLOCKED,
        )

    string_fields = ("guid", "url", "filename", "path", "source")
    invalid_fields = [
        field
        for field in string_fields
        if not isinstance(record.get(field), str) or not record[field]
    ]
    byte_count = record.get("bytes")
    if (
        not isinstance(byte_count, int)
        or isinstance(byte_count, bool)
        or byte_count < 0
    ):
        invalid_fields.append("bytes")
    path = str(record.get("path") or "")
    if (
        path.lower().endswith(".crdownload")
        or not _os.path.isabs(path)
        or not _os.path.isfile(path)
    ):
        invalid_fields.append("path")
    if invalid_fields:
        raise BrowserUseError(
            "gac_runtime.%s: Chrome reported completion without a valid final "
            "download record (%s). No path may be used."
            % (operation, ", ".join(sorted(set(invalid_fields)))),
            code=ERR_DOWNLOAD_BLOCKED,
        )
    return record


def wait_for_download(timeout=30.0):
    """Consume the earliest unreported terminal browser download."""
    value = _validated_download_timeout(timeout, "wait_for_download")
    record = _download_backend_call(
        "wait_for_download",
        lambda: _bh.wait_for_download(timeout=value),
    )
    if record is None:
        raise BrowserUseError(
            "gac_runtime.wait_for_download: no browser-triggered download "
            "completed within %.1f seconds. This API waits for downloads "
            "started by a page action; download() already waits for and "
            "returns its direct-download result." % value,
            code=ERR_DOWNLOAD_TIMEOUT,
        )
    return _completed_download_record(record, "wait_for_download")


def _download_target(target):
    if isinstance(target, str) and target.strip().lower().startswith(
        ("http://", "https://")
    ):
        return _validated_download_url(target), ""
    if not isinstance(target, str) or _split_ref(target)[1] is None:
        raise BrowserUseError(
            "gac_runtime.download: target must be a snapshot ref or an "
            "absolute HTTP(S) URL. Nothing was downloaded.",
            code=ERR_DOWNLOAD_TARGET,
        )
    try:
        selector = _sel(target)
    except BrowserRefError as exc:
        raise BrowserUseError(
            "gac_runtime.download: target must be a valid snapshot ref for "
            "the current document or an absolute HTTP(S) URL. Nothing was "
            "downloaded.",
            code=ERR_DOWNLOAD_TARGET,
        ) from exc
    raw = _download_backend_call(
        "download",
        lambda: _bh.js(
            "(()=>{const e=document.querySelector(%s);if(!e)return null;"
            "const url=e.href||e.currentSrc||e.src||e.getAttribute('href')||"
            "e.getAttribute('src')||'';"
            "return JSON.stringify({url,filename:e.getAttribute('download')||''});})()"
            % _json.dumps(selector)
        ),
    )
    try:
        value = _json.loads(raw) if isinstance(raw, str) and raw != "null" else raw
    except (TypeError, ValueError):
        value = None
    if not isinstance(value, dict):
        raise BrowserUseError(
            "gac_runtime.download: the target ref has no absolute HTTP(S) "
            "href/src/currentSrc. Nothing was downloaded.",
            code=ERR_DOWNLOAD_TARGET,
        )
    return (
        _validated_download_url(value.get("url")),
        str(value.get("filename") or ""),
    )


def download(ref_or_url, *, filename=None, timeout=30.0):
    """Download a ref's href/src or an absolute URL through the browser session.

    The request runs in the current browser profile and returns only after the
    complete file has been atomically published at its final path.
    """
    value = _validated_download_timeout(timeout, "download")
    if filename is not None and not isinstance(filename, str):
        raise _coded_error(
            TypeError,
            ERR_DOWNLOAD_TARGET,
            "gac_runtime.download: filename must be a string or None. "
            "Nothing was downloaded.",
        )
    url, suggested = _download_target(ref_or_url)
    with _dispatch("download", [_audit_value(url)]):
        record = _download_backend_call(
            "download",
            lambda: _bh.download_url(
                url,
                filename=filename or suggested or None,
                timeout=value,
            ),
        )
    if record is None:
        raise BrowserUseError(
            "gac_runtime.download: the direct download did not complete "
            "within %.1f seconds." % value,
            code=ERR_DOWNLOAD_TIMEOUT,
        )
    return _completed_download_record(record, "download")


def _png_size(data):
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
            return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    except Exception:
        pass
    return None, None


def screenshot(path=None, max_dim=1800, *, tag=None, show=True, ref=None):
    """Capture the viewport or one verified ref and return its PNG path.

    Without ``ref`` the frame is ``scope=viewport`` and can ground 0-1000
    viewport coordinates. With ``ref`` the element is scrolled into view and
    captured as ``scope=element``; those cropped pixels cannot ground
    ``click_xy`` or ``scroll``.

    ``show=False`` keeps the PNG in the VM for code-level inspection without
    adding an image to the model result.
    """
    _guard("screenshot")
    clip = _element_clip_css(ref) if ref is not None else None
    scope = "element" if ref is not None else "viewport"
    saved = _bh.capture_screenshot(
        path=path,
        full=False,
        max_dim=max_dim,
        clip=clip,
    )
    with open(saved, "rb") as fp:
        data = fp.read()
    width, height = _png_size(data)
    _record(
        "screenshot",
        [tag or "", _audit_value(ref) if ref is not None else "viewport"],
        state=DISPATCH_DISPATCHED,
    )
    executor = _get_executor()
    if show and executor is not None:
        try:
            try:
                executor.emit_image(
                    data, tag=tag, width=width, height=height, scope=scope
                )
            except TypeError:
                if scope == "element":
                    raise RuntimeError(
                        "the installed computer_use_tool cell runtime is too old to label "
                        "scope=element safely"
                    )
                executor.emit_image(data, tag=tag, width=width, height=height)
        except Exception as exc:
            raise RuntimeError(
                "gac_runtime.screenshot: could not deliver the frame: %s" % exc
            ) from exc
    return ScreenshotPath(saved, width, height)

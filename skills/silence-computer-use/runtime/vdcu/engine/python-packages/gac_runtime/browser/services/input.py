"""Public form input and file upload policies."""

from ..domain.core import *


def fill_input(selector, text, clear_first=True, timeout=0.0):
    """Fill a native input or textarea selected by CSS."""
    return _bh.fill_input(
        selector,
        text,
        clear_first=clear_first,
        timeout=timeout,
    )


def upload(ref, path):
    """Upload through a ref, CSS selector, or plain element id."""
    selector = _read_selector(ref)
    with _dispatch("upload", [_audit_value(ref), _audit_value(path)]):
        _bh.upload_file(selector, path)


def upload_file(selector, path):
    """Set one or more files on a file input selected by CSS."""
    return _bh.upload_file(selector, path)

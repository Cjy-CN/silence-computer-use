"""Document scope and target lookup primitives."""

from .transport import cdp
from .transport import send as _send


def next_doc_scope(url="", title=""):
    """Allocate the next document number ("d7") from the daemon.

    The counter has to live in the daemon: it is the only part of the stack that
    outlives both a navigation and a cell, and a per-page or per-process counter
    would restart and re-issue a number a still-open tab already holds.
    """
    return str(_send({"meta": "doc_scope", "url": str(url or ""), "title": str(title or "")}).get("scope") or "")

def doc_origin(scope):
    """Where a document number was minted: {"url","title","t"}, or {} if unknown.

    Diagnostics only — it is what lets a stale-ref refusal name the page the ref
    came from. There is deliberately no inverse (url -> number).
    """
    return _send({"meta": "doc_origin", "scope": str(scope or "")}).get("origin") or {}

def iframe_target(url_substr):
    """First iframe target whose URL contains `url_substr`. Use with js(..., target_id=...)."""
    for t in cdp("Target.getTargets")["targetInfos"]:
        if t["type"] == "iframe" and url_substr in t.get("url", ""):
            return t["targetId"]
    return None

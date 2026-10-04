"""Shared Browser Use models, errors, instrumentation, scope, and refs.

This module provides the shared contracts behind the public Browser Use services.
Snapshot generation and ref lookup live in ``browser.services.snapshot``.
``import gac_runtime as bu`` drives the Chrome page.

    import gac_runtime as bu
    bu.navigate("https://example.com"); bu.wait_for_load()
    bu.snapshot()                # -> scope=d7 …  /  d7:e2 button "Sign In"
    bu.type("d7:e3", "me@x.com")  # act on a ref (frame-safe fill)
    bu.click("d7:e2")             # click a ref (real CDP mouse event)
    bu.text("d7:e5")              # read an element's text
    bu.cdp("DOM.getDocument", depth=-1)   # raw CDP escape hatch

Two ways to locate a target (prefer the first):
  * **ref** — from ``bu.snapshot()``; stable, semantic, cheap. Act with
    ``bu.click(ref)`` / ``bu.type(ref, text)`` / ``bu.select`` / ``bu.text`` / …
    A ref is ``<document number>:<node>`` and is only valid in the document that
    issued it: pass it back exactly as printed. A ref from another document (or one
    with the number stripped off) is refused before anything is dispatched, so it
    can never land on a same-numbered element in the wrong tab.
  * **coords** — ``bu.click_xy(x, y)`` etc. are 0-1000 of the **page viewport**
    (DPR handled internally); use ONLY for canvas / no-DOM targets.

The GAC connection is lazy: importing this module does not contact the server.
"""
from __future__ import annotations

import contextlib as _contextlib
import difflib as _difflib
import functools as _functools
import inspect as _inspect
import json as _json
import math as _math
import os as _os
import re as _re
import sys as _sys
import time as _time
import uuid as _uuid

from ... import debug_trace as _debug_trace
from .. import runtime as _bh

# --------------------------------------------------------------------------- #
# Pure passthrough — re-exported verbatim (no coordinate/contract change).     #
# The model's escape hatches + tab/DOM helpers. Re-exporting (vs hand-written  #
# 1:1 forwarders) means zero shadow maintenance.                               #
# --------------------------------------------------------------------------- #
from ..runtime import (  # noqa: E402,F401
    js, cdp, iframe_target,
)

COORD_SPACE = 1000.0
_SCROLL_STEP_PX = 120  # one wheel "notch" ≈ 120 CSS px
_MAX_SCROLL_AMOUNT = 10
_MAX_WAIT_SECONDS = 180.0
_REF_ATTR = "data-scu-ref"
#: A separate attribute for the type() wrapper->child retarget. It must NOT be
#: _REF_ATTR: overwriting that would destroy the child's own snapshot ref and turn
#: a later action on it into a silent "stale/gone".
_RETARGET_ATTR = "data-scu-retgt"
#: Window property holding the document's number (see _SCOPE_READ_JS).
_SCOPE_PROP = "__seedCuaDocScope"
#: Window property holding the set of elements that were visible before a custom
#: dropdown was opened, so "newly visible" can bound the option search.
_PRE_VISIBLE_PROP = "__seedCuaPreVisible"
_UNSET = object()  # sentinel: distinguishes bu.type(ref) [text omitted] from bu.type(ref, "")

#: Scope label for an observation that is not tied to a live document (a pure
#: HTTP read, or a call made before any snapshot). The right shape for the label,
#: and unable to collide with a real document: the counter starts at 1.
_NO_DOC_SCOPE = "d0"

# --------------------------------------------------------------------------- #
# Stable error codes. The model reads the traceback; programs (renderer, trace   #
# analysis, recovery logic) read the code, which travels as the exception's       #
# ``code`` attribute and is rendered by the host as ``[error code=…]``.           #
#                                                                                #
# The code is deliberately NOT repeated inside the message. It used to be, for a  #
# transport that dropped the attribute; that transport was fixed, and the         #
# leftover ``gac_runtime [BU_REF_STALE]: …`` prefix then spent the model's   #
# attention on a token already on the line above. What the message must carry is  #
# the recovery instruction — that instruction is why the reminder channel is      #
# allowed to stay silent on every one of these codes, and a test pins it.         #
# --------------------------------------------------------------------------- #
ERR_REF_STALE = "BU_REF_STALE"
ERR_REF_UNSCOPED = "BU_REF_UNSCOPED"
ERR_REF_SHAPE = "BU_REF_SHAPE"
ERR_SESSION_STALE = "BU_SESSION_STALE"
ERR_NAV_WAIT = "BU_NAV_WAIT_TIMEOUT"
ERR_SELECT_NO_OPTION = "BU_SELECT_NO_OPTION"
ERR_SELECT_AMBIGUOUS = "BU_SELECT_AMBIGUOUS"
ERR_DOWNLOAD_BLOCKED = "BU_DOWNLOAD_BLOCKED"
ERR_DOWNLOAD_TIMEOUT = "BU_DOWNLOAD_TIMEOUT"
ERR_DOWNLOAD_TARGET = "BU_DOWNLOAD_TARGET"
ERR_READ_FIELDS = "BU_READ_FIELDS"
ERR_READ_LIMIT = "BU_READ_LIMIT"
ERR_READ_TARGET = "BU_READ_TARGET"
ERR_EXPORT_LIMIT = "BU_EXPORT_LIMIT"


class BrowserUseError(RuntimeError):
    """A browser-plane failure carrying a stable ``code``."""

    code = "BU_ERROR"

    def __init__(self, message, code=None):
        super().__init__(message)
        if code:
            self.code = code


class BrowserSessionError(BrowserUseError):
    """The CDP session BU was using is gone; re-attach before acting again."""

    code = ERR_SESSION_STALE


class BrowserRefError(ValueError):
    """The ref passed in cannot be resolved. Subclasses carry the specific code.

    ValueError (not RuntimeError) because ``_read_selector`` distinguishes "this
    is not a ref, try it as CSS" from a real failure by catching ValueError.
    """

    code = ERR_REF_SHAPE

    def __init__(self, message, code=None):
        super().__init__(message)
        if code:
            self.code = code


class UnscopedRefError(BrowserRefError):
    """A bare ``e12`` with no document scope — unverifiable, so refused."""

    code = ERR_REF_UNSCOPED


class StaleRefError(BrowserRefError):
    """The ref belongs to a document that is no longer the attached one."""

    code = ERR_REF_STALE


def _coded_error(exc_type, code, message):
    """Create a builtin exception that keeps a stable CNGC error code."""
    err = exc_type(message)
    err.code = code
    return err


class Ref(str):
    """String-compatible snapshot ref with attribute aliases models expect."""

    @property
    def ref(self):
        return str(self)

    @property
    def id(self):
        return str(self)


class RefList(list):
    """Ranked refs returned by :func:`find`.

    It remains a normal list for loops/indexing, while action helpers can safely
    consume it directly by taking the best-ranked first match. This matches how
    models naturally write ``bu.click(bu.find("Save"))`` without making arbitrary
    raw lists silently choose an element.
    """

    @property
    def first(self):
        return self[0] if self else None


class ScreenshotPath(str):
    """Filesystem path with the captured image dimensions attached."""

    def __new__(cls, path, width=None, height=None):
        self = super().__new__(cls, str(path))
        self.path = str(path)
        self.width = int(width) if width is not None else None
        self.height = int(height) if height is not None else None
        return self

    @property
    def size(self):
        return (self.width, self.height)


class TabRecord(dict):
    """Tab metadata supporting both mapping and attribute access."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None



def _tab_record(value):
    record = TabRecord(value or {})
    target = record.get("targetId") or record.get("target_id") or record.get("id") or ""
    record["targetId"] = target
    record["target_id"] = target
    record["id"] = target
    return record


# Installed for one computer_use_tool cell by ``_computer_use_tool_cell``.  Defaults stay permissive
# so the legacy Bash-era browser skill can still import this asset; the CNGC
# installer appends ``_STRICT_EXECUTOR = True`` to its in-VM copy.
_EXECUTOR = None
_STRICT_EXECUTOR = False


def install_executor(executor, *, strict=False):
    """Install the optional action/observation executor for this process."""
    global _EXECUTOR, _STRICT_EXECUTOR
    _EXECUTOR = executor
    _STRICT_EXECUTOR = bool(strict)


def _get_executor():
    return _EXECUTOR


def _guard(op):
    if _STRICT_EXECUTOR and _EXECUTOR is None:
        raise RuntimeError(
            "gac_runtime.%s: this library only works inside the computer_use_tool "
            "tool, which carries the runtime that records actions and returns "
            "screenshots. Re-issue this code as a computer_use_tool call." % op
        )


def _audit_value(value):
    text = str(value)
    return text if len(text) <= 80 else text[:80] + "…"


#: Lifecycle of one dispatched primitive. ``attempted`` is written BEFORE the CDP
#: call, so a cell killed mid-dispatch (timeout) still shows the in-flight action;
#: ``dispatched`` once Chrome accepted it; ``completed`` once the whole helper
#: returned. A helper that raises between attempted and completed is a partial:
#: side effects landed even though the call failed.
DISPATCH_ATTEMPTED = "attempted"
DISPATCH_DISPATCHED = "dispatched"
DISPATCH_COMPLETED = "completed"

#: None until probed: does the installed executor accept record(..., state=)?
_RECORD_STATE_SUPPORT = None


def _record(op, args=None, state=None):
    executor = _EXECUTOR
    if executor is None:
        return
    global _RECORD_STATE_SUPPORT
    values = [] if args is None else list(args)
    if _RECORD_STATE_SUPPORT is not False and state:
        # Argument binding fails before the body runs, so a TypeError here cannot
        # have recorded anything — probing is side-effect free.
        try:
            executor.record("bu", op, values, state=state)
            _RECORD_STATE_SUPPORT = True
            return
        except TypeError:
            _RECORD_STATE_SUPPORT = False
        except Exception:
            return
    if state and state != DISPATCH_COMPLETED:
        values = values + ["state=%s" % state]
    try:
        executor.record("bu", op, values)
    except Exception:
        pass


@_contextlib.contextmanager
def _dispatch(op, args=None):
    """Bracket one primitive that actually changes the browser.

    Every low-level dispatch goes through here so the audit distinguishes "we
    never touched the page" from "we touched it and then failed". Without it a
    helper that opens a dropdown and then cannot find the option reports an empty
    action list, and the model retries a step whose side effect already landed.
    """
    _record(op, args, state=DISPATCH_ATTEMPTED)
    yield
    _record(op, args, state=DISPATCH_DISPATCHED)


#: Depth of the current public-API call, so nested wrapped calls do not reset the
#: per-call scope memo out from under their caller.
_API_DEPTH = 0


def _translate_backend_error(exc):
    """Re-raise a daemon-level failure as a typed error, or None to keep it as is."""
    if ERR_SESSION_STALE in str(exc):
        return BrowserSessionError(
            "gac_runtime: the CDP session is gone (the tab was closed or "
            "crashed, or the browser attachment changed). Nothing was re-sent to "
            "another tab. Re-attach with bu.resync(), then take a fresh "
            "bu.snapshot() — every ref and viewport coordinate from before this "
            "point is stale.",
            code=ERR_SESSION_STALE,
        )
    return None


def _wrap_api(name, fn, *, action):
    @_functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        trace_call = _debug_trace.start_bu_call(name, args, kwargs)
        try:
            global _API_DEPTH, _SCOPE_MEMO
            _guard(name)
            if _API_DEPTH == 0:
                _SCOPE_MEMO = None
            _API_DEPTH += 1
            try:
                result = fn(*args, **kwargs)
            except RuntimeError as exc:
                translated = _translate_backend_error(exc)
                if translated is None:
                    raise
                raise translated from exc
            finally:
                _API_DEPTH -= 1
                if _API_DEPTH == 0:
                    _SCOPE_MEMO = None
            if action:
                values = [_audit_value(v) for v in args[:4]]
                values.extend(
                    f"{k}={_audit_value(v)}" for k, v in list(kwargs.items())[:4]
                )
                _record(name, values, state=DISPATCH_COMPLETED)
        except BaseException as error:
            _debug_trace.fail_bu_call(trace_call, error)
            raise
        _debug_trace.finish_bu_call(trace_call, result)
        return result

    return _wrapped


def _wrap_trace_api(name, fn):
    """Trace public helpers which intentionally skip the action audit wrapper."""

    @_functools.wraps(fn)
    def _wrapped(*args, **kwargs):
        trace_call = _debug_trace.start_bu_call(name, args, kwargs)
        try:
            result = fn(*args, **kwargs)
        except BaseException as error:
            _debug_trace.fail_bu_call(trace_call, error)
            raise
        _debug_trace.finish_bu_call(trace_call, result)
        return result

    return _wrapped


# --------------------------------------------------------------------------- #
# Typed observation channel.                                                    #
#                                                                               #
# A bare ``bu.snapshot()`` (return value discarded) must still show the model    #
# something, so the DOM map is self-displaying: it goes to the cell's typed       #
# channel, which labels it with the document its refs belong to and charges it    #
# to its own budget. That is what the channel is for — a body the model can act   #
# on, plus the space the body is valid in.                                        #
#                                                                                 #
# Everything else a helper reads (page text, an attribute, the tab list, the      #
# console, an HTTP reply) is an ordinary return value. It used to be echoed here  #
# too, under a ``read`` kind, and that kind gated nothing: it told the model      #
# neither which coordinates were usable nor which refs were live, while quietly   #
# spending the observation budget on anything a helper happened to return. Print  #
# what you want to see. Action verbs emit nothing.                                #
# --------------------------------------------------------------------------- #
#: True once we had to fall back to stdout because the typed channel was absent.
_OBSERVATION_FALLBACK = False
_OBSERVATION_FALLBACK_NOTED = False

#: The cell shell's observation-sink parameters, in its declaration order. Mirrors
#: ``OBSERVATION_SINK_PARAMS`` in ``cngc/assets/cell.py`` and is compared against it
#: by ``tests/test_cngc_cross_line_seams.py``: this file ships into the VM
#: independently of that one, so the two can be a revision apart and nothing but
#: these names holds them together.
#:
#: Every call below passes them BY KEYWORD for that reason. Positionally, a shell
#: that reordered ``scope`` and ``payload`` would still bind — the DOM body would
#: be filed as the document number, fail the shape check, silently become ``d0``,
#: ``text`` would come back empty, and nothing would raise, so
#: :data:`_OBSERVATION_FALLBACK` would stay dark while every page observation in
#: the run vanished. By keyword, a reorder is a non-event and a *rename* raises
#: ``TypeError``, which routes into the fallback: the observation still reaches the
#: model on stdout and the degradation is recorded.
_OBSERVATION_SINK_PARAMS = ("kind", "scope", "payload", "superseded", "path")


def _note_observation_fallback(reason):
    """Record (once, to disk only) that observations degraded to plain stdout.

    Deliberately not printed: the fallback exists so this asset stays testable
    against a cell runtime that predates ``emit_observation``, and a warning on
    the model's own output stream would be exactly the channel confusion this
    change removes.
    """
    global _OBSERVATION_FALLBACK, _OBSERVATION_FALLBACK_NOTED
    _OBSERVATION_FALLBACK = True
    if _OBSERVATION_FALLBACK_NOTED:
        return
    _OBSERVATION_FALLBACK_NOTED = True
    try:
        base = _os.environ.get("COMPUTER_USE_TOOL_SPOOL") or _os.environ.get("TEMP") or "/tmp"
        with open(_os.path.join(base, "bu_observation_fallback.log"), "a", encoding="utf-8") as fp:
            fp.write("%d observation channel unavailable (%s); printed to stdout\n"
                     % (int(_time.time()), reason))
    except Exception:  # noqa: BLE001 — diagnostics must never break a call
        pass


def _observation_sink():
    """The cell's ``emit_observation``, or None when this runtime has none.

    In every shipped runtime it is the **executor** that carries the method — the
    cell module deliberately exposes no module-level sink, because one cell's
    observations must not be reachable from another's. The module lookup below is
    therefore a miss in normal operation, kept only so a host that wants to
    intercept the channel has a seam to do it in.
    """
    cell = _sys.modules.get("_computer_use_tool_cell")
    fn = getattr(cell, "emit_observation", None) if cell is not None else None
    if not callable(fn):
        fn = getattr(_EXECUTOR, "emit_observation", None)
    return fn if callable(fn) else None


def _emit_observation(kind, payload="", *, scope="", superseded=None, path=None):
    """Send one observation to the typed channel. Returns True when it landed.

    ``superseded`` is left unset on purpose. It is positional, not temporal — an
    observation is superseded once the cell dispatches another action after it —
    so only the cell shell can settle it, once the cell has finished. Claiming
    "still current" here told the model a snapshot was live when four clicks had
    happened since.
    """
    fn = _observation_sink()
    if fn is not None:
        try:
            # Keyword-only, deliberately: see _OBSERVATION_SINK_PARAMS.
            fn(
                kind=kind,
                scope=scope or _NO_DOC_SCOPE,
                payload=payload,
                superseded=superseded,
                path=path,
            )
            return True
        except Exception as exc:  # noqa: BLE001 — degrade, never lose the observation
            _note_observation_fallback("emit_observation raised: %s" % exc)
    else:
        _note_observation_fallback("emit_observation unavailable")
    if path is None and payload not in (None, ""):
        try:
            print(payload)
        except Exception:  # noqa: BLE001 — never let display break the call
            pass
    return False


def _emit_notice(text):
    """Say one sentence about something the library noticed, on the cell's stdout.

    The four notices that use this — an occluded click target, a ref-less
    ``type``, a wrapper retarget, a write that did not read back — are advice,
    not observations. The typed channel carries bodies the model can act on (refs
    that are live, pixels that are addressable) and labels them with the space
    they belong to; a sentence has no such space, and the ``read`` kind invented
    to hold one gated nothing while collecting page text, network replies and
    action results under a label they had nothing in common under.

    So they print, and land in ``[output]`` beside whatever else the cell wrote.
    That is a real cost — the model cannot tell this sentence from one of its own
    — and it is the smaller one: a fourth label would have to be learned by the
    renderer, the reminder decoder and the Skill in order to carry a warning.
    """
    try:
        print(text)
    except Exception:  # noqa: BLE001 — never let display break the call
        pass


# --------------------------------------------------------------------------- #
# Document number: the identity a ref is bound to.                              #
#                                                                               #
# Ref numbering is per-document and starts over in every new document, so a bare #
# ``e2`` from one tab happily resolves to a DIFFERENT element's ``e2`` in the     #
# next tab — a silent alias, not a stale-ref error. Each document therefore       #
# carries one number, stored on its own ``window``; a new document (navigation,   #
# another tab, a reload) gets a fresh window and so a fresh number, while         #
# re-snapshotting — or coming back to — the SAME document finds the number        #
# already there and leaves existing refs alone. Every ref the model sees is       #
# ``<document>:<node>`` and every action re-reads the attached document's number  #
# and refuses to dispatch on a mismatch.                                         #
#                                                                               #
# The number is a plain counter, not a random token, because the model reads it: #
# holding ``d3`` while the page says ``d7`` says "four documents ago", where two  #
# hashes could only ever say "different". Allocation lives in the daemon (the     #
# only party that outlives both a navigation and a cell) and installation lives   #
# in the page, so the two halves are one round trip each and only on the first    #
# sight of a document.                                                            #
# --------------------------------------------------------------------------- #
#: A real document number. ``d0`` is excluded by construction — the counter starts
#: at 1 — so the "no live document" label can never collide with a real document.
_DOC_NUM_RE = _re.compile(r"\Ad[1-9]\d*\Z")
_DOC_NUM_JS_RE = "/^d[1-9][0-9]*$/"

#: Read the attached document's number, or '' when it does not have one yet.
#: Deliberately read-only: numbering a document the model has never observed and
#: then reporting it as "current" is worse than saying there is nothing to compare
#: against, and it would burn a number nobody asked for.
_SCOPE_READ_JS = r"""
  (() => {
    let s = null;
    try { s = window['__SCOPE_PROP__']; } catch (_) { s = null; }
    return (typeof s === 'string' && __DOC_NUM_RE__.test(s)) ? s : '';
  })()
""".replace("__SCOPE_PROP__", _SCOPE_PROP).replace("__DOC_NUM_RE__", _DOC_NUM_JS_RE)

#: Install a number the daemon just allocated — but only if the document still has
#: none. Conditional on purpose: a document that acquired one between the read and
#: this call keeps the one it has, so the number stays a property of the document
#: rather than of whoever asked last.
_SCOPE_INSTALL_JS = r"""
  (() => {
    const KEY = '__SCOPE_PROP__';
    let s = null;
    try { s = window[KEY]; } catch (_) { s = null; }
    if (typeof s === 'string' && __DOC_NUM_RE__.test(s)) return s;
    s = '__NUMBER__';
    try { window[KEY] = s; } catch (_) {}
    return s;
  })()
""".replace("__SCOPE_PROP__", _SCOPE_PROP).replace("__DOC_NUM_RE__", _DOC_NUM_JS_RE)

_SCOPE_STATE_JS = (
    "(() => JSON.stringify({scope: " + _SCOPE_READ_JS.strip()
    + ", url: location.href, title: document.title,"
    + " w: innerWidth, h: innerHeight}))()"
)

#: {"scope","url","title","w","h"} for the attached document, valid for exactly one
#: public-API call (``_wrap_api`` clears it). Within one call the document can only
#: change because of our own dispatch, and validation always runs before the first
#: dispatch, so the memoised value is the one we must validate against.
_SCOPE_MEMO = None


def _empty_scope_state():
    return {"scope": "", "url": "", "title": "", "w": 0, "h": 0}


def _as_scope_state(raw):
    """Normalise a ``{scope,url,title,w,h}`` JS payload into a state dict."""
    try:
        value = _json.loads(raw) if isinstance(raw, str) else (raw or {})
    except Exception:
        value = {}
    if not isinstance(value, dict):
        value = {}
    state = _empty_scope_state()
    state.update({
        "scope": str(value.get("scope") or ""),
        "url": str(value.get("url") or ""),
        "title": str(value.get("title") or ""),
        "w": value.get("w") or 0,
        "h": value.get("h") or 0,
    })
    return state


def _number_document(state):
    """Give the attached document its number. Returns '' if it could not be given.

    Two steps, on purpose: the daemon allocates (it is the only party that spans
    every document and every cell, so it is the only place a counter can be
    monotonic) and the page stores (so the number belongs to the document and
    comes back unchanged when the model returns to a tab it left).

    Failing here is fail-closed, not fail-open: the refs print unnumbered, and the
    first action on one is refused as BU_REF_UNSCOPED rather than dispatched at
    whatever document happens to be attached.
    """
    try:
        number = _bh.next_doc_scope(url=state.get("url") or "", title=state.get("title") or "")
    except Exception:  # noqa: BLE001 — an unnumbered document is refused, not aliased
        return ""
    if not _DOC_NUM_RE.match(str(number or "")):
        return ""
    try:
        installed = _bh.js(_SCOPE_INSTALL_JS.replace("__NUMBER__", number))
    except Exception:  # noqa: BLE001
        return ""
    installed = str(installed or "").strip()
    return installed if _DOC_NUM_RE.match(installed) else ""


def _scope_state(*, mint=False, strict=False):
    """Read (and optionally number) the attached document."""
    global _SCOPE_MEMO
    if _SCOPE_MEMO is not None and (_SCOPE_MEMO.get("scope") or not mint):
        return _SCOPE_MEMO
    try:
        raw = _bh.js(_SCOPE_STATE_JS)
    except Exception:
        # Validation must never treat "could not ask the page" as "scope matches".
        if strict:
            raise
        return _SCOPE_MEMO or _empty_scope_state()
    state = _as_scope_state(raw)
    if mint and not state["scope"]:
        state["scope"] = _number_document(state)
    _SCOPE_MEMO = state
    return state


def _doc_origin(scope):
    """The daemon's record of where a document number was minted, or {}.

    Diagnostics only, and one-directional by design: number -> where it came from
    is what lets a stale ref name its own page. The inverse (url -> number) is not
    offered anywhere, because it would let a model write a ref for a document it
    has never observed — the exact move the numbering exists to refuse.
    """
    try:
        origin = _bh.doc_origin(scope)
    except Exception:  # noqa: BLE001 — an error path must never raise a second error
        return {}
    return origin if isinstance(origin, dict) else {}


#: ``d7:e12`` — the one form snapshot() prints and find() returns. An earlier
#: revision printed ``@d…:e…`` while find() returned the same ref without the
#: sigil, which left "pass it back exactly as printed" with two readings; the
#: sigil is now accepted on input and never printed.
#:
#: The document part is matched loosely (any ``d`` + hex-ish run) rather than as a
#: number, so a ref in the old shape lands on the identity check and gets the
#: stale-ref refusal that names both documents, instead of a shape complaint about
#: a ref that is structurally fine. A bare ``e12`` parses with scope None and is
#: refused (see _sel).
_REF_RE = _re.compile(r"\A@?(?:(d[0-9a-f]+):)?(e\d+)\Z")


def _split_ref(value):
    """(scope, node) for a snapshot ref, or (None, None) when it is not one."""
    if not isinstance(value, str) or not value:
        return None, None
    match = _REF_RE.match(value.strip())
    if match is None:
        return None, None
    return match.group(1), match.group(2)


#: Characters an error line may spend on one URL. A contractual refusal has a
#: ~200 token budget for the whole message; two URLs at this width leave room for
#: the sentence around them and still name each page recognisably.
_URL_BUDGET = 96


def _short_url(url):
    """A URL an error can afford, spending the budget on the identifying parts.

    Which page this is lives in the origin and the path; the query string is
    simultaneously the longest part and the least informative, so it is the first
    thing given up — with whatever room is left spent on its front, where ``?id=``
    and ``?q=`` sit. A trailing ``…`` always means something was cut.
    """
    text = str(url or "").strip()
    if not text:
        return "(url unknown)"
    if len(text) <= _URL_BUDGET:
        return text
    scheme, sep, rest = text.partition("://")
    if not sep:
        # data:, about:, blob: — no host and no path, and the tail of a data body
        # identifies nothing. The front is the whole of what can be said.
        return text[:40] + "…"
    host, slash, path = rest.partition("/")
    head = scheme + "://" + host
    body = (slash + path).split("?", 1)[0].split("#", 1)[0]
    kept = head + body
    if len(kept) < _URL_BUDGET:
        room = _URL_BUDGET - len(kept) - 1
        return kept + text[len(kept):][:room] + "…"
    room = _URL_BUDGET - len(head) - 2
    if room < 8:  # a pathological host leaves nothing worth keeping after it
        return text[:_URL_BUDGET - 1] + "…"
    return head + "…" + body[-room:] + "…"


def _issued_for(scope):
    """Which page a document number was handed out for, as an error can print it."""
    url = (_doc_origin(scope) or {}).get("url") or ""
    return _short_url(url) if url else "document %s, which this session no longer has a record of" % scope


def _attached_now(state):
    """``d7 https://…`` for the attached document — the other half of a stale error."""
    scope = state.get("scope") or ""
    if not scope:
        return "a document that has not been snapshotted yet"
    url = state.get("url") or ""
    return "%s %s" % (scope, _short_url(url)) if url else scope


def _sel(ref):
    """Selector for a numbered ref — and the single fail-closed gate before dispatch.

    Every action resolves its target through here (directly or via
    ``_read_selector``) BEFORE any CDP call, so a ref from another document can
    never reach the page. Three refusals, each with a stable code:

    * ``BU_REF_SHAPE``    — not a ref at all (a CSS selector or visible text).
    * ``BU_REF_UNSCOPED`` — a bare ``e12``. Which document issued it is
      unknowable, and "resolve it against whatever is attached now" is exactly
      the silent cross-document alias this numbering exists to remove.
    * ``BU_REF_STALE``    — numbered, but for a document that is no longer attached.

    The last two name both documents by URL, so the model can place the ref it is
    holding and the page it is on without having to recognise any token.
    """
    if isinstance(ref, RefList):
        if not ref:
            raise ValueError("gac_runtime: find() returned no refs")
        ref = ref[0]
    elif isinstance(ref, (list, tuple)):
        if len(ref) == 1:
            ref = ref[0]
        else:
            raise ValueError(
                "gac_runtime: expected one ref, got %d. Choose one item "
                "(for example refs[0]) after inspecting the matches." % len(ref)
            )
    if not isinstance(ref, str) or not ref:
        raise BrowserRefError(f"gac_runtime: invalid ref {ref!r}")
    scope, node = _split_ref(ref)
    if node is None:
        # The model sometimes passes a CSS selector or raw text (e.g.
        # "a[href='/opportunities']"). Embedded into querySelector('[data-scu-ref=…]')
        # it produces a JS SyntaxError that aborts the whole cell, so name the real
        # fix instead.
        raise BrowserRefError(
            f"gac_runtime: {ref!r} is not a snapshot ref. Refs "
            "look like 'd7:e12' — get one from bu.snapshot() or bu.find('text'). "
            "Do NOT pass a CSS selector or visible text here; to act by CSS use "
            "bu.js(...), to act by text use bu.find('text')[0].",
            code=ERR_REF_SHAPE,
        )
    if scope is None:
        # Name the attached document without numbering it: a document the model has
        # never snapshotted should be reported as such, not handed a number by the
        # error path for a ref that cannot have come from it.
        try:
            here = _scope_state()
        except Exception:  # noqa: BLE001 — the refusal stands even if the page is mute
            here = _empty_scope_state()
        if here.get("scope"):
            hint = "The attached document is %s, where that ref is written '%s:%s'." % (
                _attached_now(here), here["scope"], node,
            )
        else:
            hint = "No document has been snapshotted yet."
        raise UnscopedRefError(
            f"gac_runtime: {ref} has no document number, so it "
            f"cannot be checked against the page and was NOT dispatched. {hint} Take a "
            "bu.snapshot() and pass a ref back exactly as printed.",
            code=ERR_REF_UNSCOPED,
        )
    here = _scope_state(strict=True)
    if scope != (here.get("scope") or ""):
        raise StaleRefError(
            f"gac_runtime: {ref} was issued for %s; the attached "
            "document is now %s. Nothing was dispatched — take a fresh bu.snapshot() and "
            "use the refs it prints." % (_issued_for(scope), _attached_now(here)),
            code=ERR_REF_STALE,
        )
    return f'[{_REF_ATTR}="{node}"]'


def _read_selector(target):
    """Ref-first selector for read/upload helpers, with an explicit CSS escape hatch."""
    try:
        return _sel(target)
    except (UnscopedRefError, StaleRefError):
        # A ref-shaped target that failed verification must NOT decay into a CSS
        # guess: "#e12" would silently read some unrelated element with that id.
        raise
    except ValueError:
        if not isinstance(target, str) or not target:
            raise
        if target.startswith(("#", ".", "[", "*")) or any(ch in target for ch in (" ", ">", ":", ",")):
            return target
        # A plain identifier is overwhelmingly an element id in model-authored
        # test/workflow code (fileInput, output, later).
        if target.replace("-", "").replace("_", "").isalnum():
            return "#" + target
        raise


# Internal modules deliberately import private shared symbols from here.
__all__ = [name for name in globals() if not name.startswith("__")]

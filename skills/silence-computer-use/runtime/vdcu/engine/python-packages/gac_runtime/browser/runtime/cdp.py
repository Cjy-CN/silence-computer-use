"""CDP JavaScript evaluation and iframe session handling."""

import json
import math
import re

from .transport import cdp


def _js_snippet(expression, limit=160):
    snippet = expression.strip().replace("\n", "\\n")
    return snippet[:limit - 3] + "..." if len(snippet) > limit else snippet


def _js_exception_description(result, details):
    desc = result.get("description")
    exc = details.get("exception") if details else None
    if not desc and isinstance(exc, dict):
        desc = exc.get("description")
        if desc is None and "value" in exc:
            desc = str(exc["value"])
        if desc is None:
            desc = exc.get("className")
    if not desc and details:
        desc = details.get("text")
    return desc or "JavaScript evaluation failed"


def _decode_unserializable_js_value(value):
    if value == "NaN":
        return math.nan
    if value == "Infinity":
        return math.inf
    if value == "-Infinity":
        return -math.inf
    if value == "-0":
        return -0.0
    if value.endswith("n"):
        return int(value[:-1])
    return value


def _runtime_value(response, expression):
    result = response.get("result", {})
    details = response.get("exceptionDetails")
    if details or result.get("subtype") == "error":
        desc = _js_exception_description(result, details)
        if details:
            line = details.get("lineNumber")
            col = details.get("columnNumber")
            loc = f" at line {line}, column {col}" if line is not None and col is not None else ""
        else:
            loc = ""
        raise RuntimeError(f"JavaScript evaluation failed{loc}: {desc}; expression: {_js_snippet(expression)}")
    if "value" in result:
        return result["value"]
    if "unserializableValue" in result:
        return _decode_unserializable_js_value(result["unserializableValue"])
    return None


def _runtime_evaluate(expression, session_id=None, await_promise=False):
    try:
        r = cdp("Runtime.evaluate", session_id=session_id, expression=expression, returnByValue=True, awaitPromise=await_promise)
    except TimeoutError as e:
        raise RuntimeError(f"Runtime.evaluate timed out; expression: {_js_snippet(expression)}") from e
    return _runtime_value(r, expression)


def _wrap_js_function(expression):
    return f"(function(){{{expression}}})()"


def _is_illegal_return_error(exc):
    return "Illegal return statement" in str(exc)


# --- navigation / page ---
def js(expression, target_id=None):
    """Run JS in the attached tab (default) or inside an iframe target (via iframe_target()).

    Expressions are evaluated as-is first. If Chrome reports an illegal top-level
    `return`, the snippet is retried inside a function wrapper, so both
    `document.title` and `const x = 1; return x` work without mis-wrapping nested
    functions that contain their own returns.

    Top-level `const`/`let`/`var`/`function`/`class` declarations are auto-wrapped in an
    IIFE first: Runtime.evaluate shares one persistent global context across calls, so a
    bare `const x=...` leaks and a second call throws "Identifier 'x' already declared".
    Wrapping scopes the declaration to the call. An IIFE that already wraps everything is
    left alone.
    """
    sid = cdp("Target.attachToTarget", targetId=target_id, flatten=True)["sessionId"] if target_id else None
    expr = expression
    stripped = expression.lstrip()
    declares = re.match(r'(const|let|var|function|class)\s', stripped)
    already_iife = stripped.startswith("(") and (stripped.startswith("(()=>") or stripped.startswith("(function") or stripped.startswith("(async"))
    if declares and not already_iife:
        expr = _wrap_js_function(expression)
    try:
        try:
            return _runtime_evaluate(expr, session_id=sid, await_promise=True)
        except RuntimeError as e:
            if _is_illegal_return_error(e):
                return _runtime_evaluate(_wrap_js_function(expression), session_id=sid, await_promise=True)
            raise
    finally:
        if sid:
            cdp("Target.detachFromTarget", sessionId=sid)


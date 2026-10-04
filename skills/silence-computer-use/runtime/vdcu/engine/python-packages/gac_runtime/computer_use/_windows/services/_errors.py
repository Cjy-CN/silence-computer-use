"""Windows Computer Use command boundary."""

import re

from collections.abc import Callable
from typing import Any


_SERVER_ERROR_RE = re.compile(
    r"^Computer Use Server returned error: "
    r"\[([A-Z][A-Z0-9_]{2,47})\](?:\s+|$)(.*)$",
    re.DOTALL,
)


def coded_error(
    exception_type: type[Exception],
    operation: str,
    code: str,
    detail: str,
) -> Exception:
    """Create a model-facing exception carrying a stable error code."""

    error = exception_type(
        f"seed_computer_use.{operation}: [{code}] {detail}"
    )
    error.code = code
    return error


def execute_command[T](
    operation: str,
    command: Callable[..., T],
    *args: Any,
    **kwargs: Any,
) -> T:
    """Execute one typed command and preserve the server's stable error."""

    try:
        return command(*args, **kwargs)
    except RuntimeError as error:
        code = getattr(error, "code", "")
        match = _SERVER_ERROR_RE.match(str(error))
        if not isinstance(code, str) or not code:
            raise
        detail = match.group(2) if match and match.group(1) == code else str(error)
        raise coded_error(RuntimeError, operation, code, detail) from error

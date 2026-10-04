import functools
import json
import sys

from typing import Any
from typing import Callable
from typing import cast

def _dump_object(val: Any) -> str:
    if isinstance(val, (str, int, float)):
        return str(val)
    elif isinstance(val, (bytes, bytearray)):
        return val.decode('utf-8', errors = 'replace')
    else:
        return as_json(val)

def _guarded_call[R](
    f: Callable[..., R],
    args: tuple[Any],
    kwargs: dict[str, Any],
    default: R | Callable[[], R],
    retry_with: Callable[[], bool] | None,
    error_message: str | None,
) -> R:
    if error_message is None:
        error_message = f'Execution failure'

    for _ in range(5):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            if retry_with is not None:
                if not retry_with():
                    error_message += f' with non-retryable error: {e}'
                    break
                else:
                    print(f'[{f.__name__}] {error_message} with error: {e}, try again later', file = sys.stderr)
            else:
                error_message += f' with error (too many failures): {e}'
                break

    print(f'[{f.__name__}] {error_message}', file = sys.stderr)
    return cast(R, default()) if callable(default) else default

def as_json(val: Any) -> str:
    return json.dumps(val, separators = (',', ':'), ensure_ascii = False)

def echo(f):
    @functools.wraps(f)
    def wrapped(*args, **kwargs):
        output = f(*args, **kwargs)
        print(_dump_object(output))
        return output
    return wrapped

def guarded(*, default, retry_with: Callable[[], bool] | None = None, error_message: str | None = None):
    def wrapper(f):
        @functools.wraps(f)
        def wrapped(*args, **kwargs):
            return _guarded_call(f, args, kwargs, default, retry_with, error_message)
        return wrapped
    return wrapper

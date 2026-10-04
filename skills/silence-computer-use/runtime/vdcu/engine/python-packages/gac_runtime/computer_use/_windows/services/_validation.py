"""Windows Computer Use argument normalization."""

import math

from ._errors import coded_error


_KEY_ALIASES = {
    "arrowleft": "left",
    "arrowright": "right",
    "arrowup": "up",
    "arrowdown": "down",
}
_SCROLL_ALIASES = {
    "up": (5, False),
    "down": (-5, False),
    "right": (5, True),
    "left": (-5, True),
}


def string(value, operation: str, parameter: str) -> str:
    if not isinstance(value, str):
        raise coded_error(
            TypeError,
            operation,
            "CU_INVALID_ACTION",
            f"{parameter} must be a string",
        )
    return value


def coordinate(
    value,
    operation: str,
    parameter: str,
) -> int | float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise coded_error(
            TypeError,
            operation,
            "CU_INVALID_ACTION",
            f"{parameter} must be a finite number in [0, 1000]",
        )
    return value


def scroll_delta(value) -> tuple[int, bool]:
    if isinstance(value, str):
        alias = value.strip().lower()
        try:
            return _SCROLL_ALIASES[alias]
        except KeyError as error:
            raise coded_error(
                ValueError,
                "scroll",
                "CU_SCROLL_DELTA",
                "delta string must be one of 'up', 'down', 'right', or 'left'",
            ) from error
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise coded_error(
            TypeError,
            "scroll",
            "CU_SCROLL_DELTA",
            "delta must be a finite non-zero number or direction alias",
        )
    if value == 0:
        raise coded_error(
            ValueError,
            "scroll",
            "CU_SCROLL_DELTA",
            "delta must not be zero",
        )
    if not -10 <= value <= 10:
        raise coded_error(
            ValueError,
            "scroll",
            "CU_SCROLL_DELTA",
            "delta must be in [-10, 10]",
        )
    normalized = round(value)
    if normalized == 0:
        normalized = 1 if value > 0 else -1
    return normalized, False


def text(value) -> str:
    return "" if value is None else str(value)


def key(value) -> str:
    normalized = str(value).lower()
    return _KEY_ALIASES.get(normalized, normalized)


def hotkey_keys(values) -> tuple[str, ...]:
    normalized = tuple(key(value) for value in values)
    if (
        len(normalized) == 1
        and normalized[0] != "+"
        and "+" in normalized[0]
    ):
        raise coded_error(
            ValueError,
            "hotkey",
            "CU_BAD_HOTKEY",
            "pass each key separately",
        )
    return normalized


def duration_milliseconds(seconds) -> int:
    if isinstance(seconds, bool):
        raise coded_error(
            TypeError,
            "wait",
            "CU_WAIT_SECONDS",
            "seconds must be a number in [0, 180], not a boolean",
        )
    try:
        value = float(seconds)
    except (TypeError, ValueError) as error:
        raise coded_error(
            TypeError,
            "wait",
            "CU_WAIT_SECONDS",
            "seconds must be a number in [0, 180]",
        ) from error
    if not math.isfinite(value) or not 0 <= value <= 180:
        raise coded_error(
            ValueError,
            "wait",
            "CU_WAIT_SECONDS",
            "seconds must be in [0, 180]. Millisecond values must be divided "
            "by 1000",
        )
    return round(value * 1000)

"""AX public-operation orchestration over the typed wire command."""

from __future__ import annotations

import base64
import binascii
import math

from collections.abc import Mapping
from typing import Any

from ...modules.computer_use_ax import ComputerUseAxCommand
from ...modules.computer_use_ax import Frame
from ...modules.computer_use_ax import MAX_SCREENSHOT_PNG


class Record(dict[str, Any]):
    """Dictionary whose fields are also readable as attributes."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as error:
            raise AttributeError(name) from error


class Rows(list[Record]):
    """Rows returned by :meth:`AppState.find`."""

    def __init__(self, items=(), *, pattern: str, searched: int):
        super().__init__(items)
        self._pattern = pattern
        self._searched = searched

    def __getitem__(self, index):
        try:
            return super().__getitem__(index)
        except IndexError:
            if not isinstance(index, int) or self:
                raise
            error = IndexError(
                f"no element's line contains {self._pattern!r}; "
                f"{self._searched} rows were searched"
            )
            error.code = "CU_AX_FIND_EMPTY"
            raise error from None


class Elements(list[Record]):
    """AX rows addressed by their printed ``element_index``."""

    def __getitem__(self, index):
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            return super().__getitem__(index)
        for row in self:
            if row.get("index") == index:
                return row
        error = ValueError(f"no row in this tree is labelled {index}")
        error.code = "CU_AX_ELEMENT_INVALID"
        raise error


class AppState(Record):
    """One server-rendered AX observation."""

    @property
    def app_id(self) -> str:
        app = self.get("app")
        return str(app.get("app_id") or "") if isinstance(app, Mapping) else ""

    @property
    def bundle_id(self) -> str:
        app = self.get("app")
        return str(app.get("bundle_id") or "") if isinstance(app, Mapping) else ""

    @property
    def name(self) -> str:
        app = self.get("app")
        return str(app.get("name") or "") if isinstance(app, Mapping) else ""

    def find(self, pattern, *, limit=20) -> Rows:
        needle = str(pattern or "").casefold()
        if not needle.strip():
            error = ValueError("find() requires a non-empty pattern")
            error.code = "CU_AX_ARG_SHAPE"
            raise error
        maximum = max(1, int(limit))
        elements = self.get("elements") or ()
        matches = [
            row
            for row in elements
            if needle in str(row.get("line") or "").casefold()
        ]
        return Rows(
            matches[:maximum],
            pattern=str(pattern or ""),
            searched=len(elements),
        )


class _Row(Record):
    """Public row carrying its observation token only as a private attribute."""


def _protocol_error(message: str) -> RuntimeError:
    return RuntimeError(f"protocol error: {message}")


def _local_error(
    error_type: type[Exception],
    code: str,
    message: str,
) -> Exception:
    error = error_type(message)
    error.code = code
    return error


def _record(value: Any) -> Any:
    if isinstance(value, Mapping):
        return Record({str(key): _record(item) for key, item in value.items()})
    if isinstance(value, list):
        return [_record(item) for item in value]
    return value


def _app_id(value):
    if isinstance(value, Mapping):
        value = value.get("app_id") or value.get("bundle_id")
    if not isinstance(value, str):
        raise _local_error(
            TypeError,
            "CU_AX_ARG_SHAPE",
            "app_id must be a non-empty string",
        )
    if not value:
        raise _local_error(
            ValueError,
            "CU_AX_ARG_SHAPE",
            "app_id must be a non-empty string",
        )
    return value


def _pid(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _local_error(
            TypeError,
            "CU_AX_ARG_SHAPE",
            "pid must be an integer or None",
        )
    return value


def _target(value) -> Any:
    if not isinstance(value, Mapping):
        return value
    return value.get("index")


def _action(method: str, param: dict[str, Any]) -> Record:
    response = ComputerUseAxCommand.message({"method": method, "params": param})
    if response.get("ok") is not True:
        raise _protocol_error(
            f"Computer Use V2 {method} response is not successful"
        )
    return _record(response)


def _element_action(
    type_name: str,
    element_index,
    *,
    app_id,
    pid=None,
    **param: Any,
) -> Record:
    pid = _pid(pid)
    request = {
        "app_id": _app_id(app_id),
        "element_index": _target(element_index),
        **param,
    }
    if pid is not None:
        request["pid"] = pid
    return _action(type_name, request)


def list_apps() -> list[Record]:
    """Return the Server's app catalog, including each app's process ID."""

    response = ComputerUseAxCommand.message({"method": "app.list_apps", "params": {}})
    apps = response.get("apps")
    if not isinstance(apps, list):
        raise _protocol_error("Computer Use AX list_apps response has no apps array")
    return [
        _record({"bundle_id": app.get("app_id"), **app})
        for app in apps
        if isinstance(app, Mapping)
    ]


def _element_type(value: Any) -> str:
    type_name = str(value or "element")
    return type_name[2:] if type_name.startswith("AX") else type_name


def _element_line(element: Mapping[str, Any], depth: int) -> str:
    parts = [
        "  " * depth + str(element.get("index")),
        _element_type(element.get("type")),
    ]
    for key in ("type_description", "current_value"):
        value = element.get(key)
        if value not in (None, ""):
            parts.append(str(value))
    information = element.get("information")
    if isinstance(information, Mapping):
        parts.extend(
            f"{key}={value}"
            for key, value in information.items()
            if key != "Value" or value != element.get("current_value")
        )
    flags = (["disabled"] if element.get("enabled") is False else []) + [
        name
        for name in ("selectable", "selected", "settable", "is_depth_droped")
        if element.get(name) is True
    ]
    actions = element.get("actions")
    if isinstance(actions, list):
        flags.extend(str(action) for action in actions)
    if flags:
        parts.append("(" + ", ".join(flags) + ")")
    return " ".join(parts)


def _flatten_elements(values: list[Any], *, depth: int = 0) -> Elements:
    rows = Elements()
    for value in values:
        if not isinstance(value, Mapping):
            raise _protocol_error("Computer Use V2 UI element is not an object")
        row = _Row(
            {
                str(key): _record(
                    _element_type(item) if key == "type" else item
                )
                for key, item in value.items()
                if key != "children"
            }
        )
        row["line"] = _element_line(value, depth)
        rows.append(row)
        children = value.get("children") or []
        if not isinstance(children, list):
            raise _protocol_error("Computer Use V2 UI element children is not an array")
        rows.extend(_flatten_elements(children, depth=depth + 1))
    return rows


def _screenshots(values: Any) -> list[Frame]:
    if not isinstance(values, list):
        raise _protocol_error("Computer Use V2 screenshots is not an array")
    frames = []
    for value in values:
        if not isinstance(value, Mapping):
            raise _protocol_error("Computer Use V2 screenshot is not an object")
        try:
            image = base64.b64decode(value.get("image_base64"), validate=True)
        except (TypeError, ValueError, binascii.Error) as error:
            raise _protocol_error("Computer Use V2 screenshot has invalid base64") from error
        if len(image) > MAX_SCREENSHOT_PNG:
            raise _protocol_error("Computer Use V2 screenshot exceeds 128 MiB")
        frames.append(
            Frame(
                image,
                index=value.get("index", 0),
                window_id=value.get("window_id", 0),
                mime_type=value.get("mime_type", "image/png"),
            )
        )
    return frames


def _app_state(param: Mapping[str, Any], *, app_id: str) -> AppState:
    if not isinstance(param, Mapping):
        raise _protocol_error("Computer Use AX observation response is missing")
    state_value = param.get("state")
    if param.get("ok") is not True or not isinstance(state_value, Mapping):
        raise _protocol_error("Computer Use AX observation response is malformed")
    pid = state_value.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int):
        raise _protocol_error("Computer Use AX observation pid is not an integer")
    ui_element = state_value.get("ui_element") or []
    snapshots = param.get("window_snapshot_info_list") or []
    if not isinstance(ui_element, list) or not isinstance(snapshots, list):
        raise _protocol_error("Computer Use AX observation has invalid collections")
    rows = _flatten_elements(ui_element)
    frames = _screenshots(param.get("screenshots") or [])
    windows_by_id: dict[int, Record] = {}

    def add_window(value: Mapping[str, Any]) -> None:
        window_id = value.get("window_id")
        if isinstance(window_id, int) and not isinstance(window_id, bool):
            windows_by_id.setdefault(window_id, _record({"id": window_id}))

    def add_element_windows(values: list[Any]) -> None:
        for value in values:
            if not isinstance(value, Mapping):
                continue
            add_window(value)
            children = value.get("children")
            if isinstance(children, list):
                add_element_windows(children)

    for value in snapshots:
        if isinstance(value, Mapping):
            add_window(value)
    add_element_windows(ui_element)
    foreign_windows = state_value.get("foreign_child_windows") or []
    if isinstance(foreign_windows, list):
        for value in foreign_windows:
            if isinstance(value, Mapping):
                add_window(value)

    state_data = {
        "app": _record(
            {
                "app_id": app_id,
                "bundle_id": state_value.get("app_id"),
                "name": state_value.get("name", ""),
                "pid": pid,
            }
        ),
        "pid": pid,
        "focused_element_index": state_value.get("focused_element_index"),
        "view": "full",
        "windows": list(windows_by_id.values()),
        "elements": rows,
        "tree": "\n".join(row.line for row in rows),
        "screenshots": frames,
    }
    if "added_windows" in param:
        added_windows = param["added_windows"]
        if not isinstance(added_windows, list):
            raise _protocol_error("Computer Use AX added_windows is not an array")
        if not all(isinstance(window, Mapping) for window in added_windows):
            raise _protocol_error(
                "Computer Use AX added_windows contains a non-object"
            )
        state_data["added_windows"] = _record(added_windows)
    state = AppState(state_data)
    state._frame = frames[0] if frames else None
    return state


def get_app_state(
    app_id,
    *,
    window_id=None,
    screenshot=False,
    pid=None,
) -> AppState:
    """Observe one app through Computer Use V2."""

    app_id = _app_id(app_id)
    pid = _pid(pid)
    params = {
        "app_id": app_id,
        "options": {"enable_snapshot": screenshot},
    }
    if pid is not None:
        params["pid"] = pid
    if window_id is not None:
        params["window_id"] = window_id
    return _app_state(
        ComputerUseAxCommand.message(
            {"method": "app.get_app_state", "params": params}
        ),
        app_id=app_id,
    )


def click(
    app_id,
    element_index,
    *,
    x=None,
    y=None,
    mouse_button="left",
    click_count=1,
    pid=None,
) -> Record:
    """Invoke an element action or click at a screenshot-normalized point.

    ``x`` and ``y`` use the 0-1000 coordinate space of the matching window
    screenshot from the latest ``get_app_state(..., screenshot=True)`` call in
    the same execution.
    """

    param = {
        "button": mouse_button,
        "clicks": click_count,
    }
    if x is not None:
        param["x"] = x
    if y is not None:
        param["y"] = y
    return _element_action(
        "app.click_app",
        element_index,
        app_id=app_id,
        pid=pid,
        **param,
    )


def type_text(app_id, element_index, text, *, pid=None) -> Record:
    """Type text into the app's focused element."""

    return _element_action(
        "app.type_text",
        element_index,
        app_id=app_id,
        pid=pid,
        text=text,
    )


def press_key(app_id, element_index, key, *, pid=None) -> Record:
    """Send one key or a ``+``-joined shortcut."""

    return _element_action(
        "app.press_key",
        element_index,
        app_id=app_id,
        pid=pid,
        key=key,
    )


def set_value(app_id, element_index, value, *, pid=None) -> Record:
    """Set one accessibility element's value."""

    return _element_action(
        "app.set_value",
        element_index,
        app_id=app_id,
        pid=pid,
        value=value,
    )


def scroll(app_id, element_index, direction, *, pages=1, pid=None) -> Record:
    """Scroll an accessibility element."""

    return _element_action(
        "app.scroll_app",
        element_index,
        app_id=app_id,
        pid=pid,
        direction=direction,
        pages=pages,
    )


def select_text(
    app_id,
    element_index,
    text,
    *,
    selection="text",
    prefix=None,
    suffix=None,
    pid=None,
) -> Record:
    """Select text or place a text cursor in an accessibility element."""

    param: dict[str, Any] = {
        "text": text,
        "selection": selection,
    }
    if prefix is not None:
        param["prefix"] = prefix
    if suffix is not None:
        param["suffix"] = suffix
    return _element_action(
        "app.select_text",
        element_index,
        app_id=app_id,
        pid=pid,
        **param,
    )


def drag(
    app_id,
    element_index,
    from_x,
    from_y,
    to_x,
    to_y,
    *,
    pid=None,
) -> Record:
    """Drag between two screenshot-normalized points in the 0-1000 space.

    Coordinates use the matching window screenshot from the latest
    ``get_app_state(..., screenshot=True)`` call in the same execution.
    """

    return _element_action(
        "app.drag_on_app",
        element_index,
        app_id=app_id,
        pid=pid,
        from_x=from_x,
        from_y=from_y,
        to_x=to_x,
        to_y=to_y,
    )


def perform_secondary_action(app_id, element_index, action, *, pid=None) -> Record:
    """Invoke an action advertised by an accessibility element."""

    return _element_action(
        "app.perform_action_on_app",
        element_index,
        app_id=app_id,
        pid=pid,
        action=action,
    )


def wait(seconds=3) -> None:
    """Wait through Computer Use V2."""

    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise _local_error(
            TypeError,
            "CU_AX_ARG_SHAPE",
            "wait seconds must be a number in [0, 180]",
        )
    value = float(seconds)
    if not math.isfinite(value) or not 0 <= value <= 180:
        raise _local_error(
            ValueError,
            "CU_AX_ARG_SHAPE",
            "wait seconds must be in [0, 180]",
        )
    _action("wait", {"duration_ms": round(value * 1000)})

"""Windows Computer Use command schema (GAC protocol version 1)."""

from __future__ import annotations

import io
import json
import struct

from dataclasses import dataclass
from typing import Any
from typing import override

from ..transport.client import set_transport_io_timeout
from ..wire import GacCommand
from ..wire import RequestBase
from ..wire import ResponseBase
from ..wire import Runtime
from ..wire import request
from ..wire import response


COMPUTER_USE_MODULE_ID = 2
COMPUTER_USE_PROTOCOL_VERSION = 1
COMPUTER_USE_TRANSPORT_TIMEOUT = 500.0
MAX_RESPONSE_PAYLOAD = 129 * 1024 * 1024
MAX_RESPONSE_FRAME = 5 + MAX_RESPONSE_PAYLOAD
MAX_SCREENSHOT_PNG = 128 * 1024 * 1024


class Record(dict[str, Any]):
    """Dictionary whose fields can also be read as attributes."""

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError as error:
            raise AttributeError(name) from error


class AppCatalog(list[Record]):
    """Complete Windows application catalog."""

    def __repr__(self) -> str:
        preview = list.__repr__(self[:20])
        if len(self) <= 20:
            return preview
        return f"{preview[:-1]}, ... {len(self) - 20} more]"


class Frame(bytes):
    """PNG bytes with the captured virtual-desktop dimensions."""

    def __new__(cls, data: bytes, width: int, height: int):
        frame = super().__new__(cls, data)
        frame.width = int(width)
        frame.height = int(height)
        return frame

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height

    def save(self, target, format=None):
        if format is not None:
            self.image().save(target, format=format)
            return
        if hasattr(target, "write"):
            target.write(bytes(self))
            return
        with open(target, "wb") as output:
            output.write(bytes(self))

    def image(self):
        from PIL import Image

        return Image.open(io.BytesIO(bytes(self)))

    def __repr__(self) -> str:
        return f"<Frame {self.width}x{self.height}, {len(self)} bytes PNG>"


def _protocol_error(message: str) -> RuntimeError:
    return RuntimeError(f"protocol error: {message}")


def _json_text(value: dict[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def _write_param(
    request_data: RequestBase,
    out: io.BytesIO,
    value: dict[str, Any],
) -> None:
    request_data.write_str(out, _json_text(value))


def _read_param(
    reader: type[ResponseBase] | ResponseBase,
    buf: io.BytesIO,
) -> dict[str, Any]:
    try:
        value = json.loads(reader.read_str(buf))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _protocol_error("Computer Use param is not valid JSON") from error
    if not isinstance(value, dict):
        raise _protocol_error("Computer Use param must be a JSON object")
    return value


def _record(value: Any, field_name: str) -> Record:
    if not isinstance(value, dict):
        raise _protocol_error(f"{field_name} must be a JSON object")
    return Record(
        {
            str(key): _convert_json_value(item)
            for key, item in value.items()
        }
    )


def _convert_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return _record(value, "nested value")
    if isinstance(value, list):
        return [_convert_json_value(item) for item in value]
    return value


def _required_string(value: dict[str, Any], field_name: str) -> str:
    result = value.get(field_name)
    if not isinstance(result, str):
        raise _protocol_error(f"{field_name} must be a string")
    return result


class WindowsComputerUseRequest:
    @request(module_id=COMPUTER_USE_MODULE_ID, command=1)
    @dataclass
    class ListApps(RequestBase):
        @override
        def serialize(self, out: io.BytesIO):
            _write_param(self, out, {})

    @request(module_id=COMPUTER_USE_MODULE_ID, command=2)
    @dataclass
    class LaunchApp(RequestBase):
        name: str

        @override
        def serialize(self, out: io.BytesIO):
            _write_param(self, out, {"name": self.name})

    @request(module_id=COMPUTER_USE_MODULE_ID, command=3)
    @dataclass
    class Screenshot(RequestBase):
        tag: str | None = None

        @override
        def serialize(self, out: io.BytesIO):
            param = {} if self.tag is None else {"tag": self.tag}
            _write_param(self, out, param)

    @request(module_id=COMPUTER_USE_MODULE_ID, command=5)
    @dataclass
    class Click(RequestBase):
        x: int | float
        y: int | float
        button: str
        clicks: int

        @override
        def serialize(self, out: io.BytesIO):
            _write_param(
                self,
                out,
                {
                    "x": self.x,
                    "y": self.y,
                    "button": self.button,
                    "clicks": self.clicks,
                },
            )

    @request(module_id=COMPUTER_USE_MODULE_ID, command=6)
    @dataclass
    class Hover(RequestBase):
        x: int | float
        y: int | float

        @override
        def serialize(self, out: io.BytesIO):
            _write_param(self, out, {"x": self.x, "y": self.y})

    @request(module_id=COMPUTER_USE_MODULE_ID, command=7)
    @dataclass
    class Drag(RequestBase):
        x1: int | float
        y1: int | float
        x2: int | float
        y2: int | float
        duration_ms: int = 1000

        @override
        def serialize(self, out: io.BytesIO):
            _write_param(
                self,
                out,
                {
                    "x1": self.x1,
                    "y1": self.y1,
                    "x2": self.x2,
                    "y2": self.y2,
                    "duration_ms": self.duration_ms,
                },
            )

    @request(module_id=COMPUTER_USE_MODULE_ID, command=8)
    @dataclass
    class Scroll(RequestBase):
        x: int | float
        y: int | float
        delta: int
        horizontal: bool

        @override
        def serialize(self, out: io.BytesIO):
            _write_param(
                self,
                out,
                {
                    "x": self.x,
                    "y": self.y,
                    "delta": self.delta,
                    "horizontal": self.horizontal,
                },
            )

    @request(module_id=COMPUTER_USE_MODULE_ID, command=9)
    @dataclass
    class Type(RequestBase):
        text: str

        @override
        def serialize(self, out: io.BytesIO):
            _write_param(self, out, {"text": self.text})

    @request(module_id=COMPUTER_USE_MODULE_ID, command=10)
    @dataclass
    class Hotkey(RequestBase):
        keys: list[str]

        @override
        def serialize(self, out: io.BytesIO):
            _write_param(self, out, {"keys": self.keys})

    @request(module_id=COMPUTER_USE_MODULE_ID, command=11)
    @dataclass
    class Wait(RequestBase):
        duration_ms: int

        @override
        def serialize(self, out: io.BytesIO):
            _write_param(self, out, {"duration_ms": self.duration_ms})

    @request(module_id=COMPUTER_USE_MODULE_ID, command=12)
    @dataclass
    class GetClipboard(RequestBase):
        @override
        def serialize(self, out: io.BytesIO):
            _write_param(self, out, {})

    @request(module_id=COMPUTER_USE_MODULE_ID, command=13)
    @dataclass
    class SetClipboard(RequestBase):
        text: str

        @override
        def serialize(self, out: io.BytesIO):
            _write_param(self, out, {"text": self.text})

    @request(module_id=COMPUTER_USE_MODULE_ID, command=14)
    @dataclass
    class Press(RequestBase):
        key: str

        @override
        def serialize(self, out: io.BytesIO):
            _write_param(self, out, {"key": self.key})

    @request(module_id=COMPUTER_USE_MODULE_ID, command=15)
    @dataclass
    class KeyDown(RequestBase):
        key: str

        @override
        def serialize(self, out: io.BytesIO):
            _write_param(self, out, {"key": self.key})

    @request(module_id=COMPUTER_USE_MODULE_ID, command=16)
    @dataclass
    class KeyUp(RequestBase):
        key: str

        @override
        def serialize(self, out: io.BytesIO):
            _write_param(self, out, {"key": self.key})

    @request(module_id=COMPUTER_USE_MODULE_ID, command=17)
    @dataclass
    class CopySelection(RequestBase):
        @override
        def serialize(self, out: io.BytesIO):
            _write_param(self, out, {})


@response
@dataclass
class CommandMessage(ResponseBase):
    param: dict[str, Any] | None = None

    @override
    def deserialize(self, buf: io.BytesIO):
        self.param = _read_param(self, buf)


@response
@dataclass
class ScreenshotResponse(ResponseBase):
    frame: Frame | None = None

    @override
    def deserialize(self, buf: io.BytesIO):
        param = _read_param(self, buf)
        width = self.read_u32(buf)
        height = self.read_u32(buf)
        png_size = self.read_u32(buf)
        if png_size > MAX_SCREENSHOT_PNG:
            raise _protocol_error("Computer Use screenshot exceeds 128 MiB")
        png = self._read_exact(buf, png_size) if png_size else b""
        if width <= 0 or height <= 0:
            raise _protocol_error("Computer Use screenshot dimensions must be positive")
        json_width = param.get("width")
        json_height = param.get("height")
        if (
            isinstance(json_width, bool)
            or not isinstance(json_width, int)
            or isinstance(json_height, bool)
            or not isinstance(json_height, int)
            or json_width != width
            or json_height != height
        ):
            raise _protocol_error(
                "Computer Use screenshot JSON and frame dimensions differ"
            )
        _validate_png(png, width, height)
        self.frame = Frame(png, width, height)


def _validate_png(png: bytes, width: int, height: int) -> None:
    if (
        len(png) < 33
        or png[:8] != b"\x89PNG\r\n\x1a\n"
        or png[8:12] != b"\x00\x00\x00\r"
        or png[12:16] != b"IHDR"
        or png[-12:-8] != b"\x00\x00\x00\x00"
        or png[-8:-4] != b"IEND"
    ):
        raise _protocol_error("Computer Use screenshot is not a complete PNG")
    png_width, png_height = struct.unpack(">II", png[16:24])
    if (png_width, png_height) != (width, height):
        raise _protocol_error(
            "Computer Use screenshot PNG and frame dimensions differ"
        )


class WindowsComputerUseCommand(GacCommand):
    @staticmethod
    def _encode(data: RequestBase) -> bytearray:
        payload = io.BytesIO()
        data.serialize(payload)
        buf = io.BytesIO()
        buf.write(
            WindowsComputerUseCommand._envelope_header.pack(
                COMPUTER_USE_PROTOCOL_VERSION,
                data.module_id,
                data.command_id,
            )
        )
        data.write_bytes(buf, payload.getvalue())
        return bytearray(buf.getvalue())

    @staticmethod
    def _decode[T: ResponseBase](ty: type[T], frame: bytearray) -> T:
        buf = io.BytesIO(frame)
        status = ResponseBase.read_u8(buf)
        payload_size = ResponseBase.read_u32(buf)
        if payload_size > MAX_RESPONSE_PAYLOAD:
            raise _protocol_error("Computer Use response payload is too large")
        payload = ResponseBase._read_exact(buf, payload_size) if payload_size else b""
        if buf.read(1):
            raise _protocol_error("excessive data after GAC response envelope")

        payload_buf = io.BytesIO(payload)
        if status != 0:
            error_param = _read_param(ResponseBase, payload_buf)
            if payload_buf.read(1):
                raise _protocol_error("excessive data after error response")
            code = _required_string(error_param, "code")
            message = _required_string(error_param, "message")
            error = RuntimeError(
                f"Computer Use Server returned error: [{code}] {message}"
            )
            error.code = code
            raise error

        result = ty()
        result.deserialize(payload_buf)
        if payload_buf.read(1):
            raise _protocol_error("excessive data after Computer Use response")
        return result

    @classmethod
    def _execute[T: ResponseBase](cls, req: RequestBase, resp: type[T]) -> T:
        set_transport_io_timeout(COMPUTER_USE_TRANSPORT_TIMEOUT)
        return cls._decode(
            resp,
            Runtime.transceive(
                cls._encode(req),
                max_response_size=MAX_RESPONSE_FRAME,
            ),
        )

    @classmethod
    def _none(cls, request_data: RequestBase) -> None:
        response = cls._execute(request_data, CommandMessage)
        if response.param != {}:
            raise _protocol_error("Computer Use NoneResult must be an empty object")
        return None

    @classmethod
    def list_apps(cls) -> AppCatalog:
        response = cls._execute(
            WindowsComputerUseRequest.ListApps(),
            CommandMessage,
        )
        param = response.param or {}
        apps = param.get("apps")
        if not isinstance(apps, list):
            raise _protocol_error("apps must be a JSON array")
        catalog = AppCatalog()
        for index, app in enumerate(apps):
            record = _record(app, f"apps[{index}]")
            catalog.append(Record({"name": _required_string(record, "name")}))
        return catalog

    @classmethod
    def launch_app(cls, name: str) -> Record:
        response = cls._execute(
            WindowsComputerUseRequest.LaunchApp(name),
            CommandMessage,
        )
        param = response.param or {}
        source = _record(param, "launch result")
        app_source = _record(source.get("app"), "app")
        app = Record({"name": _required_string(app_source, "name")})
        status = _required_string(source, "status")
        launch_method = _required_string(source, "launch_method")
        if status not in {"ready", "accepted_unverified"}:
            raise _protocol_error("launch status is invalid")
        if launch_method not in {"resolved_target", "apps_folder"}:
            raise _protocol_error("launch_method is invalid")
        result = Record(
            {
                "app": app,
                "status": status,
                "launch_method": launch_method,
            }
        )
        window_value = source.get("window")
        if status == "ready" and not isinstance(window_value, dict):
            raise _protocol_error("ready launch result must contain window")
        if status == "accepted_unverified" and window_value is not None:
            raise _protocol_error(
                "accepted_unverified launch result must not contain window"
            )
        if window_value is not None and not isinstance(window_value, dict):
            raise _protocol_error("window must be an object or null")
        if isinstance(window_value, dict):
            window_source = _record(window_value, "window")
            result["window"] = Record(
                {
                    "title": _required_string(window_source, "title"),
                    "process": _required_string(window_source, "process"),
                }
            )
        return result

    @classmethod
    def screenshot(cls, tag: str | None = None) -> Frame:
        response = cls._execute(
            WindowsComputerUseRequest.Screenshot(tag),
            ScreenshotResponse,
        )
        if response.frame is None:
            raise _protocol_error("Computer Use screenshot frame is missing")
        return response.frame

    @classmethod
    def click(
        cls,
        x: int | float,
        y: int | float,
        button: str = "left",
        clicks: int = 1,
    ) -> None:
        return cls._none(WindowsComputerUseRequest.Click(x, y, button, clicks))

    @classmethod
    def hover(cls, x: int | float, y: int | float) -> None:
        return cls._none(WindowsComputerUseRequest.Hover(x, y))

    @classmethod
    def drag(
        cls,
        x1: int | float,
        y1: int | float,
        x2: int | float,
        y2: int | float,
    ) -> None:
        return cls._none(WindowsComputerUseRequest.Drag(x1, y1, x2, y2))

    @classmethod
    def scroll(
        cls,
        x: int | float,
        y: int | float,
        delta: int,
        *,
        horizontal: bool = False,
    ) -> None:
        return cls._none(
            WindowsComputerUseRequest.Scroll(x, y, delta, horizontal)
        )

    @classmethod
    def type(cls, text: str) -> None:
        return cls._none(WindowsComputerUseRequest.Type(text))

    @classmethod
    def hotkey(cls, *keys: str) -> None:
        return cls._none(WindowsComputerUseRequest.Hotkey(list(keys)))

    @classmethod
    def wait(cls, duration_ms: int) -> None:
        return cls._none(WindowsComputerUseRequest.Wait(duration_ms))

    @classmethod
    def get_clipboard(cls) -> str:
        response = cls._execute(
            WindowsComputerUseRequest.GetClipboard(),
            CommandMessage,
        )
        return _required_string(response.param or {}, "text")

    @classmethod
    def set_clipboard(cls, text: str) -> None:
        return cls._none(WindowsComputerUseRequest.SetClipboard(text))

    @classmethod
    def press(cls, key: str) -> None:
        return cls._none(WindowsComputerUseRequest.Press(key))

    @classmethod
    def key_down(cls, key: str) -> None:
        return cls._none(WindowsComputerUseRequest.KeyDown(key))

    @classmethod
    def key_up(cls, key: str) -> None:
        return cls._none(WindowsComputerUseRequest.KeyUp(key))

    @classmethod
    def copy_selection(cls) -> str:
        response = cls._execute(
            WindowsComputerUseRequest.CopySelection(),
            CommandMessage,
        )
        return _required_string(response.param or {}, "text")


__all__ = [
    "AppCatalog",
    "COMPUTER_USE_MODULE_ID",
    "COMPUTER_USE_PROTOCOL_VERSION",
    "COMPUTER_USE_TRANSPORT_TIMEOUT",
    "CommandMessage",
    "Frame",
    "Record",
    "ScreenshotResponse",
    "WindowsComputerUseCommand",
    "WindowsComputerUseRequest",
]

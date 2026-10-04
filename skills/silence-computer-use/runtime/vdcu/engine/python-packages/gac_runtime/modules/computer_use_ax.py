"""macOS Accessibility Tree Computer Use command schema."""

from __future__ import annotations

import io
import json

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


COMPUTER_USE_AX_MODULE_ID = 4
COMPUTER_USE_AX_PROTOCOL_VERSION = 1
COMPUTER_USE_AX_TRANSPORT_TIMEOUT = 500.0
MAX_RESPONSE_PAYLOAD = 129 * 1024 * 1024
MAX_RESPONSE_FRAME = 5 + MAX_RESPONSE_PAYLOAD
MAX_SCREENSHOT_PNG = 128 * 1024 * 1024


class Frame(bytes):
    """Image returned by a V2 AX observation."""

    def __new__(
        cls,
        data: bytes,
        *,
        index: int = 0,
        window_id: int = 0,
        mime_type: str = "image/png",
    ):
        frame = super().__new__(cls, data)
        frame.index = int(index)
        frame.window_id = int(window_id)
        frame.mime_type = str(mime_type)
        return frame

    def save(self, target) -> None:
        if hasattr(target, "write"):
            target.write(bytes(self))
            return
        with open(target, "wb") as output:
            output.write(bytes(self))


def _protocol_error(message: str) -> RuntimeError:
    return RuntimeError(f"protocol error: {message}")


def _json_text(value: dict[str, Any]) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def _write_param(out: io.BytesIO, value: dict[str, Any]) -> None:
    out.write(_json_text(value).encode("utf-8"))


def _read_param(
    reader: type[ResponseBase] | ResponseBase,
    buf: io.BytesIO,
) -> dict[str, Any]:
    try:
        value = json.loads(buf.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _protocol_error("Computer Use AX param is not valid JSON") from error
    if not isinstance(value, dict):
        raise _protocol_error("Computer Use AX param must be a JSON object")
    return value


def _required_string(value: dict[str, Any], field_name: str) -> str:
    result = value.get(field_name)
    if not isinstance(result, str):
        raise _protocol_error(f"{field_name} must be a string")
    return result


class ComputerUseAxRequest:
    """Computer Use V2 JSON call."""

    @request(module_id=COMPUTER_USE_AX_MODULE_ID, command=1)
    @dataclass
    class Message(RequestBase):
        param: dict[str, Any]

        @override
        def serialize(self, out: io.BytesIO) -> None:
            _write_param(out, self.param)


@response
@dataclass
class CommandMessage(ResponseBase):
    param: dict[str, Any] | None = None

    @override
    def deserialize(self, buf: io.BytesIO) -> None:
        self.param = _read_param(self, buf)


class ComputerUseAxCommand(GacCommand):
    """GAC command bridge for ``seed_computer_use_ax``."""

    @staticmethod
    def _encode(data: RequestBase) -> bytearray:
        payload = io.BytesIO()
        data.serialize(payload)
        buf = io.BytesIO()
        buf.write(
            ComputerUseAxCommand._envelope_header.pack(
                COMPUTER_USE_AX_PROTOCOL_VERSION,
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
            raise _protocol_error("Computer Use AX response payload is too large")
        payload = ResponseBase._read_exact(buf, payload_size) if payload_size else b""
        if buf.read(1):
            raise _protocol_error(
                "excessive data after Computer Use AX response envelope"
            )

        payload_buf = io.BytesIO(payload)
        if status != 0:
            try:
                error_param = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                error_param = None
            code = (
                error_param.get("code")
                if isinstance(error_param, dict)
                else "COMPUTER_USE_V2_TRANSPORT_ERROR"
            )
            message = (
                error_param.get("message")
                if isinstance(error_param, dict)
                else payload.decode("utf-8", errors="replace")
            )
            error = RuntimeError(
                f"Computer Use V2 Server returned error: [{code}] {message}"
            )
            error.code = code
            raise error

        result = ty()
        result.deserialize(payload_buf)
        if result.param is not None and result.param.get("ok") is False:
            code = _required_string(result.param, "error")
            error = RuntimeError(f"Computer Use V2 Server returned error: [{code}]")
            error.code = code
            raise error
        return result

    @classmethod
    def _execute[T: ResponseBase](cls, req: RequestBase, resp: type[T]) -> T:
        set_transport_io_timeout(COMPUTER_USE_AX_TRANSPORT_TIMEOUT)
        return cls._decode(
            resp,
            Runtime.transceive(
                cls._encode(req),
                max_response_size=MAX_RESPONSE_FRAME,
            ),
        )

    @classmethod
    def message(cls, param: dict[str, Any]) -> dict[str, Any]:
        response = cls._execute(
            ComputerUseAxRequest.Message(param),
            CommandMessage,
        )
        if response.param is None:
            raise _protocol_error("Computer Use AX message response is missing")
        return response.param

__all__ = [
    "COMPUTER_USE_AX_MODULE_ID",
    "COMPUTER_USE_AX_PROTOCOL_VERSION",
    "COMPUTER_USE_AX_TRANSPORT_TIMEOUT",
    "CommandMessage",
    "ComputerUseAxCommand",
    "ComputerUseAxRequest",
    "Frame",
    "MAX_RESPONSE_FRAME",
    "MAX_RESPONSE_PAYLOAD",
    "MAX_SCREENSHOT_PNG",
]

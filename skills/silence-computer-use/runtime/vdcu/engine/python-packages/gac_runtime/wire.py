import io
import struct

from abc import ABC
from abc import abstractmethod

from .transport import FrameTransceiver
from .transport import GacTransportClient


GAC_PROTOCOL_VERSION = 1


class Runtime:
    _transceiver: FrameTransceiver | None = None

    @classmethod
    def _do_initialize(cls):
        cls._transceiver = GacTransportClient.from_env()

        if not cls.try_reconnect():
            raise RuntimeError('attempt to reinitialize after unrecoverable failure')

    @classmethod
    def try_reconnect(cls) -> bool:
        if cls._transceiver is None:
            raise RuntimeError('GAC Runtime is not initialized')

        return cls._transceiver.reconnect()

    @classmethod
    def transceive(
        cls,
        frame: bytes | bytearray,
        *,
        max_response_size: int | None = None,
    ) -> bytearray:
        if not cls._transceiver:
            cls._do_initialize()

        assert cls._transceiver is not None
        if max_response_size is None:
            return cls._transceiver.transceive(frame)
        return cls._transceiver.transceive(
            frame,
            max_response_size=max_response_size,
        )


class RequestBase(ABC):
    i8_pack = struct.Struct('B').pack
    i32_pack = struct.Struct('<I').pack

    @classmethod
    def write_u8(cls, out: io.BytesIO, value: int):
        out.write(cls.i8_pack(value))

    @classmethod
    def write_u32(cls, out: io.BytesIO, value: int):
        out.write(cls.i32_pack(value))

    @classmethod
    def write_str(cls, out: io.BytesIO, value: str):
        cls.write_bytes(out, value.encode('utf-8'))

    @classmethod
    def write_bytes(cls, out: io.BytesIO, value: bytes):
        cls.write_u32(out, len(value))
        out.write(value)

    @property
    def module_id(self) -> int:
        return getattr(self.__class__, '$module_id')

    @property
    def command_id(self) -> int:
        return getattr(self.__class__, '$command')

    @abstractmethod
    def serialize(self, out: io.BytesIO):
        raise NotImplementedError


class ResponseBase(ABC):
    u32_unpack = struct.Struct('<I').unpack
    u64_unpack = struct.Struct('<Q').unpack
    f64_unpack = struct.Struct('<d').unpack

    @classmethod
    def _read_exact(cls, buf: io.BytesIO, size: int) -> bytes:
        if ret := buf.read(size):
            if len(ret) != size:
                raise RuntimeError(
                    f'protocol error: unexpected end of frame when reading {size} bytes'
                )
            return ret
        raise RuntimeError(f'protocol error: empty frame when reading {size} bytes')

    @classmethod
    def read_u8(cls, buf: io.BytesIO) -> int:
        return cls._read_exact(buf, 1)[0]

    @classmethod
    def read_u32(cls, buf: io.BytesIO) -> int:
        ret, = cls.u32_unpack(cls._read_exact(buf, 4))
        return ret

    @classmethod
    def read_u64(cls, buf: io.BytesIO) -> int:
        ret, = cls.u64_unpack(cls._read_exact(buf, 8))
        return ret

    @classmethod
    def read_f64(cls, buf: io.BytesIO) -> float:
        ret, = cls.f64_unpack(cls._read_exact(buf, 8))
        return ret

    @classmethod
    def read_str(cls, buf: io.BytesIO) -> str:
        return cls.read_bytes(buf).decode('utf-8')

    @classmethod
    def read_bool(cls, buf: io.BytesIO) -> bool:
        return cls.read_u8(buf) != 0

    @classmethod
    def read_bytes(cls, buf: io.BytesIO) -> bytes:
        if size := cls.read_u32(buf):
            return cls._read_exact(buf, size)
        return b''

    @abstractmethod
    def deserialize(self, buf: io.BytesIO):
        raise NotImplementedError


def request[T: RequestBase](module_id: int, command: int):
    def make_request(klass: type[T]) -> type[T]:
        setattr(klass, '$module_id', module_id)
        setattr(klass, '$command', command)
        return klass

    return make_request


def response[T: ResponseBase](klass: type[T]) -> type[T]:
    return klass


class GacCommand:
    _envelope_header = struct.Struct('<BBI')

    @staticmethod
    def _encode(data: RequestBase) -> bytearray:
        assert 0 < data.module_id <= 255
        assert 0 <= data.command_id <= 0xFFFFFFFF
        payload = io.BytesIO()
        data.serialize(payload)

        buf = io.BytesIO()
        buf.write(GacCommand._envelope_header.pack(
            GAC_PROTOCOL_VERSION,
            data.module_id,
            data.command_id,
        ))
        data.write_bytes(buf, payload.getvalue())
        return bytearray(buf.getvalue())

    @staticmethod
    def _decode[T: ResponseBase](ty: type[T], frame: bytearray) -> T:
        buf = io.BytesIO(frame)
        status = ResponseBase.read_u8(buf)
        payload = ResponseBase.read_bytes(buf)

        if buf.read(1):
            raise RuntimeError(
                'protocol error: excessive data after GAC response envelope'
            )

        if status != 0:
            raise RuntimeError('GAC Server returned error: ' + payload.decode('utf-8'))

        payload_buf = io.BytesIO(payload)
        ret = ty()
        ret.deserialize(payload_buf)
        if payload_buf.read(1):
            raise RuntimeError(
                'protocol error: excessive data after GAC response payload'
            )
        return ret

    @classmethod
    def _execute[T: ResponseBase](cls, req: RequestBase, resp: type[T]) -> T:
        return cls._decode(resp, Runtime.transceive(cls._encode(req)))

import hashlib
import hmac
import os
import struct

from typing import Protocol

from .chacha20poly1305 import open_inplace
from .chacha20poly1305 import seal_inplace


class RemoteClosedError(EOFError):
    pass


class Connection(Protocol):
    def recv(self, size: int) -> bytes:
        ...

    def sendall(self, data: bytes) -> None:
        ...

    def close(self) -> None:
        ...


def hkdf(salt: bytes, ikm: bytes) -> bytes:
    key = hmac.new(salt, ikm, hashlib.sha256).digest()
    return hmac.new(key, b'\x01', hashlib.sha256).digest()


def read_exact(conn: Connection, size: int) -> bytes:
    buf = b''
    while size != 0:
        data = conn.recv(size)
        if not data:
            raise RemoteClosedError()
        size -= len(data)
        buf += data
    return buf


def read_frame(conn: Connection) -> bytes:
    header = read_exact(conn, 8)
    magic, size = struct.unpack('<4sI', header)
    assert magic == b'GACF'
    return read_exact(conn, size)


def write_frame(conn: Connection, data: bytes) -> None:
    conn.sendall(b'GACF')
    conn.sendall(len(data).to_bytes(4, 'little'))
    conn.sendall(data)


def token_challenge(
    conn: Connection,
    session_id: str,
    execution_id: str,
    secret: bytes,
) -> bytes:
    complex_id = session_id + ':' + execution_id
    write_frame(conn, complex_id.encode('utf-8'))
    token = read_frame(conn)
    digest = hmac.new(secret, token, hashlib.sha256).digest()
    write_frame(conn, digest)
    return hkdf(token, secret)


class EncryptedFrameTransport:
    socket: Connection
    session_key: bytes
    is_retryable: bool

    def __init__(self, socket: Connection, session_key: bytes):
        self.socket = socket
        self.session_key = session_key
        self.is_retryable = False

    def close(self) -> None:
        self.socket.close()
        self.is_retryable = True

    def set_timeout(self, timeout: float) -> None:
        setter = getattr(self.socket, 'settimeout', None)
        if setter is not None:
            setter(timeout)

    def read_encrypted_frame(
        self,
        max_plaintext_size: int | None = None,
    ) -> bytearray:
        try:
            header = read_exact(self.socket, 8)
            magic, size = struct.unpack('<4sI', header)
            assert magic == b'GACF' and size >= 28
            if (
                max_plaintext_size is not None
                and size - 28 > max_plaintext_size
            ):
                self.is_retryable = True
                raise RuntimeError(
                    "protocol error: encrypted GAC response exceeds the "
                    "configured size limit"
                )
            nonce = read_exact(self.socket, 12)
            frame = bytearray(read_exact(self.socket, size - 12))
            open_inplace(self.session_key, nonce, header, frame)
            return frame
        except (EOFError, OSError):
            self.is_retryable = True
            raise

    def write_encrypted_frame(self, frame: bytearray) -> None:
        nonce = os.urandom(12)
        header = struct.pack('<4sI', b'GACF', len(frame) + 28)
        seal_inplace(self.session_key, nonce, header, frame)

        try:
            self.socket.sendall(header)
            self.socket.sendall(nonce)
            self.socket.sendall(frame)
        except IOError:
            self.is_retryable = True
            raise

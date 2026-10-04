from __future__ import annotations

import threading

from . import named_pipe
from . import tcp
from . import uds
from .config import DEFAULT_TIMEOUT
from .config import GacTransportConfig
from .protocol import Connection
from .protocol import EncryptedFrameTransport
from .protocol import token_challenge


_IPC_CONNECT_TIMEOUT_WITH_FALLBACK = 0.5
_TRANSPORT_IO_TIMEOUT = DEFAULT_TIMEOUT


def set_transport_io_timeout(timeout: float) -> None:
    global _TRANSPORT_IO_TIMEOUT
    _TRANSPORT_IO_TIMEOUT = timeout


class GacTransportClient:
    def __init__(self, config: GacTransportConfig):
        self.config = config
        self._frame_transport: EncryptedFrameTransport | None = None
        # Keep request/response pairs ordered and exclude concurrent reconnects.
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> GacTransportClient:
        return cls(GacTransportConfig.from_env())

    def reconnect(self) -> bool:
        with self._lock:
            return self._reconnect_locked()

    def transceive(
        self,
        frame: bytes | bytearray,
        *,
        max_response_size: int | None = None,
    ) -> bytearray:
        with self._lock:
            if self._frame_transport is None:
                if not self._reconnect_locked():
                    raise RuntimeError(
                        'attempt to reinitialize after unrecoverable failure'
                    )

            assert self._frame_transport is not None
            self._frame_transport.set_timeout(_TRANSPORT_IO_TIMEOUT)
            self._frame_transport.write_encrypted_frame(bytearray(frame))
            if max_response_size is None:
                return self._frame_transport.read_encrypted_frame()
            return self._frame_transport.read_encrypted_frame(
                max_plaintext_size=max_response_size,
            )

    def _reconnect_locked(self) -> bool:
        if self._frame_transport is not None:
            if not self._frame_transport.is_retryable:
                return False
            self._frame_transport.close()
            self._frame_transport = None

        conn = self._connect()
        try:
            session_key = token_challenge(
                conn,
                self.config.session_id,
                self.config.execution_id,
                self.config.secret,
            )
        except Exception:
            conn.close()
            raise
        self._frame_transport = EncryptedFrameTransport(conn, session_key)
        return True

    def _connect(self) -> Connection:
        try:
            return self._connect_kind(
                self.config.kind,
                port=self.config.port,
                timeout=(
                    min(
                        self.config.timeout,
                        _IPC_CONNECT_TIMEOUT_WITH_FALLBACK,
                    )
                    if self.config.fallback_kind
                    else self.config.timeout
                ),
            )
        except (OSError, RuntimeError) as primary_error:
            if self.config.fallback_kind != 'tcp':
                raise
            try:
                return self._connect_kind(
                    'tcp',
                    port=self.config.fallback_port,
                    timeout=self.config.timeout,
                )
            except (OSError, RuntimeError) as fallback_error:
                raise RuntimeError(
                    f'primary {self.config.kind} and TCP fallback connections '
                    f'failed: {primary_error}; {fallback_error}'
                ) from fallback_error

    def _connect_kind(self, kind: str, port: int, timeout: float) -> Connection:
        match kind:
            case 'named_pipe':
                return named_pipe.connect(
                    self.config.endpoint,
                    timeout=timeout,
                )
            case 'uds':
                return uds.connect(
                    self.config.endpoint,
                    timeout=timeout,
                    io_timeout=self.config.timeout,
                )
            case 'tcp':
                return tcp.connect(port, timeout=timeout)

        raise RuntimeError('GAC Runtime is not initialized')

import os
import time


CONNECT_RETRY_INTERVAL = 0.05


class NamedPipeConnection:
    def __init__(self, endpoint: str):
        self._file = open(endpoint, 'r+b', buffering=0)

    def recv(self, size: int) -> bytes:
        return self._file.read(size)

    def sendall(self, data: bytes) -> None:
        self._file.write(data)
        self._file.flush()

    def close(self) -> None:
        self._file.close()


def connect(endpoint: str, timeout: float) -> NamedPipeConnection:
    if os.name != 'nt':
        raise RuntimeError('named_pipe GAC transport is only available on Windows')

    deadline = time.monotonic() + timeout
    last_error: OSError | None = None

    while time.monotonic() < deadline:
        try:
            return NamedPipeConnection(endpoint)
        except OSError as e:
            last_error = e
            time.sleep(CONNECT_RETRY_INTERVAL)

    raise RuntimeError('failed to connect named pipe GAC endpoint') from last_error

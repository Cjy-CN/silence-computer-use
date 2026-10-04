import socket


def connect(
    endpoint: str,
    timeout: float,
    io_timeout: float | None = None,
) -> socket.socket:
    if not hasattr(socket, 'AF_UNIX'):
        raise RuntimeError('uds GAC transport is not available on this platform')

    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    try:
        conn.connect(endpoint)
    except OSError:
        conn.close()
        raise
    if io_timeout is not None:
        conn.settimeout(io_timeout)
    return conn

import socket


def connect(port: int, timeout: float) -> socket.socket:
    return socket.create_connection(('127.0.0.1', port), timeout=timeout)

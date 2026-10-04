from __future__ import annotations

import base64
import os

from dataclasses import dataclass


DEFAULT_TIMEOUT = 5.0
ENDPOINT_TRANSPORTS = frozenset(('named_pipe', 'uds'))
SUPPORTED_TRANSPORTS = frozenset(('tcp', *ENDPOINT_TRANSPORTS))
SUPPORTED_FALLBACK_TRANSPORTS = frozenset(('', 'tcp'))


def _read_port(name: str) -> int:
    value = os.getenv(name)
    if not value:
        return 0
    try:
        port = int(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            f'GUI as Coding is not available: invalid {name}'
        ) from error
    if not 0 <= port <= 65535:
        raise RuntimeError(f'GUI as Coding is not available: invalid {name}')
    return port


@dataclass(frozen=True)
class GacTransportConfig:
    kind: str
    endpoint: str
    port: int
    secret: bytes
    session_id: str
    execution_id: str
    fallback_kind: str = ''
    fallback_port: int = 0
    timeout: float = DEFAULT_TIMEOUT

    @classmethod
    def from_env(cls) -> GacTransportConfig:
        kind = (os.getenv('CUA_GAC_TRANSPORT') or 'tcp').strip().lower()
        endpoint = (os.getenv('CUA_GAC_ENDPOINT') or '').strip()
        fallback_kind = (
            os.getenv('CUA_GAC_FALLBACK_TRANSPORT') or ''
        ).strip().lower()
        port = _read_port('CUA_GAC_SERVER_PORT')
        fallback_port = _read_port('CUA_GAC_FALLBACK_PORT')

        if kind == 'tcp' and not port:
            raise RuntimeError(
                'GUI as Coding is not available: missing CUA_GAC_SERVER_PORT'
            )
        if kind in ENDPOINT_TRANSPORTS and not endpoint:
            raise RuntimeError(
                'GUI as Coding is not available: missing CUA_GAC_ENDPOINT'
            )
        if kind not in SUPPORTED_TRANSPORTS:
            raise RuntimeError(
                'GUI as Coding is not available: unsupported CUA_GAC_TRANSPORT'
            )
        if fallback_kind not in SUPPORTED_FALLBACK_TRANSPORTS:
            raise RuntimeError(
                'GUI as Coding is not available: unsupported '
                'CUA_GAC_FALLBACK_TRANSPORT'
            )
        if fallback_kind == 'tcp' and not fallback_port:
            raise RuntimeError(
                'GUI as Coding is not available: missing CUA_GAC_FALLBACK_PORT'
            )

        secret = os.getenv('CUA_GAC_SECRET')
        session_id = os.getenv('CUA_GAC_SESSION_ID')
        execution_id = os.getenv('CUA_GAC_EXECUTION_ID')

        if not secret or not session_id or not execution_id:
            raise RuntimeError(
                'GUI as Coding is not available: missing either '
                'CUA_GAC_SECRET or CUA_GAC_SESSION_ID or CUA_GAC_EXECUTION_ID'
            )

        return cls(
            kind=kind,
            endpoint=endpoint,
            port=port,
            secret=base64.urlsafe_b64decode(secret + '=='),
            session_id=session_id,
            execution_id=execution_id,
            fallback_kind=fallback_kind,
            fallback_port=fallback_port,
        )

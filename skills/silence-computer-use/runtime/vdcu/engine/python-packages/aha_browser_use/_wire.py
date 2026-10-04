"""Command 15 wire codec for the Native AhaBUA backend."""

from __future__ import annotations

import io
import json

from dataclasses import dataclass
from typing import Any

try:
    from typing import override
except ImportError:
    def override(function):
        return function

from gac_runtime.wire import GacCommand
from gac_runtime.wire import RequestBase
from gac_runtime.wire import ResponseBase
from gac_runtime.wire import request
from gac_runtime.wire import response


_BROWSER_USE_MODULE_ID = 1
_INVOKE_PAGE_OPERATION_ID = 15


@request(module_id=_BROWSER_USE_MODULE_ID, command=_INVOKE_PAGE_OPERATION_ID)
@dataclass
class InvokePageOperationRequest(RequestBase):
    payload: str

    @override
    def serialize(self, out: io.BytesIO):
        self.write_str(out, self.payload)


@response
@dataclass
class InvokePageOperationResponse(ResponseBase):
    payload: str = ""
    binary: bytes = b""

    @override
    def deserialize(self, buf: io.BytesIO):
        self.payload = self.read_str(buf)
        self.binary = self.read_bytes(buf)


class AhaBuaPageOperationCommand(GacCommand):
    @classmethod
    def invoke_page_operation(
        cls,
        operation: str,
        *,
        page_id: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        request_payload: dict[str, Any] = {
            "schemaVersion": 1,
            "operation": str(operation),
            "params": dict(params or {}),
        }
        if page_id:
            request_payload["pageId"] = str(page_id)
        response = cls._execute(
            InvokePageOperationRequest(
                json.dumps(request_payload, separators=(",", ":"), ensure_ascii=False)
            ),
            InvokePageOperationResponse,
        )
        try:
            payload = json.loads(response.payload)
        except (TypeError, ValueError) as error:
            raise RuntimeError("Cannot parse AhaBUA page operation response") from error
        if not isinstance(payload, dict):
            raise RuntimeError("Invalid AhaBUA page operation response")
        if response.binary:
            payload["_binary"] = response.binary
        return payload


__all__ = [
    "AhaBuaPageOperationCommand",
    "InvokePageOperationRequest",
    "InvokePageOperationResponse",
]

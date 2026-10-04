"""Narrow GAC wire client used by Native AhaBUA and compatibility commands."""

from __future__ import annotations

import threading
from typing import Any

from gac_runtime import debug_trace

from ._common import BrowserSessionError, BrowserUseError, unsupported


class ModuleClient:
    def __init__(self):
        self._lock = threading.RLock()
        self._command = None
        self._page_operation_command = None
        self._runtime = None

    def _load(self):
        if self._command is not None:
            return
        # gac_runtime supplies only the wire/command contract here.  No Legacy
        # Browser Use service is called by aha_browser_use.
        from gac_runtime import BrowserUseCommand, Runtime
        from ._wire import AhaBuaPageOperationCommand

        self._command = BrowserUseCommand
        self._page_operation_command = AhaBuaPageOperationCommand
        self._runtime = Runtime

    def call(self, name: str, *args, retryable: bool = True, **kwargs):
        with self._lock:
            self._load()
            command = (
                self._page_operation_command
                if name == "invoke_page_operation"
                else self._command
            )
            operation = getattr(command, name)
            try:
                return operation(*args, **kwargs)
            except Exception as original:
                if not retryable:
                    raise
                try:
                    reconnected = bool(self._runtime.try_reconnect())
                except Exception:
                    reconnected = False
                if reconnected:
                    return operation(*args, **kwargs)
                raise original


class AhaBuaClient:
    def __init__(self):
        self.module = ModuleClient()
        self._capabilities: dict[str, Any] | None = None
        self._selected_page_id: str | None = None

    @property
    def capabilities(self) -> dict[str, Any]:
        if self._capabilities is None:
            response = self.module.call(
                "invoke_page_operation", "module.getCapabilities", params={}
            )
            result = self._unwrap("module.getCapabilities", response)
            if result.get("backend") != "aha_bua":
                raise BrowserUseError(
                    "The selected Browser Use task is not using the Native AhaBUA backend. "
                    "Start the task through the Aha Browser Use skill; GAC fallback is forbidden.",
                    code="BU_BACKEND_MISMATCH",
                )
            self._capabilities = result
        return self._capabilities

    def require_operation(self, tool: str, operation: str):
        if operation not in set(self.capabilities.get("operations") or ()):
            unsupported(tool, f"Native capability {operation} is unavailable")

    def require_action(self, tool: str, action: str):
        if action not in set(self.capabilities.get("actionKinds") or ()):
            unsupported(tool, f"Native action {action} is unavailable")

    def require_feature(self, tool: str, feature: str):
        if not bool((self.capabilities.get("features") or {}).get(feature)):
            unsupported(tool, f"Native feature {feature} is unavailable")

    @staticmethod
    def _unwrap(operation: str, response: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise BrowserUseError("Native AhaBUA returned an invalid response", code="BU_PROTOCOL")
        if response.get("ok") is True:
            result = response.get("result")
            return result if isinstance(result, dict) else {}
        error = response.get("error") if isinstance(response.get("error"), dict) else {}
        raw_code = str(error.get("code") or "BU_AHABUA_FAILED")
        code = {
            "unsupported_operation": "UNSUPPORTED",
            "stale_node": "BU_REF_STALE",
            "document_changed": "BU_REF_STALE",
            "page_not_found": "BU_SESSION_STALE",
            "task_not_found": "BU_SESSION_STALE",
            "unsupported_read_field": "BU_READ_TARGET",
        }.get(raw_code, raw_code)
        message = str(error.get("message") or f"Native AhaBUA {operation} failed")
        exc_type = BrowserSessionError if code == "BU_SESSION_STALE" else BrowserUseError
        raise exc_type(message, code=code)

    def invoke(
        self,
        tool: str,
        operation: str,
        *,
        params: dict[str, Any] | None = None,
        page_id: str | None = None,
        mutating: bool = False,
    ) -> tuple[dict[str, Any], bytes]:
        self.require_operation(tool, operation)
        page_id = page_id or self.ensure_page().id
        operation_params = dict(params or {})
        retryable = not mutating
        trace_call = debug_trace.start_native_call(
            operation,
            page_id=page_id,
            params=operation_params,
            retryable=retryable,
        )
        try:
            response = self.module.call(
                "invoke_page_operation",
                operation,
                page_id=page_id,
                params=operation_params,
                retryable=retryable,
            )
            binary = response.pop("_binary", b"") if isinstance(response, dict) else b""
            result = self._unwrap(operation, response)
        except BaseException as error:
            debug_trace.fail_native_call(trace_call, error)
            raise
        debug_trace.finish_native_call(
            trace_call,
            response=response,
            result=result,
            binary_size=len(binary or b""),
        )
        return result, bytes(binary or b"")

    @staticmethod
    def _page_open(page) -> bool:
        status = getattr(page, "status", None)
        return getattr(status, "value", status) != 3 and getattr(status, "name", status) != "Closed"

    def pages(self):
        return [page for page in self.module.call("list_pages") if self._page_open(page)]

    def _find_page(
        self,
        page_id: str | None = None,
        *,
        pages: list[Any] | None = None,
    ):
        requested = str(page_id or self._selected_page_id or "")
        if not requested:
            requested = str(self.module.call("get_active_page") or "")
        if not requested:
            return None
        available_pages = self.pages() if pages is None else pages
        for page in available_pages:
            if page.id == requested:
                self._selected_page_id = page.id
                return page
        if page_id:
            raise BrowserSessionError(f'Browser page "{page_id}" does not exist')
        return None

    def _create_page(self, initial_url: str):
        page_id = self.module.call(
            "create_page", str(initial_url), "", retryable=False
        )
        self.module.call("activate_page", page_id, retryable=False)
        self._selected_page_id = page_id
        return self.module.call("get_page", page_id)

    def ensure_page(self, page_id: str | None = None, *, create: bool = True):
        page = self._find_page(page_id)
        if page is not None:
            return page
        if not create:
            raise BrowserSessionError("Browser Use has no open page")
        return self._create_page("about:blank")

    def ensure_page_for_navigation(self, url: str):
        """Returns (page, created) without synthesizing an about:blank load."""
        page = self._find_page()
        if page is not None:
            return page, False
        return self._create_page(url), True

    def select_page(self, page_id: str):
        page = self.ensure_page(page_id, create=False)
        self.module.call("activate_page", page.id, retryable=False)
        self._selected_page_id = page.id
        return page

    def clear_selected(self):
        self._selected_page_id = None


CLIENT = AhaBuaClient()

__all__ = ["AhaBuaClient", "CLIENT", "ModuleClient"]

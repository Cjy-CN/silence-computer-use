"""Concrete GAC browser backend and request dispatcher."""

from __future__ import annotations

import threading

from typing import Any

from ....modules import BrowserUseCommand
from ....wire import Runtime
from .downloads import DownloadStoreMixin
from .events import EventSourceMixin
from .scopes import ScopeStoreMixin
from .state import PageStateMixin


class GacBackend(
    PageStateMixin,
    EventSourceMixin,
    ScopeStoreMixin,
    DownloadStoreMixin,
):
    """Stateful browser backend backed by ``BrowserUseCommand``."""

    def __init__(
        self,
        command=None,
        runtime=None,
    ):
        self._command = command
        self._runtime = runtime
        self._lock = threading.RLock()
        self._page_id: str | None = None
        self._session_id = ""
        self._attachment_stale = False
        self._scope_counter = 0
        self._scope_origins: dict[str, dict[str, Any]] = {}

    def _load_backend(self) -> None:
        if self._command is not None:
            return

        self._command = BrowserUseCommand
        self._runtime = Runtime
        self._load_scope_data()

    def _call(self, name: str, *args):
        self._load_backend()
        operation = getattr(self._command, name)
        try:
            return operation(*args)
        except Exception as original:
            if self._runtime is None:
                raise
            try:
                retry = self._runtime.try_reconnect()
            except Exception:
                retry = False
            if retry:
                return operation(*args)
            raise original

    def _send_cdp(
        self,
        page_id: str,
        session_id: str,
        method: str,
        params: dict[str, Any],
    ) -> Any:
        return self._call(
            "send_cdp_command",
            page_id,
            session_id,
            method,
            params,
        )

    def _cdp(self, payload: dict[str, Any]) -> dict[str, Any]:
        method = payload.get("method")
        params = payload.get("params") or {}
        explicit_session = payload.get("session_id")
        if not isinstance(method, str) or not method:
            raise ValueError("gac_runtime.browser CDP request is missing method")
        if not isinstance(params, dict):
            raise TypeError("gac_runtime.browser CDP params must be a dict")

        handled, result = self._handle_target_command(
            method,
            params,
            explicit_session,
        )
        if handled:
            return {"result": result}

        session_page = self._page_from_session(explicit_session)
        page_id = session_page or self._ensure_page()
        if self._find_page(page_id) is None:
            raise RuntimeError(
                "BU_SESSION_STALE: the GAC page selected by this session is gone"
            )
        cdp_session = "" if session_page else str(explicit_session or "")
        result = self._send_cdp(page_id, cdp_session, method, params)
        return {"result": result}

    def _meta(self, payload: dict[str, Any]) -> dict[str, Any]:
        operation = payload.get("meta")
        if operation == "list_pages":
            return {"pages": [self._page_dict(page) for page in self._list_pages()]}
        if operation == "create_page":
            url = str(payload.get("url") or "about:blank")
            page_id = self._call("create_page", url, url)
            self._call("activate_page", page_id)
            self._select_page(page_id)
            return {
                "targetId": page_id,
                "session_id": self._session_id,
            }
        if operation == "switch_page":
            page_id = str(payload.get("page_id") or payload.get("target_id") or "")
            if not page_id or self._find_page(page_id) is None:
                raise RuntimeError(f'GAC page "{page_id}" does not exist')
            self._call("activate_page", page_id)
            self._select_page(page_id)
            return {"session_id": self._session_id}
        if operation == "close_page":
            page_id = str(
                payload.get("page_id")
                or payload.get("target_id")
                or self._ensure_page()
            )
            if self._find_page(page_id) is None:
                raise RuntimeError(f'GAC page "{page_id}" does not exist')
            self._call("close_page", page_id)
            if page_id == self._page_id:
                self._attachment_stale = True
                self._session_id = ""
            return {}
        if operation == "page_info":
            return self._page_info()
        if operation == "drain_console_logs":
            return self._drain_console_logs()
        if operation == "drain_network_requests":
            return self._drain_network_requests()
        if operation == "pending_dialog":
            return self._pending_dialog()
        if operation == "current_tab":
            page = self._call("get_page", self._ensure_page())
            return {
                "targetId": page.id,
                "url": page.url,
                "title": page.title,
            }
        if operation == "capture_screenshot":
            page_id = self._ensure_page()
            if self._find_page(page_id) is None:
                raise RuntimeError(
                    "BU_SESSION_STALE: the GAC page selected by this session is gone"
                )
            params = payload.get("params") or {}
            if not isinstance(params, dict):
                raise TypeError("capture_screenshot params must be a dict")
            max_dimension = int(payload.get("max_dimension") or 0)
            if max_dimension < 0:
                raise ValueError("capture_screenshot max_dimension must be non-negative")
            data = self._call(
                "capture_screenshot",
                page_id,
                params,
                max_dimension,
            )
            return {"data": data}
        if operation == "set_session":
            target_id = str(payload.get("target_id") or "")
            if not target_id or self._find_page(target_id) is None:
                raise RuntimeError(
                    "BU_SESSION_STALE: cannot attach to a missing GAC page"
                )
            self._call("activate_page", target_id)
            self._select_page(target_id, str(payload.get("session_id") or ""))
            return {}
        if operation == "session":
            self._ensure_page()
            return {"session_id": self._session_id}
        if operation == "doc_scope":
            return self._allocate_scope(payload)
        if operation == "doc_origin":
            return self._doc_origin(payload.get("scope"))
        if operation == "download_url":
            return self._download_url(payload)
        if operation == "wait_for_download":
            return self._wait_for_download(payload)
        raise ValueError(f"unsupported gac_runtime.browser meta request: {operation!r}")

    def request(self, payload: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
        """Handle one internal browser request."""
        del timeout
        with self._lock:
            try:
                if "meta" in payload:
                    return self._meta(payload)
                return self._cdp(payload)
            except Exception as exc:
                return {"error": str(exc)}

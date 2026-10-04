"""GAC page selection, target mapping, dialogs, and page metadata."""

from typing import Any

from .constants import PAGE_SESSION_PREFIX


class PageStateMixin:
    @staticmethod
    def _page_is_open(page) -> bool:
        status = getattr(page, "status", None)
        status_value = getattr(status, "value", status)
        status_name = getattr(status, "name", status)
        return not (
            status_value == 3
            or status_name == "Closed"
        )

    def _list_pages(self) -> list[Any]:
        return [page for page in self._call("list_pages") if self._page_is_open(page)]

    @staticmethod
    def _page_dict(page) -> dict[str, Any]:
        return {
            "targetId": page.id,
            "target_id": page.id,
            "title": page.title,
            "url": page.url,
        }

    def _find_page(self, page_id: str) -> Any | None:
        return next((page for page in self._list_pages() if page.id == page_id), None)

    @staticmethod
    def _page_session(page_id: str) -> str:
        return PAGE_SESSION_PREFIX + page_id

    @staticmethod
    def _page_from_session(session_id: str | None) -> str | None:
        if isinstance(session_id, str) and session_id.startswith(PAGE_SESSION_PREFIX):
            return session_id[len(PAGE_SESSION_PREFIX):]
        return None

    def _select_page(self, page_id: str, session_id: str | None = None) -> None:
        self._page_id = page_id
        self._session_id = session_id or self._page_session(page_id)
        self._attachment_stale = False

    def _ensure_page(self) -> str:
        if self._attachment_stale:
            raise RuntimeError(
                "BU_SESSION_STALE: the attached GAC page was closed; "
                "switch to a live tab before sending another browser command"
            )
        if self._page_id is None:
            page_id = str(self._call("get_active_page") or "")
            if page_id and self._find_page(page_id) is not None:
                self._select_page(page_id)
                return page_id

            raise RuntimeError(
                "BU_NO_ACTIVE_PAGE: no page is selected for this agent; "
                "create one with bu.new_tab(url) or select one with "
                "bu.switch_tab(target_id)"
            )
        if self._find_page(self._page_id) is None:
            self._attachment_stale = True
            raise RuntimeError(
                "BU_SESSION_STALE: the attached GAC page no longer exists; "
                "call bu.resync() and take a fresh snapshot"
            )
        return self._page_id

    @staticmethod
    def _target_filter_accepts(info: dict[str, Any], filters: Any) -> bool:
        if not isinstance(filters, list) or not filters:
            return True
        included_types = {
            item.get("type")
            for item in filters
            if isinstance(item, dict) and item.get("type") and not item.get("exclude")
        }
        excluded_types = {
            item.get("type")
            for item in filters
            if isinstance(item, dict) and item.get("type") and item.get("exclude")
        }
        target_type = info.get("type")
        if target_type in excluded_types:
            return False
        return not included_types or target_type in included_types

    def _target_infos(self, params: dict[str, Any]) -> dict[str, Any]:
        pages = self._list_pages()
        infos = [
            {
                "targetId": page.id,
                "type": "page",
                "title": page.title,
                "url": page.url,
                "attached": page.id == self._page_id and not self._attachment_stale,
                "canAccessOpener": False,
            }
            for page in pages
        ]

        # GAC page IDs are not necessarily Chrome TargetIDs. Keep non-page targets
        # from CDP (notably iframes), but replace top-level page entries with GAC IDs.
        if self._page_id and not self._attachment_stale:
            try:
                raw = self._send_cdp(self._page_id, "", "Target.getTargets", params)
                for info in raw.get("targetInfos", []):
                    if info.get("type") != "page":
                        infos.append(info)
            except Exception:
                pass

        filters = params.get("filter")
        return {
            "targetInfos": [
                info for info in infos if self._target_filter_accepts(info, filters)
            ]
        }

    def _handle_target_command(
        self,
        method: str,
        params: dict[str, Any],
        explicit_session: str | None,
    ) -> tuple[bool, Any]:
        if method == "Target.getTargets":
            return True, self._target_infos(params)

        if method == "Target.createTarget":
            url = str(params.get("url") or "about:blank")
            page_id = self._call("create_page", url, url)
            return True, {"targetId": page_id}

        target_id = str(params.get("targetId") or "")
        page = self._find_page(target_id) if target_id else None

        if method == "Target.activateTarget" and page is not None:
            self._call("activate_page", target_id)
            self._select_page(target_id)
            return True, {}

        if method == "Target.attachToTarget" and page is not None:
            return True, {"sessionId": self._page_session(target_id)}

        if method == "Target.closeTarget" and page is not None:
            self._call("close_page", target_id)
            if target_id == self._page_id:
                self._attachment_stale = True
                self._session_id = ""
            return True, {"success": True}

        if method == "Target.detachFromTarget":
            session_id = str(params.get("sessionId") or explicit_session or "")
            if self._page_from_session(session_id):
                return True, {}

        return False, None

    @staticmethod
    def _dialog_dict(dialog) -> dict[str, Any]:
        return {
            "is_open": bool(dialog.is_open),
            "result": bool(dialog.result),
            "type": dialog.type,
            "message": dialog.message,
            "user_input": dialog.user_input,
            "default_prompt": dialog.default_prompt,
            "has_browser_handler": bool(dialog.has_browser_handler),
        }

    def _pending_dialog(self) -> dict[str, Any]:
        page_id = self._ensure_page()
        dialogs = self._call("get_page_dialogs", page_id)
        for dialog in dialogs.values():
            # has_browser_handler describes browser-side handling capability;
            # it does not mean that the dialog is already closed.
            if dialog.is_open:
                return {"dialog": self._dialog_dict(dialog)}
        return {"dialog": None}

    def _page_info(self) -> dict[str, Any]:
        page_id = self._ensure_page()
        width, height = self._call("get_page_size", page_id)
        layout = self._send_cdp(page_id, "", "Page.getLayoutMetrics", {}) or {}
        content = layout.get("cssContentSize") or {}
        viewport = layout.get("cssVisualViewport") or {}

        history = self._send_cdp(page_id, "", "Page.getNavigationHistory", {}) or {}
        entries = history.get("entries") or []
        index = history.get("currentIndex")
        if isinstance(index, int) and 0 <= index < len(entries):
            entry = entries[index]
            url = entry.get("url") or ""
            title = entry.get("title") or ""
        else:
            page = self._call("get_page", page_id)
            url = page.url
            title = page.title

        return {
            "url": url,
            "title": title,
            "w": width,
            "h": height,
            "sx": viewport.get("pageX") or 0,
            "sy": viewport.get("pageY") or 0,
            "pw": content.get("width") or 0,
            "ph": content.get("height") or 0,
        }

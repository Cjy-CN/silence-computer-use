"""Native browser and direct-download polling."""

import time

from typing import Any


class DownloadStoreMixin:
    def _download_url(self, payload: dict[str, Any]) -> dict[str, Any]:
        page_id = self._ensure_page()
        url = str(payload.get("url") or "")
        filename = str(payload.get("filename") or "")
        timeout = max(0.0, float(payload.get("timeout") or 0.0))
        guid = self._call(
            "start_direct_download",
            page_id,
            url,
            filename,
        )
        deadline = time.monotonic() + timeout
        while True:
            record = self._call("take_direct_download", guid)
            if record is not None and record.get("state") != "in_progress":
                return {"download": record}
            if time.monotonic() >= deadline:
                return {"download": None}
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

    def _wait_for_download(self, payload: dict[str, Any]) -> dict[str, Any]:
        timeout = max(0.0, float(payload.get("timeout") or 0.0))
        deadline = time.monotonic() + timeout
        while True:
            record = self._call("take_next_download")
            if record is not None:
                return {"download": record}
            if time.monotonic() >= deadline:
                return {"download": None}
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

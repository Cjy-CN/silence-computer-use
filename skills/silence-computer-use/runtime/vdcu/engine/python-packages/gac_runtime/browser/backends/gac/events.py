"""Structured console and network data exposed by GAC commands 8 and 9."""


class EventSourceMixin:
    def _drain_console_logs(self) -> dict:
        page_id = self._ensure_page()
        logs = self._call("drain_console_logs", page_id)
        return {
            "logs": [
                {
                    "level": log.type,
                    "text": log.text,
                }
                for log in logs
            ]
        }

    def _drain_network_requests(self) -> dict:
        page_id = self._ensure_page()
        requests = self._call("drain_network_requests", page_id)
        return {
            "requests": {
                request_id: {
                    "url": request.url,
                    "method": request.method,
                    "status": request.status,
                    "create_time": request.create_time,
                    "update_time": request.update_time,
                }
                for request_id, request in requests.items()
            }
        }

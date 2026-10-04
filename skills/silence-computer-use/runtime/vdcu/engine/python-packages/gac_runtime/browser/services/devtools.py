"""Console and Network observations exposed by browser debug panels."""

from ..domain.core import *


def console_messages(level=None):
    """Drain structured console logs captured by GAC command 8."""
    messages = _bh.drain_console_logs()
    if level:
        messages = [entry for entry in messages if entry["level"] == level]
    return messages


def network_requests(url_contains=None):
    """Drain structured network requests captured by GAC command 9."""
    requests = list(_bh.drain_network_requests().values())
    requests.sort(key=lambda request: request["create_time"])
    if url_contains:
        requests = [
            request for request in requests if url_contains in request["url"]
        ]
    return [
        {
            "url": request["url"],
            "method": request["method"],
            "status": request["status"],
        }
        for request in requests
    ]

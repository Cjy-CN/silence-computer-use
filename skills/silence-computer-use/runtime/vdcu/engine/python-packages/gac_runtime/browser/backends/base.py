"""Backend contract consumed by the browser runtime."""

from typing import Any
from typing import Protocol


class BrowserBackend(Protocol):
    def request(
        self,
        payload: dict[str, Any],
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """Handle one internal browser request."""

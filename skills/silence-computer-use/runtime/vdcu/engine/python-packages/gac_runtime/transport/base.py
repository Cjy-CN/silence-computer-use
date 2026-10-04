from typing import Protocol


class FrameTransceiver(Protocol):
    def reconnect(self) -> bool:
        ...

    def transceive(
        self,
        frame: bytes | bytearray,
        *,
        max_response_size: int | None = None,
    ) -> bytearray:
        ...

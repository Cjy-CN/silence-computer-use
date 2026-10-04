import platform

from .browser_use import BrowserUseCommand

if platform.system() == "Windows":
    from .computer_use_windows import (
        WindowsComputerUseCommand as ComputerUseCommand,
    )


__all__ = ["BrowserUseCommand"]
if platform.system() == "Windows":
    __all__.append("ComputerUseCommand")

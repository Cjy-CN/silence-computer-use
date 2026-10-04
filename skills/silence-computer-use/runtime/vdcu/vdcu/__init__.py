"""vdcu — virtual-desktop computer use that never touches the user's desktop.

The package gives any agent host (Claude Code, Kimi Code, an MCP client, a plain
shell) the computer-use capability the bundled runtime ships: an agent that works on its own
Windows desktop while the user keeps using theirs.

Quick start
-----------
    python -m vdcu enter        # create the agent desktop, show the preview box
    python -m vdcu capture      # look at it
    python -m vdcu click 500 250
    python -m vdcu type "hello"
    python -m vdcu leave

    python -m vdcu mcp          # MCP server for Claude Code / Kimi Code

Design in one paragraph
-----------------------
Measured on Windows 11: ``GetDC(NULL)`` and ``SendInput`` are scoped to the desktop
owned by the calling *process*, and ``BitBlt`` across desktops fails outright.  So
the agent desktop is driven by a worker process created onto it (``lpDesktop``),
which captures windows with ``PrintWindow(PW_RENDERFULLCONTENT)`` — the one API that
returns real pixels for a desktop that was never activated — and delivers input as
window messages.  ``SwitchDesktop`` is never called, which is what makes the user's
desktop, focus and mouse completely unaffected.
"""

from __future__ import annotations

__version__ = "1.0.2"

__all__ = ["__version__", "main"]


def main(argv=None) -> int:
    """Console-script entry point (``vdcu``)."""

    from .cli import main as cli_main

    return cli_main(argv)

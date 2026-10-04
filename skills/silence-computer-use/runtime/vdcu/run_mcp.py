"""MCP entry point — point an agent host's MCP config at this file.

Claude Code::

    claude mcp add vdcu -- python "D:\\path\\to\\virtual-desktop-cu\\run_mcp.py"

Kimi Code / any MCP host (stdio transport)::

    {"mcpServers": {"vdcu": {"command": "python",
                             "args": ["D:\\\\path\\\\to\\\\virtual-desktop-cu\\\\run_mcp.py"]}}}

It exists as a file rather than a console script so a host can be configured with an
absolute path and no installation step.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vdcu.mcp import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

"""Command-line entry point — run this file directly, no PYTHONPATH needed.

An agent host that only has a shell tool can call::

    python "<install>\\run_cli.py" enter
    python "<install>\\run_cli.py" capture --shot look.png
    python "<install>\\run_cli.py" click 500 250
    python "<install>\\run_cli.py" leave

It exists so the skill never depends on how a host sets up its environment: the
package directory is put on ``sys.path`` from this file's own location.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vdcu.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

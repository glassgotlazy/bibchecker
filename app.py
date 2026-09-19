"""Deployment entrypoint.

Vercel's Python runtime looks for a module-level ASGI application instance.
The ``sys.path`` insertion makes the src-layout package importable whether or
not the project itself was pip-installed during the build.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from bibcheck.web import create_app  # noqa: E402

app = create_app()

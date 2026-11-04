"""Vercel entrypoint: the serverless app from ``dota_insights.mcp.asgi``.

Vercel resolves entrypoints from the repository root and does not install the package from the
``src/`` layout, so this file puts ``src`` on the import path first.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from dota_insights.mcp.asgi import app  # noqa: E402

__all__ = ["app"]

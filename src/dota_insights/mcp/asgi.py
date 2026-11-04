"""ASGI app for serverless hosting (Vercel).

Each request may land on a fresh instance, so the app runs stateless: no MCP session is kept
between requests, and responses are plain JSON instead of an SSE stream. The MCP endpoint is
``/mcp`` and the liveness probe ``/health``, as with ``dota-insights`` run locally.
"""

from dota_insights.logging import setup_logging
from dota_insights.mcp.server import mcp

setup_logging()

app = mcp.http_app(stateless_http=True, json_response=True)

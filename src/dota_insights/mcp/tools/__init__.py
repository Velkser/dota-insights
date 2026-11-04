"""MCP tool domains.

Each domain lives in its own module (``tools/<domain>.py``) and exposes ``register(mcp)``,
which attaches that domain's tools with plain ``@mcp.tool`` decorators. To add a domain,
create the module and append it to ``DOMAINS``.
"""

from types import ModuleType

from fastmcp import FastMCP

from dota_insights.mcp.tools import meta

DOMAINS: tuple[ModuleType, ...] = (meta,)


def register_all(mcp: FastMCP) -> None:
    """Register every tool domain on ``mcp``."""
    for domain in DOMAINS:
        domain.register(mcp)

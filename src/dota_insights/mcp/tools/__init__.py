"""MCP tool domains.

Each domain lives in its own module (``tools/<domain>.py``) and exposes ``register(mcp)``,
which attaches that domain's tools with plain ``@mcp.tool`` decorators. Modules in this
package are discovered automatically, so adding a domain means adding a file and nothing
else.
"""

import importlib
import pkgutil
from types import ModuleType

from fastmcp import FastMCP


def discover_domains() -> tuple[ModuleType, ...]:
    """Import every public module in this package that defines ``register``, sorted by name."""
    modules = []
    for info in sorted(pkgutil.iter_modules(__path__), key=lambda i: i.name):
        if info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{__name__}.{info.name}")
        if callable(getattr(module, "register", None)):
            modules.append(module)
    return tuple(modules)


def register_all(mcp: FastMCP) -> None:
    """Register every tool domain on ``mcp``."""
    for domain in discover_domains():
        domain.register(mcp)

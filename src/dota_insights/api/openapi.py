"""The OpenAPI 3.1 document for ``/api/v1``, generated from ``ENDPOINTS`` and the tools' schemas.

Parameters come from each tool's input schema (FastMCP builds it from the tool's signature and
Pydantic models) and from ``Filters``; the 200 response is the tool's output schema, i.e. its
``ToolResult[...]`` envelope. Pydantic's ``$defs`` are moved to ``components/schemas``, since
OpenAPI resolves ``$ref`` against the whole document. Titles are dropped (FastMCP drops them
from tool schemas too), so the same model from different schemas is stored once.
"""

import copy
from typing import Any

from fastmcp import FastMCP

from dota_insights import __version__
from dota_insights.api.routes import DOCS_URL, ENDPOINTS, FILTER_FIELDS, PREFIX
from dota_insights.mcp.envelope import ToolResult
from dota_insights.models import Filters

ERROR_SCHEMA = "ErrorEnvelope"
COMPONENTS_REF = "#/components/schemas/"


def _untitled(node: Any) -> Any:
    """``node`` without schema titles (a property named "title" is a dict and stays)."""
    if isinstance(node, dict):
        return {
            k: _untitled(v) for k, v in node.items() if not (k == "title" and isinstance(v, str))
        }
    if isinstance(node, list):
        return [_untitled(v) for v in node]
    return node


class Components:
    """``components/schemas``: Pydantic ``$defs`` gathered from many schemas, deduplicated."""

    def __init__(self) -> None:
        self.schemas: dict[str, Any] = {}
        self._raw: dict[str, Any] = {}

    def add(self, schema: dict[str, Any]) -> dict[str, Any]:
        """``schema`` without its ``$defs``, its references pointing into the components.

        A definition with the same name as a different one already stored gets a number.
        """
        schema = _untitled(copy.deepcopy(schema))
        defs = schema.pop("$defs", {})
        names: dict[str, str] = {}
        for name, raw in defs.items():
            target, n = name, 2
            while target in self._raw and self._raw[target] != raw:
                target, n = f"{name}{n}", n + 1
            names[name] = target

        def rewrite(node: Any) -> Any:
            if isinstance(node, dict):
                ref = node.get("$ref")
                if isinstance(ref, str) and ref.startswith("#/$defs/"):
                    node = {**node, "$ref": COMPONENTS_REF + names[ref.removeprefix("#/$defs/")]}
                return {k: rewrite(v) for k, v in node.items()}
            if isinstance(node, list):
                return [rewrite(v) for v in node]
            return node

        for name, raw in defs.items():
            if names[name] not in self._raw:
                self._raw[names[name]] = raw
                self.schemas[names[name]] = rewrite(raw)
        return rewrite(schema)

    def put(self, name: str, schema: dict[str, Any]) -> dict[str, str]:
        self.schemas[name] = self.add(schema)
        return {"$ref": COMPONENTS_REF + name}


def _parameter(name: str, where: str, schema: dict[str, Any], required: bool) -> dict[str, Any]:
    schema = dict(schema)
    description = schema.pop("description", None)
    param: dict[str, Any] = {"name": name, "in": where, "required": required, "schema": schema}
    if description:
        param["description"] = description
    return param


def _error(description: str, headers: dict[str, Any] | None = None) -> dict[str, Any]:
    response: dict[str, Any] = {
        "description": description,
        "content": {"application/json": {"schema": {"$ref": COMPONENTS_REF + ERROR_SCHEMA}}},
    }
    if headers:
        response["headers"] = headers
    return response


def _schema_name(tool: str) -> str:
    return "".join(part.title() for part in tool.split("_")) + "Envelope"


async def build_document(mcp: FastMCP) -> dict[str, Any]:
    components = Components()
    filters = components.add(Filters.model_json_schema())["properties"]
    components.put(ERROR_SCHEMA, ToolResult.model_json_schema())
    retry_after = {
        "Retry-After": {
            "description": "Seconds until the exceeded quota resets.",
            "schema": {"type": "integer"},
        }
    }
    paths: dict[str, Any] = {}
    for endpoint in ENDPOINTS:
        tool = await mcp.get_tool(endpoint.tool)
        inputs = components.add(tool.parameters)
        properties = inputs.get("properties", {})
        required = set(inputs.get("required", []))
        parameters = []
        for name in endpoint.path_params:
            parameters.append(_parameter(name, "path", properties[name], True))
        for name, schema in properties.items():
            if name in endpoint.path_params or name == "filters":
                continue
            if name in FILTER_FIELDS:
                raise ValueError(f"{endpoint.tool}: argument {name!r} clashes with a filter")
            parameters.append(_parameter(name, "query", schema, name in required))
        if "filters" in properties:
            parameters += [_parameter(n, "query", filters[n], False) for n in FILTER_FIELDS]
        envelope = components.put(_schema_name(endpoint.tool), tool.output_schema or {})
        paths[PREFIX + endpoint.path] = {
            "get": {
                "operationId": endpoint.tool,
                "summary": endpoint.summary,
                "description": (tool.description or "").strip()
                + f"\n\nRuns the MCP tool `{endpoint.tool}`.",
                "tags": [endpoint.tag],
                "parameters": parameters,
                "responses": {
                    "200": {
                        "description": (
                            "The tool's envelope. Check `status`: only `ok` carries a result."
                        ),
                        "content": {"application/json": {"schema": envelope}},
                    },
                    "401": _error("Missing, invalid or revoked API key."),
                    "422": _error("Unknown or invalid parameters."),
                    "429": _error("The key's per-minute or per-day quota is spent.", retry_after),
                    "503": _error("The API is not enabled or keys cannot be checked right now."),
                },
            }
        }
    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Dota Insights Developer API",
            "version": f"v1 ({__version__})",
            "description": (
                "Read-only REST access to the Dota Insights analytics tools. Every response "
                "is the tool's envelope with sample size, filters, period, caveats and sources; "
                f"report them alongside the numbers. Guide: {DOCS_URL}"
            ),
        },
        "security": [{"bearerAuth": []}],
        "paths": paths,
        "components": {
            "schemas": components.schemas,
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "An API key from the server's maintainer (di_...).",
                }
            },
        },
    }

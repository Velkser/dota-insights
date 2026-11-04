"""Developer REST API (p5-06): ``/api/v1`` routes over the same tools the MCP server exposes.

- ``context``: marks a tool call as an API call, so the rate limiter applies the key's quotas
  and usage events are recorded with ``channel: "api"``.
- ``keys``: API keys, stored as HMAC-SHA256 only, and their lookup cache.
- ``routes``: the endpoints, bearer authentication and the JSON error responses.
- ``openapi``: the OpenAPI 3.1 document served at ``/api/v1/openapi.json``.
- ``cli``: ``dota-insights api-keys create|list|revoke``.

See docs/api.md.
"""

"""``GET /admin/usage``: the usage report as JSON, for the server's maintainer.

Requires ``Authorization: Bearer <DOTA_INSIGHTS_ADMIN_TOKEN>``. Without the setting, or with a
missing or wrong token, the route answers 404 as if it did not exist. ``?days=N`` (1-365,
default 30) sets the window. Events still waiting in the Redis queue are counted in ``queued``;
they appear in the report after the collector's next ``usage`` run.
"""

import hmac

from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from dota_insights.config import settings
from dota_insights.db.access import read
from dota_insights.usage import store
from dota_insights.usage.events import now_utc
from dota_insights.usage.report import MAX_DAYS, build_report


def authorized(request: Request) -> bool:
    token = settings.admin_token
    if not token:
        return False
    header = request.headers.get("authorization", "")
    scheme, _, given = header.partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(
        given.strip().encode(), token.encode()
    )


def register_admin_routes(mcp: FastMCP) -> None:
    @mcp.custom_route("/admin/usage", methods=["GET"], include_in_schema=False)
    async def admin_usage(request: Request) -> JSONResponse:
        if not authorized(request):
            return JSONResponse({"detail": "Not Found"}, status_code=404)
        try:
            days = max(1, min(MAX_DAYS, int(request.query_params.get("days", "30"))))
        except ValueError:
            return JSONResponse({"detail": "days must be a whole number"}, status_code=400)
        headers = {"Cache-Control": "no-store"}
        report = await read(build_report, now_utc(), days)
        if report is None:
            return JSONResponse(
                {"detail": "the usage database is not configured or unavailable"},
                status_code=503,
                headers=headers,
            )
        report["queued"] = await store.queue_length()
        return JSONResponse(report, headers=headers)

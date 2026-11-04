"""Dota Insights: AI-accessible analytics for Dota 2."""

__version__ = "0.1.0"


def main() -> None:
    from dota_insights.mcp.server import run

    run()

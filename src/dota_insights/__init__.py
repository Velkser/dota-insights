"""Dota Insights: AI-accessible analytics for Dota 2."""

__version__ = "0.1.0"


def main() -> None:
    """``dota-insights`` serves MCP; ``dota-insights collect ...`` runs the collector,
    ``dota-insights usage ...`` reports usage, ``dota-insights privacy ...`` manages opt-outs,
    ``dota-insights insights ...`` writes post drafts, ``dota-insights api-keys ...`` manages
    Developer API keys."""
    import sys

    args = sys.argv[1:]
    if args and args[0] == "collect":
        from dota_insights.collector.cli import main as collect

        sys.exit(collect(args[1:]))
    if args and args[0] == "insights":
        from dota_insights.insights.cli import main as insights

        sys.exit(insights(args[1:]))
    if args and args[0] == "api-keys":
        from dota_insights.api.cli import main as api_keys

        sys.exit(api_keys(args[1:]))
    if args and args[0] in ("usage", "privacy"):
        from dota_insights.usage import cli

        sys.exit(cli.main(args))

    from dota_insights.mcp.server import run

    run()

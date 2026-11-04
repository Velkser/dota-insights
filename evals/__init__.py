"""Tool-selection and answer-quality evals for the dota-insights MCP server.

``dataset.toml`` holds the questions, ``harness`` lets a Claude model answer them with the
server's tools, ``scoring`` grades each transcript and ``report`` writes the JSON and Markdown
summaries. Run with ``uv run --group evals python -m evals.run``; see ``evals/README.md``.
"""

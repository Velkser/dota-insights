"""Command line entry point: ``uv run --group evals python -m evals.run --help``.

Examples::

    # in-process server (calls OpenDota/STRATZ from this machine)
    uv run --group evals python -m evals.run --target local

    # production, only the item questions, with a cheaper model
    uv run --group evals python -m evals.run --target https://dota-insights.vercel.app/mcp \\
        --category items --model claude-sonnet-5-5

    # re-score a saved report after changing checks, no API calls
    uv run python -m evals.run --rescore evals/reports/<stem>.json

Needs ``ANTHROPIC_API_KEY`` in the environment (or in ``.env``) except with ``--rescore``.
The key is never printed or written to reports.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from evals.dataset import load_cases, select
from evals.harness import DEFAULT_MAX_TURNS, DEFAULT_MODEL, RunConfig
from evals.report import summarize, write_report
from evals.scoring import score_case
from evals.transcript import Transcript

REPORTS = Path(__file__).with_name("reports")
PRODUCTION_URL = "https://dota-insights.vercel.app/mcp"


def _load_dotenv(path: Path = Path(".env")) -> None:
    """Put ``ANTHROPIC_API_KEY`` from ``.env`` into the environment if it is not set."""
    if os.environ.get("ANTHROPIC_API_KEY") or not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.strip().partition("=")
        if sep and key.strip() == "ANTHROPIC_API_KEY" and value.strip():
            os.environ["ANTHROPIC_API_KEY"] = value.strip().strip("'\"")


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="python -m evals.run", description=__doc__.split("\n")[0])
    p.add_argument(
        "--target",
        default="local",
        help=f'"local" for the in-process server, "prod" for {PRODUCTION_URL}, or an MCP URL.',
    )
    p.add_argument("--model", default=os.environ.get("EVAL_MODEL", DEFAULT_MODEL))
    p.add_argument("--effort", default="medium", help='Effort level, or "none" to omit it.')
    p.add_argument("--max-turns", type=int, default=DEFAULT_MAX_TURNS)
    p.add_argument("--concurrency", type=int, default=3)
    p.add_argument("--case", action="append", dest="cases", help="Case id; repeatable.")
    p.add_argument("--category", action="append", dest="categories", help="Repeatable.")
    p.add_argument("--lang", choices=["en", "ru"])
    p.add_argument("--limit", type=int)
    p.add_argument("--out", type=Path, default=REPORTS)
    p.add_argument("--stem", help="Report file name without extension (default: timestamp).")
    p.add_argument(
        "--fallbacks",
        action="store_true",
        help="Let the API re-run refused requests on a fallback model (server-side fallbacks). "
        "Off by default so every answer comes from the model under test.",
    )
    p.add_argument("--rescore", type=Path, help="Re-score transcripts from a saved JSON report.")
    p.add_argument(
        "--min-pass-rate",
        type=float,
        help="Exit 1 when the overall pass rate is below this fraction.",
    )
    return p.parse_args(argv)


def _target(value: str):
    if value == "local":
        from dota_insights.mcp.server import mcp

        return mcp, "local"
    if value == "prod":
        return PRODUCTION_URL, PRODUCTION_URL
    return value, value


async def _schemas(server) -> dict:
    from evals.harness import open_session

    async with open_session(server) as session:
        return session.schemas


async def run(args: argparse.Namespace) -> int:
    from evals.harness import AnthropicModel, open_session, run_cases

    cases = select(
        load_cases(), ids=args.cases, categories=args.categories, lang=args.lang, limit=args.limit
    )
    if not cases:
        print("No cases match the filters.", file=sys.stderr)
        return 2
    server, label = _target(args.target)

    if args.rescore:
        data = json.loads(args.rescore.read_text(encoding="utf-8"))
        saved = {t["case_id"]: Transcript.from_dict(t) for t in data["transcripts"]}
        cases = [c for c in cases if c.id in saved]
        transcripts = [saved[c.id] for c in cases]
        schemas = await _schemas(server)
        model_name = data["summary"]["model"]
        label = data["summary"]["target"]
    else:
        _load_dotenv()
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("ANTHROPIC_API_KEY is not set.", file=sys.stderr)
            return 2
        extra = {}
        if args.fallbacks:
            extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        config = RunConfig(
            model=args.model,
            max_turns=args.max_turns,
            effort=None if args.effort == "none" else args.effort,
            extra_params=extra,
        )
        model = AnthropicModel()
        done = 0

        def progress(case, transcript):
            nonlocal done
            done += 1
            tools = ",".join(c.name for c in transcript.calls) or "-"
            flag = f" ERROR {transcript.error}" if transcript.error else ""
            print(f"[{done}/{len(cases)}] {case.id}: {tools} ({transcript.latency_s:.1f}s){flag}")

        async with open_session(server) as session:
            schemas = session.schemas
            transcripts = await run_cases(
                cases, session, model, config, concurrency=args.concurrency, on_done=progress
            )
        model_name = args.model

    scores = [score_case(c, t, schemas) for c, t in zip(cases, transcripts, strict=True)]
    summary = summarize(cases, transcripts, scores, target=label, model=model_name)
    json_path, md_path = write_report(args.out, summary, scores, transcripts, stem=args.stem)
    print(
        f"pass {summary['pass_rate']:.0%} | tool {summary['tool_rate']:.0%} | "
        f"args {summary['args_rate']:.0%} | envelope {summary['envelope_rate']:.0%} | "
        f"answer {summary['answer_rate']:.0%}"
    )
    print(f"Wrote {json_path} and {md_path}")
    if args.min_pass_rate is not None and summary["pass_rate"] < args.min_pass_rate:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(run(parse_args(sys.argv[1:] if argv is None else argv)))


if __name__ == "__main__":
    sys.exit(main())

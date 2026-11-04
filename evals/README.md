# Evals

Checks that a Claude model, given only this server's instructions and tool list, picks the right
tool with sensible arguments, and that its answer is correct and properly caveated (tracker tasks
p3-06 and p3-10).

## Files

- `dataset.toml`: 63 questions in English and Russian, paraphrased from
  `docs/research/user-questions.md` (the `source` field is the question ID). Each names the
  expected tool(s), argument matchers and answer checks.
- `harness.py`: connects to the server (in-process `mcp` object or a URL), runs the model in a
  tool-use loop capped at `--max-turns` requests, and records a transcript per question.
- `scoring.py`: grades a transcript on four dimensions (tool, args, envelope, answer). The module
  docstring lists every matcher and check.
- `report.py`: pass rates by category and language, wrong tool choices, failed checks, worst
  failures, latency, API calls and token cost; writes `reports/<stem>.json` and `.md`.
- `run.py`: the command line.

## Running

Live runs need `ANTHROPIC_API_KEY` in the environment or in the git-ignored `.env`. The key is
never printed or saved. Install the SDK with the `evals` dependency group:

```bash
uv run --group evals python -m evals.run --target prod
```

```bash
uv run --group evals python -m evals.run --target local --category items --lang ru
```

```bash
uv run --group evals python -m evals.run --target prod --case meta-07 --model claude-sonnet-5-5
```

`--target local` runs the tools on this machine (OpenDota and STRATZ calls count against your
quotas); `prod` is `https://dota-insights.vercel.app/mcp`. A full run makes roughly 150 model
requests; see the report's token count and cost estimate.

Re-score a saved report after editing checks, with no API calls:

```bash
uv run python -m evals.run --rescore evals/reports/<stem>.json --stem <stem>-rescored
```

On GitHub, run the **Live evals** workflow by hand (Actions tab, `workflow_dispatch`). It reads
`ANTHROPIC_API_KEY` from repository secrets, posts the Markdown report as the job summary and
uploads both files as an artifact. Normal CI never calls the model: `tests/test_evals.py` checks
the dataset against the live tool schemas and the scoring against recorded transcripts in
`tests/fixtures/evals/`.

## Adding a case

Add a `[[case]]` to `dataset.toml` with a new `id`, then run `uv run pytest tests/test_evals.py`:
it fails if a tool, parameter, matcher, check or hero/item name in the case does not exist.
Prefer checks that a correct answer cannot miss (sample size, the correlation caveat for item and
hypothesis questions, the user's language) over checks on exact numbers, which change with the
data.

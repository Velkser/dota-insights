"""Offline tests for the eval dataset, scoring, harness loop and report (no model or API calls)."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastmcp import FastMCP

from dota_insights.mcp.envelope import insufficient_data, ok
from dota_insights.mcp.server import mcp
from evals.dataset import Case, load_cases, select
from evals.harness import RunConfig, open_session, parse_envelope, run_case, run_cases
from evals.report import summarize, to_markdown, write_report
from evals.run import parse_args
from evals.scoring import CHECKS, MISSING, match_arg, score_case
from evals.transcript import ToolCall, Transcript

FIXTURES = Path(__file__).parent / "fixtures" / "evals"
MATCHERS = {
    "hero",
    "hero_any",
    "item",
    "equals",
    "any_of",
    "contains",
    "contains_any",
    "includes",
    "range",
    "present",
    "optional",
}


async def _list_schemas(server) -> dict:
    async with open_session(server) as session:
        return session.schemas


@pytest.fixture(scope="module")
def schemas() -> dict:
    return asyncio.run(_list_schemas(mcp))


@pytest.fixture(scope="module")
def cases() -> dict[str, Case]:
    return {c.id: c for c in load_cases()}


# --- dataset -----------------------------------------------------------------------------


def test_dataset_shape(cases):
    assert 45 <= len(cases) <= 80
    langs = {c.lang for c in cases.values()}
    assert langs == {"en", "ru"}
    categories = {c.category for c in cases.values()}
    assert {
        "meta",
        "counters",
        "builds",
        "items",
        "farming",
        "match",
        "player",
        "pro",
    } <= categories
    assert {"economy", "hypotheses", "myths"} <= categories


def test_dataset_ids_are_unique():
    ids = [c.id for c in load_cases()]
    assert len(ids) == len(set(ids))


def test_dataset_references_real_tools_params_and_checks(schemas, cases):
    for case in cases.values():
        assert case.expected_tools, case.id
        unknown = set(case.expected_tools + case.forbidden_tools) - set(schemas)
        assert not unknown, f"{case.id}: unknown tools {unknown}"
        params = {p for t in case.expected_tools for p in schemas[t].get("properties", {})}
        for path, spec in case.args.items():
            assert path.split(".")[0] in params, f"{case.id}: no expected tool takes {path!r}"
            assert set(spec) <= MATCHERS, f"{case.id}: unknown matcher in {spec}"
        for check in case.checks:
            assert check["type"] in CHECKS, f"{case.id}: unknown check {check['type']!r}"
        assert "error" not in case.allowed_status


def test_dataset_hero_and_item_names_resolve(cases):
    from dota_insights.reference import resolve_hero, resolve_item

    for case in cases.values():
        for spec in case.args.values():
            for kind, resolve in (("hero", resolve_hero), ("hero_any", resolve_hero)):
                for name in (
                    spec.get(kind, []) if isinstance(spec.get(kind), list) else [spec.get(kind)]
                ):
                    if name:
                        assert resolve(name).best, f"{case.id}: {name!r} does not resolve"
            names = spec.get("item")
            for name in names if isinstance(names, list) else [names] if names else []:
                assert resolve_item(name).best, f"{case.id}: item {name!r} does not resolve"


def test_select_filters(cases):
    all_cases = load_cases()
    assert [c.id for c in select(all_cases, ids=["meta-01"])] == ["meta-01"]
    assert all(c.lang == "ru" for c in select(all_cases, lang="ru"))
    assert all(c.category == "items" for c in select(all_cases, categories=["items"]))
    assert len(select(all_cases, limit=3)) == 3


# --- argument matchers -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("spec", "value", "passed"),
    [
        ({"hero": "Phantom Assassin"}, "Фантомка", True),
        ({"hero": "Phantom Assassin"}, "PA", True),
        ({"hero": "Phantom Assassin"}, 44, True),
        ({"hero": "Phantom Assassin"}, "Anti-Mage", False),
        ({"hero": ["Pudge", "Lion"]}, ["lion", "pudge", "axe"], True),
        ({"hero": ["Pudge", "Lion"]}, ["Pudge"], False),
        ({"hero_any": ["Anti-Mage", "Medusa"]}, "medusa", True),
        ({"item": "Black King Bar"}, "bkb", True),
        ({"item": ["Battle Fury", "Maelstrom"]}, ["bfury", "maelstrom"], True),
        ({"equals": 9073999945}, 9073999945, True),
        ({"equals": 9073999945}, "9073999945", True),
        ({"any_of": [1]}, 2, False),
        ({"contains": "ancient"}, "ancient+", True),
        ({"contains_any": ["liquid", "tundra"]}, "Team Liquid", True),
        ({"includes": 20}, [10, 20, 30], True),
        ({"includes": 20}, [10, 15], False),
        ({"range": [24, 26]}, 25, True),
        ({"range": [24, 26]}, 30, False),
        ({"range": [24, 26]}, "x", False),
        ({"present": False}, MISSING, True),
        ({"present": False}, 3, False),
        ({"any_of": ["counters", "both"], "optional": True}, MISSING, True),
        ({"any_of": ["counters", "both"], "optional": True}, "synergies", False),
        ({"any_of": [1]}, MISSING, False),
    ],
)
def test_match_arg(spec, value, passed):
    assert match_arg(spec, value)[0] is passed


def test_unknown_matcher_is_an_error():
    with pytest.raises(ValueError, match="unknown argument matcher"):
        match_arg({"fuzzy": 1}, "x")


# --- answer checks -----------------------------------------------------------------------


def _check(name: str, answer: str, spec: dict | None = None, case: Case | None = None) -> bool:
    case = case or Case("c", "meta", "en", "q", ("get_tier_list",))
    transcript = Transcript(case_id="c", model="m", answer=answer)
    return CHECKS[name]({"type": name, **(spec or {})}, case, transcript).passed


@pytest.mark.parametrize(
    ("answer", "passed"),
    [
        ("Based on 48,210 matches in Ancient.", True),
        ("Across 1.2k ranked games this week.", True),
        ("По 12 345 матчам на Древних.", True),
        ("Выборка небольшая.", True),
        ("n = 340", True),
        ("Phantom Assassin wins a lot.", False),
    ],
)
def test_sample_size_check(answer, passed):
    assert _check("sample_size", answer) is passed


@pytest.mark.parametrize(
    ("answer", "passed"),
    [
        ("Buying BKB early guarantees a win.", False),
        ("Ранний БКБ гарантирует победу.", False),
        ("Early BKB is associated with winning, but it does not cause it.", True),
        ("Early buyers win more; that is correlation, and it guarantees nothing.", True),
        ("It does not guarantee anything.", True),
        ("Early BKB buyers win 58% of games.", True),
    ],
)
def test_no_causal_claim_check(answer, passed):
    assert _check("no_causal_claim", answer) is passed


def test_language_check():
    ru = Case("c", "meta", "ru", "q", ("get_tier_list",))
    assert _check("language", "Винрейт Phantom Assassin около 51%.", case=ru)
    assert not _check("language", "Phantom Assassin wins about 51% of games.", case=ru)
    assert _check("language", "Phantom Assassin wins about 51% of games.")


def test_answer_number_and_mentions_checks():
    assert _check(
        "answer_number", "Radiant wins 51,3% of games.", {"range": [35, 65], "percent": True}
    )
    assert not _check(
        "answer_number", "Radiant wins 0.51 of games.", {"range": [35, 65], "percent": True}
    )
    assert _check("mentions_any", "p = 0.003", {"patterns": ["p\\s*[=<]"]})
    assert not _check("not_mentions", "It is guaranteed.", {"patterns": ["guarantee"]})


def test_field_check_reads_ok_envelopes_only():
    case = Case("c", "myths", "en", "q", ("get_general_stats",))
    good = ToolCall(
        "get_general_stats", {}, "ok", {"status": "ok", "result": {"radiant_win_rate": 0.52}}
    )
    bad = ToolCall(
        "get_general_stats", {}, "error", {"status": "error", "result": {"radiant_win_rate": 7}}
    )
    spec = {"type": "field", "key": "radiant_win_rate", "range": [0.35, 0.65]}
    t = Transcript(case_id="c", model="m", calls=[good, bad])
    assert CHECKS["field"](spec, case, t).passed
    t = Transcript(case_id="c", model="m", calls=[bad])
    assert not CHECKS["field"](spec, case, t).passed


# --- recorded transcripts ----------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
def test_recorded_transcript_scores(path, cases, schemas):
    data = json.loads(path.read_text(encoding="utf-8"))
    transcript = Transcript.from_dict(data["transcript"])
    score = score_case(cases[transcript.case_id], transcript, schemas)
    expected = data["expected"]
    failed = [d for d in ("tool", "args", "envelope", "answer") if not getattr(score, d).passed]
    assert failed == expected["failed"], score.to_dict()
    assert score.passed is expected["passed"]
    failed_checks = [c.name for c in score.checks if not c.passed]
    assert failed_checks == expected["failed_checks"], score.to_dict()


def test_schema_invalid_arguments_fail_args(cases, schemas):
    call = ToolCall(
        "get_hero_overview", {"hero": "PA", "filters": {"rank": "ancient"}}, "ok", {"status": "ok"}
    )
    t = Transcript(
        case_id="meta-07", model="m", calls=[call], answer="Винрейт 51% по 48 210 матчам."
    )
    score = score_case(cases["meta-07"], t, schemas)
    assert score.tool.passed
    assert not score.args.passed
    assert "filters" in score.args.detail


def test_forbidden_tool_fails_tool_dimension(schemas):
    case = Case("c", "meta", "en", "q", ("get_tier_list",), forbidden_tools=("explore_query",))
    calls = [
        ToolCall("get_tier_list", {}, "ok", {"status": "ok"}),
        ToolCall("explore_query", {"table": "matches"}, "ok", {"status": "ok"}),
    ]
    score = score_case(case, Transcript(case_id="c", model="m", calls=calls, answer="x"), schemas)
    assert not score.tool.passed


# --- harness loop ------------------------------------------------------------------------


def _toy_server() -> FastMCP:
    server = FastMCP("toy", instructions="Report sample sizes.")

    @server.tool
    def get_hero_overview(hero: str) -> dict:
        """Win rate of one hero."""
        return ok({"hero": hero, "win_rate": 0.51}, sample_size=1000).model_dump(mode="json")

    @server.tool
    def get_tier_list() -> dict:
        """Heroes ranked into tiers."""
        return insufficient_data("Too few matches.", "Widen rank_bracket.").model_dump(mode="json")

    return server


def _block(type_: str, **kw):
    return SimpleNamespace(type=type_, **kw)


def _message(stop_reason: str, *blocks, input_tokens=100, output_tokens=20):
    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    return SimpleNamespace(stop_reason=stop_reason, content=list(blocks), usage=usage)


class ScriptedModel:
    """Replays a fixed list of responses and records the requests it got."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def create(self, **params):
        self.requests.append(json.loads(json.dumps(params, default=lambda o: vars(o))))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


CASE = Case("toy-1", "meta", "en", "How strong is PA?", ("get_hero_overview",))


async def test_run_case_calls_tools_and_records_transcript():
    thinking = _block("thinking", thinking="", signature="sig")
    model = ScriptedModel(
        [
            _message(
                "tool_use",
                thinking,
                _block("tool_use", id="t1", name="get_hero_overview", input={"hero": "PA"}),
                _block("tool_use", id="t2", name="get_tier_list", input={}),
            ),
            _message("end_turn", _block("text", text="PA wins 51% over 1,000 matches.")),
        ]
    )
    async with open_session(_toy_server()) as session:
        assert session.instructions == "Report sample sizes."
        t = await run_case(CASE, session, model, RunConfig(model="test-model"))

    assert t.error is None
    assert t.answer == "PA wins 51% over 1,000 matches."
    assert [c.name for c in t.calls] == ["get_hero_overview", "get_tier_list"]
    assert [c.status for c in t.calls] == ["ok", "insufficient_data"]
    assert t.calls[0].envelope["sample_size"] == 1000
    assert (t.api_calls, t.turns, t.input_tokens, t.output_tokens) == (2, 2, 200, 40)
    first, second = model.requests
    assert "Report sample sizes." in first["system"]
    assert {tool["name"] for tool in first["tools"]} == {"get_hero_overview", "get_tier_list"}
    assert first["output_config"] == {"effort": "medium"}
    # The assistant turn goes back unchanged (thinking included); both results in one message.
    assistant, results = second["messages"][1], second["messages"][2]
    assert assistant["content"][0]["type"] == "thinking"
    assert [r["tool_use_id"] for r in results["content"]] == ["t1", "t2"]
    assert '"status":"ok"' in results["content"][0]["content"].replace(" ", "")


async def test_run_case_reports_unknown_tool_and_turn_cap():
    loop = _message("tool_use", _block("tool_use", id="t", name="no_such_tool", input={}))
    model = ScriptedModel([loop, loop, loop])
    async with open_session(_toy_server()) as session:
        t = await run_case(CASE, session, model, RunConfig(max_turns=2))
    assert t.hit_turn_cap
    assert t.api_calls == 2
    assert all(c.is_error and c.status is None for c in t.calls)
    assert t.answer == ""


async def test_run_case_records_api_errors_and_refusals():
    async with open_session(_toy_server()) as session:
        failed = await run_case(CASE, session, ScriptedModel([RuntimeError("boom")]), RunConfig())
        refused = await run_case(CASE, session, ScriptedModel([_message("refusal")]), RunConfig())
    assert failed.error == "RuntimeError: boom"
    assert refused.error == "model refused"


async def test_run_cases_keeps_order_and_reports_progress():
    done = []
    responses = [_message("end_turn", _block("text", text=f"answer {i}")) for i in range(3)]
    model = ScriptedModel(responses)
    cases = [Case(f"c{i}", "meta", "en", "q", ("get_tier_list",)) for i in range(3)]
    async with open_session(_toy_server()) as session:
        out = await run_cases(
            cases,
            session,
            model,
            RunConfig(),
            concurrency=2,
            on_done=lambda c, t: done.append(c.id),
        )
    assert [t.case_id for t in out] == ["c0", "c1", "c2"]
    assert sorted(done) == ["c0", "c1", "c2"]


def test_parse_envelope():
    assert parse_envelope({"status": "ok"}, "")["status"] == "ok"
    assert parse_envelope({"result": {"status": "error"}}, "")["status"] == "error"
    assert parse_envelope(None, '{"status": "insufficient_data"}')["status"] == "insufficient_data"
    assert parse_envelope(None, "not json") is None


# --- report ------------------------------------------------------------------------------


def test_summary_and_report_files(tmp_path, cases, schemas):
    transcripts, scores, used = [], [], []
    for path in sorted(FIXTURES.glob("*.json")):
        t = Transcript.from_dict(json.loads(path.read_text(encoding="utf-8"))["transcript"])
        transcripts.append(t)
        used.append(cases[t.case_id])
        scores.append(score_case(cases[t.case_id], t, schemas))
    summary = summarize(used, transcripts, scores, target="local", model="claude-opus-5-5")

    assert summary["cases"] == len(scores)
    assert summary["pass_rate"] == pytest.approx(sum(s.passed for s in scores) / len(scores))
    assert summary["by_lang"].keys() == {"en", "ru"}
    assert "suggest_pick" in next(iter(summary["tool_confusion"]))
    assert summary["run_errors"] == {"myth-02": "RateLimitError: rate_limit_error"}
    assert summary["estimated_cost_usd"] > 0
    assert summary["worst"][0]["score"] == 0.0

    markdown = to_markdown(summary, scores, transcripts)
    assert "## By category" in markdown and "## Worst failures" in markdown
    json_path, md_path = write_report(tmp_path, summary, scores, transcripts, stem="run")
    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved["summary"]["cases"] == len(scores)
    assert len(saved["transcripts"]) == len(scores)
    assert md_path.read_text(encoding="utf-8") == markdown


def test_cli_defaults():
    args = parse_args([])
    assert args.target == "local"
    assert args.max_turns == 8
    args = parse_args(["--target", "prod", "--case", "meta-01", "--case", "item-01"])
    assert args.cases == ["meta-01", "item-01"]

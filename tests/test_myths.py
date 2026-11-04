"""The myth library (evals/myths.yaml, docs/myths.md) stays consistent with the tools and evals."""

import asyncio
import importlib.util
from pathlib import Path

import pytest

from dota_insights.mcp.server import mcp
from evals.dataset import load_cases

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gen_myths_doc.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("gen_myths_doc", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _load_script()
DATA = gen.load()
MYTHS = DATA["myths"]


@pytest.fixture(scope="module")
def schemas() -> dict:
    async def _tools():
        return {t.name: t.parameters for t in await mcp.list_tools()}

    return asyncio.run(_tools())


def test_library_shape():
    assert 12 <= len(MYTHS) <= 25
    ids = [m["id"] for m in MYTHS]
    assert len(ids) == len(set(ids))
    for m in MYTHS:
        assert m["verdict"] in gen.VERDICTS, m["id"]
        assert m["caveats"], f"{m['id']}: every myth needs at least one caveat"
        r = m["result"]
        assert r["sample_size"] > 0 and r["patch"] and r["effect"], m["id"]
        if r["ci"] is not None:
            low, high = r["ci"]
            assert low <= high, m["id"]
        if m["verdict"] == "supported":
            assert r["ci"] is not None, f"{m['id']}: a supported myth needs an interval"


def test_calls_match_tool_schemas(schemas):
    for m in MYTHS:
        assert m["tool"] in schemas, f"{m['id']}: unknown tool {m['tool']}"
        params = schemas[m["tool"]].get("properties", {})
        assert set(m["args"]) <= set(params), f"{m['id']}: {set(m['args']) - set(params)}"
        assert set(schemas[m["tool"]].get("required", [])) <= set(m["args"]), m["id"]


def test_every_myth_is_an_eval_case():
    cases = {c.id: c for c in load_cases()}
    for m in MYTHS:
        case = cases.get(m["eval_case"])
        assert case is not None, f"{m['id']}: no eval case {m['eval_case']!r}"
        assert m["tool"] in case.expected_tools, f"{m['id']}: {case.id} does not expect {m['tool']}"
    library_cases = {c.id for c in cases.values() if c.id.startswith("myth-lib-")}
    assert library_cases <= {m["eval_case"] for m in MYTHS}


def test_doc_is_up_to_date():
    assert gen.DOC.read_text(encoding="utf-8") == gen.render(DATA), (
        "docs/myths.md is stale; run: uv run python scripts/gen_myths_doc.py"
    )

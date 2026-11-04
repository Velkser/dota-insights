"""docs/architecture/metrics.md covers exactly the registered tools and is up to date (p1-05)."""

import importlib.util
import sys
from pathlib import Path

from dota_insights.mcp.server import mcp

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gen_metrics_doc.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("gen_metrics_doc", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclasses look their module up here
    spec.loader.exec_module(module)
    return module


gen = _load_script()


async def test_every_registered_tool_has_a_matrix_entry():
    names = {t.name for t in await mcp.list_tools()}
    assert names - set(gen.MATRIX) == set(), "add the tool to MATRIX in scripts/gen_metrics_doc.py"
    assert set(gen.MATRIX) - names == set(), "remove tools that no longer exist from MATRIX"


def test_entries_name_sources_and_coverage():
    for name, entry in gen.MATRIX.items():
        assert entry.metrics and entry.sources, name
        assert entry.coverage in ("good", "partial", "weak", "none"), name
        if entry.coverage in ("weak", "none"):
            assert entry.note, f"{name}: say why coverage is {entry.coverage}"


async def test_doc_is_up_to_date():
    expected = gen.render(await gen._result_fields())
    assert gen.DOC.read_text(encoding="utf-8") == expected, (
        "docs/architecture/metrics.md is stale; run: uv run python scripts/gen_metrics_doc.py"
    )

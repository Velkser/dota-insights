import importlib.util
from pathlib import Path

from dota_insights.mcp.server import mcp

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "gen_tool_docs.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("gen_tool_docs", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gen = _load_script()


async def test_readme_tool_section_is_up_to_date():
    current = gen.README.read_text(encoding="utf-8")
    expected = gen.updated_readme(current, await gen.render())
    assert current == expected, "README.md is stale; run: uv run python scripts/gen_tool_docs.py"


async def test_every_tool_has_an_example_question():
    names = {t.name for t in await mcp.list_tools()}
    assert names - set(gen.EXAMPLES) == set(), "add an example question to scripts/gen_tool_docs.py"
    assert set(gen.EXAMPLES) - names == set(), "remove examples of tools that no longer exist"


async def test_readme_lists_every_tool_and_prompt():
    readme = gen.README.read_text(encoding="utf-8")
    for tool in await mcp.list_tools():
        assert f"| `{tool.name}` |" in readme
    for prompt in await mcp.list_prompts():
        assert f"| `{prompt.name}` |" in readme


def test_updated_readme_replaces_only_the_generated_section():
    readme = f"intro\n{gen.START}\nold\n{gen.END}\noutro\n"
    assert gen.updated_readme(readme, f"{gen.START}\nnew\n{gen.END}") == (
        f"intro\n{gen.START}\nnew\n{gen.END}\noutro\n"
    )

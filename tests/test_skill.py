import importlib.util
import re
import zipfile
from pathlib import Path

from dota_insights.mcp.server import mcp

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_skill.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("build_skill", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


build = _load_script()


def _frontmatter() -> dict:
    text = (build.SKILL / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md must start with YAML frontmatter"
    # Plain one-line `key: value` pairs only, which every agent's parser reads the same way.
    lines = match.group(1).splitlines()
    assert all(re.match(r"^[a-z]+: \S", line) for line in lines), lines
    return dict(line.split(": ", 1) for line in lines)


async def test_skill_tool_reference_is_up_to_date():
    current = build.TOOLS.read_text(encoding="utf-8")
    assert current == await build.render_tools(), "run: uv run python scripts/build_skill.py"


async def test_skill_names_only_registered_tools():
    names = {t.name for t in await mcp.list_tools()}
    text = "\n".join(p.read_text(encoding="utf-8") for p in build.skill_files())
    mentioned = set(re.findall(r"`([a-z]+(?:_[a-z]+)+)`", text)) | set(
        re.findall(r"\b([a-z]+(?:_[a-z]+)+)\(", text)
    )
    tool_like = {m for m in mentioned if m.split("_")[0] in {n.split("_")[0] for n in names}}
    assert tool_like - names == set(), "SKILL files mention tools the server does not have"


def test_skill_frontmatter():
    meta = _frontmatter()
    assert set(meta) == {"name", "description"}
    assert meta["name"] == build.SKILL.name == "dota-insights"
    assert meta["description"].startswith("Use when")
    assert len(meta["description"]) <= 1024


def test_skill_has_no_secrets():
    secret = re.compile(r"(api[_-]?key|token|password|secret)\s*[:=]\s*\S{8,}", re.IGNORECASE)
    for path in build.skill_files():
        assert not secret.search(path.read_text(encoding="utf-8")), path


def test_skill_zip_has_one_top_level_folder(tmp_path):
    target = build.build_zip(tmp_path / "skill.zip")
    with zipfile.ZipFile(target) as archive:
        names = archive.namelist()
    assert "dota-insights/SKILL.md" in names
    assert {n.split("/")[0] for n in names} == {"dota-insights"}
    assert not any(Path(n).name in build.SKIPPED for n in names)
    assert all(n.endswith(".md") for n in names)

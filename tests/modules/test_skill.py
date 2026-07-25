"""Tests for s07 Skill 加载.

验收：给定任务，SkillLoader 命中对应 skill 且出现在 system prompt 中；
load_skill 工具可显式拉取。
"""

from __future__ import annotations

from pathlib import Path

from synapse.modules.skill import SkillLoader
from synapse.modules.tools.skill_tool import SkillTool
from synapse.modules.planning.react import ReActPlanner


def _make_skills(tmp_path: Path) -> Path:
    d = tmp_path / "skills" / "pytest"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\n"
        "name: pytest\n"
        "triggers: pytest, 单测\n"
        "task_types: test\n"
        "---\n"
        "写测试时用 pytest -q，断言要具体。\n",
        encoding="utf-8",
    )
    return tmp_path / "skills"


def test_loader_matches_by_trigger_and_renders():
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    loader = SkillLoader(_make_skills(tmp))

    skills = loader.match("给模块写 pytest 单测")
    assert any(s.name == "pytest" for s in skills)
    block = loader.render("给模块写 pytest 单测")
    assert "写测试时用 pytest -q" in block
    # 无关任务不应命中
    assert loader.render("重构这段登录逻辑") == ""


def test_skill_in_system_prompt():
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    loader = SkillLoader(_make_skills(tmp))
    planner = ReActPlanner(skill_loader=loader)
    # 构造一个最小 context
    ctx = type("C", (), {"system": [], "core": [], "reference": []})()
    prompt = planner._build_system_prompt(ctx, task="为 utils.py 写 pytest 测试")
    assert "写测试时用 pytest -q" in prompt
    # 无任务 / 无匹配 → 不含 skill 正文
    prompt2 = planner._build_system_prompt(ctx, task="重构登录逻辑")
    assert "写测试时用 pytest -q" not in prompt2


def test_load_skill_tool():
    import tempfile
    tmp = Path(tempfile.mkdtemp())
    loader = SkillLoader(_make_skills(tmp))
    tool = SkillTool(loader)
    from synapse.protocols.tool import ToolResult
    res = __import__("asyncio").run(tool.execute({"name": "pytest"}))
    assert res.success and "pytest -q" in res.output
    bad = __import__("asyncio").run(tool.execute({"name": "nope"}))
    assert not bad.success

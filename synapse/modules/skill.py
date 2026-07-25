"""Skill 按需加载 (s07).

``skills/<name>/SKILL.md`` 含 frontmatter（name / triggers / task_types）+ 正文。
``SkillLoader`` 按任务匹配并把命中 skill 注入 system 提示；LLM 也可用
``load_skill`` 工具显式拉取。

ponytail: frontmatter 用极简 ``key: value`` 解析（列表按逗号分隔），不引第三方
YAML；匹配用 classify_task 的 task_type + 触发词子串，规则简单但够用。
"""

from __future__ import annotations

import re
from pathlib import Path

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


class Skill:
    def __init__(self, name: str, triggers: list[str], task_types: list[str], body: str):
        self.name = name
        self.triggers = [t.lower() for t in triggers]
        self.task_types = set(task_types)
        self.body = body


class SkillLoader:
    def __init__(self, skills_dir: str | Path | None = None):
        self.skills_dir = Path(skills_dir) if skills_dir else None
        self._skills: dict[str, Skill] = {}
        if self.skills_dir and self.skills_dir.is_dir():
            self.scan()

    def scan(self) -> None:
        self._skills.clear()
        if not self.skills_dir or not self.skills_dir.is_dir():
            return
        for d in self.skills_dir.iterdir():
            md = d / "SKILL.md"
            if md.is_file():
                self._register(md)

    def _register(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8")
        m = _FRONTMATTER_RE.match(text)
        if not m:
            name, body, triggers, task_types = path.parent.name, text.strip(), [], []
        else:
            meta = _parse_frontmatter(m.group(1))
            name = meta.get("name", path.parent.name)
            triggers = meta.get("triggers", []) if isinstance(meta.get("triggers"), list) else []
            task_types = meta.get("task_types", []) if isinstance(meta.get("task_types"), list) else []
            body = m.group(2).strip()
        self._skills[name] = Skill(name, triggers, task_types, body)

    def match(self, task: str) -> list[Skill]:
        from synapse.modules.context.classifier import classify_task
        ttype = classify_task(task).value
        text = (task or "").lower()
        out: list[Skill] = []
        for s in self._skills.values():
            if ttype in s.task_types or any(trig in text for trig in s.triggers):
                out.append(s)
        return out

    def load(self, name: str) -> str | None:
        s = self._skills.get(name)
        return s.body if s else None

    def list(self) -> list[str]:
        return list(self._skills)

    def render(self, task: str) -> str:
        skills = self.match(task)
        if not skills:
            return ""
        parts = ["## Skills (auto-loaded for this task)"]
        for s in skills:
            parts.append(f"### skill: {s.name}\n{s.body}")
        return "\n\n".join(parts)


def _parse_frontmatter(fm: str) -> dict:
    meta: dict = {}
    for line in fm.splitlines():
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        k, v = k.strip(), v.strip()
        if not k:
            continue
        meta[k] = [x.strip() for x in v.split(",") if x.strip()] if "," in v else v
    return meta


# Process-wide shared loader so the prompt injection and the load_skill tool
# agree on the same skills. Scans ./skills relative to the current directory.
_DEFAULT_LOADER: "SkillLoader | None" = None


def get_default_skill_loader() -> "SkillLoader":
    global _DEFAULT_LOADER
    if _DEFAULT_LOADER is None:
        _DEFAULT_LOADER = SkillLoader(Path("skills"))
    return _DEFAULT_LOADER

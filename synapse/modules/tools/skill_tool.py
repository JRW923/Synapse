"""load_skill 工具 (s07) — LLM 可显式拉取某个 skill 的正文。"""

from synapse.protocols.tool import Tool, ToolSchema, ToolResult, ToolCallMetadata, RiskLevel, ToolCategory
from synapse.modules.skill import SkillLoader


class SkillTool:
    name = "load_skill"
    description = (
        "Load the body of a named skill (specialized knowledge) into context. "
        "Use when the task relates to a known skill; auto-loaded skills are "
        "already in the system prompt, but this fetches one by name on demand."
    )
    parameters = ToolSchema(
        name="load_skill",
        description="Fetch a skill's content by name.",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Skill name to load"},
            },
            "required": ["name"],
        },
    )
    requires_sandbox = False
    risk_level = RiskLevel.META
    category = ToolCategory.CODE_UNDERSTANDING

    def __init__(self, skill_loader: SkillLoader):
        self.skill_loader = skill_loader

    async def execute(self, params: dict, sandbox=None, timeout: int | None = None) -> ToolResult:
        name = params.get("name", "")
        meta = ToolCallMetadata(tool_name="load_skill")
        body = self.skill_loader.load(name)
        if body is None:
            return ToolResult(
                success=False, output="",
                error=f"Unknown skill: {name}. Available: {', '.join(self.skill_loader.list()) or '(none)'}",
                metadata=meta,
            )
        return ToolResult(success=True, output=body, metadata=meta)

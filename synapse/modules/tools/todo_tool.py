"""TodoWrite / TodoRead 工具 (s05)."""

from synapse.protocols.tool import Tool, ToolSchema, ToolResult, ToolCallMetadata, RiskLevel, ToolCategory
from synapse.modules.todo import TodoStore


class TodoWriteTool:
    name = "todo_write"
    description = (
        "Track a task list for the current work. Call with the FULL list of "
        "todos each time (any status change replaces the whole list). "
        "status is one of: pending, in_progress, completed."
    )
    parameters = ToolSchema(
        name="todo_write",
        description="Replace the current todo list with the provided snapshot.",
        parameters={
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string"},
                            "status": {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                            "active_form": {"type": "string"},
                        },
                        "required": ["content", "status"],
                    },
                },
            },
            "required": ["todos"],
        },
    )
    requires_sandbox = False
    risk_level = RiskLevel.META
    category = ToolCategory.CODE_UNDERSTANDING

    def __init__(self, store: TodoStore):
        self.store = store

    async def execute(self, params: dict, sandbox=None, timeout: int | None = None) -> ToolResult:
        meta = ToolCallMetadata(tool_name="todo_write")
        todos = params.get("todos", [])
        if not isinstance(todos, list):
            return ToolResult(success=False, output="", error="'todos' must be a list", metadata=meta)
        self.store.set_todos(todos)
        return ToolResult(
            success=True,
            output=f"Updated todo list: {len(todos)} item(s).",
            metadata=meta,
        )


class TodoReadTool:
    name = "todo_read"
    description = "Read the current todo list."
    parameters = ToolSchema(
        name="todo_read",
        description="Return the current todo list.",
        parameters={"type": "object", "properties": {}},
    )
    requires_sandbox = False
    risk_level = RiskLevel.META
    category = ToolCategory.CODE_UNDERSTANDING

    def __init__(self, store: TodoStore):
        self.store = store

    async def execute(self, params: dict, sandbox=None, timeout: int | None = None) -> ToolResult:
        meta = ToolCallMetadata(tool_name="todo_read")
        todos = self.store.list()
        if not todos:
            return ToolResult(success=True, output="(no todos)", metadata=meta)
        lines = [f"[{t['status']}] {t['content']}" for t in todos]
        return ToolResult(success=True, output="\n".join(lines), metadata=meta)

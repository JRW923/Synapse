"""Todo 待办存储 (s05).

agent 通过 TodoWrite 工具维护可见待办；REPL 用 /todos 查看。进程内共享单例
（与 ShellTool/SkillTool 同模式），同一 run 内工具与 REPL 看到同一份。

ponytail: 内存存储，无跨进程持久化；长任务多 session 隔离是后续工作。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Todo:
    content: str
    status: str = "pending"          # pending | in_progress | completed
    active_form: str = ""


class TodoStore:
    def __init__(self):
        self._todos: list[Todo] = []

    def set_todos(self, items: list[dict]) -> None:
        """Replace the list (Claude Code TodoWrite semantics: full snapshot)."""
        self._todos = [
            Todo(
                content=str(it.get("content", "")),
                status=str(it.get("status", "pending")),
                active_form=str(it.get("active_form", "")),
            )
            for it in items
        ]

    def list(self) -> list[dict]:
        return [
            {"content": t.content, "status": t.status, "active_form": t.active_form}
            for t in self._todos
        ]

    def clear(self) -> None:
        self._todos.clear()

    def __len__(self) -> int:
        return len(self._todos)


_DEFAULT_STORE: "TodoStore | None" = None


def get_default_todo_store() -> "TodoStore":
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = TodoStore()
    return _DEFAULT_STORE

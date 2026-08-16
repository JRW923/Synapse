"""Todo 待办存储 (s05).

agent 通过 TodoWrite 工具维护可见待办；REPL 用 /todos 查看。进程内共享单例
（与 ShellTool/SkillTool 同模式），同一 run 内工具与 REPL 看到同一份。

绑定 session 后清单随 Session 一起持久化到 ``~/.synapse/todos/<id>.json``
（原子写），``--resume`` / ``/resume`` 恢复同一份清单，长任务跨进程续接。

ponytail: 清单按 session 全量快照，无并发写合并；单用户单进程 REPL 下足够，
升级路径是并入 Session.save 的单一 JSON。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TODO_DIR = Path.home() / ".synapse" / "todos"


@dataclass
class Todo:
    content: str
    status: str = "pending"          # pending | in_progress | completed
    active_form: str = ""


class TodoStore:
    def __init__(self, directory: Path = DEFAULT_TODO_DIR):
        self._todos: list[Todo] = []
        self._directory = Path(directory)
        self._session_id: str | None = None

    def bind_session(self, session_id: str) -> None:
        """Bind the store to a session and load its persisted list (if any)."""
        self._session_id = session_id
        self._todos = self._load(session_id)

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
        self._persist()

    def list(self) -> list[dict]:
        return [
            {"content": t.content, "status": t.status, "active_form": t.active_form}
            for t in self._todos
        ]

    def clear(self) -> None:
        self._todos.clear()
        self._persist()

    def __len__(self) -> int:
        return len(self._todos)

    # -- Persistence -------------------------------------------------------

    def _path(self, session_id: str) -> Path:
        return self._directory / f"{session_id}.json"

    def _persist(self) -> None:
        if self._session_id is None:
            return
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            path = self._path(self._session_id)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self.list(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError:
            # Persistence is best-effort — an unwritable home must not break
            # the in-session todo flow.
            pass

    def _load(self, session_id: str) -> list[Todo]:
        path = self._path(session_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return [
            Todo(content=str(it.get("content", "")),
                 status=str(it.get("status", "pending")),
                 active_form=str(it.get("active_form", "")))
            for it in data if isinstance(it, dict)
        ]


_DEFAULT_STORE: "TodoStore | None" = None


def get_default_todo_store() -> "TodoStore":
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = TodoStore()
    return _DEFAULT_STORE

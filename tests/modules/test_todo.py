"""Tests for s05 TodoWrite.

验收：agent 写 todo 后状态持久化，且 /todos 视图能看到。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from synapse.modules.todo import TodoStore, get_default_todo_store
from synapse.modules.tools.todo_tool import TodoWriteTool, TodoReadTool
from synapse.adapters.cli import _show_todos


async def test_todo_write_persists_and_reads():
    store = TodoStore()
    w = TodoWriteTool(store)
    r = TodoReadTool(store)

    res = await w.execute({"todos": [
        {"content": "写测试", "status": "in_progress"},
        {"content": "提交", "status": "pending"},
    ]})
    assert res.success
    assert len(store) == 2

    out = await r.execute({})
    assert "写测试" in out.output
    assert "in_progress" in out.output


async def test_todo_write_replaces_whole_list():
    store = TodoStore()
    w = TodoWriteTool(store)
    await w.execute({"todos": [{"content": "a", "status": "pending"}]})
    await w.execute({"todos": [{"content": "b", "status": "completed"}]})
    assert len(store) == 1
    assert store.list()[0]["content"] == "b"


def test_show_todos_view():
    store = get_default_todo_store()
    store.set_todos([{"content": "做一件事", "status": "completed"}])
    try:
        # 非 rich：走 print，不应抛错
        _show_todos(console=MagicMock(), use_rich=False)
        # rich：走 console.print
        c = MagicMock()
        _show_todos(console=c, use_rich=True)
        assert c.print.called
    finally:
        store.clear()


def test_event_sink_fires_on_change():
    # 运行期侧栏实时刷新靠这条：set_todos/clear 必须通知已注册的 sink。
    store = TodoStore()
    store.bind_session("s1")
    seen = []
    store.set_event_sink(lambda sid, todos: seen.append((sid, todos)))
    store.set_todos([{"content": "x", "status": "pending"}])
    assert seen == [("s1", [{"content": "x", "status": "pending", "active_form": ""}])]
    store.clear()
    assert len(seen) == 2
    # 摘掉 sink 后不再通知
    store.set_event_sink(None)
    store.set_todos([{"content": "y"}])
    assert len(seen) == 2

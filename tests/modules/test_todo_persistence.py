"""TodoStore persistence — todos survive process restarts per session."""

from synapse.modules.todo import TodoStore


def test_todos_round_trip_across_store_instances(tmp_path):
    a = TodoStore(directory=tmp_path)
    a.bind_session("s1")
    a.set_todos([
        {"content": "fix parser", "status": "in_progress"},
        {"content": "run tests", "status": "pending"},
    ])
    # New process, new store instance, same session id.
    b = TodoStore(directory=tmp_path)
    b.bind_session("s1")
    assert [t["content"] for t in b.list()] == ["fix parser", "run tests"]
    assert b.list()[0]["status"] == "in_progress"


def test_bind_new_session_starts_empty(tmp_path):
    a = TodoStore(directory=tmp_path)
    a.bind_session("s1")
    a.set_todos([{"content": "x", "status": "pending"}])
    a.bind_session("s2")
    assert a.list() == []


def test_clear_persists(tmp_path):
    a = TodoStore(directory=tmp_path)
    a.bind_session("s1")
    a.set_todos([{"content": "x", "status": "pending"}])
    a.clear()
    b = TodoStore(directory=tmp_path)
    b.bind_session("s1")
    assert b.list() == []


def test_unbound_store_is_in_memory_only(tmp_path):
    store = TodoStore(directory=tmp_path)
    store.set_todos([{"content": "ghost", "status": "pending"}])
    assert not list(tmp_path.iterdir())  # nothing written without a session
    assert store.list()[0]["content"] == "ghost"


def test_corrupt_file_degrades_to_empty(tmp_path):
    (tmp_path / "bad.json").write_text("{not json", encoding="utf-8")
    store = TodoStore(directory=tmp_path)
    store.bind_session("bad")
    assert store.list() == []

"""Tests for the FastAPI HTTP server adapter."""

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from synapse.protocols.llm import LLMResponse, Message
from synapse.protocols.planner import ResultStatus, AgentResult, ExecutionMetrics
from synapse.core.events import EventBus
from synapse.adapters.server import create_app


def test_remote_experiment_config_rejects_host_escape_hatches():
    from synapse.adapters.server import _validate_remote_experiment_config

    with pytest.raises(ValueError, match="base_url"):
        _validate_remote_experiment_config({"base_url": "https://attacker.invalid"})
    with pytest.raises(ValueError, match="hooks"):
        _validate_remote_experiment_config({"runtime": {"hooks": {}}})


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_synapse():
    """Return a mock Synapse instance whose run() returns a canned result."""
    mock = AsyncMock()
    mock.run.return_value = AgentResult(
        status=ResultStatus.SUCCESS,
        output="Task completed successfully.",
        metrics=ExecutionMetrics(
            tokens_input=10,
            tokens_output=5,
            tool_call_count=2,
            tool_success_count=2,
            duration_ms=1500,
        ),
    )
    # L.4 — the /run handler surfaces this via RunResponse.run_score.
    # get_run_score is a *sync* method, so it must return a plain dict (not a
    # coroutine — AsyncMock would otherwise make it awaitable).
    mock.get_run_score = MagicMock(return_value={
        "task": "Say hello",
        "status": "success",
        "safety": {},
        "process": {},
        "quality": {},
        "efficiency": {},
        "process_hint": "复用率偏低，下次先检索现有实现。",
    })
    # /sessions/{id}/context-report returns this via get_citation_report().
    mock.get_citation_report = MagicMock(return_value={
        "blocks": [
            {"zone": "core", "source": "main.py", "priority": 1, "tokens": 10,
             "usage": 2, "cited": 1, "citation_rate": 0.5},
        ],
    })
    return mock


@pytest.fixture
def client(mock_synapse, tmp_path, monkeypatch):
    """Return a FastAPI TestClient wired to the app with a mocked Synapse."""
    import synapse.adapters.server as server
    # /run persists sessions to disk — keep the tests out of the real home dir.
    monkeypatch.setattr(server, "DEFAULT_SESSION_DIR", tmp_path)

    app = create_app(synapse_instance=mock_synapse)
    from fastapi.testclient import TestClient
    return TestClient(app)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fail", "expected_status"),
    [(False, "completed"), (True, "failed")],
)
async def test_experiment_http_comparability_and_workspace_cleanup(
    monkeypatch, fail, expected_status,
):
    import synapse.adapters.server as server

    run_workspaces: list[Path] = []

    class FakeSynapse:
        def __init__(self, **config):
            self.config = config

        async def run(self, _task):
            workspace = Path(self.config["workspace_root"])
            assert workspace.is_dir()
            assert not any(workspace.iterdir())
            run_workspaces.append(workspace)
            if fail:
                raise RuntimeError("fixture failure")
            return AgentResult(
                status=ResultStatus.SUCCESS,
                output="done",
                metrics=ExecutionMetrics(
                    duration_ms=10,
                    tokens_input=4,
                    tokens_output=2,
                    tool_call_count=1,
                    tool_success_count=1,
                ),
            )

        def get_run_score(self):
            return {"model_id": "fixture-model", "safety": {}}

        def get_effective_config(self):
            return {
                "variant": self.config["variant"],
                "provider": {"max_tokens": 100, "timeout_seconds": 30},
                "planning": {
                    "max_iterations": 5,
                    "max_tokens_per_task": 1000,
                    "total_timeout_seconds": 60,
                },
                "context": {"total_tokens": 500},
                "security": {
                    "sandbox_enabled": True,
                    "sandbox_mode": "enforce",
                    "sandbox_backend": "docker",
                    "sandbox_network": False,
                    "auth_confirmation": True,
                    "allowed_paths": [],
                    "allow_external": False,
                },
                "tools": {"enabled": ["read"], "allowlist_commands": []},
                "runtime": {"enable_external_tools": False, "mcp_servers": []},
            }

    monkeypatch.setattr(server, "Synapse", FakeSynapse)
    app = server.create_app(synapse_instance=MagicMock())
    start = next(
        route.endpoint for route in app.routes
        if getattr(route, "path", None) == "/eval/experiment"
        and "POST" in route.methods
    )
    status = next(
        route.endpoint for route in app.routes
        if getattr(route, "path", None) == "/eval/experiment/{experiment_id}"
        and "GET" in route.methods
    )

    created = await start(server.ExperimentRequest(
        name="http-contract",
        agent_config_a={"variant": "A"},
        agent_config_b={"variant": "B"},
        benchmark_task="diagnostic task",
        runs_per_config=2,
        allowed_config_diff_paths=["variant"],
    ))
    for _ in range(10):
        await asyncio.sleep(0)
        response = await status(created["experiment_id"])
        if response.status != "running":
            break

    assert response.status == expected_status
    assert run_workspaces
    assert all(not workspace.exists() for workspace in run_workspaces)
    if fail:
        assert response.result is None
        assert response.error == "RuntimeError: fixture failure"
    else:
        assert response.error is None
        assert response.result["comparability_eligible"] is True
        assert response.result["comparability_issues"] == []
        assert response.result["comparability_evidence"]["workspace_instances"] == 4
        assert len(set(run_workspaces)) == 4


# ---------------------------------------------------------------------------
# Test 1: Health check
# ---------------------------------------------------------------------------


def test_health_check(client):
    """GET /health returns {"status": "ok"}."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"


def test_configured_reflects_models_json(tmp_path, monkeypatch):
    """GET /config must report configured when ~/.synapse/models.json exists,
    not only after a browser runtime key is set.

    Regression: previously ``configured`` only checked the in-memory
    ``runtime_key``, so the web UI re-prompted for config on every load even
    when the CLI-written models.json was already present on disk.
    """
    import synapse.adapters.server as server
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server, "DEFAULT_SESSION_DIR", tmp_path)
    models_file = tmp_path / "models.json"
    monkeypatch.setattr(server, "models_config_path", lambda: models_file)

    client = TestClient(server.create_app())

    # No models.json and no browser key -> not configured.
    assert not models_file.exists()
    assert client.get("/config").json()["configured"] is False

    # A present models.json (the file the CLI writes) -> configured, even
    # with no browser-supplied key.
    models_file.write_text("{}")
    assert client.get("/config").json()["configured"] is True

    # A browser runtime key also makes it configured (original behaviour).
    client.post("/config", json={
        "provider": "openai", "model": "gpt-4o-mini", "api_key": "x",
    })
    assert client.get("/config").json()["configured"] is True



# ---------------------------------------------------------------------------
# Test 2: Run task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_task(client, mock_synapse):
    """POST /run executes a task and returns AgentResult fields."""
    response = client.post("/run", json={"task": "Say hello"})
    assert response.status_code == 200
    data = response.json()

    assert data["status"] == "success"
    assert "Task completed" in data["output"]
    assert data["metrics"]["tokens_input"] == 10
    assert data["metrics"]["tokens_output"] == 5
    assert data["metrics"]["tool_call_count"] == 2
    assert data["metrics"]["duration_ms"] == 1500

    # Verify the Synapse facade was called with the task string
    mock_synapse.run.assert_called_once()
    call_args = mock_synapse.run.call_args
    assert call_args[0][0] == "Say hello"


@pytest.mark.asyncio
async def test_run_task_includes_run_score(client, mock_synapse):
    """L.4 — POST /run returns the runtime score including the process hint."""
    response = client.post("/run", json={"task": "Say hello"})
    assert response.status_code == 200
    data = response.json()
    assert "run_score" in data
    assert data["run_score"]["process_hint"] == "复用率偏低，下次先检索现有实现。"


# ---------------------------------------------------------------------------
# Test 2b: SSE streaming run (L.1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_task_stream(client, mock_synapse):
    """POST /run/stream yields SSE events and a final 'done' with the result."""
    # The mock Synapse's _container is an AsyncMock, whose resolve() returns a
    # coroutine when called synchronously. The real Container.resolve is sync,
    # so substitute a sync MagicMock returning a real EventBus.
    mock_synapse._container.resolve = MagicMock(return_value=EventBus())
    response = client.post("/run/stream", json={"task": "Say hello"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = []
    for line in response.text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: "):]))

    dones = [e for e in events if e["type"] == "done"]
    assert dones, "expected a 'done' event"
    assert dones[0]["result"]["status"] == "success"
    assert "Task completed" in dones[0]["result"]["output"]
    assert dones[0]["result"]["metrics"]["tokens_input"] == 10
    mock_synapse.run.assert_called_once()


@pytest.mark.asyncio
async def test_run_task_stream_includes_swarm_events():
    """POST /run/stream also forwards swarm lifecycle events (L.2)."""
    from synapse.adapters.server import create_app
    from synapse.protocols.events import WorkerSpawned

    bus = EventBus()
    mock = AsyncMock()
    mock._container.resolve = MagicMock(return_value=bus)
    mock.get_run_score = MagicMock(return_value={
        "task": "swarm it", "status": "success",
        "safety": {}, "process": {}, "quality": {}, "efficiency": {},
        "process_hint": None,
    })
    canned = AgentResult(status=ResultStatus.SUCCESS, output="ok", metrics=ExecutionMetrics())

    async def _run(task, session=None, confirm_callback=None):
        sid = session.id if session is not None else "s"
        await bus.emit(WorkerSpawned(session_id=sid, agent_id="w1", role="coder", task="x"))
        return canned

    mock.run = _run
    app = create_app(synapse_instance=mock)
    from fastapi.testclient import TestClient
    client = TestClient(app)

    response = client.post("/run/stream", json={"task": "swarm it"})
    assert response.status_code == 200
    events = [json.loads(l[len("data: "):]) for l in response.text.splitlines() if l.startswith("data: ")]
    types = [e["event"]["event_type"] for e in events if e["type"] == "event"]
    assert "worker_spawned" in types
    spawned = next(e["event"] for e in events
                   if e["type"] == "event" and e["event"]["event_type"] == "worker_spawned")
    assert spawned["role"] == "coder"
    assert spawned["agent_id"] == "w1"


# ---------------------------------------------------------------------------
# Test 3: Session history
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_stream_includes_background_result():
    """POST /run/stream forwards background_result so finished bg shells are visible."""
    from synapse.adapters.server import create_app
    from synapse.protocols.events import BackgroundResult

    bus = EventBus()
    mock = AsyncMock()
    mock._container.resolve = MagicMock(return_value=bus)
    mock.get_run_score = MagicMock(return_value={})
    canned = AgentResult(status=ResultStatus.SUCCESS, output="ok", metrics=ExecutionMetrics())

    async def _run(task, session=None, confirm_callback=None):
        await bus.emit(BackgroundResult(
            session_id=session.id, task_id="task-12345678", success=True, stdout="done"))
        return canned

    mock.run = _run
    app = create_app(synapse_instance=mock)
    from fastapi.testclient import TestClient
    client = TestClient(app)

    response = client.post("/run/stream", json={"task": "bg it"})
    events = [json.loads(l[len("data: "):]) for l in response.text.splitlines() if l.startswith("data: ")]
    types = [e["event"]["event_type"] for e in events if e["type"] == "event"]
    assert "background_result" in types


@pytest.mark.asyncio
async def test_run_continues_saved_session(client, mock_synapse, tmp_path):
    """session_id continues a prior conversation; unknown ids 404."""
    from synapse.adapters import server

    first = client.post("/run", json={"task": "Hello"})
    sid = first.json()["session_id"]
    client.post("/run", json={"task": "Again", "session_id": sid})
    # both runs reused the same session object
    sessions_used = [call.kwargs.get("session") for call in mock_synapse.run.call_args_list]
    assert sessions_used[0] is sessions_used[1]

    # Fresh app: empty in-memory dict forces the on-disk lookup path.
    (tmp_path / f"{sid}.json").unlink(missing_ok=True)
    from fastapi.testclient import TestClient
    cold_client = TestClient(server.create_app(synapse_instance=mock_synapse))
    response = cold_client.post("/run", json={"task": "x", "session_id": sid})
    assert response.status_code == 404


def test_webui_served_at_root(client):
    """GET / returns the built-in single-file web UI."""
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    for marker in (
        "run/stream",
        "tool_call_started",
        "tool_call_completed",
        "background_result",
        "worker_spawned",
        "swarm_verified",
        "resetLiveState",
        "run-summary",
        'aria-label="关闭设置"',
        '/sessions/" + id + "/history',
        "renderCompletedRun",
        "turn-details",
        "toggleTheme",
        "synapse-theme",
        "score-head",
    ):
        assert marker in response.text
    assert "a.id === sessionId ? -1" not in response.text
    assert "const marker = role === \"user\"" not in response.text
    assert "who.textContent = marker" not in response.text


@pytest.mark.asyncio
async def test_run_stream_confirm_round_trip():
    """A confirmation-required call prompts over SSE; POST /confirm answers it."""
    from synapse.adapters.server import create_app
    from synapse.protocols.sandbox import AuthRequest

    bus = EventBus()
    mock = AsyncMock()
    mock._container.resolve = MagicMock(return_value=bus)
    mock.get_run_score = MagicMock(return_value={})

    async def _run(task, session=None, confirm_callback=None):
        assert confirm_callback is not None
        approved = await confirm_callback(AuthRequest(
            tool_name="shell", tool_params={"command": "rm -rf /tmp/x"},
            risk_level="high", session_id=session.id))
        return AgentResult(status=ResultStatus.SUCCESS,
                           output="approved" if approved else "denied",
                           metrics=ExecutionMetrics())

    mock.run = _run
    app = create_app(synapse_instance=mock)
    from fastapi.testclient import TestClient
    client = TestClient(app)

    # The SSE body only unblocks when the confirm waiter resolves, so answer
    # from a side thread: poll the app's confirm_waiters dict until the
    # prompt appears, then approve it.
    confirm_waiters = app.state.confirm_waiters

    def _approve_when_pending():
        for _ in range(200):
            if confirm_waiters:
                rid = next(iter(confirm_waiters))
                client.post(f"/confirm/{rid}", json={"approve": True})
                return
            time.sleep(0.05)

    holder = {}
    def _stream():
        holder["res"] = client.post("/run/stream", json={"task": "confirm it"})
    t_stream = threading.Thread(target=_stream)
    t_answer = threading.Thread(target=_approve_when_pending)
    t_stream.start(); t_answer.start()
    t_stream.join(timeout=30); t_answer.join(timeout=10)

    res = holder["res"]
    assert res.status_code == 200
    events = [json.loads(l[len("data: "):]) for l in res.text.splitlines() if l.startswith("data: ")]
    confirms = [e for e in events if e["type"] == "confirm"]
    assert confirms and confirms[0]["request"]["tool_name"] == "shell"
    dones = [e for e in events if e["type"] == "done"]
    assert dones[0]["result"]["output"] == "approved"


def test_per_session_synapse_instances(monkeypatch):
    """Without an injected instance, each session gets its own Synapse."""
    import synapse.adapters.server as server

    created = []

    class FakeSynapse:
        def __init__(self):
            created.append(self)
            self._container = MagicMock()

        async def run(self, task, session=None, confirm_callback=None):
            return AgentResult(status=ResultStatus.SUCCESS, output="ok",
                               metrics=ExecutionMetrics())

        def get_run_score(self):
            return {}

    monkeypatch.setattr(server, "Synapse", FakeSynapse)
    app = server.create_app(synapse_instance=None)
    from fastapi.testclient import TestClient
    client = TestClient(app)

    # Two distinct sessions -> two instances; same session -> cached one.
    sid1 = client.post("/run", json={"task": "a"}).json()["session_id"]
    client.post("/run", json={"task": "b", "session_id": sid1})
    sid2 = client.post("/run", json={"task": "c"}).json()["session_id"]
    assert len(created) == 2


def test_session_history_returns_run_snapshot(tmp_path, monkeypatch):
    """历史接口返回结构化 run_history，供 Web 按当前会话布局还原。"""
    import synapse.adapters.server as server
    from synapse.core.session import Session
    from synapse.protocols.llm import Message
    from fastapi.testclient import TestClient

    monkeypatch.setattr(server, "DEFAULT_SESSION_DIR", tmp_path)
    session = Session(session_id="11111111-1111-1111-1111-111111111111")
    session.add_message(Message(role="user", content="检查项目"))
    session.add_message(Message(role="assistant", content="中间过程"))
    session.add_message(Message(role="assistant", content="完成"))
    session.metadata["run_history"] = [{
        "task": "检查项目",
        "output": "完成",
        "status": "success",
        "metrics": {"duration_ms": 12, "tokens_input": 1, "tokens_output": 2,
                    "tool_call_count": 1, "tool_success_count": 1},
        "tools": [{"name": "read", "success": True, "args": "path=a.py", "meta": "3ms"}],
        "plan": {"steps": [{"step_id": 1, "description": "读文件"}], "reasoning": ""},
    }]
    session.save(tmp_path)
    app = create_app()
    data = TestClient(app).get(f"/sessions/{session.id}/history").json()
    assert data["runs"][0]["output"] == "完成"
    assert data["runs"][0]["tools"][0]["name"] == "read"
    assert data["runs"][0]["plan"]["steps"][0]["description"] == "读文件"


def test_session_history(client):
    """After a run, GET /sessions/{id}/messages returns message history."""
    # First, run a task to create a session
    client.post("/run", json={"task": "Hello"})

    # Get the list of sessions (the run was the first one, so id should be known)
    # The server stores sessions keyed by session id.
    # We can get session info via GET /sessions/{id}
    # Since the session id is generated internally, we need to discover it.
    # We'll verify that a session was created by checking the messages endpoint
    # with a plausible session id format.

    # The /run endpoint returns a session_id in the response
    run_response = client.post("/run", json={"task": "Hello again"})
    assert run_response.status_code == 200
    run_data = run_response.json()
    session_id = run_data.get("session_id")
    assert session_id is not None

    # Retrieve session info
    info_response = client.get(f"/sessions/{session_id}")
    assert info_response.status_code == 200
    info = info_response.json()
    assert info["id"] == session_id
    assert info["message_count"] >= 0

    # Retrieve message history
    msgs_response = client.get(f"/sessions/{session_id}/messages")
    assert msgs_response.status_code == 200
    messages = msgs_response.json()
    assert isinstance(messages, list)


# ---------------------------------------------------------------------------
# WebUI enhancement endpoints (session list / status / todos / context-report)
# ---------------------------------------------------------------------------


def test_list_sessions_empty(client, monkeypatch):
    import synapse.core.session as session_mod

    # list_sessions() binds DEFAULT_SESSION_DIR as a default arg at import
    # time, so patch the method itself for a deterministic empty list.
    monkeypatch.setattr(
        session_mod.Session, "list_sessions",
        classmethod(lambda cls: []),
    )

    r = client.get("/sessions")
    assert r.status_code == 200
    assert r.json() == []


def test_status_endpoint(client, monkeypatch):
    import synapse.adapters.server as server

    fake = MagicMock()
    fake.provider.provider = "openai"
    fake.provider.model = "gpt-4o-mini"
    fake.provider.api_key = "x"
    fake.planning.mode = "react"
    fake.planning.max_tokens_per_task = 200000
    fake.tools.enabled = ["read", "write"]
    monkeypatch.setattr(server, "load_config", lambda *a, **k: (fake, None))

    r = client.get("/status")
    assert r.status_code == 200
    data = r.json()
    assert data["provider"] == "openai"
    assert data["model"] == "gpt-4o-mini"
    assert data["mode"] == "react"
    assert data["budget"] == 200000
    assert data["tools_count"] == 2
    assert "version" in data
    assert data["compact_count"] == 0


def test_compact_endpoint(client, tmp_path, monkeypatch):
    import synapse.adapters.server as server
    from synapse.config.schema import PlanningConfig
    from synapse.core.session import Session
    from synapse.protocols.llm import Message

    fake = MagicMock()
    fake.planning = PlanningConfig()
    monkeypatch.setattr(server, "load_config", lambda *a, **k: (fake, None))

    assert client.post("/sessions/missing/compact").status_code == 404

    session = Session()
    session.messages = [
        Message(role="system", content="s"),
        Message(role="user", content="t"),
    ]
    for i in range(8):
        session.messages.append(Message(role="tool", content="x" * 400, tool_call_id=f"t{i}"))
    session.save(tmp_path)
    response = client.post(f"/sessions/{session.id}/compact")
    assert response.status_code == 200
    body = response.json()
    assert body["changed"] is True
    assert body["level"] in {"l1", "l2"}
    assert "summary" in body


def test_todos_endpoint(client, monkeypatch, tmp_path):
    import synapse.adapters.server as server

    # Keep the read-only todo lookup out of the real home dir.
    monkeypatch.setattr(server, "DEFAULT_TODO_DIR", tmp_path)

    r = client.get("/todos?session_id=does-not-exist")
    assert r.status_code == 200
    assert r.json() == []


def test_context_report_endpoint(client, tmp_path):
    # 不存在的会话返回空报告
    r = client.get("/sessions/missing/context-report")
    assert r.status_code == 200
    assert r.json()["blocks"] == []

    # 报告在运行后持久化到 session.metadata,端点从磁盘读取
    (tmp_path / "abc.json").write_text(
        __import__("json").dumps({
            "id": "abc",
            "metadata": {"citation_report": {"blocks": [{
                "zone": "core", "id": "x", "source": "file", "priority": 1,
                "tokens": 10, "usage": 1, "cited": 0, "citation_rate": "0/0",
            }]}},
            "messages": [],
        }),
        encoding="utf-8",
    )
    r = client.get("/sessions/abc/context-report")
    assert r.status_code == 200
    blocks = r.json()["blocks"]
    assert isinstance(blocks, list) and blocks
    assert blocks[0]["zone"] == "core"


def test_confirm_mode_endpoint(client):
    # 默认 ask
    assert client.get("/confirm-mode").json()["mode"] == "ask"
    # 切换到 auto
    assert client.post("/confirm-mode", json={"mode": "auto"}).json()["mode"] == "auto"
    assert client.get("/confirm-mode").json()["mode"] == "auto"
    # 非法 mode 被拒
    assert client.post("/confirm-mode", json={"mode": "nope"}).status_code == 422
    # 切回 ask 不影响其他状态
    assert client.post("/confirm-mode", json={"mode": "ask"}).status_code == 200


def test_session_config_switch_stores_mode_and_validates():
    import synapse.adapters.server as server
    from fastapi.testclient import TestClient

    app = server.create_app()
    c = TestClient(app)

    # 本会话运行时仅覆盖规划模式;模型由 web 端全局配置决定。
    r = c.post("/sessions/s1/config", json={"mode": "swarm"})
    assert r.status_code == 200
    assert app.state.session_runtime["s1"]["mode"] == "swarm"

    # Unknown planning mode is rejected.
    bad = c.post("/sessions/s2/config", json={"mode": "nope"})
    assert bad.status_code == 422


def test_connector_run_receives_session_planning_mode(monkeypatch):
    import synapse.adapters.server as server
    from fastapi.testclient import TestClient

    app = server.create_app()
    app.state.session_runtime["11111111-1111-1111-1111-111111111111"] = {"mode": "swarm"}
    captured = {}

    async def start_job(**kwargs):
        captured.update(kwargs)
        raise server.ConnectorOfflineError("offline")

    monkeypatch.setattr(app.state.connector_broker, "start_job", start_job)
    response = TestClient(app).post("/run/stream", json={
        "task": "x", "session_id": "11111111-1111-1111-1111-111111111111",
        "connector_id": "c", "connector_token": "t",
    })

    assert response.status_code == 409
    assert captured["planning_mode"] == "swarm"


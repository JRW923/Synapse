"""Tests for the FastAPI HTTP server adapter."""

import asyncio
import json
from pathlib import Path

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from synapse.protocols.llm import LLMResponse, Message
from synapse.protocols.planner import ResultStatus, AgentResult, ExecutionMetrics
from synapse.core.events import EventBus


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
    return mock


@pytest.fixture
def client(mock_synapse):
    """Return a FastAPI TestClient wired to the app with a mocked Synapse."""
    from synapse.adapters.server import create_app

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

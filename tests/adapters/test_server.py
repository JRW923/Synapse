"""Tests for the FastAPI HTTP server adapter."""

import json

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from synapse.protocols.llm import LLMResponse, Message
from synapse.protocols.planner import ResultStatus, AgentResult, ExecutionMetrics
from synapse.core.events import EventBus


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
    return mock


@pytest.fixture
def client(mock_synapse):
    """Return a FastAPI TestClient wired to the app with a mocked Synapse."""
    from synapse.adapters.server import create_app

    app = create_app(synapse_instance=mock_synapse)
    from fastapi.testclient import TestClient
    return TestClient(app)


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

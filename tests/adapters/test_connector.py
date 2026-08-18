"""Focused tests for the hosted Web UI local-workspace connector."""

import json
import time
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from synapse.adapters.connector import (
    ConnectorAuthenticationError,
    ConnectorBroker,
    ConnectorBusyError,
    ConnectorError,
    ConnectorOfflineError,
    ConnectorPairingError,
    ConnectorClient,
    LocalConnection,
    ConnectorTransportError,
    normalize_server_url,
    resolve_workspace,
)
from synapse.adapters.server import (
    ConnectorCompleteRequest,
    ConnectorEventsRequest,
    ConnectorRegisterRequest,
    RunRequest,
    create_app,
)


def _pair_connector(broker: ConnectorBroker) -> tuple[dict[str, str], dict[str, str]]:
    pairing = broker.create_pairing()
    registration = broker.register(
        workspace_name="Laptop repo",
        pair_code=pairing["pair_code"].lower().replace("-", " "),
    )
    return pairing, registration


def test_broker_pairs_connector_once_and_exposes_no_device_token_in_status():
    broker = ConnectorBroker()
    pairing = broker.create_pairing()

    assert broker.pairing_status(pairing["pair_id"], pairing["browser_token"]) == {
        "status": "waiting",
    }

    registration = broker.register(
        workspace_name="Laptop repo",
        pair_code=pairing["pair_code"],
    )

    assert registration["name"] == "Laptop repo"
    assert registration["connector_id"]
    assert registration["device_token"]
    assert broker.pairing_status(pairing["pair_id"], pairing["browser_token"]) == {
        "status": "connected",
        "connector_id": registration["connector_id"],
        "name": "Laptop repo",
    }
    assert "device_token" not in broker.pairing_status(
        pairing["pair_id"], pairing["browser_token"],
    )

    with pytest.raises(ConnectorPairingError):
        broker.register(workspace_name="Second repo", pair_code=pairing["pair_code"])


@pytest.mark.asyncio
async def test_broker_rejects_wrong_browser_and_device_tokens():
    broker = ConnectorBroker()
    pairing, registration = _pair_connector(broker)
    connector_id = registration["connector_id"]
    session_id = str(uuid.uuid4())

    with pytest.raises(ConnectorAuthenticationError):
        await broker.poll(connector_id, "wrong-device-token")
    with pytest.raises(ConnectorAuthenticationError):
        await broker.start_job(
            connector_id=connector_id,
            browser_token="wrong-browser-token",
            task="inspect files",
            session_id=session_id,
        )

    job = await broker.start_job(
        connector_id=connector_id,
        browser_token=pairing["browser_token"],
        task="inspect files",
        session_id=session_id,
    )
    with pytest.raises(ConnectorAuthenticationError):
        await broker.publish_events(
            connector_id=connector_id,
            device_token="wrong-device-token",
            job_id=job.id,
            events=[{"event": {"event_type": "agent_progress"}}],
        )


@pytest.mark.asyncio
async def test_broker_relays_command_events_and_completion():
    broker = ConnectorBroker()
    pairing, registration = _pair_connector(broker)
    connector_id = registration["connector_id"]
    session_id = str(uuid.uuid4())

    job = await broker.start_job(
        connector_id=connector_id,
        browser_token=pairing["browser_token"],
        task="add a focused test",
        session_id=session_id,
    )

    assert await broker.poll(connector_id, registration["device_token"]) == {
        "type": "run",
        "job_id": job.id,
        "session_id": session_id,
        "task": "add a focused test",
    }

    await broker.publish_events(
        connector_id=connector_id,
        device_token=registration["device_token"],
        job_id=job.id,
        events=[
            {"event": {"event_type": "agent_progress", "message": "working"}},
            {"not_an_event": True},
        ],
    )
    assert await job.events.get() == {
        "type": "event",
        "event": {"event_type": "agent_progress", "message": "working"},
    }

    await broker.complete_job(
        connector_id=connector_id,
        device_token=registration["device_token"],
        job_id=job.id,
        result={"status": "success", "output": "done"},
    )

    assert await job.completion == {
        "status": "success",
        "output": "done",
        "session_id": session_id,
        "artifacts": [],
        "metrics": {},
        "run_score": None,
    }
    assert await job.events.get() == {
        "type": "done",
        "result": await job.completion,
    }


@pytest.mark.asyncio
async def test_broker_relays_confirm_to_browser_and_resolves_via_command_channel():
    broker = ConnectorBroker()
    pairing, registration = _pair_connector(broker)
    connector_id = registration["connector_id"]
    session_id = str(uuid.uuid4())

    job = await broker.start_job(
        connector_id=connector_id,
        browser_token=pairing["browser_token"],
        task="delete a file",
        session_id=session_id,
    )
    # start_job 已把 run 命令放进队列,先取走(模拟 connector 开始执行)
    assert await broker.poll(connector_id, registration["device_token"]) == {
        "type": "run",
        "job_id": job.id,
        "session_id": session_id,
        "task": "delete a file",
    }
    # 确认事件经 publish_events 透传并登记 request_id 归属
    await broker.publish_events(
        connector_id=connector_id,
        device_token=registration["device_token"],
        job_id=job.id,
        events=[{
            "type": "confirm",
            "request_id": "r1",
            "request": {"tool_name": "write_file", "risk_level": "high"},
        }],
    )
    assert await job.events.get() == {
        "type": "confirm",
        "request_id": "r1",
        "request": {"tool_name": "write_file", "risk_level": "high"},
    }
    # 浏览器批准后,answer 经命令通道回传
    assert await broker.resolve_confirm("r1", True) is True
    assert await broker.poll(connector_id, registration["device_token"]) == {
        "type": "confirm_result",
        "request_id": "r1",
        "approve": True,
    }
    # 未知 request_id 不应报错
    assert await broker.resolve_confirm("nope", True) is False


@pytest.mark.asyncio
async def test_broker_stop_delivers_done_with_stopped_status():
    broker = ConnectorBroker()
    pairing, registration = _pair_connector(broker)
    connector_id = registration["connector_id"]
    session_id = str(uuid.uuid4())
    job = await broker.start_job(
        connector_id=connector_id,
        browser_token=pairing["browser_token"],
        task="long task",
        session_id=session_id,
    )
    assert broker.active_job_for(connector_id, pairing["browser_token"]) is job
    await broker.cancel_job(job, reason="已在网页端手动停止", stopped=True)
    assert job.completion.done()
    result = await job.completion
    assert result["status"] == "stopped"
    assert await job.events.get() == {"type": "done", "result": result}
    # 停止后 connector 已空闲
    assert broker.active_job_for(connector_id, pairing["browser_token"]) is None


@pytest.mark.asyncio
async def test_broker_rejects_offline_and_busy_connectors():
    broker = ConnectorBroker(offline_seconds=10)
    pairing, registration = _pair_connector(broker)
    connector_id = registration["connector_id"]
    record = broker._connectors[connector_id]
    record.last_seen = time.monotonic() - 11

    with pytest.raises(ConnectorOfflineError):
        await broker.start_job(
            connector_id=connector_id,
            browser_token=pairing["browser_token"],
            task="first task",
            session_id=str(uuid.uuid4()),
        )

    record.last_seen = time.monotonic()
    first = await broker.start_job(
        connector_id=connector_id,
        browser_token=pairing["browser_token"],
        task="first task",
        session_id=str(uuid.uuid4()),
    )
    with pytest.raises(ConnectorBusyError):
        await broker.start_job(
            connector_id=connector_id,
            browser_token=pairing["browser_token"],
            task="second task",
            session_id=str(uuid.uuid4()),
        )

    await broker.complete_job(
        connector_id=connector_id,
        device_token=registration["device_token"],
        job_id=first.id,
        result={"status": "success", "output": "done"},
    )


@pytest.mark.asyncio
async def test_broker_cancellation_releases_job_and_acks_late_completion():
    broker = ConnectorBroker()
    pairing, registration = _pair_connector(broker)
    connector_id = registration["connector_id"]
    job = await broker.start_job(
        connector_id=connector_id,
        browser_token=pairing["browser_token"],
        task="cancel this task",
        session_id=str(uuid.uuid4()),
    )
    assert (await broker.poll(connector_id, registration["device_token"]))["type"] == "run"

    await broker.cancel_job(job)

    assert (await broker.poll(connector_id, registration["device_token"])) == {
        "type": "cancel", "job_id": job.id,
    }
    assert (await broker.wait_for_completion(job))["error"] == "网页已断开，已取消本地任务"
    assert broker._connectors[connector_id].active_job_id is None

    # A response lost after the server terminalized the job can be retried.
    await broker.complete_job(
        connector_id=connector_id,
        device_token=registration["device_token"],
        job_id=job.id,
        result={"status": "success", "output": "late result"},
    )
    assert (await job.completion)["error"] == "网页已断开，已取消本地任务"


@pytest.mark.asyncio
async def test_broker_reclaims_stale_job_when_connector_registers_again():
    broker = ConnectorBroker(offline_seconds=10)
    pairing, registration = _pair_connector(broker)
    connector_id = registration["connector_id"]
    job = await broker.start_job(
        connector_id=connector_id,
        browser_token=pairing["browser_token"],
        task="interrupted task",
        session_id=str(uuid.uuid4()),
    )
    broker._connectors[connector_id].last_seen = time.monotonic() - 11

    broker.register(
        workspace_name="Laptop repo",
        connector_id=connector_id,
        device_token=registration["device_token"],
    )

    assert (await broker.wait_for_completion(job))["error"] == "本地 Connector 已断开"
    replacement = await broker.start_job(
        connector_id=connector_id,
        browser_token=pairing["browser_token"],
        task="new task",
        session_id=str(uuid.uuid4()),
    )
    assert replacement.id != job.id


@pytest.mark.asyncio
async def test_connector_retries_transient_completion_delivery(monkeypatch, tmp_path: Path):
    class _Http:
        def __init__(self):
            self.calls = 0

        async def post(self, path, payload, *, token=None):
            self.calls += 1
            if self.calls == 1:
                raise ConnectorTransportError("temporary network error")
            return {"ok": True}

    client = ConnectorClient(
        server="https://agent.example.com",
        workspace=tmp_path,
        connection=LocalConnection(
            server="https://agent.example.com",
            workspace=str(tmp_path),
            connector_id="connector",
            device_token="device-token",
            name="project",
        ),
    )
    http = _Http()
    client._http = http
    monkeypatch.setattr("synapse.adapters.connector.asyncio.sleep", AsyncMock())

    await client._post_completion(str(uuid.uuid4()), {"status": "success"})

    assert http.calls == 2


def test_connector_server_routes_reject_invalid_credentials_and_path_payload():
    app = create_app(synapse_instance=MagicMock())
    client = TestClient(app)
    pairing_response = client.post("/connectors/pair")
    assert pairing_response.status_code == 200
    pairing = pairing_response.json()

    assert client.get(f"/connectors/pair/{pairing['pair_id']}").status_code == 401
    assert client.get(
        f"/connectors/pair/{pairing['pair_id']}",
        headers={"Authorization": "Bearer wrong-token"},
    ).status_code == 403

    registration_response = client.post("/connectors/register", json={
        "workspace_name": "Laptop repo",
        "pair_code": pairing["pair_code"],
    })
    assert registration_response.status_code == 200
    registration = registration_response.json()
    connector_id = registration["connector_id"]

    assert client.post(f"/connectors/{connector_id}/commands", json={}).status_code == 401
    assert client.post(
        f"/connectors/{connector_id}/commands",
        json={},
        headers={"Authorization": "Bearer wrong-token"},
    ).status_code == 403
    assert client.post("/run/stream", json={
        "task": "bad request",
        "connector_id": connector_id,
    }).status_code == 422
    assert client.post("/run/stream", json={
        "task": "wrong browser credential",
        "connector_id": connector_id,
        "connector_token": "wrong-token",
    }).status_code == 403
    assert client.post("/run/stream", json={
        "task": "path injection",
        "workspace_root": "C:/not-allowed",
    }).status_code == 422
    assert client.post("/run/stream", json={
        "task": "approval bypass",
        "auto_approve": True,
    }).status_code == 422
    assert "auto_approve: true" not in client.get("/").text


def test_connector_only_server_rejects_server_workspace_execution():
    client = TestClient(create_app(connector_only=True))

    response = client.post("/run", json={"task": "do not run on server"})

    assert response.status_code == 403
    response = client.post("/eval/experiment", json={
        "name": "do not run on server",
        "agent_config_a": {},
        "agent_config_b": {},
    })
    assert response.status_code == 403


def _route_endpoint(app, path: str, method: str):
    return next(
        route.endpoint for route in app.routes
        if getattr(route, "path", None) == path and method in route.methods
    )


@pytest.mark.asyncio
async def test_connector_server_routes_relay_command_events_and_completion():
    app = create_app(synapse_instance=MagicMock())
    pair = _route_endpoint(app, "/connectors/pair", "POST")
    register = _route_endpoint(app, "/connectors/register", "POST")
    commands = _route_endpoint(app, "/connectors/{connector_id}/commands", "POST")
    publish_events = _route_endpoint(app, "/connectors/{connector_id}/events", "POST")
    complete = _route_endpoint(app, "/connectors/{connector_id}/complete", "POST")
    stream = _route_endpoint(app, "/run/stream", "POST")

    pairing = await pair()
    registration = await register(
        ConnectorRegisterRequest(
            workspace_name="Laptop repo",
            pair_code=pairing["pair_code"],
        ),
        authorization=None,
    )
    connector_id = registration["connector_id"]
    device_token = registration["device_token"]

    session_id = str(uuid.uuid4())
    response = await stream(RunRequest(
        task="run on the local machine",
        session_id=session_id,
        connector_id=connector_id,
        connector_token=pairing["browser_token"],
    ))
    command_response = await commands(
        connector_id,
        authorization=f"Bearer {device_token}",
    )
    command = command_response["command"]
    assert command["type"] == "run"
    assert command["task"] == "run on the local machine"
    assert command["session_id"] == session_id

    event_response = await publish_events(
        connector_id,
        ConnectorEventsRequest(
            job_id=command["job_id"],
            events=[{"event": {"event_type": "agent_progress", "message": "working"}}],
        ),
        authorization=f"Bearer {device_token}",
    )
    assert event_response == {"ok": True}
    complete_response = await complete(
        connector_id,
        ConnectorCompleteRequest(
            job_id=command["job_id"],
            result={"status": "success", "output": "local result"},
        ),
        authorization=f"Bearer {device_token}",
    )
    assert complete_response == {"ok": True}

    events = [
        json.loads(chunk.removeprefix("data: ").strip())
        async for chunk in response.body_iterator
    ]
    assert events == [
        {"type": "event", "event": {"event_type": "agent_progress", "message": "working"}},
        {
            "type": "done",
            "result": {
                "status": "success",
                "output": "local result",
                "session_id": session_id,
                "artifacts": [],
                "metrics": {},
                "run_score": None,
            },
        },
    ]


def test_connector_url_and_workspace_validation(tmp_path: Path):
    assert normalize_server_url(" https://agent.example.com/api/ ") == (
        "https://agent.example.com/api"
    )
    assert normalize_server_url("http://localhost:8000/") == "http://localhost:8000"
    for value in (
        "agent.example.com",
        "http://agent.example.com",
        "https://agent.example.com/?next=/evil",
        "https://agent.example.com/#fragment",
    ):
        with pytest.raises(ConnectorError):
            normalize_server_url(value)

    assert resolve_workspace(str(tmp_path)) == tmp_path.resolve()
    not_a_directory = tmp_path / "not-a-directory"
    not_a_directory.write_text("file", encoding="utf-8")
    with pytest.raises(ConnectorError):
        resolve_workspace(str(not_a_directory))
    with pytest.raises(ConnectorError):
        resolve_workspace(str(tmp_path / "missing"))


def test_connector_constructs_synapse_with_its_fixed_local_workspace(
    monkeypatch, tmp_path: Path,
):
    import synapse.adapters.connector as connector_module

    created = []

    class FakeSynapse:
        def __init__(self, **kwargs):
            created.append(kwargs)

    monkeypatch.setattr(connector_module, "Synapse", FakeSynapse)
    workspace = tmp_path / "project"
    workspace.mkdir()
    client = ConnectorClient(
        server="https://agent.example.com",
        workspace=workspace.resolve(),
        connection=LocalConnection(
            server="https://agent.example.com",
            workspace=str(workspace.resolve()),
            connector_id="connector",
            device_token="device-token",
            name="project",
        ),
        config_path="C:/local/synapse.yaml",
    )

    client._get_synapse()

    assert created == [{
        "config_path": "C:/local/synapse.yaml",
        "workspace_root": str(workspace.resolve()),
        "enable_external_tools": False,
        "sandbox_enabled": True,
        "sandbox_mode": "enforce",
        "sandbox_network": False,
        "auth_confirmation": True,
        "allowed_paths": [],
        "allow_external": False,
        "hooks": {},
        "paths": [],
        "mcp_servers": [],
    }]


@pytest.mark.asyncio
async def test_connector_forwards_safe_tool_params_summary(monkeypatch, tmp_path: Path):
    import synapse.adapters.connector as connector_module
    from synapse.core.events import EventBus
    from synapse.protocols.events import ToolCallStarted
    from synapse.protocols.planner import AgentResult, ExecutionMetrics, ResultStatus

    workspace = tmp_path / "project"
    workspace.mkdir()
    monkeypatch.setattr(connector_module, "CONNECTOR_SESSION_DIR", tmp_path / "sessions")
    bus = EventBus()
    forwarded: list[tuple[str, dict]] = []

    class _Container:
        def resolve(self, _type):
            return bus

    class _Synapse:
        _container = _Container()

        async def run(self, _task, *, session, confirm_callback):
            await bus.emit(ToolCallStarted(
                session_id=session.id,
                tool_name="http",
                tool_params={
                    "headers": {
                        "Authorization": "Bearer super-secret-token",
                        "x-api-key": "sk-local-secret",
                    },
                    "query": "x" * 100,
                    "items": [{"refresh_token": "also-secret"}],
                },
            ))
            return AgentResult(
                status=ResultStatus.SUCCESS,
                output="done",
                metrics=ExecutionMetrics(),
            )

        def get_run_score(self):
            return {}

    client = ConnectorClient(
        server="https://agent.example.com",
        workspace=workspace,
        connection=LocalConnection(
            server="https://agent.example.com",
            workspace=str(workspace),
            connector_id="connector",
            device_token="device-token",
            name="project",
        ),
    )
    client._synapse = _Synapse()

    async def post(path, payload, **_kwargs):
        forwarded.append((path, payload))
        return {"ok": True}

    monkeypatch.setattr(client._http, "post", post)
    await client._run_job(str(uuid.uuid4()), str(uuid.uuid4()), "inspect")

    event_payload = next(
        payload for path, payload in forwarded if path.endswith("/events")
    )
    tool_params = event_payload["events"][0]["event"]["tool_params"]
    serialized = json.dumps(event_payload, ensure_ascii=False)

    assert "super-secret-token" not in serialized
    assert "sk-local-secret" not in serialized
    assert "also-secret" not in serialized
    assert tool_params["headers.Authorization"] == "<redacted>"
    assert tool_params["headers.x-api-key"] == "<redacted>"
    assert tool_params["items[0].refresh_token"] == "<redacted>"
    assert tool_params["query"] == "x" * 77 + "..."

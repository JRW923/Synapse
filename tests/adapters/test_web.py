"""Tests for the single-process ``synapse web`` command.

The command folds the HTTP server and the local workspace Connector into one
process and auto-binds the Connector to the web UI, so the user no longer needs
a second terminal or a manual pairing code.  These tests cover:

* the ``GET /connectors/local`` endpoint (default + after auto-bind),
* the end-to-end ``synapse web`` command (auto-bind + single Ctrl+C exit),
* the friendly "model not configured" path.
"""

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from synapse.adapters.server import create_app


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _temp_models_json(home: Path) -> None:
    (home / ".synapse").mkdir(parents=True, exist_ok=True)
    (home / ".synapse" / "models.json").write_text(json.dumps({
        "default_provider": "openrouter",
        "default_model": "openai/gpt-oss-20b:free",
        "providers": {
            "openrouter": {
                "api_key": "test-key-dummy",
                "base_url": "https://openrouter.ai/api/v1",
                "protocol": "openai",
                "models": [{"id": "openai/gpt-oss-20b:free"}],
            }
        },
    }), encoding="utf-8")


def test_local_connector_endpoint_default_is_null():
    app = create_app()
    client = TestClient(app)
    resp = client.get("/connectors/local")
    assert resp.status_code == 200
    assert resp.json() == {"connector_id": None}


def test_local_connector_endpoint_returns_auto_bound_connector():
    # 复刻 web 命令在进程内做的自动绑定：建配对 -> 用配对码注册 connector
    # -> 把凭据挂到 app.state.local_connector，供网页自动认领。
    app = create_app()
    broker = app.state.connector_broker
    pairing = broker.create_pairing()
    registration = broker.register(
        workspace_name="Laptop repo",
        pair_code=pairing["pair_code"],
    )
    app.state.local_connector = {
        "connector_id": registration["connector_id"],
        "browser_token": pairing["browser_token"],
        "name": registration["name"],
    }

    client = TestClient(app)
    data = client.get("/connectors/local").json()
    assert data["connector_id"] == registration["connector_id"]
    assert data["browser_token"] == pairing["browser_token"]
    assert data["name"] == registration["name"]


def test_status_reports_connector_workspace_when_bound(tmp_path: Path, monkeypatch):
    # 连接到本地 connector 时,/status 的工作区应反映 connector 的真实目录,
    # 而非服务端进程 cwd;workspace_source 标记其来源。
    home = tmp_path / "home"
    _temp_models_json(home)
    monkeypatch.setenv("HOME", str(home))
    app = create_app()
    ws = str((tmp_path / "ws").resolve())
    (tmp_path / "ws").mkdir(exist_ok=True)
    app.state.local_connector = {
        "connector_id": "cid", "browser_token": "tok", "name": "ws",
        "workspace": ws,
    }
    client = TestClient(app)
    data = client.get("/status").json()
    assert data["workspace"] == ws
    assert data["workspace_source"] == "connector"


def test_connector_stream_injects_session_id(tmp_path: Path, monkeypatch):
    # connector 模式的 SSE 也必须在首个事件前把 session_id 推给前端,
    # 否则侧栏顶栏在整个运行期都不显示当前会话(纯 web 模式已修,这里对齐)。
    import asyncio

    from synapse.adapters.connector import ConnectorJob

    home = tmp_path / "home"
    _temp_models_json(home)
    monkeypatch.setenv("HOME", str(home))
    app = create_app()
    sid = "11111111-1111-1111-1111-111111111111"

    async def fake_start_job(*, connector_id, browser_token, task, session_id,
                             confirm_mode="ask", planning_mode=None):
        loop = asyncio.get_running_loop()
        job = ConnectorJob(
            id="j1", connector_id=connector_id, session_id=session_id,
            events=asyncio.Queue(),
            completion=loop.create_future(),
        )
        job.completion.set_result({})
        await job.events.put({"type": "event", "event": {"event_type": "agent_progress", "message": "x"}})
        await job.events.put({"type": "done", "result": {"session_id": session_id, "output": "ok"}})
        return job

    app.state.connector_broker.start_job = fake_start_job
    client = TestClient(app)
    chunks = []
    with client.stream(
        "POST", "/run/stream",
        json={"task": "t", "connector_id": "c", "connector_token": "k", "session_id": sid},
    ) as resp:
        for line in resp.iter_lines():
            if line.startswith("data: "):
                chunks.append(json.loads(line[6:]))
            if len(chunks) >= 3:
                break

    assert chunks[0]["type"] == "session"
    assert chunks[0]["session_id"] == sid
    # 后续事件也透传 session_id,前端无需等 done 就能拿到。
    assert chunks[1]["session_id"] == sid


def test_list_sessions_includes_active_connector_session(tmp_path: Path, monkeypatch):
    # connector 模式运行中的会话要进列表,前端才能给当前行加高亮。
    home = tmp_path / "home"
    _temp_models_json(home)
    monkeypatch.setenv("HOME", str(home))
    app = create_app()
    sid = "22222222-2222-2222-2222-222222222222"
    app.state.connector_broker.active_session_ids = lambda: [sid]
    client = TestClient(app)
    ids = [s["id"] for s in client.get("/sessions").json()]
    assert sid in ids


def test_config_accepts_base_url(tmp_path: Path, monkeypatch):
    # 允许用户自定义兼容端点(base_url),POST 后落入 runtime_key,
    # 之后 _synapse_for 会透传给 Synapse 的 overrides,而非被忽略。
    home = tmp_path / "home"
    _temp_models_json(home)
    monkeypatch.setenv("HOME", str(home))
    app = create_app()
    client = TestClient(app)

    resp = client.post("/config", json={
        "provider": "openai",
        "model": "gpt-4o-mini",
        "api_key": "sk-test-dummy",
        "base_url": "https://my-self-hosted.example/v1",
    })
    assert resp.status_code == 200

    data = client.get("/config").json()
    assert data["configured"] is True
    assert data["base_url"] == "https://my-self-hosted.example/v1"


def _wait_for_local_connector(port: int, timeout: float = 12.0) -> dict | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/connectors/local", timeout=2
            ) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ConnectionError, ValueError):
            data = None
        if data and data.get("connector_id"):
            return data
        time.sleep(0.25)
    return None


def _stop_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    if os.name == "nt":
        proc.terminate()
    else:
        proc.send_signal(signal.SIGINT)


def test_web_command_auto_binds_and_exits_on_sigint(tmp_path: Path):
    home = tmp_path / "home"
    _temp_models_json(home)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    port = _free_port()

    proc = subprocess.Popen(
        [sys.executable, "-m", "synapse", "web",
         "--workspace", str(workspace), "--port", str(port)],
        env={**os.environ, "HOME": str(home), "USERPROFILE": str(home),
             "BROWSER": "/bin/true"},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
    )
    try:
        bound = _wait_for_local_connector(port)
        assert bound is not None, "web 命令应自动绑定本地 connector"
        assert bound["connector_id"]
        assert bound["browser_token"]

        # 单 Ctrl+C 应干净退出（服务停服、进程结束）。
        _stop_process(proc)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AssertionError("Ctrl+C 后 web 进程未在 10s 内退出")
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.kill()


def test_web_command_without_models_starts_configuration_ui(tmp_path: Path):
    # 未配置模型时仍应启动网页，让用户浏览历史并从设置完成配置。
    home = tmp_path / "home_no_models"
    home.mkdir(parents=True, exist_ok=True)  # 故意不写 models.json
    workspace = tmp_path / "ws2"
    workspace.mkdir()
    port = _free_port()

    proc = subprocess.Popen(
        [sys.executable, "-m", "synapse", "web",
         "--workspace", str(workspace), "--port", str(port)],
        env={**os.environ, "HOME": str(home), "USERPROFILE": str(home),
             "BROWSER": "/bin/true"},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
    )
    try:
        deadline = time.time() + 12
        data = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/config", timeout=2) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    break
            except (urllib.error.URLError, ConnectionError, ValueError):
                time.sleep(0.25)
        assert data is not None
        assert data["configured"] is False
    finally:
        if proc.poll() is None:
            proc.kill()


def test_web_command_ignores_stale_saved_connection(tmp_path: Path):
    # 回归：connections.json 里残留了上一次运行（已销毁的内存 broker）的
    # connector_id/device_token。web 模式每次都是全新 broker，旧凭据必然
    # 对不上，register 会回 403。web 命令必须忽略落盘凭据、用本次新建的
    # 配对码重新注册，否则重复运行 synapse web 必现 403。
    home = tmp_path / "home_stale"
    _temp_models_json(home)
    workspace = tmp_path / "ws3"
    workspace.mkdir()
    port = _free_port()
    server = f"http://127.0.0.1:{port}"
    (home / ".synapse").mkdir(parents=True, exist_ok=True)
    (home / ".synapse" / "connections.json").write_text(json.dumps({
        "version": 1,
        "connections": [{
            "server": server,
            "workspace": str(workspace),
            "connector_id": "deadbeef" * 4,
            "device_token": "stale-token",
            "name": "ws3",
        }],
    }), encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, "-m", "synapse", "web",
         "--workspace", str(workspace), "--port", str(port)],
        env={**os.environ, "HOME": str(home), "BROWSER": "/bin/true"},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
    )
    try:
        bound = _wait_for_local_connector(port)
        assert bound is not None, "残留凭据不应导致 403，web 应忽略它并重新注册"
        assert bound["connector_id"]
    finally:
        if proc.poll() is None:
            _stop_process(proc)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

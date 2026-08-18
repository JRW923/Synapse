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


def test_web_command_auto_binds_and_exits_on_sigint(tmp_path: Path):
    home = tmp_path / "home"
    _temp_models_json(home)
    workspace = tmp_path / "ws"
    workspace.mkdir()
    port = _free_port()

    proc = subprocess.Popen(
        [sys.executable, "-m", "synapse", "web",
         "--workspace", str(workspace), "--port", str(port)],
        env={**os.environ, "HOME": str(home), "BROWSER": "/bin/true"},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        bound = _wait_for_local_connector(port)
        assert bound is not None, "web 命令应自动绑定本地 connector"
        assert bound["connector_id"]
        assert bound["browser_token"]

        # 单 Ctrl+C 应干净退出（服务停服、进程结束）。
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise AssertionError("Ctrl+C 后 web 进程未在 10s 内退出")
        assert proc.returncode is not None
    finally:
        if proc.poll() is None:
            proc.kill()


def test_web_command_without_models_exits_cleanly(tmp_path: Path):
    # 未配置模型时：应给出清晰中文提示并正常退出，而非抛出 uvicorn 堆栈。
    home = tmp_path / "home_no_models"
    home.mkdir(parents=True, exist_ok=True)  # 故意不写 models.json
    workspace = tmp_path / "ws2"
    workspace.mkdir()
    port = _free_port()

    proc = subprocess.Popen(
        [sys.executable, "-m", "synapse", "web",
         "--workspace", str(workspace), "--port", str(port)],
        env={**os.environ, "HOME": str(home), "BROWSER": "/bin/true"},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        out, _ = proc.communicate(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()

    assert proc.returncode is not None
    assert "尚未配置模型" in out


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
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        bound = _wait_for_local_connector(port)
        assert bound is not None, "残留凭据不应导致 403，web 应忽略它并重新注册"
        assert bound["connector_id"]
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()

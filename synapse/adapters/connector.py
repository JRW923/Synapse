"""Local-workspace connector for the hosted Synapse Web UI.

The connector deliberately runs a complete ``Synapse`` instance on the user's
machine.  The server only brokers opaque jobs and display events; it never
receives a local workspace path, model credential, or tool configuration.
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from synapse.adapters.library import Synapse
from synapse.config.models import models_config_path
from synapse.core.events import EventBus
from synapse.core.session import DEFAULT_SESSION_DIR, Session
from synapse.protocols.events import EventType
from synapse.protocols.planner import Planner


PAIRING_TTL_SECONDS = 10 * 60
CONNECTOR_OFFLINE_SECONDS = 45
POLL_TIMEOUT_SECONDS = 25
EVENT_BATCH_SIZE = 20
EVENT_QUEUE_SIZE = 256
COMPLETED_JOB_TTL_SECONDS = 5 * 60
# ponytail: 浏览器确认超时(秒)。超时即拒绝,避免危险调用卡死整个任务。
CONFIRM_TIMEOUT_S = 300.0
CONNECTIONS_PATH = Path.home() / ".synapse" / "connections.json"


class ConnectorError(RuntimeError):
    """Base error for the local-workspace connector."""


class ConnectorAuthenticationError(ConnectorError):
    """A browser or connector credential did not match."""


class ConnectorPairingError(ConnectorError):
    """A one-time pairing code is invalid, expired, or already used."""


class ConnectorOfflineError(ConnectorError):
    """The selected connector has not recently polled the server."""


class ConnectorBusyError(ConnectorError):
    """The selected connector already owns an active job."""


class ConnectorJobError(ConnectorError):
    """A connector tried to publish to an unknown or foreign job."""


class ConnectorTransportError(ConnectorError):
    """A local connector could not complete an HTTP request."""

    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code

    @property
    def retryable(self) -> bool:
        return self.status_code is None or self.status_code in {408, 429} or self.status_code >= 500


@dataclass
class ConnectorJob:
    """One browser task delegated to one local connector."""

    id: str
    connector_id: str
    session_id: str
    events: asyncio.Queue[dict[str, Any]]
    completion: asyncio.Future[dict[str, Any]]
    completed_at: float | None = None


@dataclass
class _ConnectorRecord:
    id: str
    device_token_hash: str
    browser_token_hash: str
    name: str
    commands: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)
    last_seen: float = field(default_factory=time.monotonic)
    active_job_id: str | None = None


@dataclass
class _PairingRecord:
    id: str
    code_hash: str
    browser_token_hash: str
    expires_at: float
    connector_id: str | None = None


def _secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _same_secret(actual: str, expected_hash: str) -> bool:
    return hmac.compare_digest(_secret_hash(actual), expected_hash)


def _new_pair_code() -> str:
    raw = secrets.token_hex(12).upper()
    return "-".join(raw[index:index + 4] for index in range(0, len(raw), 4))


def _normalize_pair_code(value: str) -> str:
    return "".join(char for char in value.upper() if char.isalnum())


def _valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (TypeError, ValueError, AttributeError):
        return False
    return True


def _display_name(value: str) -> str:
    name = value.strip()
    if not name or len(name) > 80 or "/" in name or "\\" in name:
        raise ConnectorError("本地连接名称无效")
    return name


def _error_result(session_id: str, message: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "output": message,
        "session_id": session_id,
        "artifacts": [],
        "metrics": {},
        "run_score": None,
        "error": message,
    }


class ConnectorBroker:
    """In-memory pairing and job relay used by the FastAPI adapter.

    A real public deployment must place application identity in front of this
    broker.  Until then a browser pairing token is a capability, not a user
    account.  The broker never stores a local filesystem path or a raw device
    token.
    """

    def __init__(
        self,
        *,
        pairing_ttl_seconds: int = PAIRING_TTL_SECONDS,
        offline_seconds: int = CONNECTOR_OFFLINE_SECONDS,
    ):
        self._pairing_ttl_seconds = pairing_ttl_seconds
        self._offline_seconds = offline_seconds
        # ponytail: pairing and jobs live only in one process; upgrade to a
        # shared durable store before running multiple server workers.
        self._connectors: dict[str, _ConnectorRecord] = {}
        self._pairings: dict[str, _PairingRecord] = {}
        self._jobs: dict[str, ConnectorJob] = {}
        # request_id -> connector_id:浏览器确认请求的归属,answer 经命令通道回传
        self._pending_confirms: dict[str, str] = {}

    def create_pairing(self) -> dict[str, str]:
        self._sweep()
        pair_id = str(uuid.uuid4())
        pair_code = _new_pair_code()
        browser_token = secrets.token_urlsafe(32)
        self._pairings[pair_id] = _PairingRecord(
            id=pair_id,
            code_hash=_secret_hash(_normalize_pair_code(pair_code)),
            browser_token_hash=_secret_hash(browser_token),
            expires_at=time.monotonic() + self._pairing_ttl_seconds,
        )
        return {
            "pair_id": pair_id,
            "pair_code": pair_code,
            "browser_token": browser_token,
            "expires_in_seconds": str(self._pairing_ttl_seconds),
        }

    def pairing_status(self, pair_id: str, browser_token: str) -> dict[str, Any]:
        self._sweep()
        pairing = self._pairings.get(pair_id)
        if pairing is None or not _same_secret(browser_token, pairing.browser_token_hash):
            raise ConnectorAuthenticationError("配对会话不存在或已失效")
        if pairing.connector_id is None:
            return {"status": "waiting"}
        record = self._connectors.get(pairing.connector_id)
        if record is None:
            return {"status": "waiting"}
        return {
            "status": "connected",
            "connector_id": record.id,
            "name": record.name,
        }

    def register(
        self,
        *,
        workspace_name: str,
        pair_code: str | None = None,
        connector_id: str | None = None,
        device_token: str | None = None,
    ) -> dict[str, Any]:
        """Create/reconnect a device, optionally binding it to a web pairing."""
        self._sweep()
        name = _display_name(workspace_name)
        record: _ConnectorRecord
        new_device_token: str | None = None
        pairing = self._find_pairing(pair_code) if pair_code else None
        if pairing is not None and pairing.connector_id is not None:
            raise ConnectorPairingError("该网页配对码已被使用")

        if connector_id is not None:
            if not device_token:
                raise ConnectorAuthenticationError("缺少本地连接凭据")
            record = self._authenticate_device(connector_id, device_token)
            record.name = name
        else:
            if pairing is None:
                raise ConnectorPairingError("首次连接需要网页配对码")
            connector_id = uuid.uuid4().hex
            new_device_token = secrets.token_urlsafe(32)
            record = _ConnectorRecord(
                id=connector_id,
                device_token_hash=_secret_hash(new_device_token),
                browser_token_hash="",
                name=name,
            )
            self._connectors[connector_id] = record

        if pairing is not None:
            record.browser_token_hash = pairing.browser_token_hash
            pairing.connector_id = record.id

        if not record.browser_token_hash:
            raise ConnectorPairingError("请先在网页中创建新的配对码")

        record.last_seen = time.monotonic()
        response: dict[str, Any] = {
            "connector_id": record.id,
            "name": record.name,
        }
        if new_device_token is not None:
            response["device_token"] = new_device_token
        return response

    async def poll(self, connector_id: str, device_token: str) -> dict[str, Any] | None:
        record = self._authenticate_device(connector_id, device_token)
        record.last_seen = time.monotonic()
        try:
            command = await asyncio.wait_for(
                record.commands.get(), timeout=POLL_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            return None
        record.last_seen = time.monotonic()
        return command

    async def start_job(
        self,
        *,
        connector_id: str,
        browser_token: str,
        task: str,
        session_id: str,
        confirm_mode: str = "ask",
        planning_mode: str | None = None,
    ) -> ConnectorJob:
        self._sweep()
        record = self._authenticate_browser(connector_id, browser_token)
        if time.monotonic() - record.last_seen > self._offline_seconds:
            raise ConnectorOfflineError("本地 Connector 未连接")
        if record.active_job_id is not None:
            raise ConnectorBusyError("本地 Connector 正在执行另一个任务")
        if not _valid_uuid(session_id):
            raise ConnectorJobError("会话标识无效")
        if planning_mode is not None and planning_mode not in {
            "react", "plan_execute", "hierarchical", "swarm",
        }:
            raise ConnectorJobError("规划模式无效")

        loop = asyncio.get_running_loop()
        job = ConnectorJob(
            id=str(uuid.uuid4()),
            connector_id=record.id,
            session_id=session_id,
            events=asyncio.Queue(EVENT_QUEUE_SIZE),
            completion=loop.create_future(),
        )
        self._jobs[job.id] = job
        record.active_job_id = job.id
        command = {
            "type": "run",
            "job_id": job.id,
            "session_id": session_id,
            "task": task,
            "confirm_mode": confirm_mode,
        }
        if planning_mode is not None:
            command["planning_mode"] = planning_mode
        await record.commands.put(command)
        return job

    async def publish_events(
        self,
        *,
        connector_id: str,
        device_token: str,
        job_id: str,
        events: list[dict[str, Any]],
    ) -> None:
        self._authenticate_device(connector_id, device_token)
        job = self._job_for_connector(job_id, connector_id)
        if job.completion.done():
            raise ConnectorJobError("任务已结束")
        for item in events:
            if not isinstance(item, dict):
                continue
            event = item.get("event")
            if isinstance(event, dict):
                self._enqueue_event(job, {"type": "event", "event": event})
            elif item.get("type") == "confirm" and isinstance(item.get("request_id"), str):
                # 浏览器确认:登记 request_id→connector,answer 由命令通道回传
                self._pending_confirms[item["request_id"]] = job.connector_id
                self._enqueue_event(job, item)

    async def complete_job(
        self,
        *,
        connector_id: str,
        device_token: str,
        job_id: str,
        result: dict[str, Any],
    ) -> None:
        record = self._authenticate_device(connector_id, device_token)
        job = self._job_for_connector(job_id, connector_id)
        if job.completion.done():
            return
        payload = dict(result)
        payload["session_id"] = job.session_id
        payload.setdefault("artifacts", [])
        payload.setdefault("metrics", {})
        payload.setdefault("run_score", None)
        self._finish_job(job, payload, {"type": "done", "result": payload})

    async def wait_for_completion(self, job: ConnectorJob) -> dict[str, Any]:
        """Wait for a result while treating a stale Connector as disconnected."""
        while True:
            try:
                return await asyncio.wait_for(
                    asyncio.shield(job.completion), timeout=self._liveness_timeout(),
                )
            except asyncio.TimeoutError:
                await self._fail_if_offline(job)

    async def next_event(self, job: ConnectorJob) -> dict[str, Any]:
        """Get the next browser event without waiting forever after a disconnect."""
        while True:
            try:
                return await asyncio.wait_for(
                    job.events.get(), timeout=self._liveness_timeout(),
                )
            except asyncio.TimeoutError:
                await self._fail_if_offline(job)

    async def cancel_job(
        self,
        job: ConnectorJob,
        reason: str = "网页已断开，已取消本地任务",
        *,
        stopped: bool = False,
    ) -> None:
        if job.completion.done():
            return
        record = self._connectors.get(job.connector_id)
        if record is not None and record.active_job_id == job.id:
            await record.commands.put({"type": "cancel", "job_id": job.id})
        if stopped:
            # 用户主动停止:作为正常完成返回,而非错误
            payload = _error_result(job.session_id, reason)
            payload["status"] = "stopped"
            self._finish_job(job, payload, {"type": "done", "result": payload})
        else:
            await self._fail_job(job.id, reason)

    def active_job_for(
        self, connector_id: str, browser_token: str,
    ) -> ConnectorJob | None:
        """Return the connector's in-flight job, or None when idle."""
        record = self._authenticate_browser(connector_id, browser_token)
        if record.active_job_id is None:
            return None
        return self._jobs.get(record.active_job_id)

    def active_session_ids(self) -> list[str]:
        """正在运行的 connector 任务对应的 session id(供 web 侧栏运行期高亮)。"""
        out = []
        for job in self._jobs.values():
            if not job.completion.done():
                out.append(job.session_id)
        return out

    async def resolve_confirm(
        self, request_id: str, approve: bool, *, remember: bool = False,
    ) -> bool:
        """Relay a browser confirm answer back to the connector over the command channel."""
        connector_id = self._pending_confirms.pop(request_id, None)
        if connector_id is None:
            return False
        record = self._connectors.get(connector_id)
        if record is None:
            return False
        await record.commands.put({
            "type": "confirm_result",
            "request_id": request_id,
            "approve": bool(approve),
            "remember": bool(remember),
        })
        return True

    async def disconnect(self, connector_id: str, device_token: str) -> None:
        record = self._authenticate_device(connector_id, device_token)
        record.last_seen = 0
        if record.active_job_id is not None:
            await self._fail_job(record.active_job_id, "本地 Connector 已断开")

    def _authenticate_device(self, connector_id: str, device_token: str) -> _ConnectorRecord:
        record = self._connectors.get(connector_id)
        if record is None or not device_token or not _same_secret(
            device_token, record.device_token_hash,
        ):
            raise ConnectorAuthenticationError("本地连接凭据无效")
        return record

    def _authenticate_browser(self, connector_id: str, browser_token: str) -> _ConnectorRecord:
        record = self._connectors.get(connector_id)
        if record is None or not record.browser_token_hash or not browser_token:
            raise ConnectorAuthenticationError("本地连接未配对")
        if not _same_secret(browser_token, record.browser_token_hash):
            raise ConnectorAuthenticationError("本地连接凭据无效")
        return record

    def _find_pairing(self, pair_code: str) -> _PairingRecord:
        normalized = _normalize_pair_code(pair_code)
        if not normalized:
            raise ConnectorPairingError("配对码无效")
        code_hash = _secret_hash(normalized)
        for pairing in self._pairings.values():
            if hmac.compare_digest(pairing.code_hash, code_hash):
                if pairing.expires_at <= time.monotonic():
                    raise ConnectorPairingError("配对码已过期")
                return pairing
        raise ConnectorPairingError("配对码无效")

    def _job_for_connector(self, job_id: str, connector_id: str) -> ConnectorJob:
        job = self._jobs.get(job_id)
        if job is None or job.connector_id != connector_id:
            raise ConnectorJobError("任务不存在或不属于该连接")
        return job

    async def _fail_job(self, job_id: str, message: str) -> None:
        self._fail_job_nowait(job_id, message)

    def _fail_job_nowait(self, job_id: str, message: str) -> None:
        job = self._jobs.get(job_id)
        if job is None or job.completion.done():
            return
        payload = _error_result(job.session_id, message)
        self._finish_job(job, payload, {"type": "error", "error": message})

    def _finish_job(
        self,
        job: ConnectorJob,
        payload: dict[str, Any],
        event: dict[str, Any],
    ) -> None:
        if job.completion.done():
            return
        record = self._connectors.get(job.connector_id)
        if record is not None and record.active_job_id == job.id:
            record.active_job_id = None
        job.completion.set_result(payload)
        job.completed_at = time.monotonic()
        self._enqueue_event(job, event, terminal=True)

    @staticmethod
    def _enqueue_event(
        job: ConnectorJob,
        event: dict[str, Any],
        *,
        terminal: bool = False,
    ) -> None:
        try:
            job.events.put_nowait(event)
        except asyncio.QueueFull:
            if not terminal:
                return
            with contextlib.suppress(asyncio.QueueEmpty):
                job.events.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                job.events.put_nowait(event)

    def _liveness_timeout(self) -> float:
        return max(1.0, min(float(POLL_TIMEOUT_SECONDS), float(self._offline_seconds)))

    async def _fail_if_offline(self, job: ConnectorJob) -> None:
        record = self._connectors.get(job.connector_id)
        if record is None or time.monotonic() - record.last_seen > self._offline_seconds:
            await self._fail_job(job.id, "本地 Connector 已断开")

    def _sweep(self) -> None:
        now = time.monotonic()
        for pair_id, pairing in list(self._pairings.items()):
            if pairing.expires_at <= now:
                self._pairings.pop(pair_id, None)
        for job_id, job in list(self._jobs.items()):
            if job.completed_at is not None and now - job.completed_at > COMPLETED_JOB_TTL_SECONDS:
                self._jobs.pop(job_id, None)
        for connector_id, record in list(self._connectors.items()):
            if (
                record.active_job_id is not None
                and now - record.last_seen > self._offline_seconds
            ):
                self._fail_job_nowait(record.active_job_id, "本地 Connector 已断开")
            if record.active_job_id is None and now - record.last_seen > 3600:
                self._connectors.pop(connector_id, None)


@dataclass
class LocalConnection:
    server: str
    workspace: str
    connector_id: str
    device_token: str
    name: str


class ConnectionStore:
    """Small private store for connector device credentials."""

    def __init__(self, path: Path = CONNECTIONS_PATH):
        self.path = path

    def find(self, server: str, workspace: Path) -> LocalConnection | None:
        for connection in self._load():
            if connection.server == server and connection.workspace == str(workspace):
                return connection
        return None

    def upsert(self, connection: LocalConnection) -> None:
        records = [
            item for item in self._load()
            if not (
                item.server == connection.server
                and item.workspace == connection.workspace
            )
        ]
        records.append(connection)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "connections": [
                {
                    "server": item.server,
                    "workspace": item.workspace,
                    "connector_id": item.connector_id,
                    "device_token": item.device_token,
                    "name": item.name,
                }
                for item in records
            ],
        }
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, self.path)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def _load(self) -> list[LocalConnection]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        output: list[LocalConnection] = []
        for item in raw.get("connections", []):
            if not isinstance(item, dict):
                continue
            values = (
                item.get("server"), item.get("workspace"),
                item.get("connector_id"), item.get("device_token"), item.get("name"),
            )
            if not all(isinstance(value, str) and value for value in values):
                continue
            output.append(LocalConnection(
                server=item["server"],
                workspace=item["workspace"],
                connector_id=item["connector_id"],
                device_token=item["device_token"],
                name=item["name"],
            ))
        return output


def normalize_server_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ConnectorError("服务器地址必须是完整的 http(s) URL")
    if parsed.query or parsed.fragment:
        raise ConnectorError("服务器地址不能包含查询参数或片段")
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" and hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ConnectorError("公网 Connector 必须使用 HTTPS")
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"


def resolve_workspace(workspace: str) -> Path:
    root = Path(workspace).expanduser().resolve()
    if not root.is_dir():
        raise ConnectorError(f"工作区不存在或不是目录：{root}")
    return root


def _discover_config_path(workspace: Path, explicit: str | None) -> str | None:
    if explicit:
        return explicit
    current = workspace
    while True:
        candidate = current / "synapse.yaml"
        if candidate.exists():
            return str(candidate)
        if current.parent == current:
            break
        current = current.parent
    user_config = Path.home() / ".synapse" / "config.yaml"
    return str(user_config) if user_config.exists() else None


class _JsonHttpClient:
    """Tiny JSON-over-HTTPS client implemented with the Python standard library."""

    def __init__(self, server: str):
        self.server = server.rstrip("/")

    async def get(self, path: str, *, token: str | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(self._request, "GET", path, None, token, 15)

    async def post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        token: str | None = None,
        timeout: int = 35,
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._request, "POST", path, payload, token, timeout)

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        token: str | None,
        timeout: int,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            f"{self.server}{path}", data=data, headers=headers, method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(body).get("detail", body)
            except json.JSONDecodeError:
                detail = body
            raise ConnectorTransportError(
                f"服务器拒绝请求（HTTP {exc.code}）：{detail}", status_code=exc.code,
            ) from exc
        except (OSError, urllib.error.URLError) as exc:
            raise ConnectorTransportError(f"无法连接服务器：{exc}") from exc
        try:
            decoded = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ConnectorTransportError("服务器返回了无效 JSON") from exc
        if not isinstance(decoded, dict):
            raise ConnectorTransportError("服务器返回了无效响应")
        return decoded


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _redact_workspace(value: Any, workspace: Path) -> Any:
    if isinstance(value, str):
        absolute = str(workspace)
        normalized = absolute.replace("\\", "/")
        output = value.replace(absolute, "<workspace>")
        if normalized != absolute:
            output = output.replace(normalized, "<workspace>")
        return output
    if isinstance(value, dict):
        return {str(key): _redact_workspace(item, workspace) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_workspace(item, workspace) for item in value]
    return value


_TOOL_PARAM_VALUE_LIMIT = 80
_SENSITIVE_TOOL_PARAM_KEY_PARTS = (
    "apikey",
    "token",
    "authorization",
    "auth",
    "password",
    "passwd",
    "pwd",
    "secret",
    "cookie",
    "credential",
)


def _is_sensitive_tool_param_key(key: Any) -> bool:
    normalized = "".join(char for char in str(key).lower() if char.isalnum())
    return any(part in normalized for part in _SENSITIVE_TOOL_PARAM_KEY_PARTS)


def _truncate_tool_param_value(value: Any) -> str:
    text = str(value)
    return text if len(text) <= _TOOL_PARAM_VALUE_LIMIT else text[:77] + "..."


def _safe_tool_params_summary(params: Any) -> dict[str, str]:
    """Flatten local tool parameters without relaying credentials verbatim."""
    summary: dict[str, str] = {}

    def visit(value: Any, path: str) -> None:
        if isinstance(value, dict):
            if not value:
                summary[path or "value"] = "{}"
            for key, nested in value.items():
                key_text = str(key)
                nested_path = f"{path}.{key_text}" if path else key_text
                if _is_sensitive_tool_param_key(key_text):
                    summary[nested_path] = "<redacted>"
                else:
                    visit(nested, nested_path)
            return
        if isinstance(value, (list, tuple)):
            if not value:
                summary[path or "value"] = "[]"
            for index, nested in enumerate(value):
                visit(nested, f"{path}[{index}]")
            return
        summary[path or "value"] = _truncate_tool_param_value(value)

    visit(params, "")
    return summary


def _make_confirm(event_queue: asyncio.Queue, pending: dict, mode: str = "ask") -> Callable:
    """Browser confirm bridge: push a confirm event onto the SSE stream and
    block until the answer arrives over the broker command channel.

    mode="auto" 直接放行(全权同意);mode="ask" 才弹窗等浏览器回答。
    """

    async def _confirm(request) -> bool:
        if mode == "auto":
            return True
        request_id = uuid.uuid4().hex
        params = getattr(request, "tool_params", {}) or {}
        item = {
            "type": "confirm",
            "request_id": request_id,
            "request": {
                "tool_name": getattr(request, "tool_name", "tool"),
                "risk_level": getattr(request, "risk_level", ""),
                "tool_params": _safe_tool_params_summary(params),
            },
        }
        try:
            event_queue.put_nowait(item)
        except asyncio.QueueFull:
            await event_queue.put(item)
        waiter = (asyncio.Event(), [False], request)
        pending[request_id] = waiter
        try:
            await asyncio.wait_for(waiter[0].wait(), timeout=CONFIRM_TIMEOUT_S)
            return bool(waiter[1][0])
        except asyncio.TimeoutError:
            pending.pop(request_id, None)
            return False

    return _confirm


class ConnectorClient:
    """Long-polling client that runs hosted jobs in one local Synapse runtime."""

    def __init__(
        self,
        *,
        server: str,
        workspace: Path,
        connection: LocalConnection,
        config_path: str | None = None,
    ):
        self.server = server
        self.workspace = workspace
        self.connection = connection
        self._config_path = config_path
        self._http = _JsonHttpClient(server)
        self._synapse: Synapse | None = None
        self._synapse_mode: str | None = None
        self._sessions: dict[str, Session] = {}
        self._active_task: asyncio.Task[None] | None = None
        self._active_job_id: str | None = None
        # request_id -> (asyncio.Event, [answer]);浏览器确认等待器,由命令通道唤醒
        self._pending_confirms: dict[str, tuple[asyncio.Event, list[bool]]] = {}

    async def register(self, pair_code: str | None = None) -> bool:
        payload: dict[str, Any] = {"workspace_name": self.connection.name}
        token = None
        if self.connection.connector_id:
            payload["connector_id"] = self.connection.connector_id
            token = self.connection.device_token
        if pair_code:
            payload["pair_code"] = _normalize_pair_code(pair_code)
        response = await self._http.post("/connectors/register", payload, token=token)
        self.connection.connector_id = str(response["connector_id"])
        new_token = response.get("device_token")
        if new_token:
            self.connection.device_token = str(new_token)
            return True
        return False

    async def run_forever(self) -> None:
        print(f"已连接到 {self.server}，本地工作区：{self.connection.name}")
        retry_delay = 1.0
        try:
            while True:
                try:
                    response = await self._http.post(
                        f"/connectors/{self.connection.connector_id}/commands",
                        {},
                        token=self.connection.device_token,
                    )
                except ConnectorTransportError as exc:
                    if not exc.retryable:
                        raise
                    print(
                        f"与服务器连接暂时中断，{int(retry_delay)} 秒后重试：{exc}",
                        file=sys.stderr,
                    )
                    await asyncio.sleep(retry_delay)
                    retry_delay = min(retry_delay * 2, 15.0)
                    continue
                retry_delay = 1.0
                command = response.get("command")
                if not isinstance(command, dict):
                    continue
                kind = command.get("type")
                if kind == "run":
                    await self._start_job(command)
                elif kind == "cancel":
                    self._cancel_active_job(str(command.get("job_id", "")))
                elif kind == "confirm_result":
                    self._resolve_confirm(
                        str(command.get("request_id", "")),
                        bool(command.get("approve", False)),
                        bool(command.get("remember", False)),
                    )
        finally:
            if self._active_task is not None and not self._active_task.done():
                self._active_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._active_task
            with contextlib.suppress(ConnectorError):
                await self._http.post(
                    f"/connectors/{self.connection.connector_id}/disconnect",
                    {},
                    token=self.connection.device_token,
                    timeout=10,
                )

    async def _start_job(self, command: dict[str, Any]) -> None:
        job_id = str(command.get("job_id", ""))
        session_id = str(command.get("session_id", ""))
        task = command.get("task")
        if not _valid_uuid(job_id) or not _valid_uuid(session_id) or not isinstance(task, str):
            return
        if self._active_task is not None and not self._active_task.done():
            await self._complete_error(job_id, session_id, "本地 Connector 正在执行另一个任务")
            return
        self._active_job_id = job_id
        self._active_task = asyncio.create_task(
            self._run_job(
                job_id, session_id, task,
                str(command.get("confirm_mode", "ask")),
                command.get("planning_mode"),
            ),
        )

    def _cancel_active_job(self, job_id: str) -> None:
        if job_id != self._active_job_id or self._active_task is None:
            return
        if self._synapse is not None:
            with contextlib.suppress(Exception):
                planner = self._synapse._container.resolve(Planner)
                if hasattr(planner, "request_cancel"):
                    planner.request_cancel()
        self._active_task.cancel()

    def _resolve_confirm(self, request_id: str, approve: bool, remember: bool = False) -> None:
        waiter = self._pending_confirms.pop(request_id, None)
        if waiter is None:
            return
        event, answer, request = waiter
        # 按操作类型记住允许:写入 ActionAuthorizer 的会话级白名单,
        # 之后同签名调用会被 authorizer 直接放行(不再触发 confirm 回调)。
        if remember and request is not None and self._synapse is not None:
            with contextlib.suppress(Exception):
                from synapse.modules.security.auth import ActionAuthorizer
                auth = self._synapse._container.resolve(ActionAuthorizer)
                auth.remember_approval(request)
        answer[0] = approve
        event.set()

    def _get_synapse(self, planning_mode: str | None = None) -> Synapse:
        if self._synapse is None:
            # The server never supplies these settings.  This local construction
            # keeps the existing tool/auth/sandbox pipeline on the user's host.
            # These overrides also prevent a workspace config from widening the
            # hosted Connector's filesystem or code-execution boundary.
            self._synapse = Synapse(
                config_path=self._config_path,
                workspace_root=str(self.workspace),
                enable_external_tools=False,
                sandbox_enabled=True,
                sandbox_mode="enforce",
                sandbox_network=False,
                auth_confirmation=True,
                allowed_paths=[],
                allow_external=False,
                hooks={},
                paths=[],
                mcp_servers=[],
                **({"mode": planning_mode} if planning_mode else {}),
            )
            self._synapse_mode = planning_mode
        return self._synapse

    def _session(self, session_id: str) -> Session:
        cached = self._sessions.get(session_id)
        if cached is not None:
            return cached
        path = DEFAULT_SESSION_DIR / f"{session_id}.json"
        if path.exists():
            session = Session.load(path)
        else:
            session = Session(session_id=session_id)
        self._sessions[session_id] = session
        return session

    async def _run_job(
        self, job_id: str, session_id: str, task: str, confirm_mode: str = "ask",
        planning_mode: str | None = None,
    ) -> None:
        self._pending_confirms.clear()
        event_queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(EVENT_QUEUE_SIZE)
        sender = asyncio.create_task(self._forward_events(job_id, event_queue))
        subscriptions: list[tuple[str, Any]] = []
        sequence = 0

        async def on_event(event) -> None:
            nonlocal sequence
            raw_data = {
                key: value for key, value in event.__dict__.items()
                if not key.startswith("_")
            }
            if getattr(event, "event_type", "") == EventType.TOOL_CALL_STARTED.value:
                raw_data["tool_params"] = _safe_tool_params_summary(
                    raw_data.get("tool_params", {}),
                )
            data = _redact_workspace(_json_safe(raw_data), self.workspace)
            data["event_type"] = _json_safe(getattr(event, "event_type", ""))
            item = {"sequence": sequence, "event": data}
            sequence += 1
            try:
                event_queue.put_nowait(item)
            except asyncio.QueueFull:
                if data["event_type"] != EventType.LLM_TOKEN.value:
                    await event_queue.put(item)

        payload: dict[str, Any]
        try:
            if self._synapse is not None and planning_mode != self._synapse_mode:
                await self._synapse.aclose()
                self._synapse = None
            synapse = self._get_synapse(planning_mode)
            event_bus = synapse._container.resolve(EventBus)
            for event_type in EventType:
                event_bus.subscribe(event_type.value, on_event)
                subscriptions.append((event_type.value, on_event))
            session = self._session(session_id)
            result = await synapse.run(
                task,
                session=session,
                confirm_callback=_make_confirm(
                    event_queue, self._pending_confirms, confirm_mode,
                ),
            )
            # 持久化上下文引用报告,供 /sessions/{id}/context-report 读取
            # (端点读磁盘 session,不能依赖新建 synapse 的内存追踪器)
            report = synapse.get_citation_report()
            if report is not None:
                session.metadata["citation_report"] = report
            session.save(DEFAULT_SESSION_DIR)
            payload = self._result_payload(result, synapse, session_id)
        except asyncio.CancelledError:
            payload = _error_result(session_id, "本地 Connector 已停止任务")
        except Exception as exc:
            payload = _error_result(session_id, f"本地执行失败：{type(exc).__name__}: {exc}")
        finally:
            if self._synapse is not None:
                event_bus = self._synapse._container.resolve(EventBus)
                for event_type, handler in subscriptions:
                    event_bus.unsubscribe(event_type, handler)
            await event_queue.put(None)
            with contextlib.suppress(Exception):
                await sender
        try:
            await self._post_completion(job_id, payload)
        finally:
            self._active_job_id = None

    async def _post_completion(self, job_id: str, payload: dict[str, Any]) -> None:
        retry_delay = 1.0
        while True:
            try:
                await self._http.post(
                    f"/connectors/{self.connection.connector_id}/complete",
                    {"job_id": job_id, "result": payload},
                    token=self.connection.device_token,
                )
                return
            except ConnectorTransportError as exc:
                if not exc.retryable:
                    print(f"任务结果未能回传服务器：{exc}", file=sys.stderr)
                    return
                print(
                    f"任务结果回传失败，{int(retry_delay)} 秒后重试：{exc}",
                    file=sys.stderr,
                )
                await asyncio.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 15.0)

    async def _forward_events(
        self,
        job_id: str,
        queue: asyncio.Queue[dict[str, Any] | None],
    ) -> None:
        finished = False
        while not finished:
            item = await queue.get()
            consumed = 1
            batch: list[dict[str, Any]] = []
            try:
                if item is None:
                    finished = True
                else:
                    batch.append(item)
                while not finished and len(batch) < EVENT_BATCH_SIZE:
                    try:
                        item = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    consumed += 1
                    if item is None:
                        finished = True
                    else:
                        batch.append(item)
                if batch:
                    try:
                        await self._http.post(
                            f"/connectors/{self.connection.connector_id}/events",
                            {"job_id": job_id, "events": batch},
                            token=self.connection.device_token,
                        )
                    except ConnectorError as exc:
                        print(f"任务进度未能回传服务器：{exc}", file=sys.stderr)
            finally:
                for _ in range(consumed):
                    queue.task_done()

    def _result_payload(self, result, synapse: Synapse, session_id: str) -> dict[str, Any]:
        metrics = result.metrics
        payload = {
            "status": getattr(result.status, "value", str(result.status)),
            "output": result.output,
            "session_id": session_id,
            "run_id": "",
            "artifacts": [
                {
                    "path": artifact.path,
                    "content": artifact.content,
                    "action": artifact.action,
                }
                for artifact in result.artifacts
            ],
            "metrics": {
                "tokens_input": metrics.tokens_input,
                "tokens_output": metrics.tokens_output,
                "tool_call_count": metrics.tool_call_count,
                "tool_success_count": metrics.tool_success_count,
                "duration_ms": metrics.duration_ms,
                "thrashing_events": metrics.thrashing_events,
            },
            "run_score": synapse.get_run_score(),
        }
        return _redact_workspace(_json_safe(payload), self.workspace)

    async def _complete_error(self, job_id: str, session_id: str, message: str) -> None:
        with contextlib.suppress(ConnectorError):
            await self._http.post(
                f"/connectors/{self.connection.connector_id}/complete",
                {"job_id": job_id, "result": _error_result(session_id, message)},
                token=self.connection.device_token,
            )


async def run_connector(
    *,
    server: str,
    workspace: str,
    name: str | None = None,
    config_path: str | None = None,
    pair: bool = False,
    pair_code: str | None = None,
    on_registered: Callable[[LocalConnection], None] | None = None,
    fresh: bool = False,
    store_path: Path = CONNECTIONS_PATH,
) -> None:
    """Start a foreground connector; Ctrl+C only disconnects the client.

    ``pair_code`` lets a launcher supply the one-time code programmatically
    (e.g. the ``synapse web`` single-process mode) instead of prompting.
    ``on_registered`` fires once the connector is bound, carrying the
    connection so callers can publish local-only credentials to the web UI.
    """
    if not models_config_path().exists():
        raise ConnectorError(
            f"尚未配置模型。请先运行 synapse，完成本机模型配置：{models_config_path()}"
        )
    normalized_server = normalize_server_url(server)
    root = resolve_workspace(workspace)
    display_name = _display_name(name or root.name or "workspace")
    store = ConnectionStore(store_path)
    # ``synapse web`` spins up a brand-new in-process broker every run, so any
    # credentials persisted from a previous run belong to a dead broker and
    # would make register() fail with 403. Force a fresh registration there.
    connection = None if fresh else store.find(normalized_server, root)
    needs_pair = pair or connection is None
    if pair_code is None and needs_pair:
        pair_code = getpass.getpass("请输入网页显示的一次性配对码：")
        if not _normalize_pair_code(pair_code):
            raise ConnectorPairingError("未输入配对码")
    if connection is None:
        connection = LocalConnection(
            server=normalized_server,
            workspace=str(root),
            connector_id="",
            device_token="",
            name=display_name,
        )
    else:
        connection.name = display_name

    client = ConnectorClient(
        server=normalized_server,
        workspace=root,
        connection=connection,
        config_path=_discover_config_path(root, config_path),
    )
    changed = await client.register(pair_code)
    # Don't persist credentials for an ephemeral broker: they can never
    # reconnect and would only poison the next run's register() into a 403.
    if not fresh and (changed or needs_pair):
        store.upsert(connection)
    if on_registered is not None:
        on_registered(connection)
    await client.run_forever()

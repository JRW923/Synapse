"""FastAPI HTTP server wrapping the Synapse facade.

Exposes endpoints for running tasks, inspecting sessions, and managing
A/B experiments.  Designed to be mounted via ``create_app()`` or run
directly with ``uvicorn``.

Usage::

    uvicorn synapse.adapters.server:app --port 8000
    # or via CLI:
    synapse serve --port 8000
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import tempfile
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field as PydanticField

from synapse import __version__
from synapse.adapters.library import Synapse
from synapse.adapters.connector import (
    ConnectorAuthenticationError,
    ConnectorBroker,
    ConnectorBusyError,
    ConnectorError,
    ConnectorJob,
    ConnectorJobError,
    ConnectorOfflineError,
    ConnectorPairingError,
)
from synapse.config import load_config
from synapse.core.events import EventBus
from synapse.protocols.events import TodoUpdated
from synapse.core.session import DEFAULT_SESSION_DIR, Session
from synapse.modules.todo import DEFAULT_TODO_DIR, TodoStore, get_default_todo_store
from synapse.protocols.planner import AgentResult, ExecutionMetrics
from synapse.eval.experiments import Experiment, ExperimentResult
from synapse.eval.runner import _runner_comparability_envelope


_EXPERIMENT_METRIC_DIRECTIONS: dict[str, Literal["higher", "lower"]] = {
    "agent_reported_success": "higher",
    "duration_ms": "lower",
    "tokens": "lower",
    "tool_calls": "lower",
    "tool_success_rate": "higher",
    "safety_risk_attempts": "lower",
    "safety_policy_blocks": "lower",
    "safety_violations": "lower",
}

_REMOTE_EXPERIMENT_FORBIDDEN_KEYS = {
    "api_key", "base_url", "config_path", "hooks", "plugins", "mcp_servers",
    "enable_external_tools", "workspace_root", "allow_external", "allowed_paths",
    "sandbox_enabled", "sandbox_mode", "sandbox_backend", "sandbox_network",
    "sandbox_docker_image", "auth_confirmation",
}


@contextlib.contextmanager
def _swallow(where: str):
    """Decoration must never abort the run — same contract as the CLI renderer."""
    try:
        yield
    except Exception:
        pass


def _validate_remote_experiment_config(value: Any, *, _path: str = "config") -> None:
    """Reject request-controlled host/network escape hatches before Synapse builds."""
    if isinstance(value, dict):
        for key, nested in value.items():
            name = str(key).strip().lower()
            if name in _REMOTE_EXPERIMENT_FORBIDDEN_KEYS:
                raise ValueError(f"{_path}.{key} is not allowed for HTTP experiments")
            _validate_remote_experiment_config(nested, _path=f"{_path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _validate_remote_experiment_config(nested, _path=f"{_path}[{index}]")


def _bearer_token(authorization: str | None) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing connector credential")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing connector credential")
    return token


def _raise_connector_error(exc: ConnectorError) -> None:
    if isinstance(exc, ConnectorAuthenticationError):
        raise HTTPException(status_code=403, detail="Connector authentication failed") from exc
    if isinstance(exc, ConnectorPairingError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, (ConnectorOfflineError, ConnectorBusyError)):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, ConnectorJobError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


#: How long a streamed run waits for the user to answer a /confirm prompt
#: before denying by default (fail-safe: the tool call is refused, not lost).
_CONFIRM_TIMEOUT_S = 300.0


# ---------------------------------------------------------------------------
# Pydantic models for request / response
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    # HTTP callers are untrusted.  In particular, they must not smuggle a
    # workspace path or an approval bypass into the server process.
    model_config = ConfigDict(extra="forbid")

    task: str
    session_id: str | None = None  # continue a prior conversation (in-memory or saved on disk)
    # A local-workspace task is selected by opaque connector credentials.  A
    # browser never submits a path, model key, or local execution setting.
    connector_id: str | None = None
    connector_token: str | None = None


class ConnectorRegisterRequest(BaseModel):
    workspace_name: str
    pair_code: str | None = None
    connector_id: str | None = None


class ConnectorEventsRequest(BaseModel):
    job_id: str
    events: list[dict[str, Any]]


class ConnectorCompleteRequest(BaseModel):
    job_id: str
    result: dict[str, Any]


class KeyConfig(BaseModel):
    """Browser-supplied API credentials for a local single-user instance.

    Stored in process memory only (``app.state.runtime_key``); never written to
    the server's ``models.json``. Lets ``synapse serve`` start with no config and
    have the user paste their own key in the web UI.
    """

    provider: str = "openai"
    model: str = "gpt-4o-mini"
    api_key: str
    base_url: str = ""  # 自定义兼容端点(留空则用内置默认地址)


class ConfirmDecision(BaseModel):
    approve: bool
    remember: bool = False  # 按操作类型记住允许(仅 approve 时有意义)


class StopRequest(BaseModel):
    """Browser-initiated interrupt for a local Connector run."""

    connector_id: str | None = None
    connector_token: str | None = None


class ConfirmModeRequest(BaseModel):
    """Global confirmation policy for connector runs."""

    mode: str  # "ask" | "auto"


class SessionConfig(BaseModel):
    """Per-session runtime override set from the web UI.

    模型/provider/api_key 一律由 web 端全局配置(``/config`` → ``runtime_key``)
    决定,本会话运行时只可覆盖规划模式 ``mode``,避免两处都能配模型造成冲突。
    """

    mode: str | None = None


_VALID_MODES = {"react", "plan_execute", "hierarchical", "swarm"}


class MetricsResponse(BaseModel):
    tokens_input: int = 0
    tokens_output: int = 0
    tool_call_count: int = 0
    tool_success_count: int = 0
    duration_ms: int = 0
    thrashing_events: int = 0


class ArtifactResponse(BaseModel):
    path: str
    content: str
    action: str


class RunResponse(BaseModel):
    status: str
    output: str
    session_id: str
    run_id: str = ""
    artifacts: list[ArtifactResponse] = []
    metrics: MetricsResponse = MetricsResponse()
    run_score: dict[str, Any] | None = None  # L.4 — runtime score + process hint


class SessionInfo(BaseModel):
    id: str
    message_count: int
    estimated_tokens: int
    metadata: dict[str, Any]


class MessageResponse(BaseModel):
    role: str
    content: str


class ExperimentRequest(BaseModel):
    name: str
    variables: dict[str, Any] = PydanticField(default_factory=dict)
    agent_config_a: dict[str, Any]
    agent_config_b: dict[str, Any]
    benchmark_task: str = "Say hello."
    runs_per_config: int = PydanticField(default=6, ge=1)
    primary_metric: Literal[
        "agent_reported_success", "duration_ms", "tokens", "tool_calls",
        "tool_success_rate", "safety_risk_attempts", "safety_policy_blocks",
        "safety_violations",
    ] = "duration_ms"
    direction: Literal["higher", "lower"] | None = None
    seed: int = 0
    metric_directions: dict[str, Literal["higher", "lower"]] = PydanticField(
        default_factory=lambda: dict(_EXPERIMENT_METRIC_DIRECTIONS)
    )
    guardrail_metrics: list[str] = PydanticField(
        default_factory=lambda: ["agent_reported_success", "safety_violations"]
    )
    allowed_config_diff_paths: list[str] | None = None


class ExperimentStatusResponse(BaseModel):
    id: str
    status: str  # "running" | "completed" | "failed" | "cancelled"
    result: dict[str, Any] | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# In-memory stores
# ---------------------------------------------------------------------------


@dataclass
class ExperimentRecord:
    id: str
    status: str  # "running" | "completed" | "failed" | "cancelled"
    result: ExperimentResult | None = None
    error: str | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# Benchmark helper for experiments
# ---------------------------------------------------------------------------


def _make_benchmark(task_description: str):
    """Return an async callable suitable for ``Experiment.benchmark``.

    The callable creates a fresh isolated ``Synapse`` instance and returns
    agent status plus efficiency and safety diagnostics.
    """

    async def _run(
        config: dict[str, Any], _seed: int,
    ) -> tuple[dict[str, float], dict[str, Any]]:
        run_config = dict(config)
        run_config["enable_eval"] = True
        run_config["strict_overrides"] = True
        synapse = Synapse(**run_config)
        try:
            result = await synapse.run(task_description)
            score = synapse.get_run_score() or {}
            safety = score.get("safety", {})
            metrics = result.metrics
            effective = synapse.get_effective_config()
            return ({
                "agent_reported_success": float(result.status.value == "success"),
                "duration_ms": float(metrics.duration_ms),
                "tokens": float(metrics.tokens_input + metrics.tokens_output),
                "tool_calls": float(metrics.tool_call_count),
                "tool_success_rate": (
                    metrics.tool_success_count / metrics.tool_call_count
                    if metrics.tool_call_count else 0.0
                ),
                "safety_risk_attempts": float(
                    safety.get("injection_attempts", 0)
                    + safety.get("dangerous_command_attempts", 0)
                ),
                "safety_policy_blocks": float(safety.get("auth_blocks", 0)),
                "safety_violations": float(
                    safety.get("sandbox_violations", 0)
                    + safety.get("out_of_workspace_access", 0)
                ),
            }, _runner_comparability_envelope(effective, score))
        finally:
            close = getattr(synapse, "aclose", None)
            if close is not None:
                await close()

    return _run


async def _effective_experiment_config(config: dict[str, Any]) -> dict[str, Any]:
    import tempfile

    with tempfile.TemporaryDirectory(prefix="synapse-server-experiment-config-") as tmp:
        run_config = dict(config)
        run_config["enable_eval"] = True
        run_config["workspace_root"] = tmp
        run_config["strict_overrides"] = True
        synapse = Synapse(**run_config)
        try:
            return synapse.get_effective_config()
        finally:
            close = getattr(synapse, "aclose", None)
            if close is not None:
                await close()


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    synapse_instance: Synapse | None = None,
    *,
    connector_only: bool = False,
) -> FastAPI:
    """Build and return a configured FastAPI application.

    Parameters
    ----------
    synapse_instance:
        An optional pre-configured :class:`Synapse` instance.  When not
        provided a default ``Synapse(provider="anthropic")`` is used.
        Tests should inject a mock.
    connector_only:
        Reject server-side Agent execution and only relay local Connector
        jobs.  This is the intended mode for a hosted local-workspace UI.
    """
    app = FastAPI(title="Synapse API", version="0.1.0")

    # ---- state -----------------------------------------------------------
    sessions: dict[str, Session] = {}
    experiments: dict[str, ExperimentRecord] = {}
    experiment_lock = asyncio.Lock()
    connector_broker = ConnectorBroker()
    app.state.connector_broker = connector_broker

    # One Synapse per session: each instance serializes runs through its own
    # _run_lock, so different sessions execute concurrently and each SSE
    # stream subscribes only to its own EventBus (no cross-talk).
    # ponytail: unbounded per-session cache — fine for a local single-user
    # server; upgrade path is LRU eviction + aclose() once multi-tenant.
    synapse_instances: dict[str, Synapse] = {}

    # Per-session runtime overrides (provider/model/mode/api_key) set via the
    # web UI "switch model / mode" control. Session-scoped on purpose — we do
    # NOT persist to models.json, so the CLI default is never silently changed.
    # ponytail: same unbounded-cache ceiling as synapse_instances.
    app.state.session_runtime: dict[str, dict] = {}

    # Browser-set API key (local single-user). In-memory only; set via POST
    # /config from the web UI. When set, every session is built with it so the
    # user runs on their own key. None means fall back to the server's
    # models.json (or a friendly 400 if that's also absent).
    app.state.runtime_key = None

    # Local single-process connector (``synapse web``): when set, holds the
    # {connector_id, browser_token, name} so the web UI can auto-bind without
    # a manual pairing code. None means no local connector is registered.
    # ponytail: only meaningful for an unauthenticated local-only server;
    # the ``web`` command binds 127.0.0.1 and never exposes this publicly.
    app.state.local_connector = None

    # request_id -> [asyncio.Event, approved|None] for in-flight confirmations.
    # The Event lives on the run's event loop; /confirm may arrive on another
    # thread's loop, so the wakeup must go through call_soon_threadsafe.
    confirm_waiters: dict[str, list] = {}
    app.state.confirm_waiters = confirm_waiters

    # 全局确认模式:ask=每次询问(仅确认受限操作才弹窗);auto=全部允许。
    # 随 run 命令下发给本地 connector,由 confirm 回调兑现。
    app.state.confirm_mode = "ask"

    def _synapse_for(session_id: str) -> Synapse:
        """Per-session facade; an injected instance (tests) is always reused."""
        if synapse_instance is not None:
            return synapse_instance
        ov = app.state.session_runtime.get(session_id) or {}
        rc = app.state.runtime_key
        # 模型/provider/api_key 以 web 端全局配置(runtime_key)为准;
        # 本会话运行时仅可覆盖规划模式 mode,不重复提供模型配置入口。
        provider = rc["provider"] if rc else None
        model = rc["model"] if rc else None
        api_key = rc["api_key"] if rc else None
        mode = ov.get("mode")

        build_kwargs: dict = {}
        if provider:
            build_kwargs["provider"] = provider
        if model:
            build_kwargs["model"] = model
        if api_key:
            build_kwargs["api_key"] = api_key
        if mode:
            build_kwargs["mode"] = mode
        # 自定义兼容端点(base_url)透传给 Synapse 的 overrides,经 config.provider
        # 自动下传到真实 LLM 客户端,零新增路由。
        base_url = rc.get("base_url") if rc else None
        if base_url:
            build_kwargs["base_url"] = base_url

        if build_kwargs:
            inst = synapse_instances.get(session_id)
            # Rebuild only when the cached instance was built under a different
            # override set (e.g. a model/mode switch evicted it). The POST
            # /sessions/{id}/config handler also evicts, so this is a belt-and-
            # braces guard against returning a stale instance.
            if inst is None or getattr(inst, "_synapse_override", None) != build_kwargs:
                if inst is not None:
                    with _swallow("evict stale synapse instance"):
                        try:
                            asyncio.ensure_future(inst.aclose())
                        except Exception:
                            pass
                inst = Synapse(**build_kwargs)
                inst._synapse_override = build_kwargs
                synapse_instances[session_id] = inst
            return inst
        # Fallback to the server's models.json; fail friendly if absent so the
        # web UI's "set your own key" gate is the only path needed.
        inst = synapse_instances.get(session_id)
        if inst is None:
            try:
                inst = Synapse()
            except Exception as exc:  # ConfigError / missing models.json
                raise HTTPException(
                    status_code=400,
                    detail="尚未配置模型：请先点击右上角「设置」填入你自己的 API Key。",
                )
            synapse_instances[session_id] = inst
        return inst

    def _make_confirm_bridge(put):
        """Turn confirm_callback into an SSE prompt + POST /confirm round-trip.

        ``put`` is the SSE queue's async put, so the prompt rides the same
        stream as the run's events. Denies on timeout so an abandoned tab
        refuses the risky call instead of hanging the run forever.
        """
        async def _confirm(request) -> bool:
            request_id = uuid.uuid4().hex
            waiter = [asyncio.Event(), None]
            waiter.append(asyncio.get_running_loop())
            confirm_waiters[request_id] = waiter
            try:
                await put({"type": "confirm", "request_id": request_id, "request": {
                    "tool_name": request.tool_name,
                    "tool_params": request.tool_params,
                    "risk_level": request.risk_level,
                }})
                await asyncio.wait_for(waiter[0].wait(), timeout=_CONFIRM_TIMEOUT_S)
                return waiter[1]
            except asyncio.TimeoutError:
                return False
            finally:
                confirm_waiters.pop(request_id, None)
        return _confirm

    def _resolve_session(session_id: str | None) -> Session:
        """New session, or continue one seen earlier in-process / saved on disk."""
        if not session_id:
            session = Session()
        elif session_id in sessions:
            session = sessions[session_id]
        else:
            path = DEFAULT_SESSION_DIR / f"{session_id}.json"
            if path.exists():
                session = Session.load(path)
            else:
                raise HTTPException(status_code=404, detail="Session not found")
        # Bind the process-global todo store to this session so the agent's
        # TodoWrite persists under the right id (mirrors the CLI REPL).
        with _swallow("resolve_session: bind todo store"):
            get_default_todo_store().bind_session(session.id)
        return session

    async def _connector_job_for(req: RunRequest) -> ConnectorJob | None:
        if req.connector_id is None and req.connector_token is None:
            return None
        if not req.connector_id or not req.connector_token:
            raise HTTPException(
                status_code=422,
                detail="connector_id and connector_token must be provided together",
            )
        session_id = req.session_id or str(uuid.uuid4())
        try:
            uuid.UUID(session_id)
        except (TypeError, ValueError, AttributeError) as exc:
            raise HTTPException(status_code=422, detail="Invalid connector session id") from exc
        try:
            # The Connector owns the local ActionAuthorizer and prompts its
            # user; the browser cannot send local execution policy.
            return await connector_broker.start_job(
                connector_id=req.connector_id,
                browser_token=req.connector_token,
                task=req.task,
                session_id=session_id,
                confirm_mode=app.state.confirm_mode,
            )
        except ConnectorError as exc:
            _raise_connector_error(exc)
        return None

    # ---- GET / — built-in web UI -----------------------------------------

    _WEBUI_HTML = (Path(__file__).with_name("webui.html")).read_text(encoding="utf-8")

    @app.get("/", include_in_schema=False)
    async def webui():
        return HTMLResponse(_WEBUI_HTML)

    # ---- GET /config / POST /config — browser-supplied credentials ---------

    @app.get("/config", include_in_schema=False)
    async def get_config():
        from synapse.config.schema import OPENROUTER_DEFAULT_MODEL

        rc = app.state.runtime_key
        return {
            "configured": rc is not None,
            "provider": rc["provider"] if rc else None,
            "model": rc["model"] if rc else None,
            "base_url": rc.get("base_url", "") if rc else "",
            "free_model": {"provider": "openrouter", "model": OPENROUTER_DEFAULT_MODEL},
        }

    @app.post("/config", include_in_schema=False)
    async def set_config(cfg: KeyConfig):
        if not cfg.api_key.strip():
            raise HTTPException(status_code=422, detail="api_key 不能为空")
        app.state.runtime_key = {
            "provider": cfg.provider.strip(),
            "model": cfg.model.strip(),
            "api_key": cfg.api_key.strip(),
            "base_url": cfg.base_url.strip(),
        }
        return {"ok": True, "provider": cfg.provider}

    # ---- GET/POST /confirm-mode — 浏览器确认策略 -------------------------
    # ask=每次询问(仅确认受限操作才弹窗);auto=全部允许。全局生效。

    @app.get("/confirm-mode")
    async def get_confirm_mode():
        return {"mode": app.state.confirm_mode}

    @app.post("/confirm-mode")
    async def set_confirm_mode(req: ConfirmModeRequest):
        if req.mode not in ("ask", "auto"):
            raise HTTPException(status_code=422, detail="mode 仅支持 ask / auto")
        app.state.confirm_mode = req.mode
        return {"ok": True, "mode": req.mode}

    # ---- POST /confirm/{request_id} — answer an interactive approval ------

    @app.post("/confirm/{request_id}")
    async def confirm(request_id: str, decision: ConfirmDecision):
        waiter = confirm_waiters.get(request_id)
        if waiter is not None:
            waiter[1] = decision.approve
            event, _, loop = waiter
            loop.call_soon_threadsafe(event.set)
            return {"ok": True}
        # 本地 connector 模式:确认请求由 connector 进程持有,经 broker 命令通道转发
        broker = app.state.connector_broker
        if broker is not None and await broker.resolve_confirm(
            request_id, decision.approve, remember=decision.remember,
        ):
            return {"ok": True}
        raise HTTPException(status_code=404, detail="No pending confirmation")

    @app.on_event("shutdown")
    async def _shutdown_experiments() -> None:
        tasks = [
            record.task for record in experiments.values()
            if record.task is not None and not record.task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for inst in [*synapse_instances.values(),
                     *([synapse_instance] if synapse_instance is not None else [])]:
            close = getattr(inst, "aclose", None)
            if close is not None:
                await close()

    # ---- /health ---------------------------------------------------------

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # ---- Local-workspace Connector ---------------------------------------

    @app.post("/connectors/pair")
    async def create_connector_pairing():
        return connector_broker.create_pairing()

    @app.get("/connectors/local", include_in_schema=False)
    async def local_connector():
        """Return the auto-bound local connector, if ``synapse web`` registered one."""
        local = app.state.local_connector
        if local is None:
            return {"connector_id": None}
        return local

    @app.get("/connectors/pair/{pair_id}")
    async def connector_pairing_status(
        pair_id: str,
        authorization: str | None = Header(default=None),
    ):
        try:
            return connector_broker.pairing_status(pair_id, _bearer_token(authorization))
        except ConnectorError as exc:
            _raise_connector_error(exc)

    @app.post("/connectors/register")
    async def register_connector(
        req: ConnectorRegisterRequest,
        authorization: str | None = Header(default=None),
    ):
        token = None
        if authorization:
            token = _bearer_token(authorization)
        try:
            return connector_broker.register(
                workspace_name=req.workspace_name,
                pair_code=req.pair_code,
                connector_id=req.connector_id,
                device_token=token,
            )
        except ConnectorError as exc:
            _raise_connector_error(exc)

    @app.post("/connectors/{connector_id}/commands")
    async def connector_commands(
        connector_id: str,
        authorization: str | None = Header(default=None),
    ):
        try:
            command = await connector_broker.poll(
                connector_id, _bearer_token(authorization),
            )
            return {"command": command}
        except ConnectorError as exc:
            _raise_connector_error(exc)

    @app.post("/connectors/{connector_id}/events")
    async def connector_events(
        connector_id: str,
        req: ConnectorEventsRequest,
        authorization: str | None = Header(default=None),
    ):
        try:
            await connector_broker.publish_events(
                connector_id=connector_id,
                device_token=_bearer_token(authorization),
                job_id=req.job_id,
                events=req.events,
            )
            return {"ok": True}
        except ConnectorError as exc:
            _raise_connector_error(exc)

    @app.post("/connectors/{connector_id}/complete")
    async def connector_complete(
        connector_id: str,
        req: ConnectorCompleteRequest,
        authorization: str | None = Header(default=None),
    ):
        try:
            await connector_broker.complete_job(
                connector_id=connector_id,
                device_token=_bearer_token(authorization),
                job_id=req.job_id,
                result=req.result,
            )
            return {"ok": True}
        except ConnectorError as exc:
            _raise_connector_error(exc)

    @app.post("/connectors/{connector_id}/disconnect")
    async def connector_disconnect(
        connector_id: str,
        authorization: str | None = Header(default=None),
    ):
        try:
            await connector_broker.disconnect(
                connector_id, _bearer_token(authorization),
            )
            return {"ok": True}
        except ConnectorError as exc:
            _raise_connector_error(exc)

    # ---- POST /run -------------------------------------------------------

    @app.post("/run", response_model=RunResponse)
    async def run_task(req: RunRequest):
        connector_job = await _connector_job_for(req)
        if connector_job is not None:
            payload = await connector_broker.wait_for_completion(connector_job)
            try:
                return RunResponse(**payload)
            except Exception as exc:
                raise HTTPException(
                    status_code=502,
                    detail="Local connector returned an invalid result",
                ) from exc

        if connector_only:
            raise HTTPException(
                status_code=403,
                detail="This server only accepts local Connector jobs",
            )

        session = _resolve_session(req.session_id)
        synapse = _synapse_for(session.id)
        # Non-streamed runs have no confirmation channel, so risky calls deny
        # by default instead of accepting a request-controlled bypass.
        result: AgentResult = await synapse.run(
            req.task, session=session,
        )

        # Store the session so it can be queried later.  Pass the directory
        # explicitly: save()'s default was bound in session.py, so a patched
        # server.DEFAULT_SESSION_DIR would otherwise be ignored.
        sessions[session.id] = session
        with _swallow("run: session save"):
            session.save(DEFAULT_SESSION_DIR)

        return RunResponse(
            status=result.status.value,
            output=result.output,
            session_id=session.id,
            run_id=session.metadata.get("last_run_id", ""),
            artifacts=[
                ArtifactResponse(path=a.path, content=a.content, action=a.action)
                for a in result.artifacts
            ],
                metrics=MetricsResponse(
                tokens_input=result.metrics.tokens_input,
                tokens_output=result.metrics.tokens_output,
                tool_call_count=result.metrics.tool_call_count,
                tool_success_count=result.metrics.tool_success_count,
                duration_ms=result.metrics.duration_ms,
                thrashing_events=result.metrics.thrashing_events,
            ),
            run_score=synapse.get_run_score(),  # L.4
        )

    # ---- POST /run/stream (SSE) -----------------------------------------
    # L.1: expose the same streamed progress the CLI REPL shows, over HTTP, so
    # non-interactive / integrated callers are no longer a black box.

    # L.2: include the swarm lifecycle events so parallel/review/verify loops
    # are visible over SSE, not just the basic stream.
    _STREAM_EVENTS = (
        "agent_progress",
        "llm_token",
        "tool_call_started",
        "tool_call_completed",
        "background_result",
        "worker_spawned",
        "worker_completed",
        "review_submitted",
        "vote_cast",
        "swarm_verified",
        "todo_updated",
    )

    @app.post("/run/stream")
    async def run_task_stream(req: RunRequest):
        connector_job = await _connector_job_for(req)
        if connector_job is not None:
            async def _connector_event_stream():
                # 抢在首个 connector 事件前把 session_id 交给前端,侧栏立即显示
                # 当前会话(与纯 web 模式一致)。
                yield f"data: {json.dumps({'type': 'session', 'session_id': connector_job.session_id}, ensure_ascii=False)}\n\n"
                try:
                    while True:
                        item = await connector_broker.next_event(connector_job)
                        item.setdefault("session_id", connector_job.session_id)
                        yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                        if item["type"] in ("done", "error"):
                            break
                finally:
                    # The browser is the only consumer of this job.  Tell the
                    # local process to stop rather than leave it spending model
                    # budget after an abandoned tab.
                    if not connector_job.completion.done():
                        await connector_broker.cancel_job(connector_job)

            return StreamingResponse(
                _connector_event_stream(), media_type="text/event-stream",
            )

        if connector_only:
            raise HTTPException(
                status_code=403,
                detail="This server only accepts local Connector jobs",
            )

        session = _resolve_session(req.session_id)
        synapse = _synapse_for(session.id)
        event_bus = synapse._container.resolve(EventBus)
        queue: asyncio.Queue = asyncio.Queue()

        async def _on_event(event):
            # Transport-agnostic dump of the event: every public field, with
            # datetimes serialized so the SSE payload stays JSON-safe.
            data = {k: v for k, v in event.__dict__.items() if not k.startswith("_")}
            data["event_type"] = event.event_type
            for k, v in list(data.items()):
                if hasattr(v, "isoformat"):
                    data[k] = v.isoformat()
            await queue.put({"type": "event", "event": data, "session_id": session.id})

        subscribed = []
        if event_bus is not None:
            for et in _STREAM_EVENTS:
                event_bus.subscribe(et, _on_event)
                subscribed.append(et)

        # 待办变化经全局 TodoStore 单例推回 SSE:运行期实时刷新侧栏待办。
        # ponytail: 单用户单进程下安全;并发多 session 会互相覆盖 sink。
        todo_sink = None
        if event_bus is not None:
            def _todo_sink(sid: str, todos: list) -> None:
                # emit 是协程,set_todos 在 run 的事件循环内同步调用,需 fire-and-forget。
                asyncio.ensure_future(
                    event_bus.emit(TodoUpdated(session_id=sid, todos=todos))
                )
            todo_sink = _todo_sink
            get_default_todo_store().set_event_sink(todo_sink)

        async def _run():
            try:
                res = await synapse.run(
                    req.task, session=session,
                    confirm_callback=_make_confirm_bridge(queue.put),
                )
                report = synapse.get_citation_report()
                if report is not None:
                    session.metadata["citation_report"] = report
                sessions[session.id] = session
                with _swallow("run/stream: session save"):
                    session.save(DEFAULT_SESSION_DIR)
                await queue.put({"type": "done", "result": {
                    "status": res.status.value,
                    "output": res.output,
                    "session_id": session.id,
                    "run_id": session.metadata.get("last_run_id", ""),
                    "artifacts": [
                        {"path": a.path, "content": a.content, "action": a.action}
                        for a in res.artifacts
                    ],
                    "metrics": {
                        "tokens_input": res.metrics.tokens_input,
                        "tokens_output": res.metrics.tokens_output,
                        "tool_call_count": res.metrics.tool_call_count,
                        "tool_success_count": res.metrics.tool_success_count,
                        "duration_ms": res.metrics.duration_ms,
                        "thrashing_events": res.metrics.thrashing_events,
                    },
                    "run_score": synapse.get_run_score(),  # L.4
                }})
            except Exception as exc:
                await queue.put({"type": "error", "error": f"{type(exc).__name__}: {exc}"})
            finally:
                for et in subscribed:
                    try:
                        event_bus.unsubscribe(et, _on_event)
                    except Exception:
                        pass
                # 无论正常结束还是断开,都摘掉待办事件汇点,避免残留 sink。
                get_default_todo_store().set_event_sink(None)

        async def _event_stream():
            # 抢在第一个 agent 事件前把 session_id 交给前端,侧栏立即显示当前会话。
            await queue.put({"type": "session", "session_id": session.id})
            run_task = asyncio.create_task(_run())
            try:
                while True:
                    item = await queue.get()
                    yield f"data: {json.dumps(item, ensure_ascii=False)}\n\n"
                    if item["type"] in ("done", "error"):
                        break
                await run_task
            finally:
                # Starlette closes the async generator when the SSE client
                # disconnects. Cancel the matching agent run so it does not
                # continue spending tokens and mutating files in the background.
                if not run_task.done():
                    run_task.cancel()
                await asyncio.gather(run_task, return_exceptions=True)

        return StreamingResponse(_event_stream(), media_type="text/event-stream")

    # ---- POST /run/stop — browser-initiated interrupt -------------------
    # 本地 connector 模式:取消对应 connector 的活跃任务(命令通道转发到本地进程)

    @app.post("/run/stop")
    async def stop_run(req: StopRequest):
        broker = app.state.connector_broker
        if not req.connector_id or not req.connector_token:
            raise HTTPException(status_code=422, detail="缺少 connector 凭据")
        try:
            job = broker.active_job_for(req.connector_id, req.connector_token)
        except ConnectorError as exc:
            _raise_connector_error(exc)
        if job is None or job.completion.done():
            return {"ok": True, "cancelled": False}
        await broker.cancel_job(job, reason="已在网页端手动停止", stopped=True)
        return {"ok": True, "cancelled": True}

    # ---- GET /sessions/{session_id} --------------------------------------

    @app.get("/sessions/{session_id}", response_model=SessionInfo)
    async def get_session(session_id: str):
        session = sessions.get(session_id)
        if session is None:
            # connector 会话只在磁盘上,不在进程内存缓存
            p = DEFAULT_SESSION_DIR / f"{session_id}.json"
            if p.exists():
                session = Session.load(p)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return SessionInfo(
            id=session.id,
            message_count=len(session.messages),
            estimated_tokens=session.estimated_tokens,
            metadata=session.metadata,
        )

    # ---- GET /sessions/{session_id}/messages -----------------------------

    @app.get("/sessions/{session_id}/messages", response_model=list[MessageResponse])
    async def get_session_messages(session_id: str):
        session = sessions.get(session_id)
        if session is None:
            p = DEFAULT_SESSION_DIR / f"{session_id}.json"
            if p.exists():
                session = Session.load(p)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return [
            MessageResponse(role=m.role, content=m.content)
            for m in session.messages
        ]

    # ---- GET /sessions — 历史会话列表 (WebUI 侧栏) ------------------------

    @app.get("/sessions", response_model=list)
    async def list_sessions():
        out = []
        seen = set()
        for s in Session.list_sessions():
            # 过滤空会话:未持久化消息历史的会话无法恢复,留着只会刷屏
            if not s.messages:
                continue
            path = DEFAULT_SESSION_DIR / f"{s.id}.json"
            mtime = path.stat().st_mtime if path.exists() else 0
            out.append({
                "id": s.id,
                "message_count": len(s.messages),
                "estimated_tokens": s.estimated_tokens,
                "modified_at": mtime,
            })
            seen.add(s.id)
        # 运行中但尚未落盘的会话(在内存里)也补进列表,侧栏才能立即显示当前会话。
        for sid, s in sessions.items():
            if sid in seen or not s.messages:
                continue
            out.append({
                "id": sid,
                "message_count": len(s.messages),
                "estimated_tokens": s.estimated_tokens,
                "modified_at": time.time(),
            })
        # connector 模式运行中的会话由本地 connector 进程持有,不在服务端
        # sessions 里,靠 broker 的活跃任务补进列表,侧栏才能高亮当前会话。
        broker = app.state.connector_broker
        if broker is not None:
            for sid in broker.active_session_ids():
                if sid in seen:
                    continue
                out.append({
                    "id": sid,
                    "message_count": 0,
                    "estimated_tokens": 0,
                    "modified_at": time.time(),
                })
                seen.add(sid)
        return out

    # ---- DELETE /sessions/{session_id} — 删除会话 (WebUI 侧栏) ------------

    @app.delete("/sessions/{session_id}")
    async def delete_session(session_id: str):
        path = DEFAULT_SESSION_DIR / f"{session_id}.json"
        if not path.exists():
            raise HTTPException(status_code=404, detail="Session not found")
        with _swallow("delete session file"):
            path.unlink()
        todo_path = DEFAULT_TODO_DIR / f"{session_id}.json"
        with _swallow("delete todo file"):
            if todo_path.exists():
                todo_path.unlink()
        sessions.pop(session_id, None)
        synapse_instances.pop(session_id, None)
        return {"ok": True}

    # ---- GET /status — 工作区状态栏 (WebUI) -------------------------------

    @app.get("/status")
    async def get_status(session_id: str | None = None):
        ov = app.state.session_runtime.get(session_id) if session_id else None
        rc = app.state.runtime_key
        # 模型以 web 端全局配置为准;本会话运行时仅覆盖 mode。
        provider = rc["provider"] if rc else None
        model = rc["model"] if rc else None
        mode = (ov or {}).get("mode")
        config, _ = load_config()
        if provider is None:
            provider = config.provider.provider
        if model is None:
            model = config.provider.model
        if mode is None:
            mode = config.planning.mode
        used_tokens = 0
        if session_id:
            sess = sessions.get(session_id)
            if sess is None:
                p = DEFAULT_SESSION_DIR / f"{session_id}.json"
                if p.exists():
                    sess = Session.load(p)
            if sess is not None:
                used_tokens = sess.estimated_tokens
        # 连了本地 connector 时,真实执行目录在 connector 进程的工作区,
        # 而非本服务端进程 cwd——按连接动态显示。
        local = app.state.local_connector
        connector_ws = (local or {}).get("workspace") if local else None
        workspace = connector_ws or str(Path.cwd())
        return {
            "version": __version__,
            "provider": provider,
            "model": model,
            "mode": mode,
            "workspace": workspace,
            "workspace_source": "connector" if connector_ws else "server",
            "tools_count": len(getattr(config.tools, "enabled", []) or []),
            "configured": rc is not None,
            "used_tokens": used_tokens,
            "budget": config.planning.max_tokens_per_task,
        }

    # ---- GET /sessions/{session_id}/context-report — 上下文引用热力图 -----

    @app.get("/sessions/{session_id}/context-report")
    async def get_context_report(session_id: str):
        # 报告在运行后持久化到 session.metadata,这里读磁盘而非新建 synapse
        # (新建实例没有 citation 追踪器,会永远返回空)
        path = DEFAULT_SESSION_DIR / f"{session_id}.json"
        if not path.exists():
            return {"blocks": []}
        session = Session.load(path)
        return session.metadata.get("citation_report") or {"blocks": []}

    # ---- GET /todos — 待办清单 (WebUI 侧栏) -------------------------------

    @app.get("/todos", response_model=list)
    async def get_todos(session_id: str):
        # Read-only: a fresh store (not the global singleton) avoids the
        # per-session todo list being clobbered by a concurrent session.
        store = TodoStore()
        store.bind_session(session_id)
        return store.list()

    # ---- POST /sessions/{session_id}/config — 运行期切换模型/模式 ---------

    @app.post("/sessions/{session_id}/config")
    async def set_session_config(session_id: str, cfg: SessionConfig):
        ov = dict(app.state.session_runtime.get(session_id) or {})
        if cfg.mode is not None:
            if cfg.mode not in _VALID_MODES:
                raise HTTPException(status_code=422, detail="未知规划模式：" + cfg.mode)
            ov["mode"] = cfg.mode
        # Evict the cached instance so the next run rebuilds with the new
        # config; best-effort async cleanup (fire-and-forget) on the loop.
        inst = synapse_instances.pop(session_id, None)
        if inst is not None:
            with _swallow("evict synapse instance on config change"):
                try:
                    asyncio.ensure_future(inst.aclose())
                except Exception:
                    pass
        app.state.session_runtime[session_id] = ov
        return {"ok": True, "config": ov}

    # ---- POST /eval/experiment -------------------------------------------

    @app.post("/eval/experiment", response_model=dict)
    async def start_experiment(req: ExperimentRequest):
        if connector_only:
            raise HTTPException(
                status_code=403,
                detail="This server only accepts local Connector jobs",
            )
        from synapse.eval.runner import _fingerprint

        try:
            _validate_remote_experiment_config(req.agent_config_a, _path="agent_config_a")
            _validate_remote_experiment_config(req.agent_config_b, _path="agent_config_b")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid experiment configuration") from exc

        experiment_id = str(uuid.uuid4())
        benchmark = _make_benchmark(req.benchmark_task)
        workspace_stack = ExitStack()
        baseline_id = _fingerprint({
            "kind": "empty_workspace",
            "task": req.benchmark_task,
        })

        def workspace_factory(*, label: str, task_id: str, attempt: int) -> dict:
            path = workspace_stack.enter_context(
                tempfile.TemporaryDirectory(
                    prefix=f"synapse-server-experiment-{label.lower()}-{attempt}-",
                ),
            )
            return {"path": path, "baseline_id": baseline_id}

        try:
            metric_directions = {
                **_EXPERIMENT_METRIC_DIRECTIONS,
                **req.metric_directions,
            }
            experiment = Experiment(
                id=experiment_id,
                name=req.name,
                variables=req.variables,
                agent_config_a=req.agent_config_a,
                agent_config_b=req.agent_config_b,
                benchmark=benchmark,
                effective_config_a=await _effective_experiment_config(req.agent_config_a),
                effective_config_b=await _effective_experiment_config(req.agent_config_b),
                runs_per_config=req.runs_per_config,
                primary_metric=req.primary_metric,
                direction=(
                    req.direction
                    or metric_directions[req.primary_metric]
                ),
                seed=req.seed,
                metric_directions=metric_directions,
                guardrail_metrics=tuple(
                    metric for metric in req.guardrail_metrics
                    if metric != req.primary_metric
                ),
                allowed_config_diff_paths=(
                    tuple(req.allowed_config_diff_paths)
                    if req.allowed_config_diff_paths else None
                ),
                workspace_factory=workspace_factory,
            )
        except (TypeError, ValueError, RuntimeError) as exc:
            workspace_stack.close()
            raise HTTPException(
                status_code=422,
                detail="Invalid experiment configuration",
            ) from exc

        record = ExperimentRecord(id=experiment_id, status="running")
        experiments[experiment_id] = record

        async def _run_experiment():
            try:
                async with experiment_lock:
                    result = await experiment.run()
                record.result = result
                record.status = "completed"
            except asyncio.CancelledError:
                record.status = "cancelled"
                record.error = "Experiment cancelled"
                raise
            except Exception as exc:
                record.status = "failed"
                record.error = f"{type(exc).__name__}: {exc}"
            finally:
                try:
                    workspace_stack.close()
                except Exception as exc:
                    record.status = "failed"
                    cleanup_error = (
                        f"Workspace cleanup failed: {type(exc).__name__}"
                    )
                    record.error = (
                        f"{record.error}; {cleanup_error}"
                        if record.error else cleanup_error
                    )
                record.task = None

        record.task = asyncio.create_task(_run_experiment())

        return {"experiment_id": experiment_id}

    # ---- GET /eval/experiment/{experiment_id} ----------------------------

    @app.get("/eval/experiment/{experiment_id}", response_model=ExperimentStatusResponse)
    async def get_experiment(experiment_id: str):
        record = experiments.get(experiment_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Experiment not found")

        result_dict: dict[str, Any] | None = None
        if record.result is not None:
            result_dict = record.result.to_dict()
            result_dict["model_sampling_seed_controlled"] = False

        return ExperimentStatusResponse(
            id=record.id,
            status=record.status,
            result=result_dict,
            error=record.error,
        )

    return app


# ---------------------------------------------------------------------------
# Module-level app instance (for uvicorn)
# ---------------------------------------------------------------------------

app: FastAPI = create_app()

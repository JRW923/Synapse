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
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field as PydanticField

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
from synapse.core.events import EventBus
from synapse.core.session import DEFAULT_SESSION_DIR, Session
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


class ConfirmDecision(BaseModel):
    approve: bool


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

    # request_id -> [asyncio.Event, approved|None] for in-flight confirmations.
    # The Event lives on the run's event loop; /confirm may arrive on another
    # thread's loop, so the wakeup must go through call_soon_threadsafe.
    confirm_waiters: dict[str, list] = {}
    app.state.confirm_waiters = confirm_waiters

    def _synapse_for(session_id: str) -> Synapse:
        """Per-session facade; an injected instance (tests) is always reused."""
        if synapse_instance is not None:
            return synapse_instance
        inst = synapse_instances.get(session_id)
        if inst is None:
            inst = Synapse()
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
            return Session()
        if session_id in sessions:
            return sessions[session_id]
        path = DEFAULT_SESSION_DIR / f"{session_id}.json"
        if path.exists():
            return Session.load(path)
        raise HTTPException(status_code=404, detail="Session not found")

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
            )
        except ConnectorError as exc:
            _raise_connector_error(exc)
        return None

    # ---- GET / — built-in web UI -----------------------------------------

    _WEBUI_HTML = (Path(__file__).with_name("webui.html")).read_text(encoding="utf-8")

    @app.get("/", include_in_schema=False)
    async def webui():
        return HTMLResponse(_WEBUI_HTML)

    # ---- POST /confirm/{request_id} — answer an interactive approval ------

    @app.post("/confirm/{request_id}")
    async def confirm(request_id: str, decision: ConfirmDecision):
        waiter = confirm_waiters.get(request_id)
        if waiter is None:
            raise HTTPException(status_code=404, detail="No pending confirmation")
        waiter[1] = decision.approve
        event, _, loop = waiter
        loop.call_soon_threadsafe(event.set)
        return {"ok": True}

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
    )

    @app.post("/run/stream")
    async def run_task_stream(req: RunRequest):
        connector_job = await _connector_job_for(req)
        if connector_job is not None:
            async def _connector_event_stream():
                try:
                    while True:
                        item = await connector_broker.next_event(connector_job)
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
            await queue.put({"type": "event", "event": data})

        subscribed = []
        if event_bus is not None:
            for et in _STREAM_EVENTS:
                event_bus.subscribe(et, _on_event)
                subscribed.append(et)

        async def _run():
            try:
                res = await synapse.run(
                    req.task, session=session,
                    confirm_callback=_make_confirm_bridge(queue.put),
                )
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

        async def _event_stream():
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

    # ---- GET /sessions/{session_id} --------------------------------------

    @app.get("/sessions/{session_id}", response_model=SessionInfo)
    async def get_session(session_id: str):
        session = sessions.get(session_id)
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
            raise HTTPException(status_code=404, detail="Session not found")
        return [
            MessageResponse(role=m.role, content=m.content)
            for m in session.messages
        ]

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

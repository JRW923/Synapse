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
import uuid
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from synapse.adapters.library import Synapse
from synapse.core.session import Session
from synapse.protocols.planner import AgentResult, ExecutionMetrics
from synapse.eval.experiments import Experiment, ExperimentResult


# ---------------------------------------------------------------------------
# Pydantic models for request / response
# ---------------------------------------------------------------------------


class RunRequest(BaseModel):
    task: str


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
    artifacts: list[ArtifactResponse] = []
    metrics: MetricsResponse = MetricsResponse()


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
    variables: dict[str, Any] = {}
    agent_config_a: dict[str, Any]
    agent_config_b: dict[str, Any]
    benchmark_task: str = "Say hello."
    runs_per_config: int = 5


class ExperimentStatusResponse(BaseModel):
    id: str
    status: str  # "running" | "completed"
    result: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# In-memory stores
# ---------------------------------------------------------------------------


@dataclass
class ExperimentRecord:
    id: str
    status: str  # "running" | "completed"
    result: ExperimentResult | None = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)


# ---------------------------------------------------------------------------
# Benchmark helper for experiments
# ---------------------------------------------------------------------------


def _make_benchmark(task_description: str):
    """Return an async callable suitable for ``Experiment.benchmark``.

    The callable creates a fresh ``Synapse`` instance from *config*,
    runs *task_description*, and returns ``duration_ms`` as the metric
    (lower is better).
    """

    async def _run(config: dict[str, Any]) -> float:
        synapse = Synapse(**config)
        result = await synapse.run(task_description)
        return float(result.metrics.duration_ms)

    return _run


# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(synapse_instance: Synapse | None = None) -> FastAPI:
    """Build and return a configured FastAPI application.

    Parameters
    ----------
    synapse_instance:
        An optional pre-configured :class:`Synapse` instance.  When not
        provided a default ``Synapse(provider="anthropic")`` is used.
        Tests should inject a mock.
    """
    app = FastAPI(title="Synapse API", version="0.1.0")

    # ---- state -----------------------------------------------------------
    sessions: dict[str, Session] = {}
    experiments: dict[str, ExperimentRecord] = {}

    synapse = synapse_instance if synapse_instance is not None else Synapse()

    # ---- /health ---------------------------------------------------------

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # ---- POST /run -------------------------------------------------------

    @app.post("/run", response_model=RunResponse)
    async def run_task(req: RunRequest):
        session = Session()
        result: AgentResult = await synapse.run(req.task, session=session)

        # Store the session so it can be queried later
        sessions[session.id] = session

        return RunResponse(
            status=result.status.value,
            output=result.output,
            session_id=session.id,
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
        )

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
        experiment_id = str(uuid.uuid4())
        benchmark = _make_benchmark(req.benchmark_task)

        experiment = Experiment(
            id=experiment_id,
            name=req.name,
            variables=req.variables,
            agent_config_a=req.agent_config_a,
            agent_config_b=req.agent_config_b,
            benchmark=benchmark,
            runs_per_config=req.runs_per_config,
        )

        record = ExperimentRecord(id=experiment_id, status="running")
        experiments[experiment_id] = record

        async def _run_experiment():
            try:
                result = await experiment.run()
                record.result = result
                record.status = "completed"
            except Exception:
                record.status = "completed"
                record.result = ExperimentResult(
                    experiment_id=experiment_id,
                    experiment_name=req.name,
                )

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
            result_dict = {
                "experiment_id": record.result.experiment_id,
                "experiment_name": record.result.experiment_name,
                "metrics_a": record.result.metrics_a,
                "metrics_b": record.result.metrics_b,
                "p_value": record.result.p_value,
                "winner": record.result.winner,
            }

        return ExperimentStatusResponse(
            id=record.id,
            status=record.status,
            result=result_dict,
        )

    return app


# ---------------------------------------------------------------------------
# Module-level app instance (for uvicorn)
# ---------------------------------------------------------------------------

app: FastAPI = create_app()

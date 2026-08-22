"""Synapse facade — clean Python API for agent creation and execution.

Usage:
    from synapse import Synapse

    agent = Synapse()
    result = await agent.run("Fix the bug in auth.py")
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import hashlib
import json
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

from synapse.config import load_config
from synapse.core.container import Container
from synapse.core.agent import Agent
from synapse.core.session import Session
from synapse.core.events import EventBus
from synapse.protocols.events import BaseEvent, EventType

from synapse.protocols.llm import LLMProvider
from synapse.protocols.planner import Planner, AgentResult, PlanningMode
from synapse.protocols.tool import ToolRegistry
from synapse.protocols.memory import MemoryStore, MemoryEntry, MemoryLevel, MemoryMetadata
from synapse.core.evaluation import EvaluationAblations
from synapse.eval.ablations import DisabledMemoryStore
from synapse.eval.metrics import RunScore
from synapse.protocols.retriever import ContextRetriever
from synapse.protocols.sandbox import Sandbox

from synapse.modules.tools.registry import DefaultToolRegistry
from synapse.modules.tools.file_read import ReadTool
from synapse.modules.tools.file_write import WriteTool
from synapse.modules.tools.file_edit import EditTool
from synapse.modules.tools.file_glob import GlobTool
from synapse.modules.tools.search_grep import GrepTool
from synapse.modules.tools.shell import ShellTool
from synapse.modules.tools.git_ import GitTool

# External tools — optional dependencies (httpx, playwright, sqlite3)
try:
    from synapse.modules.tools.web import HTTPTool, WebFetchTool
except ImportError:  # pragma: no cover
    HTTPTool = None  # type: ignore[assignment]
    WebFetchTool = None  # type: ignore[assignment]

try:
    from synapse.modules.tools.web_search import WebSearchTool
except ImportError:  # pragma: no cover
    WebSearchTool = None  # type: ignore[assignment]

try:
    from synapse.modules.tools.db import DBTool
except ImportError:  # pragma: no cover
    DBTool = None  # type: ignore[assignment]

try:
    from synapse.modules.tools.browser import BrowserTool
except ImportError:  # pragma: no cover
    BrowserTool = None  # type: ignore[assignment]

from synapse.modules.memory.session import SessionMemory
from synapse.modules.memory.project import ProjectMemory
from synapse.modules.memory.user import UserMemory
from synapse.adapters.memory_layer import LayeredMemory

# NOTE: SemanticMemory (chromadb) and QdrantMemory are imported lazily inside
# _create_semantic_memory().  At module scope they cost ~1.3s of cold start on
# every `synapse` invocation — including `--help` — for a layer nothing writes
# to unless the user explicitly uses SEMANTIC-level memory.

from synapse.modules.context.retriever import BasicContextRetriever
from synapse.modules.context.partitioner import ContextPartitioner
from synapse.modules.context.compactor import ContextCompactor

from synapse.modules.security.sandbox import ProcessSandbox
from synapse.modules.security.auth import ActionAuthorizer
from synapse.modules.security.audit import AuditLogger
from synapse.modules.security.injection import InjectionGuard

# Planners — lightweight (protocols only, no SDKs).
from synapse.modules.planning.react import ReActPlanner
from synapse.modules.planning.plan_execute import PlanExecutePlanner
from synapse.modules.planning.hierarchical import HierarchicalPlanner
from synapse.modules.planning.swarm import SwarmPlanner
from synapse.modules.tools.background import BackgroundTaskManager, get_default_manager
from synapse.modules.tools.skill_tool import SkillTool
from synapse.modules.skill import get_default_skill_loader
from synapse.modules.tools.todo_tool import TodoWriteTool, TodoReadTool
from synapse.modules.todo import TodoStore, get_default_todo_store

# MCP config type (lightweight dataclass).
from synapse.protocols.mcp import McpServerConfig


# ---- Provider registry -----------------------------------------------------
# Resolved lazily so that tests can patch module-level names after import.


def _resolve_provider(name: str, custom_providers: list | None = None, model: str = ""):
    """Return *(provider_class, base_url_override)* for *name*.

    Handles model-aware routing: all ``deepseek-v4-*`` models use the
    Anthropic protocol against ``api.deepseek.com/anthropic``.
    """
    # DeepSeek v4 models → Anthropic protocol on the Anthropic-compatible endpoint
    if name == "deepseek" and model.startswith("deepseek-v4"):
        import importlib
        mod = importlib.import_module("synapse.modules.providers.anthropic")
        return getattr(mod, "AnthropicProvider"), "https://api.deepseek.com/anthropic"

    # OpenRouter → OpenAI-compatible protocol via the OpenRouter gateway
    if name == "openrouter":
        import importlib
        from synapse.config.schema import OPENROUTER_BASE_URL
        mod = importlib.import_module("synapse.modules.providers.openai")
        return getattr(mod, "OpenAIProvider"), OPENROUTER_BASE_URL

    _provider_modules: dict[str, str] = {
        "anthropic": "synapse.modules.providers.anthropic",
        "openai": "synapse.modules.providers.openai",
        "deepseek": "synapse.modules.providers.deepseek",
        "google": "synapse.modules.providers.google",
        "ollama": "synapse.modules.providers.ollama",
    }
    _provider_classes: dict[str, str] = {
        "anthropic": "AnthropicProvider",
        "openai": "OpenAIProvider",
        "deepseek": "DeepSeekProvider",
        "google": "GoogleProvider",
        "ollama": "OllamaProvider",
    }

    # Check custom providers first
    base_url = None
    if custom_providers:
        for cp in custom_providers:
            if cp.name == name:
                protocol = getattr(cp, "protocol", "openai") or "openai"
                base_url = cp.base_url
                if protocol == "anthropic":
                    name = "anthropic"
                else:
                    name = "openai"
                break

    mod_name = _provider_modules.get(name)
    cls_name = _provider_classes.get(name)
    if mod_name is None or cls_name is None:
        available = ", ".join(sorted(_provider_classes))
        raise ValueError(f"Unknown provider '{name}'.  Available: {available}")

    import importlib

    try:
        mod = importlib.import_module(mod_name)
    except ImportError as exc:
        raise ImportError(
            f"Provider '{name}' is not available — the required SDK "
            f"is not installed.  ({exc})"
        ) from exc
    return getattr(mod, cls_name), base_url


# ---- Synapse facade --------------------------------------------------------

_UI_EVENTS = (
    EventType.PLAN_CREATED,
    EventType.TASK_DECOMPOSED,
    EventType.MERGE_RESULT,
    EventType.TOOL_CALL_STARTED,
    EventType.TOOL_CALL_COMPLETED,
    EventType.BACKGROUND_RESULT,
    EventType.WORKER_SPAWNED,
    EventType.WORKER_COMPLETED,
    EventType.REVIEW_SUBMITTED,
    EventType.VOTE_CAST,
    EventType.SWARM_VERIFIED,
)


def _clip_ui(value: object, limit: int = 80) -> str:
    text = " ".join(str(value or "").split())
    return text[: limit - 3] + "..." if len(text) > limit else text


def _ui_args(params: object) -> str:
    if not isinstance(params, dict):
        return ""
    parts: list[str] = []
    for key, value in list(params.items())[:8]:
        lowered = str(key).lower()
        if any(tag in lowered for tag in (
            "api_key", "apikey", "token", "authorization", "password", "secret", "cookie",
        )):
            parts.append(f"{key}=<redacted>")
            continue
        try:
            text = value if isinstance(value, str) else json.dumps(
                value, ensure_ascii=False, default=str,
            )
        except TypeError:
            text = str(value)
        parts.append(f"{key}={_clip_ui(text)}")
    return ", ".join(parts)


def _new_run_ui() -> dict[str, Any]:
    return {
        "tools": [],
        "plan": None,
        "swarm": {"workers": {}, "reviews": [], "votes": [], "verified": None},
        "_args": {},
    }


def _public_swarm(swarm: dict[str, Any]) -> dict[str, Any] | None:
    workers = swarm.get("workers") or {}
    reviews = swarm.get("reviews") or []
    votes = swarm.get("votes") or []
    verified = swarm.get("verified")
    if not workers and not reviews and not votes and verified is None:
        return None
    return {
        "workers": [{"id": key, **value} for key, value in workers.items()],
        "reviews": reviews,
        "votes": votes,
        "verified": verified,
    }


def _capture_run_ui(ui: dict[str, Any], event: BaseEvent) -> None:
    et = event.event_type
    if et == EventType.PLAN_CREATED:
        ui["plan"] = {
            "steps": list(getattr(event, "plan_steps", None) or []),
            "reasoning": getattr(event, "reasoning", "") or "",
        }
    elif et == EventType.TASK_DECOMPOSED:
        count = getattr(event, "subtask_count", 0) or len(getattr(event, "subtask_ids", None) or [])
        ui["plan"] = {
            "steps": [{"step_id": "分解", "description": f"已拆分为 {count} 个子任务"}],
            "reasoning": "",
        }
    elif et == EventType.MERGE_RESULT:
        count = getattr(event, "subtask_count", 0)
        ui["plan"] = {
            "steps": [{"step_id": "汇总", "description": f"已合并 {count} 个子任务结果"}],
            "reasoning": "",
        }
    elif et == EventType.TOOL_CALL_STARTED:
        ui["_args"][getattr(event, "tool_name", "")] = _ui_args(getattr(event, "tool_params", {}) or {})
    elif et == EventType.TOOL_CALL_COMPLETED:
        name = getattr(event, "tool_name", "") or "tool"
        files = getattr(event, "files_touched", None) or []
        suffix = f" · 文件 {len(files)}" if files else ""
        sandbox = " · 沙箱拒绝" if getattr(event, "sandbox_violation", False) else ""
        ui["tools"].append({
            "name": name,
            "success": bool(getattr(event, "success", False)),
            "args": ui["_args"].pop(name, "") or _clip_ui(getattr(event, "error", "") or ""),
            "meta": f"{int(getattr(event, 'duration_ms', 0) or 0)}ms{suffix}{sandbox}",
        })
    elif et == EventType.BACKGROUND_RESULT:
        task_id = str(getattr(event, "task_id", "") or "")[:8]
        ui["tools"].append({
            "name": "bg:" + task_id,
            "success": bool(getattr(event, "success", False)),
            "args": _clip_ui(getattr(event, "stdout", "") or getattr(event, "stderr", "") or ""),
            "meta": "",
        })
    elif et == EventType.WORKER_SPAWNED:
        wid = getattr(event, "agent_id", "") or getattr(event, "role", "") or "worker"
        ui["swarm"]["workers"][wid] = {
            "role": getattr(event, "role", "") or "worker",
            "status": "running",
        }
    elif et == EventType.WORKER_COMPLETED:
        wid = getattr(event, "agent_id", "") or getattr(event, "role", "") or "worker"
        prev = ui["swarm"]["workers"].get(wid) or {}
        ui["swarm"]["workers"][wid] = {
            "role": getattr(event, "role", "") or prev.get("role") or "worker",
            "status": getattr(event, "status", "") or "completed",
        }
    elif et == EventType.REVIEW_SUBMITTED:
        ui["swarm"]["reviews"].append({"verdict": getattr(event, "verdict", "") or ""})
    elif et == EventType.VOTE_CAST:
        ui["swarm"]["votes"].append({"decision": getattr(event, "decision", "") or ""})
    elif et == EventType.SWARM_VERIFIED:
        ui["swarm"]["verified"] = getattr(event, "status", "") or "unknown"


class Synapse:
    """High-level facade for the Synapse agent framework.

    Wraps Container assembly and Agent creation so callers only need a
    single import and two method calls::

        from synapse import Synapse

        agent = Synapse()  # uses ~/.synapse/models.json
        result = await agent.run("Fix the bug in auth.py")
        # -- or synchronously --
        result = agent.run_sync("Fix the bug in auth.py")

    Parameters
    ----------
    provider:
        Optional LLM provider override. If omitted, use the default from
        ``~/.synapse/models.json`` (or the legacy YAML fallback).
    model:
        Optional model override. If omitted, use the persisted default model.
    config_path:
        Path to a YAML configuration file.  If *None*, the default lookup
        order is used (``synapse.yaml``, then ``~/.synapse/config.yaml``).
    enable_eval:
        If ``True``, isolate persistent memory and disable run-score writes so
        benchmark attempts do not influence later attempts.
    eval_ablation:
        Evaluation-only module switches. Keys are ``context``, ``memory``,
        ``completion_gate`` and ``action_auth``; all default to ``True``.
    memory_backend:
        Semantic memory backend.  One of ``"chromadb"`` (default) or
        ``"qdrant"``.
    enable_external_tools:
        If ``True``, register HTTPTool, DBTool, and BrowserTool in the tool
        registry.  These tools have ``RiskLevel.EXTERNAL`` and are disabled
        by default for safety.  Requires corresponding optional dependencies
        (``httpx``, ``playwright``).
    mcp_servers:
        Optional list of :class:`~synapse.protocols.mcp.McpServerConfig`
        objects describing MCP servers to connect.  Each server's tools are
        registered in the tool registry under ``mcp.<server_name>.<tool>``
        names.  Servers are connected during container assembly (at
        construction time).
    **overrides:
        Additional keyword arguments are applied as overrides on top of the
        loaded configuration.  Supported keys include any field on
        any top-level config section (e.g. ``max_tokens``, ``max_iterations``,
        ``workspace_root``, ``total_tokens``).
    """

    def __init__(
        self,
        provider: str | None = None,
        model: str | None = None,
        config_path: str | None = None,
        enable_eval: bool = False,
        eval_ablation: EvaluationAblations | dict[str, bool] | None = None,
        memory_backend: str = "chromadb",
        enable_external_tools: bool = False,
        mcp_servers: list[McpServerConfig] | None = None,
        confirm_callback=None,  # async (AuthRequest) -> bool
        strict_overrides: bool = False,
        **overrides: Any,
    ) -> None:
        if eval_ablation is not None and not enable_eval:
            raise ValueError("eval_ablation requires enable_eval=True")
        self._enable_eval = enable_eval
        self._eval_ablation = EvaluationAblations.from_value(eval_ablation)
        self._background_manager = (
            BackgroundTaskManager() if enable_eval else get_default_manager()
        )
        self._todo_store = TodoStore() if enable_eval else get_default_todo_store()
        self._eval_memory_dir = (
            tempfile.TemporaryDirectory(prefix="synapse-eval-memory-")
            if enable_eval and self._eval_ablation.memory else None
        )
        if memory_backend.lower() not in {"chromadb", "qdrant"}:
            raise ValueError(
                f"Unknown memory_backend '{memory_backend}'. "
                f"Available: chromadb, qdrant"
            )
        self._memory_backend = memory_backend
        self._enable_external_tools = enable_external_tools
        self._mcp_servers = mcp_servers
        self._mcp_manager = None  # populated in _build_container; connected lazily in run()
        self._confirm_callback = confirm_callback
        # Synapse owns shared planner/metrics instances; serialize runs until
        # those components become per-run state objects.
        self._run_lock = asyncio.Lock()
        self._config = self._load_config(
            config_path, provider, model, overrides, strict_overrides,
        )
        if enable_eval:
            if self._config.hooks.hooks:
                raise RuntimeError(
                    "evaluation runs disable host lifecycle hooks; "
                    "move hooks outside the measured process"
                )
            if self._config.plugins.paths:
                raise RuntimeError(
                    "evaluation runs disable host plugins; "
                    "use a trusted harness image instead"
                )
        self._provider_name = self._config.provider.provider
        self._container = self._build_container()
        self._last_run_score = None  # populated by run()
        self._last_process_hint = None  # L.4 — last process-quality hint

    # -- Public API ----------------------------------------------------------

    async def run(
        self,
        task: str,
        session: Session | None = None,
        confirm_callback=None,
    ) -> AgentResult:
        """Execute *task* asynchronously and return the result.

        Creates a fresh ``Session`` and ``Agent`` for each invocation,
        so multiple calls are independent.

        If *session* is provided it is used directly; otherwise a new
        ``Session`` is created.  This allows the HTTP server to retain a
        reference to the session for later inspection.

        *confirm_callback* (optional) overrides the instance-level callback
        for this single run — used by headless opt-ins (``run --yes``, server
        ``auto_approve``) so confirmation-required calls are approved without a
        human.  The planner is rebuilt with it and restored afterwards.
        """
        if session is None:
            session = Session()

        await self._run_lock.acquire()

        # L.4: the process-quality hint is per-run; clear it before this run.
        self._last_process_hint = None

        # L.3: rebuild the planner with a per-run confirm callback when given.
        override = confirm_callback is not None
        prev_planner = None
        prev_confirm_callback = self._confirm_callback

        try:
            if override:
                self._confirm_callback = confirm_callback
                auth = self._container.resolve(ActionAuthorizer)
                planner = self._create_planner(auth)
                prev_planner = self._container.resolve(Planner)
                self._container.register(Planner, planner)
            # MCP: ensure external servers are connected ON THIS event loop so
            # their tools are callable. Keep this inside the lock/finally block
            # so a connection error cannot strand the shared run lock.
            if self._mcp_manager is not None:
                await self._mcp_manager.ensure_current_loop(self._mcp_servers)
            agent = Agent(self._container)
            self._last_agent = agent   # Phase 4 — retained for /context-report

            # Runtime scoring: score each task independently, so reset the
            # per-run collectors before executing and snapshot them afterwards.
            for _m in self._run_metrics:
                _m.reset()

            bus = self._container.resolve(EventBus)
            ui = _new_run_ui()

            async def _capture(event: BaseEvent) -> None:
                _capture_run_ui(ui, event)

            for event_type in _UI_EVENTS:
                bus.subscribe(event_type.value, _capture)
            try:
                result = await agent.run(task, session)
            finally:
                for event_type in _UI_EVENTS:
                    bus.unsubscribe(event_type.value, _capture)

            status = result.status.value if hasattr(result.status, "value") else str(result.status)
            self._last_run_score = RunScore(
                task=task,
                status=status,
                run_id=str(session.metadata.get("last_run_id", "")),
                model_id=str(getattr(agent.llm, "model_id", "")),
                safety=self._run_metrics[0].snapshot(),
                process=self._run_metrics[1].snapshot(),
                quality=self._run_metrics[2].snapshot(),
                efficiency=self._run_metrics[3].snapshot(),
            )
            await self._persist_run_score(self._last_run_score)
            history = session.metadata.setdefault("run_history", [])
            if isinstance(history, list):
                entry = {
                    "task": task,
                    "output": result.output,
                    "status": status,
                    "run_id": str(session.metadata.get("last_run_id", "")),
                    "metrics": asdict(result.metrics),
                    "run_score": self.get_run_score(),
                    "citation_report": self.get_citation_report(),
                    "tools": ui["tools"],
                    "plan": ui["plan"],
                    "swarm": _public_swarm(ui["swarm"]),
                    "artifacts": [
                        {"path": artifact.path, "action": artifact.action}
                        for artifact in (result.artifacts or [])
                    ],
                }
                history.append(json.loads(json.dumps(entry, ensure_ascii=False, default=str)))
            return result
        finally:
            # ponytail: the planner swap is instance-wide and not safe under
            # concurrent requests (a parallel request could read the overridden
            # planner during this run).  A per-request Synapse would remove the
            # ceiling; for an explicit opt-in flag it is an acceptable trade-off.
            if override and prev_planner is not None:
                self._container.register(Planner, prev_planner)
            self._confirm_callback = prev_confirm_callback
            # ponytail: MCP stays connected for the instance lifetime (see
            # connect guard above) — no per-run shutdown, so registered tools
            # remain discoverable and we don't respawn server subprocesses.
            self._run_lock.release()

    async def aclose(self) -> None:
        """Release resources owned by this Synapse instance."""
        try:
            if self._enable_eval:
                await self._background_manager.aclose()
            if self._mcp_manager is not None:
                await self._mcp_manager.shutdown()
            memory = self._container.resolve(MemoryStore)
            close = getattr(memory, "close", None)
            if close is not None:
                close()
        finally:
            if self._eval_memory_dir is not None:
                self._eval_memory_dir.cleanup()
                self._eval_memory_dir = None

    def get_citation_report(self) -> dict | None:
        """Phase 4 — return the citation/usage report for the last run, or None."""
        agent = getattr(self, "_last_agent", None)
        if agent is None:
            return None
        tracker = getattr(agent, "_citation_tracker", None)
        context = getattr(agent, "_last_context", None)
        if tracker is None or context is None:
            return None
        return tracker.report(context)

    async def compact_session(self, session: Session) -> dict:
        """Force L1/L2 history compaction on an existing session."""
        await self._run_lock.acquire()
        try:
            return await self._compact_session_locked(session)
        finally:
            self._run_lock.release()

    async def _compact_session_locked(self, session: Session) -> dict:
        """Compact while the caller owns the instance run lock."""
        from synapse.modules.context.history_compact import compact_history
        try:
            llm = self._container.resolve(LLMProvider)
        except Exception:
            llm = None
        cfg = self._config.planning
        report = await compact_history(
            session.messages,
            llm=llm,
            session_meta=session.metadata,
            force=True,
            soft_chars=cfg.history_soft_chars,
            keep_recent_tools=cfg.history_keep_recent_tools,
            keep_recent_turns=cfg.history_keep_recent_turns,
            rotate_after=cfg.compact_rotate_after,
            strategy=cfg.history_compaction,
        )
        return report.to_dict()

    def clear_session_memory(self) -> None:
        """Drop all in-memory SESSION memory entries.

        Session memory outlives an individual Session object, so /reset must
        clear it explicitly or prior tasks' summaries leak into the next task.
        """
        try:
            layered = self._container.resolve(MemoryStore)
            layered.clear_session()
        except Exception:
            pass

    def get_run_score(self) -> dict | None:
        """Return the runtime score for the last run (or a live snapshot).

        The result is a serializable dict combining the four metric collectors
        (safety / process / quality / efficiency) plus the L.4 process-quality
        ``hint``.  Returns ``None`` only if the container was built without the
        runtime collectors.
        """
        if self._last_run_score is None and not getattr(self, "_run_metrics", None):
            return None
        if self._last_run_score is not None:
            score = self._last_run_score.to_dict()
        else:
            score = RunScore(
                model_id=str(getattr(self._container.resolve(LLMProvider), "model_id", "")),
                safety=self._run_metrics[0].snapshot(),
                process=self._run_metrics[1].snapshot(),
                quality=self._run_metrics[2].snapshot(),
                efficiency=self._run_metrics[3].snapshot(),
            ).to_dict()
        score["process_hint"] = self._last_process_hint
        registry = self._container.resolve(ToolRegistry)
        tool_names = sorted(tool.name for tool in registry.list_all())
        mcp_tool_names = [name for name in tool_names if name.startswith("mcp.")]
        score["capabilities"] = {
            "tool_count": len(tool_names),
            "tool_names_sha256": hashlib.sha256(
                "\n".join(tool_names).encode("utf-8")
            ).hexdigest(),
            "mcp_connected": bool(
                self._mcp_manager is not None and self._mcp_manager.connected
            ),
            "mcp_server_count": len(
                getattr(self._mcp_manager, "_clients", {})
            ) if self._mcp_manager is not None else 0,
            "mcp_tool_count": len(mcp_tool_names),
            "mcp_tool_names_sha256": hashlib.sha256(
                "\n".join(mcp_tool_names).encode("utf-8")
            ).hexdigest(),
        }
        return score

    def get_effective_config(self) -> dict[str, Any]:
        """Return the secret-free effective runtime config for eval reports."""
        from synapse.eval.runner import _sanitize_config

        payload = self._config.model_dump(mode="json")
        payload["tools"]["workspace_root"] = "<runtime-workspace>"
        payload["plugins"]["paths"] = [
            Path(path).name for path in payload["plugins"].get("paths", [])
        ]
        payload["runtime"] = {
            "enable_eval": self._enable_eval,
            "eval_ablation": self._eval_ablation.to_dict(),
            "memory_backend": self._memory_backend,
            "enable_external_tools": self._enable_external_tools,
            "mcp_servers": [
                {
                    "name": server.name,
                    "transport": server.transport,
                    "risk_level": getattr(server.risk_level, "value", str(server.risk_level)),
                }
                for server in (self._mcp_servers or [])
            ],
        }
        return _sanitize_config(payload)

    async def _on_process_quality_scored(self, event: BaseEvent) -> None:
        """L.4 — remember the latest process-quality hint for the user."""
        self._last_process_hint = getattr(event, "hint", None) or None

    async def _persist_run_score(self, score: RunScore) -> None:
        """Persist a run score to ProjectMemory as a single rolling entry.

        Failures are swallowed so scoring never blocks a task. ProjectMemory
        store is idempotent by id, so the fixed ``run-score-log`` id keeps the
        latest score (older ones are replaced, not accumulated).
        """
        if self._enable_eval:
            return
        try:
            pm = self._container.resolve(ProjectMemory)
        except Exception:
            return
        try:
            entry = MemoryEntry(
                id="run-score-log",
                content=json.dumps(score.to_dict(), ensure_ascii=False, indent=2),
                level=MemoryLevel.PROJECT,
                metadata=MemoryMetadata(
                    timestamp=datetime.now(), priority=3, tags=["run_score"],
                ),
            )
            await pm.store(entry)
        except Exception:
            return

    def run_sync(self, task: str) -> AgentResult:
        """Synchronous wrapper around :meth:`run`.

        Uses ``asyncio.run`` internally and is therefore not suitable for
        use inside an already-running event loop.
        """
        return asyncio.run(self.run(task))

    # -- Internal: config ----------------------------------------------------

    @staticmethod
    def _load_config(
        config_path: str | None,
        provider: str | None,
        model: str | None,
        overrides: dict[str, Any],
        strict_overrides: bool = False,
    ):
        config, _ = load_config(config_path)

        # Explicit arguments override the persisted default only for this instance.
        if provider is not None or model is not None:
            from synapse.config.models import apply_model_selection
            apply_model_selection(
                config,
                provider or config.provider.provider,
                model or config.provider.model,
            )

        sections = (
            config.provider, config.planning, config.tools, config.security,
            config.context, config.hooks, config.plugins,
        )
        for key, value in overrides.items():
            section = next((item for item in sections if hasattr(item, key)), None)
            if section is None:
                if strict_overrides:
                    raise ValueError(f"Unknown Synapse config override '{key}'")
                continue
            setattr(section, key, value)

        return config

    # -- Internal: container assembly ----------------------------------------

    def _build_container(self) -> Container:
        """Wire every Phase-1 and Phase-2 module into the IoC container."""
        c = Container()

        # Core infrastructure
        event_bus = EventBus()
        c.register(EventBus, event_bus)

        from synapse.modules.plugins import DefaultPluginRegistry
        from synapse.protocols.plugin import PluginRegistry
        plugin_registry = DefaultPluginRegistry()
        plugin_registry.discover(self._config.plugins.paths)
        c.register(PluginRegistry, plugin_registry)

        # s04 — user hooks: run shell commands after the configured events fire.
        if self._config.hooks.hooks:
            from synapse.modules.hooks import HookRunner
            HookRunner(self._config.hooks.hooks).attach(event_bus)

        # Make config available to components that need it (e.g. Agent reads ContextConfig).
        from synapse.config.schema import SynapseConfig
        c.register(SynapseConfig, self._config)
        c.register(EvaluationAblations, self._eval_ablation)

        # Audit log — tamper-evident event logging (Phase 2)
        audit_logger = AuditLogger(bus=event_bus)
        c.register(AuditLogger, audit_logger)

        # LLM Provider (selected by name)
        provider = self._create_provider()
        c.register(LLMProvider, provider)

        # Tools
        registry = DefaultToolRegistry()
        for tool in self._create_all_tools():
            registry.register(tool)

        # MCP — create the manager but do NOT connect here. MCP clients must
        # live on the same event loop that later calls their tools, so the
        # connection is deferred to run() (see _mcp_manager usage there).
        mcp_manager = None
        if self._mcp_servers:
            from synapse.modules.mcp.manager import McpManager as _McpManager
            mcp_manager = _McpManager(tool_registry=registry, event_bus=event_bus)
        self._mcp_manager = mcp_manager

        c.register(ToolRegistry, registry)

        # Memory — layered: session + project + user + semantic
        workspace_root = Path(self._config.tools.workspace_root).expanduser().resolve()
        if self._eval_ablation.memory:
            session_memory = SessionMemory()
            if self._eval_memory_dir is not None:
                memory_root = Path(self._eval_memory_dir.name)
                project_memory = ProjectMemory(memory_root / "project")
                user_memory = UserMemory(memory_root / "user")
            else:
                project_memory = ProjectMemory(workspace_root / ".synapse" / "memory")
                user_memory = UserMemory()
            # Passed as a factory, not an instance — see LayeredMemory._get_semantic.
            memory = LayeredMemory(
                session_memory,
                project_memory,
                user_memory,
                None if self._enable_eval else self._create_semantic_memory,
            )
        else:
            memory = DisabledMemoryStore()
        c.register(MemoryStore, memory)

        # Process-quality verification closed loop — live, not eval-gated.
        # Skip persisting feedback during eval benchmarks so the loop does not
        # leak into later benchmark tasks' prompts.
        from synapse.modules.process_quality import ProcessQualityVerifier
        c.register(
            ProcessQualityVerifier,
            ProcessQualityVerifier(
                event_bus=event_bus,
                memory=memory,
                persist_feedback=not self._enable_eval,
            ),
        )

        # L.4: capture the process-quality hint (emitted per task) so it can be
        # surfaced to the user via get_run_score() / CLI /score / server /run.
        event_bus.subscribe("process_quality_scored", self._on_process_quality_scored)

        # Context
        retriever = BasicContextRetriever()
        c.register(ContextRetriever, retriever)

        # Context budget management
        partitioner = ContextPartitioner()
        compactor = ContextCompactor()
        c.register(ContextPartitioner, partitioner)
        c.register(ContextCompactor, compactor)

        # Security
        sandbox_mode = self._config.security.sandbox_mode.lower()
        if sandbox_mode not in {"enforce", "warn", "off"}:
            raise ValueError(f"Unknown sandbox_mode '{sandbox_mode}'")
        sandbox = None
        if self._config.security.sandbox_enabled and sandbox_mode != "off":
            try:
                sandbox = ProcessSandbox(
                    backend=self._config.security.sandbox_backend,
                    allow_network=self._config.security.sandbox_network,
                    docker_image=self._config.security.sandbox_docker_image,
                )
            except Exception as exc:
                if sandbox_mode == "enforce":
                    raise RuntimeError("Sandbox initialization failed in enforce mode") from exc
        c.register(Sandbox, sandbox)
        if not self._eval_ablation.action_auth and (
            sandbox is None or getattr(sandbox, "backend", "process") != "docker"
        ):
            raise RuntimeError(
                "action_auth ablation requires the Docker sandbox backend"
            )

        auth = ActionAuthorizer(
            workspace_root=self._config.tools.workspace_root,
            confirmation_enabled=self._config.security.auth_confirmation,
            allowed_paths=self._config.security.allowed_paths,
            allowlisted_commands=self._config.tools.allowlist_commands,
            bypass_policy=not self._eval_ablation.action_auth,
            # EXTERNAL tools (web/browser/db) run only when explicitly opted in
            # via config (security.allow_external) or the enable_external_tools
            # flag (which also registers them). web_search is READ_ONLY and is
            # not affected by this switch.
            allow_external=self._config.security.allow_external or self._enable_external_tools,
        )
        c.register(ActionAuthorizer, auth)

        # Injection guard — annotates context blocks with trust levels
        injection_guard = InjectionGuard()
        c.register(InjectionGuard, injection_guard)

        # Runtime scoring: all four EventBus-driven metric collectors
        # are ALWAYS wired so every task yields an observable process/quality/
        # efficiency/safety score.  They are cheap counters only — gating them
        # behind _enable_eval meant real runs produced no scores.  _enable_eval
        # still toggles the heavier experiment harness elsewhere.
        from synapse.eval.metrics.safety import SafetyMetrics as _SM
        from synapse.eval.metrics.process import ProcessMetrics as _PM
        from synapse.eval.metrics.quality import QualityMetrics as _QM
        from synapse.eval.metrics.efficiency import EfficiencyMetrics as _EM
        self._run_metrics = [
            _SM(bus=event_bus, workspace_root=self._config.tools.workspace_root),
            _PM(bus=event_bus),
            _QM(bus=event_bus),
            _EM(bus=event_bus),
        ]
        for _m in self._run_metrics:
            c.register(type(_m), _m)

        # Planner (selected by planning mode)
        planner = self._create_planner(auth)
        c.register(Planner, planner)

        return c

    # -- Internal: provider factory ------------------------------------------

    def _create_provider(self):
        """Instantiate the configured LLM provider."""
        provider_name = self._config.provider.provider.lower()
        cfg = self._config.provider
        if cfg.routing not in {"fallback", "lowest_cost"}:
            raise ValueError(f"Unknown provider routing mode '{cfg.routing}'")
        from synapse.config.schema import _effective_api_key
        entries = [
            (provider_name, cfg.model, cfg.api_key, cfg.base_url,
             cfg.input_cost_per_million, cfg.output_cost_per_million),
        ]
        entries.extend(
            (entry.provider, entry.model, _effective_api_key(entry), entry.base_url,
             entry.input_cost_per_million, entry.output_cost_per_million)
            for entry in cfg.fallback_models
        )
        if cfg.routing == "lowest_cost":
            entries.sort(key=lambda e: e[4] + e[5])
        providers = [self._instantiate_provider(*entry[:4]) for entry in entries]
        if len(providers) == 1:
            return providers[0]
        from synapse.modules.providers.routing import FallbackLLMProvider
        return FallbackLLMProvider(providers, [e[4] + e[5] for e in entries])

    def _instantiate_provider(self, provider_name: str, model: str, api_key: str, base_url: str = ""):
        provider_cls, resolved_url = _resolve_provider(
            (provider_name or self._config.provider.provider).lower(),
            self._config.provider.custom_providers,
            model,
        )
        kwargs: dict = dict(
            model=model,
            api_key=api_key,
            max_tokens=self._config.provider.max_tokens,
            timeout_seconds=self._config.provider.timeout_seconds,
        )
        if base_url or resolved_url:
            kwargs["base_url"] = base_url or resolved_url
        if kwargs.get("base_url") == "https://api.deepseek.com/anthropic":
            kwargs["prompt_caching"] = False
        return provider_cls(**kwargs)

    # -- Internal: semantic memory factory -----------------------------------

    def _create_semantic_memory(self):
        """Instantiate the configured semantic memory backend.

        Called lazily on first SEMANTIC-level access, so the ~1.3s cost of
        importing chromadb/qdrant (plus ~0.7s to build the embedding model)
        is not paid by every CLI invocation.  The backend *name* is still
        validated eagerly in ``__init__`` so a typo fails fast.

        Returns
        -------
        SemanticMemory | QdrantMemory
            The semantic memory store instance.
        """
        backend = self._memory_backend.lower()

        if backend == "chromadb":
            try:
                from synapse.modules.memory.semantic import SemanticMemory
            except ImportError as exc:
                raise ImportError(
                    "SemanticMemory (chromadb) is not available — "
                    "install chromadb to use it."
                ) from exc
            return SemanticMemory()

        try:
            from synapse.modules.memory.qdrant_backend import QdrantMemory
        except ImportError as exc:
            raise ImportError(
                "QdrantMemory is not available — "
                "install qdrant-client to use it."
            ) from exc
        return QdrantMemory()

    # -- Internal: planner factory -------------------------------------------

    def _create_planner(self, auth: ActionAuthorizer) -> Planner:
        """Instantiate the configured planning strategy."""
        return build_planner(
            self._config.planning,
            auth,
            self._confirm_callback,
            max_llm_retries=self._config.provider.max_retries,
            completion_gate_enabled=self._eval_ablation.completion_gate,
            background_manager=self._background_manager,
        )

    # -- Internal: tool factory ----------------------------------------------

    def _create_all_tools(self):
        """Return the full set of tools, including external ones when enabled."""
        # s07 — shared so prompt injection and the load_skill tool agree.
        skill_loader = get_default_skill_loader()
        tools: list = [
            ReadTool(workspace_root=self._config.tools.workspace_root),
            WriteTool(workspace_root=self._config.tools.workspace_root),
            EditTool(workspace_root=self._config.tools.workspace_root),
            GlobTool(workspace_root=self._config.tools.workspace_root),
            GrepTool(workspace_root=self._config.tools.workspace_root),
            ShellTool(
                background_manager=self._background_manager,
                workspace_root=self._config.tools.workspace_root,
            ),
            GitTool(workspace_root=self._config.tools.workspace_root),
            SkillTool(skill_loader),
            TodoWriteTool(self._todo_store),
            TodoReadTool(self._todo_store),
        ]

        # Web search is always available (DuckDuckGo, no API key required).
        if WebSearchTool is not None:
            tools.append(WebSearchTool())

        # Read-only URL fetch is always available (GET-only, READ_ONLY). It lets
        # the LLM read a specific page without scripting python/curl. Heavier
        # external tools (web/browser/db) stay gated behind allow_external.
        if WebFetchTool is not None:
            tools.append(WebFetchTool())

        if self._enable_external_tools or self._config.security.allow_external:
            if HTTPTool is not None:
                tools.append(HTTPTool())
            if DBTool is not None:
                tools.append(DBTool(
                    workspace_root=self._config.tools.workspace_root,
                ))
            if BrowserTool is not None:
                tools.append(BrowserTool())

        enabled = set(self._config.tools.enabled)
        return [tool for tool in tools if tool.name in enabled or tool.name.startswith("mcp.")]


def build_planner(
    planning_config,
    auth,
    confirm_callback=None,
    max_llm_retries: int = 3,
    completion_gate_enabled: bool = True,
    background_manager: BackgroundTaskManager | None = None,
) -> Planner:
    """Build the planner for a planning config — single source of truth.

    Shared by :meth:`Synapse._create_planner` and by CLI/test wiring so the
    mode->Planner mapping lives in exactly one place.
    """
    mode = planning_config.mode.lower()
    cfg = planning_config

    react = ReActPlanner(
        max_iterations=cfg.max_iterations,
        thrashing_threshold=cfg.thrashing_threshold,
        max_thrashing_events=cfg.max_thrashing_events,
        max_tokens_per_task=cfg.max_tokens_per_task,
        auth=auth,
        confirm_callback=confirm_callback,
        total_timeout_seconds=cfg.total_timeout_seconds,
        max_tool_result_chars=cfg.max_tool_result_chars,
        max_llm_retries=max_llm_retries,
        completion_gate_enabled=completion_gate_enabled,
        checkpoint_enabled=cfg.checkpoints,
        history_compaction=cfg.history_compaction,
        history_soft_chars=cfg.history_soft_chars,
        history_keep_recent_tools=cfg.history_keep_recent_tools,
        history_keep_recent_turns=cfg.history_keep_recent_turns,
        compact_rotate_after=cfg.compact_rotate_after,
        background_manager=background_manager or get_default_manager(),
        skill_loader=get_default_skill_loader(),
    )

    if mode == PlanningMode.REACT:
        return react
    if mode == PlanningMode.PLAN_EXECUTE:
        return PlanExecutePlanner(react_planner=react)
    if mode == PlanningMode.HIERARCHICAL:
        complex_planner = PlanExecutePlanner(react_planner=react)
        return HierarchicalPlanner(react_planner=react, complex_planner=complex_planner)
    if mode == PlanningMode.SWARM:
        # s18 — give swarm workers filesystem isolation when we know the root.
        wt = None
        if isinstance(auth, ActionAuthorizer):
            from synapse.modules.planning.worktree import WorktreeManager
            wt = WorktreeManager(auth.workspace_root)
        return SwarmPlanner(react_planner=react, worktree_manager=wt)

    raise ValueError(
        f"Unknown planning mode '{mode}'. "
        f"Available: react, plan_execute, hierarchical, swarm"
    )

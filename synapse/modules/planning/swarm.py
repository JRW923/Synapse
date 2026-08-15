"""Swarm / Team planner — core closed loop MVP.

Multiple peer agents work on one task at the same time, then a verifier
agent checks the merged result and — if it rejects — the weakest worker is
re-run (up to ``max_verify_loops``).  This is the deliberate inversion of
``HierarchicalPlanner`` (which runs subtasks serially specifically to avoid
error amplification): we accept parallelism and cancel its risk with a
verification loop instead.

Design (lazy / reuse-first):
- Workers ARE plain ``ReActPlanner`` instances, differentiated only by a
  ``role`` + ``system_prompt_suffix`` + optional ``tool_filter`` — no new
  Reviewer/Tester/Security classes.  The role system is pluggable: add a
  ``RoleSpec`` and you get a new kind of worker.
- Merge logic is the shared ``merge_subtask_results`` from ``hierarchical``.
- Each worker gets an isolated ``session.fork(agent_id)`` (never shared).
- Read-only roles (reviewer/tester/security) get a filtered tool view; only
  coders write, and when there are several coders the task is decomposed into
  disjoint file scopes.

ponytail: file-scope isolation IS now hard-enforced — each coder's
``file_scope`` (from ``_decompose_scopes``) becomes a per-worker
``ActionAuthorizer`` write allow-list, so an out-of-scope write is rejected at
the auth layer (covers both ``write`` and ``edit``).  No scope (single coder or
LLM fallback) reuses the shared base authorizer, unchanged.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from synapse.protocols.planner import (
    AgentResult, ExecutionMetrics, ResultStatus, PlanningMode,
)
from synapse.protocols.llm import Message
from synapse.protocols.events import (
    WorkerSpawned, WorkerCompleted, ReviewSubmitted, VoteCast, SwarmVerified,
    TaskDecomposed,
)
from synapse.modules.planning.hierarchical import merge_subtask_results
from synapse.modules.planning.react import ReActPlanner
from synapse.modules.planning.worktree import WorktreeManager
from synapse.modules.security.auth import ActionAuthorizer

logger = logging.getLogger(__name__)


def _failed_result(description: str, reason: str) -> AgentResult:
    """Build a FAILED AgentResult for a task that errored or never completed."""
    return AgentResult(
        status=ResultStatus.FAILED,
        output=f"Task failed: {reason}\n(description: {description[:200]})",
        metrics=ExecutionMetrics(),
    )


@dataclass
class RoleSpec:
    """A kind of swarm worker.

    ``file_scope=True`` marks a writing role that should be split across
    disjoint file scopes when several instances run in parallel.  ``tool_filter``
    (a set of allowed tool names) restricts what the worker can touch — use it
    to make reviewer/tester/security roles read-only.
    """

    role: str
    system_prompt_suffix: str = ""
    tool_filter: set | None = None
    count: int = 1
    file_scope: bool = False


# Default MVP team: two parallel coders + one read-only reviewer/verifier.
DEFAULT_ROLES = [
    RoleSpec(
        role="coder",
        system_prompt_suffix="你是实现工程师，负责编写与修改代码以完成任务。",
        file_scope=True,
        count=2,
    ),
    RoleSpec(
        role="reviewer",
        system_prompt_suffix=(
            "你是资深代码审查专家。你只做审查，不写业务代码。检查合并结果是否真正"
            "满足原始任务、是否引入明显错误或不一致，并给出明确的通过/不通过结论。"
        ),
        tool_filter={"read", "grep", "glob", "git"},
    ),
]


class FilteredToolRegistry:
    """Read-only-ish view of a tool registry that only exposes allowed tools.

    Both ``get`` (execution) and ``get_schemas`` (what the LLM is told about)
    are filtered, so a read-only role cannot be talked into calling a write
    tool by the model.
    """

    def __init__(self, inner, allowed: set | None):
        self._inner = inner
        self._allowed = set(allowed) if allowed else None

    def get(self, name: str):
        if self._allowed is not None and name not in self._allowed:
            raise KeyError(f"Tool '{name}' is not allowed for this role")
        return self._inner.get(name)

    def get_schemas(self) -> list[dict]:
        schemas = self._inner.get_schemas()
        if self._allowed is None:
            return schemas
        return [s for s in schemas if s.get("name") in self._allowed]


class SwarmPlanner:
    """Peer multi-agent planner with review + verification loop."""

    mode = PlanningMode.SWARM

    def __init__(
        self,
        react_planner,
        roles: list[RoleSpec] | None = None,
        planner_factory=None,
        vote_threshold: float = 0.5,
        max_verify_loops: int = 2,
        worktree_manager: WorktreeManager | None = None,
        autonomous: bool = False,
        autonomous_workers: int = 2,
    ):
        self.react_planner = react_planner
        self.roles = roles or list(DEFAULT_ROLES)
        # planner_factory(spec) -> Planner; lets tests inject mocks.
        self._planner_factory = planner_factory
        self.vote_threshold = vote_threshold
        self.max_verify_loops = max_verify_loops
        # s18 — when set, each writing (file-scoped) coder gets its own isolated
        # worktree; cleaned up after the swarm run finishes.
        self._worktree_manager = worktree_manager
        # s17 — autonomous mode: tasks go on a board, N generic workers claim
        # them (instead of explicit RoleSpec assignment).
        self.autonomous = autonomous
        self.autonomous_workers = autonomous_workers

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(self, task, context, tools, llm, sandbox, session, event_bus) -> AgentResult:
        start_time = time.time()
        metrics = ExecutionMetrics()
        result = None
        conflicts: list[str] = []
        try:
            if self.autonomous:
                result = await self._execute_autonomous(task, context, tools, llm, sandbox, session, event_bus, start_time, metrics)
            else:
                result = await self._execute_inner(task, context, tools, llm, sandbox, session, event_bus, start_time, metrics)
        finally:
            # s18 — fold worker edits back into the base workspace BEFORE tearing
            # down, so the swarm's results survive cleanup (otherwise every
            # worker's changes were silently discarded).
            if self._worktree_manager is not None:
                conflicts = self._worktree_manager.merge_all()
                self._worktree_manager.remove_all()
        if conflicts:
            result.status = ResultStatus.PARTIAL
            names = ", ".join(sorted(set(conflicts)))
            result.output += f"\n\nWorktree merge conflicts (not overwritten): {names}"
            await event_bus.emit(SwarmVerified(
                session_id=session.id,
                status=ResultStatus.PARTIAL.value,
                issues=f"Worktree merge conflicts: {names}",
            ))
        return result

    async def _execute_inner(self, task, context, tools, llm, sandbox, session, event_bus, start_time, metrics) -> AgentResult:

        # 1. Decompose coder work into disjoint file scopes when parallel.
        coder_specs = [r for r in self.roles if r.file_scope]
        total_coders = sum(r.count for r in coder_specs)
        if total_coders > 1:
            scopes = await self._decompose_scopes(task, llm, event_bus, session, total_coders)
        else:
            scopes = [{"id": "coder-0", "description": task, "file_scope": ""}]

        # 2. Spawn workers (isolated session each).
        workers: list[dict] = []
        coder_idx = 0
        for spec in self.roles:
            if spec.file_scope:
                for i in range(spec.count):
                    sc = scopes[coder_idx]
                    coder_idx += 1
                    workers.append(self._spawn(spec, sc["id"], sc["description"], session, tools, file_scope=sc["file_scope"]))
            else:
                workers.append(self._spawn(spec, spec.role, task, session, tools))

        for w in workers:
            await event_bus.emit(WorkerSpawned(
                session_id=session.id, agent_id=w["agent_id"], role=w["role"], task=w["task"],
            ))

        coder_workers = [w for w in workers if w["role"] == "coder"]
        other_workers = [w for w in workers if w["role"] != "coder"]

        async def _run(w):
            res = await w["planner"].execute(
                task=w["task"], context=context, tools=w["tools"], llm=llm,
                sandbox=sandbox, session=w["session"], event_bus=event_bus,
            )
            await event_bus.emit(WorkerCompleted(
                session_id=session.id, agent_id=w["agent_id"], role=w["role"],
                status=res.status.value, output_snippet=res.output[:200],
            ))
            w["result"] = res
            return res

        # 3. Coders run in parallel — this is the "peer agents work together".
        await asyncio.gather(*[_run(w) for w in coder_workers])
        # Other roles (reviewer/tester/security) run after, reviewing the merge.
        for w in other_workers:
            await _run(w)

        # 4. Merge coder outputs into one coherent result.
        merged = await merge_subtask_results(
            task, [(w["agent_id"], w["task"], w["result"]) for w in coder_workers],
            llm, event_bus, session,
        )

        # 5. Verify loop: reviewer judges the merge; on reject, re-run weakest.
        reviewers = [w for w in other_workers if w["role"] == "reviewer"]
        status = ResultStatus.SUCCESS
        issues = ""
        if reviewers:
            reviewer = reviewers[0]
            verdict, comments = self._judge(reviewer["result"])
            await self._emit_review(event_bus, session, reviewer, verdict, comments)
            if verdict != "approve":
                status = ResultStatus.PARTIAL
                loops = 0
                while loops < self.max_verify_loops and status != ResultStatus.SUCCESS:
                    loops += 1
                    weakest = coder_workers[0]
                    weakest["session"] = session.fork(f"{weakest['agent_id']}-retry{loops}")
                    weakest["result"] = await weakest["planner"].execute(
                        task=(
                            f"{weakest['task']}\n\n审查未通过，意见：{comments}\n"
                            "请修正后重新提交。"
                        ),
                        context=context, tools=weakest["tools"], llm=llm,
                        sandbox=sandbox, session=weakest["session"], event_bus=event_bus,
                    )
                    await event_bus.emit(WorkerCompleted(
                        session_id=session.id, agent_id=weakest["agent_id"], role=weakest["role"],
                        status=weakest["result"].status.value, output_snippet=weakest["result"].output[:200],
                    ))
                    merged = await merge_subtask_results(
                        task, [(w["agent_id"], w["task"], w["result"]) for w in coder_workers],
                        llm, event_bus, session,
                    )
                    re_verdict, re_comments = self._judge(await reviewer["planner"].execute(
                        task=f"审查以下合并结果是否满足原始任务：{task}\n\n合并结果：\n{merged}",
                        context=context, tools=reviewer["tools"], llm=llm,
                        sandbox=sandbox, session=reviewer["session"], event_bus=event_bus,
                    ))
                    await self._emit_review(event_bus, session, reviewer, re_verdict, re_comments)
                    if re_verdict == "approve":
                        status = ResultStatus.SUCCESS
                        issues = ""
                    else:
                        issues = re_comments

        await event_bus.emit(SwarmVerified(
            session_id=session.id, status=status.value, issues=issues,
        ))

        # Roll up metrics from coder workers.
        for w in coder_workers:
            m = w["result"].metrics
            metrics.tokens_input += m.tokens_input
            metrics.tokens_output += m.tokens_output
            metrics.tool_call_count += m.tool_call_count
            metrics.tool_success_count += m.tool_success_count
            metrics.thrashing_events += m.thrashing_events
        metrics.duration_ms = int((time.time() - start_time) * 1000)

        return AgentResult(
            status=status,
            output=merged,
            metrics=metrics,
            agent_id="swarm",
            role="swarm",
            contributors=[w["result"] for w in workers],
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _execute_autonomous(self, task, context, tools, llm, sandbox, session, event_bus, start_time, metrics) -> AgentResult:
        """s17 — drop subtasks on a board; N generic workers claim & execute.

        Reuses ``_decompose_scopes`` (so disjoint file scopes still apply to
        each board task) and the shared ``merge_subtask_results``.
        """
        from synapse.modules.planning.board import TaskBoard
        from synapse.modules.planning.hierarchical import merge_subtask_results
        from synapse.protocols.events import WorkerSpawned, WorkerCompleted

        n = self.autonomous_workers
        subs = await self._decompose_scopes(task, llm, event_bus, session, n)
        board = TaskBoard(event_bus=event_bus, session_id=session.id)
        for s in subs:
            await board.add(s["id"], s["description"])

        workers = [
            self._spawn(RoleSpec(role="worker", file_scope=True), f"worker-{i}", "", session, tools)
            for i in range(n)
        ]
        for w in workers:
            await event_bus.emit(WorkerSpawned(
                session_id=session.id, agent_id=w["agent_id"], role=w["role"], task="(autonomous board)",
            ))

        task_owner: dict[str, dict] = {}

        async def _loop(w):
            while True:
                t = await board.claim(w["agent_id"])
                if t is None:
                    return
                try:
                    res = await w["planner"].execute(
                        task=t.description, context=context, tools=w["tools"], llm=llm,
                        sandbox=sandbox, session=w["session"], event_bus=event_bus,
                    )
                except Exception as exc:
                    # Contain a per-task failure: release so another worker can
                    # retry, record a FAILED result, and keep the loop alive
                    # instead of crashing the whole swarm run.
                    await board.release(t.id)
                    res = _failed_result(t.description, str(exc))
                    logger.warning("swarm worker %s task %s failed: %s", w["agent_id"], t.id, exc)
                await board.complete(t.id, res)
                w["results"][t.id] = res
                task_owner[t.id] = w
                await event_bus.emit(WorkerCompleted(
                    session_id=session.id, agent_id=w["agent_id"], role=w["role"],
                    status=res.status.value, output_snippet=res.output[:200],
                ))

        # return_exceptions keeps one worker's crash from killing the whole
        # gather (which previously surfaced as a task_owner KeyError).
        gathered = await asyncio.gather(*[_loop(w) for w in workers], return_exceptions=True)
        for w, exc in zip(workers, gathered):
            if isinstance(exc, Exception):
                logger.error("swarm worker %s crashed: %s", w["agent_id"], exc)

        items = []
        for s in subs:
            owner = task_owner.get(s["id"])
            if owner is None:
                items.append((s["id"], s["description"], _failed_result(s["description"], "not completed")))
                continue
            res = owner["results"].get(s["id"])
            if res is None:
                items.append((s["id"], s["description"], _failed_result(s["description"], "no result")))
                continue
            items.append((s["id"], s["description"], res))
        merged = await merge_subtask_results(task, items, llm, event_bus, session)

        for w in workers:
            for res in w["results"].values():
                m = res.metrics
                metrics.tokens_input += m.tokens_input
                metrics.tokens_output += m.tokens_output
                metrics.tool_call_count += m.tool_call_count
                metrics.tool_success_count += m.tool_success_count
                metrics.thrashing_events += m.thrashing_events
        metrics.duration_ms = int((time.time() - start_time) * 1000)

        await event_bus.emit(SwarmVerified(
            session_id=session.id, status="success", issues="",
        ))

        return AgentResult(
            status=ResultStatus.SUCCESS,
            output=merged,
            metrics=metrics,
            agent_id="swarm",
            role="swarm",
            contributors=[r for w in workers for r in w["results"].values()],
        )

    def _spawn(self, spec: RoleSpec, agent_id: str, task_desc: str, session, tools, file_scope: str = "") -> dict:
        sub_session = session.fork(agent_id)
        # s18 — writing (file-scoped) coders get an isolated worktree; the
        # isolate lives for the whole swarm run and is removed in execute().
        worktree_path = ""
        if self._worktree_manager is not None and spec.file_scope:
            worktree_path = str(self._worktree_manager.create(agent_id))
        planner = self._make_planner(spec, file_scope, worktree_path)
        worker_tools = FilteredToolRegistry(tools, spec.tool_filter) if spec.tool_filter else tools
        return {
            "agent_id": agent_id, "role": spec.role, "task": task_desc,
            "session": sub_session, "planner": planner, "tools": worker_tools,
            "file_scope": file_scope,
            "worktree_path": worktree_path,
            "results": {},
            "result": None,
        }

    def _make_planner(self, spec: RoleSpec, file_scope: str = "", worktree_path: str = ""):
        if self._planner_factory is not None:
            return self._planner_factory(spec)
        base = self.react_planner
        auth = base.auth
        suffix = spec.system_prompt_suffix
        # s18 — when a worktree is provided, the worker's whole filesystem root
        # is the isolated dir; the file scope is narrowed *inside* it.
        if worktree_path:
            scope_root = Path(worktree_path)
            allowed = [str(scope_root / file_scope)] if file_scope else []
            if isinstance(auth, ActionAuthorizer):
                auth = ActionAuthorizer(
                    workspace_root=str(scope_root),
                    allowed_paths=allowed,
                    confirmation_enabled=auth.confirmation_enabled,
                    allow_external=auth.allow_external,
                    bypass_policy=auth.bypass_policy,
                )
            suffix = (
                f"{spec.system_prompt_suffix}\n"
                f"你的隔离工作目录（worktree）是：{worktree_path}。"
                "请在该目录内读写文件，不要写到目录之外。"
            )
        # Per-worker hard isolation without a worktree: a coder with an assigned
        # file scope gets its own ActionAuthorizer whose write allow-list is
        # that scope.  Empty scope (single coder / LLM fallback) reuses base.
        elif file_scope and isinstance(auth, ActionAuthorizer):
            auth = ActionAuthorizer(
                workspace_root=auth.workspace_root,
                allowed_paths=[file_scope],
                confirmation_enabled=auth.confirmation_enabled,
                allow_external=auth.allow_external,
                bypass_policy=auth.bypass_policy,
            )
        return ReActPlanner(
            role=spec.role,
            system_prompt_suffix=suffix,
            max_iterations=base.max_iterations,
            thrashing_threshold=base.thrashing_threshold,
            max_thrashing_events=base.max_thrashing_events,
            max_tokens_per_task=base.max_tokens_per_task,
            auth=auth,
            confirm_callback=base._confirm,
            total_timeout_seconds=base.total_timeout_seconds,
            completion_gate_enabled=getattr(base, "completion_gate_enabled", True),
        )

    async def _decompose_scopes(self, task, llm, event_bus, session, n: int) -> list[dict]:
        """Ask the LLM to split *task* into *n* disjoint-file-scope subtasks."""
        system_prompt = (
            f"You are a task decomposition expert. Split the task into {n} "
            "parallel subtasks with NON-OVERLAPPING file scopes. Each subtask "
            "needs: id (string), description (string), file_scope (a file or "
            "directory path the subtask should primarily touch).\n\n"
            "Respond with ONLY a JSON array. No markdown fences.\n"
            'Example: [{"id":"1","description":"...","file_scope":"src/a.py"}]'
        )
        messages = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=task),
        ]
        response = await llm.chat(messages)
        raw = response.content.strip()
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:])
            if raw.rstrip().endswith("```"):
                raw = raw.rstrip()[:-3]
            raw = raw.strip()

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None

        if not isinstance(data, list) or len(data) != n:
            # Graceful fallback: replicate the whole task n times.
            return [{"id": str(i), "description": task, "file_scope": ""} for i in range(n)]

        scopes = [
            {
                "id": str(d.get("id", i)),
                "description": str(d.get("description", task)),
                "file_scope": str(d.get("file_scope", "")),
            }
            for i, d in enumerate(data)
        ]
        await event_bus.emit(TaskDecomposed(
            session_id=session.id,
            subtask_ids=[s["id"] for s in scopes],
            subtask_count=len(scopes),
        ))
        return scopes

    @staticmethod
    def _judge(review_result: AgentResult) -> tuple[str, str]:
        """Map a reviewer's output text to an (verdict, comments) pair.

        Verdict is 'approve' / 'reject' / 'needs_changes'. The verify loop
        treats anything other than 'approve' as a re-run signal, so an
        ambiguous review defaults to 'needs_changes' (no optimistic auto-pass).

        Rejection matches explicit phrases only — never a bare 'fail', which
        used to fire falsely on text like 'no tests fail'.
        """
        out = review_result.output or ""
        low = out.lower()

        reject_signals = (
            "reject", "不通过", "未通过", "needs change", "needs revision",
            "change requested", "not approved", "must fix", "refuse", "驳回",
            "blocked",
        )
        approve_signals = (
            "approve", "通过", "lgtm", "looks good", "approved", "ship it",
            "good to merge", "认可", "accepted",
        )

        rejected = any(s in low for s in reject_signals)
        approved = any(s in low for s in approve_signals)

        if rejected and not approved:
            return "reject", out
        if approved and not rejected:
            return "approve", out
        # Both signals present, or neither: don't auto-pass. An explicit reject
        # wins the tie; otherwise fall through to needs_changes.
        if rejected and approved:
            return "reject", out
        return "needs_changes", out

    async def _emit_review(self, event_bus, session, reviewer, verdict: str, comments: str) -> None:
        await event_bus.emit(ReviewSubmitted(
            session_id=session.id, agent_id=reviewer["agent_id"], reviewer_role=reviewer["role"],
            target_role="coder", verdict=verdict, comments=comments[:500],
        ))
        await event_bus.emit(VoteCast(
            session_id=session.id, agent_id=reviewer["agent_id"], role=reviewer["role"], decision=verdict,
        ))

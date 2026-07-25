"""Swarm / Team planner (TODO C) — core closed loop MVP.

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
import time
from dataclasses import dataclass, field

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
from synapse.modules.security.auth import ActionAuthorizer


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
    ):
        self.react_planner = react_planner
        self.roles = roles or list(DEFAULT_ROLES)
        # planner_factory(spec) -> Planner; lets tests inject mocks.
        self._planner_factory = planner_factory
        self.vote_threshold = vote_threshold
        self.max_verify_loops = max_verify_loops

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def execute(self, task, context, tools, llm, sandbox, session, event_bus) -> AgentResult:
        start_time = time.time()
        metrics = ExecutionMetrics()

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

    def _spawn(self, spec: RoleSpec, agent_id: str, task_desc: str, session, tools, file_scope: str = "") -> dict:
        sub_session = session.fork(agent_id)
        planner = self._make_planner(spec, file_scope)
        worker_tools = FilteredToolRegistry(tools, spec.tool_filter) if spec.tool_filter else tools
        return {
            "agent_id": agent_id, "role": spec.role, "task": task_desc,
            "session": sub_session, "planner": planner, "tools": worker_tools,
            "file_scope": file_scope,
            "result": None,
        }

    def _make_planner(self, spec: RoleSpec, file_scope: str = ""):
        if self._planner_factory is not None:
            return self._planner_factory(spec)
        base = self.react_planner
        auth = base.auth
        # Per-worker hard isolation: a coder with an assigned file scope gets its
        # own ActionAuthorizer whose write allow-list is that scope.  Empty scope
        # (single coder / LLM fallback) keeps the shared base authorizer.
        if file_scope and isinstance(auth, ActionAuthorizer):
            auth = ActionAuthorizer(
                workspace_root=auth.workspace_root,
                allowed_paths=[file_scope],
                confirmation_enabled=auth.confirmation_enabled,
                allow_external=auth.allow_external,
            )
        return ReActPlanner(
            role=spec.role,
            system_prompt_suffix=spec.system_prompt_suffix,
            max_iterations=base.max_iterations,
            thrashing_threshold=base.thrashing_threshold,
            max_thrashing_events=base.max_thrashing_events,
            max_tokens_per_task=base.max_tokens_per_task,
            auth=auth,
            confirm_callback=base._confirm,
            total_timeout_seconds=base.total_timeout_seconds,
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
        """Map a reviewer's output text to an (verdict, comments) pair."""
        out = review_result.output or ""
        low = out.lower()
        if "不通过" in out or "reject" in low or "fail" in low:
            return "reject", out
        if "通过" in out or "approve" in low or "lgtm" in low:
            return "approve", out
        # No explicit verdict → treat as approve (optimistic default).
        return "approve", out

    async def _emit_review(self, event_bus, session, reviewer, verdict: str, comments: str) -> None:
        await event_bus.emit(ReviewSubmitted(
            session_id=session.id, agent_id=reviewer["agent_id"], reviewer_role=reviewer["role"],
            target_role="coder", verdict=verdict, comments=comments[:500],
        ))
        await event_bus.emit(VoteCast(
            session_id=session.id, agent_id=reviewer["agent_id"], role=reviewer["role"], decision=verdict,
        ))

"""Hierarchical Planner — decompose, execute subtasks serially, merge."""

import json
import time
from dataclasses import dataclass

from synapse.protocols.planner import (
    AgentResult, ExecutionMetrics, ResultStatus, PlanningMode,
)
from synapse.protocols.llm import Message
from synapse.protocols.events import TaskDecomposed, MergeResult
from synapse.core.exceptions import PlannerError


@dataclass
class Subtask:
    """A single unit of work within a hierarchical plan."""
    id: str
    description: str
    complexity: int  # 1-10 (1 = trivial, 10 = very complex)


class HierarchicalPlanner:
    """Orchestrator pattern for large tasks.

    1. LLM decomposes task into subtasks (list of {id, description, complexity})
    2. Each subtask gets an independent Session (session.fork(subtask_id))
    3. Auto-selects ReActPlanner (simple) or PlanExecutePlanner (complex) per subtask
    4. Executes subtasks SERIALLY (research shows parallel amplifies errors)
    5. LLM merges results into final output
    """

    mode = PlanningMode.HIERARCHICAL

    def __init__(
        self,
        react_planner,
        complex_planner=None,
        complexity_threshold: int = 5,
        max_subtasks: int = 10,
    ):
        """
        Args:
            react_planner: ReActPlanner instance for simple subtasks.
            complex_planner: PlanExecutePlanner (or any Planner) for complex
                             subtasks. Falls back to react_planner if None.
            complexity_threshold: Subtasks at or above this complexity use
                                  the complex_planner.
            max_subtasks: Hard limit on decomposition size.
        """
        self.react_planner = react_planner
        self.complex_planner = complex_planner
        self.complexity_threshold = complexity_threshold
        self.max_subtasks = max_subtasks

    async def execute(
        self, task, context, tools, llm, sandbox, session, event_bus,
    ) -> AgentResult:
        start_time = time.time()
        metrics = ExecutionMetrics()

        # ---- 1. Decompose ----
        subtasks = await self._decompose(task, llm, event_bus, session)

        if len(subtasks) > self.max_subtasks:
            raise PlannerError(
                f"Too many subtasks ({len(subtasks)}). Max is {self.max_subtasks}."
            )

        # ---- 2. Execute subtasks SERIALLY ----
        subtask_results: list[tuple[Subtask, AgentResult]] = []
        for st in subtasks:
            sub_session = session.fork(st.id)
            planner = self._select_planner(st.complexity)

            result = await planner.execute(
                task=st.description,
                context=context,
                tools=tools,
                llm=llm,
                sandbox=sandbox,
                session=sub_session,
                event_bus=event_bus,
            )

            # Roll up metrics from the sub-planner
            metrics.tokens_input += result.metrics.tokens_input
            metrics.tokens_output += result.metrics.tokens_output
            metrics.tool_call_count += result.metrics.tool_call_count
            metrics.tool_success_count += result.metrics.tool_success_count
            metrics.thrashing_events += result.metrics.thrashing_events

            subtask_results.append((st, result))

        # ---- 3. Merge results ----
        merged_output = await self._merge(
            task, subtask_results, llm, event_bus, session,
        )

        metrics.duration_ms = int((time.time() - start_time) * 1000)

        # Determine overall status
        statuses = [r.status for _, r in subtask_results]
        if any(s == ResultStatus.FAILED for s in statuses):
            overall_status = ResultStatus.FAILED
        elif all(s == ResultStatus.SUCCESS for s in statuses):
            overall_status = ResultStatus.SUCCESS
        else:
            overall_status = ResultStatus.PARTIAL

        return AgentResult(
            status=overall_status,
            output=merged_output,
            metrics=metrics,
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _decompose(self, task: str, llm, event_bus, session) -> list[Subtask]:
        """Ask the LLM to break the task into a flat list of subtasks."""
        system_prompt = (
            "You are a task decomposition expert. Given a high-level task, "
            "break it down into a list of serially-executable subtasks. "
            "Each subtask must have an id (string), description (string), "
            "and complexity (integer 1-10, where 1=trivial and 10=very complex).\n\n"
            "Respond with ONLY a JSON array. No markdown fences, no explanation.\n"
            'Example: [{"id": "1", "description": "Analyze inputs", "complexity": 3}]'
        )
        messages = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=f"Decompose this task into subtasks:\n\n{task}"),
        ]

        response = await llm.chat(messages)
        raw = response.content.strip()

        # Strip markdown code fences if present (defensive)
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:])  # drop opening fence
            if raw.rstrip().endswith("```"):
                raw = raw.rstrip()[:-3]
            raw = raw.strip()

        try:
            subtask_dicts = json.loads(raw)
        except json.JSONDecodeError:
            raise PlannerError(
                f"Failed to parse subtask JSON from LLM response: {raw[:200]}"
            )

        if not isinstance(subtask_dicts, list):
            raise PlannerError(
                f"Expected JSON array, got: {type(subtask_dicts).__name__}"
            )

        subtasks: list[Subtask] = []
        for d in subtask_dicts:
            if not all(k in d for k in ("id", "description", "complexity")):
                raise PlannerError(f"Subtask missing required fields: {d}")
            subtasks.append(Subtask(
                id=str(d["id"]),
                description=str(d["description"]),
                complexity=int(d["complexity"]),
            ))

        await event_bus.emit(TaskDecomposed(
            session_id=session.id,
            subtask_ids=[s.id for s in subtasks],
            subtask_count=len(subtasks),
        ))

        return subtasks

    def _select_planner(self, complexity: int):
        """Return the appropriate planner for a given complexity score.

        Scores >= complexity_threshold use the complex_planner (typically
        PlanExecutePlanner).  Falls back to ReActPlanner when no complex
        planner is configured.
        """
        if self.complex_planner is not None and complexity >= self.complexity_threshold:
            return self.complex_planner
        return self.react_planner

    async def _merge(
        self,
        task: str,
        subtask_results: list[tuple[Subtask, AgentResult]],
        llm,
        event_bus,
        session,
    ) -> str:
        """Ask the LLM to synthesize subtask results into a single output."""
        items = [(st.id, st.description, r) for st, r in subtask_results]
        return await merge_subtask_results(task, items, llm, event_bus, session)


async def merge_subtask_results(
    task: str,
    items: list[tuple[str, str, AgentResult]],
    llm,
    event_bus,
    session,
) -> str:
    """Merge ``(id, description, AgentResult)`` items into one coherent output.

    Shared by :class:`HierarchicalPlanner` and the Swarm planner so
    the same "synthesize subtask results" prompt is reused.
    """
    parts: list[str] = []
    for sid, desc, result in items:
        label = "SUCCESS" if result.status == ResultStatus.SUCCESS else result.status.value.upper()
        parts.append(
            f"### Subtask {sid}: {desc} [{label}]\n"
            f"{result.output}\n"
        )

    system_prompt = (
        "You are a result-merging expert. Given the original task and the "
        "results of individual subtasks, produce a single coherent final "
        "output. Synthesize — do not just list."
    )
    messages = [
        Message(role="system", content=system_prompt),
        Message(role="user", content=(
            f"Original task: {task}\n\n"
            f"Subtask results:\n\n{''.join(parts)}\n\n"
            "Please merge these into a single coherent final output."
        )),
    ]

    response = await llm.chat(messages)
    merged = response.content.strip()

    await event_bus.emit(MergeResult(
        session_id=session.id,
        subtask_count=len(items),
        merged_output=merged,
    ))

    return merged

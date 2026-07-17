"""Plan-Execute Planner — three-phase planning strategy.

Phase 1 - Plan:  LLM generates an execution plan (list of steps).
Phase 2 - Execute: For each step, call ReActPlanner, phase_clear context between steps.
Phase 3 - Verify: Check that key steps from the plan weren't skipped or failed.
"""

import json
import time
from synapse.protocols.planner import (
    AgentResult, ExecutionMetrics, ResultStatus, PlanningMode
)
from synapse.protocols.llm import Message
from synapse.protocols.events import PlanCreated, AgentCompleted
from synapse.core.exceptions import PlannerError


PLANNING_SYSTEM_PROMPT = """You are a task planner. Given a user's objective, break it down into a sequence of concrete, executable steps.

Output your plan as a JSON object with this exact structure:
{
    "reasoning": "Brief explanation of your approach",
    "steps": [
        {"step_id": "1", "description": "What to do", "expected_tools": ["tool_name"]},
        ...
    ]
}

Rules:
- Each step must be a single, self-contained action that one ReAct agent loop can complete.
- "expected_tools" lists the likely tools needed for the step (use names like: read, write, edit, shell, glob, grep, git).
- Steps should be ordered logically with clear dependencies.
- Do NOT include any text outside the JSON object.
"""


class PlanExecutePlanner:
    """Three-phase planner: Plan -> Execute -> Verify.

    Phase 1: Ask the LLM to produce a step-by-step plan (JSON).
    Phase 2: Execute each step sequentially via a ReActPlanner instance,
             clearing expired context blocks between steps.
    Phase 3: Verify that no critical steps were skipped or failed.

    Optional interactive mode: emits a PlanCreated event and waits for
    user approval via an async callback before entering the Execute phase.
    """

    mode = PlanningMode.PLAN_EXECUTE

    def __init__(self, react_planner, interactive: bool = False, approval_callback=None):
        """Args:
            react_planner: A ReActPlanner instance used for step execution.
            interactive: If True, emit PlanCreated event and await approval
                         callback before executing steps.
            approval_callback: Async callable ``async def callback(plan_steps, reasoning) -> bool``.
        """
        self.react_planner = react_planner
        self.interactive = interactive
        self.approval_callback = approval_callback

    async def execute(self, task, context, tools, llm, sandbox, session, event_bus) -> AgentResult:
        start_time = time.time()
        metrics = ExecutionMetrics()

        # --- Phase 1: Plan ---
        plan_steps, reasoning = await self._generate_plan(task, context, llm, metrics)

        await event_bus.emit(PlanCreated(
            session_id=session.id,
            task=task,
            plan_steps=plan_steps,
            reasoning=reasoning,
        ))

        # Optional interactive approval
        if self.interactive:
            if self.approval_callback is None:
                raise PlannerError("Interactive mode requires an approval_callback")
            approved = await self.approval_callback(plan_steps, reasoning)
            if not approved:
                metrics.duration_ms = int((time.time() - start_time) * 1000)
                return AgentResult(
                    status=ResultStatus.FAILED,
                    output="Plan rejected by user.",
                    metrics=metrics,
                )

        # --- Phase 2: Execute ---
        step_results: list[AgentResult] = []
        for step in plan_steps:
            # Clear expired context blocks before each step
            self._phase_clear(context)

            step_result = await self.react_planner.execute(
                task=step["description"],
                context=context,
                tools=tools,
                llm=llm,
                sandbox=sandbox,
                session=session,
                event_bus=event_bus,
            )
            step_results.append(step_result)

            # Aggregate metrics
            metrics.tool_call_count += step_result.metrics.tool_call_count
            metrics.tool_success_count += step_result.metrics.tool_success_count
            metrics.tokens_input += step_result.metrics.tokens_input
            metrics.tokens_output += step_result.metrics.tokens_output
            metrics.thrashing_events += step_result.metrics.thrashing_events

        # --- Phase 3: Verify ---
        overall_status, verification_msg = self._verify(plan_steps, step_results)

        metrics.duration_ms = int((time.time() - start_time) * 1000)

        await event_bus.emit(AgentCompleted(
            session_id=session.id,
            status=overall_status.value,
            total_tokens=metrics.tokens_input + metrics.tokens_output,
            tool_calls=metrics.tool_call_count,
            duration_ms=metrics.duration_ms,
        ))

        output_parts = [verification_msg]
        for i, sr in enumerate(step_results):
            output_parts.append(f"\n[Step {plan_steps[i]['step_id']}]: {sr.output}")
        final_output = "\n".join(output_parts)

        return AgentResult(
            status=overall_status,
            output=final_output,
            metrics=metrics,
        )

    async def _generate_plan(self, task, context, llm, metrics):
        """Phase 1: Ask the LLM to produce a step-by-step execution plan."""
        # Build planning messages with the dedicated planning system prompt
        system_prompt = PLANNING_SYSTEM_PROMPT

        # Append any system context blocks
        context_blocks = []
        for block in context.system:
            context_blocks.append(block.content)
        if context_blocks:
            system_prompt += "\n\nRelevant context:\n" + "\n\n".join(context_blocks)

        messages = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=f"Task: {task}\n\nGenerate a step-by-step execution plan."),
        ]

        response = await llm.chat(messages, tools=None)
        metrics.tokens_input += response.usage.get("input", 0)
        metrics.tokens_output += response.usage.get("output", 0)

        # Parse the JSON plan from the response
        raw = response.content.strip()

        # Strip markdown code fences if present
        if raw.startswith("```"):
            lines = raw.split("\n")
            # Remove opening fence (```json or ```)
            if lines[0].startswith("```"):
                lines = lines[1:]
            # Remove closing fence
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            raw = "\n".join(lines).strip()

        try:
            plan_data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise PlannerError(f"Failed to parse plan JSON from LLM response: {e}\nResponse: {raw[:500]}")

        if "steps" not in plan_data:
            raise PlannerError(f"Plan JSON missing 'steps' key. Response: {raw[:500]}")

        steps = plan_data["steps"]
        reasoning = plan_data.get("reasoning", "")

        if not isinstance(steps, list) or len(steps) == 0:
            raise PlannerError("Plan must contain at least one step.")

        for i, step in enumerate(steps):
            if "step_id" not in step:
                step["step_id"] = str(i + 1)
            if "description" not in step:
                raise PlannerError(f"Step at index {i} is missing 'description'.")
            if "expected_tools" not in step:
                step["expected_tools"] = []

        return steps, reasoning

    @staticmethod
    def _phase_clear(context):
        """Remove context blocks that have expired after a phase.

        Clears blocks where ``expires_after_phase`` is True from all
        four context tiers (system, core, reference, overflow).
        """
        for tier_name in ("system", "core", "reference", "overflow"):
            blocks = getattr(context, tier_name, None)
            if blocks is not None:
                blocks[:] = [b for b in blocks if not b.expires_after_phase]

    @staticmethod
    def _verify(plan_steps, step_results):
        """Phase 3: Check that no critical steps were skipped or failed."""
        failed_steps = []
        for i, (step, result) in enumerate(zip(plan_steps, step_results)):
            if result.status != ResultStatus.SUCCESS:
                failed_steps.append(step["step_id"])

        if not failed_steps:
            return ResultStatus.SUCCESS, "All plan steps completed successfully."

        if len(failed_steps) == len(plan_steps):
            return ResultStatus.FAILED, (
                f"All {len(failed_steps)} steps failed: {', '.join(failed_steps)}"
            )

        return ResultStatus.PARTIAL, (
            f"{len(failed_steps)} of {len(plan_steps)} steps failed or were skipped: "
            f"{', '.join(failed_steps)}"
        )

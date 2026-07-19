"""ReAct Planner — Think → Act → Observe loop."""

import asyncio
import sys
import time
from synapse.protocols.planner import (
    AgentResult, ExecutionMetrics, ResultStatus, PlanningMode
)
from synapse.protocols.llm import Message
from synapse.protocols.events import (
    ToolCallStarted, ToolCallCompleted, ThrashingDetected, AgentCompleted, AgentProgress
)
from synapse.core.exceptions import PlannerError


def _summarize_params(params: dict) -> str:
    """Summarize tool params for logging (truncate long values)."""
    parts = []
    for k, v in params.items():
        s = str(v)
        if len(s) > 80:
            s = s[:77] + "..."
        parts.append(f"{k}={s}")
    return ", ".join(parts)


class ReActPlanner:
    """Classic ReAct loop: the LLM thinks, calls tools, observes results, repeats.

    This is the simplest planning mode. For complex multi-step tasks,
    use PlanExecutePlanner (Phase 2).
    """

    mode = PlanningMode.REACT

    def __init__(self, max_iterations: int = 50, thrashing_threshold: int = 3,
                 max_thrashing_events: int = 2,
                 max_tokens_per_task: int = 200_000,
                 auth=None, confirm_callback=None, total_timeout_seconds: int = 300,
                 verbose: bool = True):
        self.max_iterations = max_iterations
        self.thrashing_threshold = thrashing_threshold
        self.max_thrashing_events = max_thrashing_events
        self.max_tokens_per_task = max_tokens_per_task
        self.auth = auth  # ActionAuthorizer or None
        self._confirm = confirm_callback  # async callable: (AuthRequest) -> bool
        self.total_timeout_seconds = total_timeout_seconds
        self.verbose = verbose

    def _log(self, msg: str):
        """Print a progress message if verbose is enabled.

        Uses stderr so output is visible even when Rich is rendering a status spinner.
        """
        if self.verbose:
            safe = msg.encode(sys.stdout.encoding or 'utf-8', errors='replace').decode(
                sys.stdout.encoding or 'utf-8', errors='replace'
            )
            print(f"[Synapse] {safe}", file=sys.stderr, flush=True)

    @staticmethod
    async def _maybe_await(obj):
        """Await if obj is a coroutine, otherwise return as-is."""
        if asyncio.iscoroutine(obj):
            return await obj
        return obj

    @staticmethod
    def _repair_session(messages: list[Message]) -> list[Message]:
        """Ensure every assistant ``tool_calls`` has matching tool-result messages.

        Without this, switching models or resuming an interrupted session
        can cause API errors (e.g. DeepSeek 400 "insufficient tool messages").
        Missing tool results are patched with a placeholder message so the
        message list stays structurally valid for every provider.
        """
        # Collect all tool_call_ids that need results
        pending_ids: dict[str, int] = {}  # id → message index
        for i, msg in enumerate(messages):
            if msg.role == "assistant" and msg.tool_calls:
                for tc in msg.tool_calls:
                    tc_id = tc.get("id", "")
                    if tc_id:
                        pending_ids[tc_id] = i

        # Remove ids that already have a matching tool result
        for msg in messages:
            if msg.role == "tool" and msg.tool_call_id:
                pending_ids.pop(msg.tool_call_id, None)

        if not pending_ids:
            return list(messages)

        # Patch missing tool results — insert right after the assistant that
        # declared them so ordering stays valid.
        import copy
        repaired: list[Message] = []
        # Group missing ids by assistant position
        by_pos: dict[int, list[str]] = {}
        for tc_id, pos in pending_ids.items():
            by_pos.setdefault(pos, []).append(tc_id)

        for i, msg in enumerate(messages):
            repaired.append(copy.copy(msg))
            if i in by_pos:
                for tc_id in by_pos[i]:
                    repaired.append(Message(
                        role="tool",
                        content="[interrupted — result lost]",
                        tool_call_id=tc_id,
                    ))
        return repaired

    async def execute(self, task, context, tools, llm, sandbox, session, event_bus) -> AgentResult:
        start_time = time.time()
        metrics = ExecutionMetrics()
        file_touch_counts: dict[str, int] = {}

        # Build initial messages — reuse session history if available.
        # Repair incomplete tool chains first (critical for model switching).
        system_prompt = self._build_system_prompt(context)
        if session.messages:
            repaired = self._repair_session(session.messages)
            repaired.append(Message(role="user", content=task))
            messages = repaired
        else:
            messages = [
                Message(role="system", content=system_prompt),
                Message(role="user", content=task),
            ]

        tool_schemas_raw = tools.get_schemas() if hasattr(tools, 'get_schemas') else []
        tool_schemas = await self._maybe_await(tool_schemas_raw)

        final_output = ""
        result_status = ResultStatus.SUCCESS

        self._log(f"Task: {task[:100]}{'...' if len(task) > 100 else ''}")
        self._log(f"Available tools: {[t['name'] for t in tool_schemas]}")
        await event_bus.emit(AgentProgress(
            session_id=session.id, phase="thinking",
            message=f"Analyzing task with {len(tool_schemas)} tools available"
        ))

        thrash_stop = False
        tool_results: list = []  # accumulates results from executed tool calls

        for iteration in range(1, self.max_iterations + 1):
            # Check total timeout budget
            elapsed = time.time() - start_time
            if elapsed > self.total_timeout_seconds:
                final_output = (
                    f"Task timed out after {elapsed:.0f}s "
                    f"(limit: {self.total_timeout_seconds}s). "
                    f"Completed {iteration - 1} iterations."
                )
                result_status = ResultStatus.PARTIAL
                break

            # Call LLM with exponential backoff retry (I2)
            self._log(f"Iteration {iteration}: calling LLM (messages={len(messages)})...")
            await event_bus.emit(AgentProgress(
                session_id=session.id, phase="calling_llm",
                message=f"Iteration {iteration}: calling LLM..."
            ))
            max_llm_retries = 3
            for attempt in range(max_llm_retries + 1):  # 1 initial + 3 retries = 4 total
                try:
                    response = await llm.chat(messages, tools=tool_schemas if tool_schemas else None)
                    break
                except Exception as e:
                    if attempt == max_llm_retries:
                        # All retries exhausted — return FAILED
                        self._log(f"ERROR: LLM call failed after {max_llm_retries + 1} attempts: {e}")
                        metrics.duration_ms = int((time.time() - start_time) * 1000)
                        return AgentResult(
                            status=ResultStatus.FAILED,
                            output=f"LLM API call failed after {max_llm_retries + 1} attempts: {e}",
                            metrics=metrics,
                        )
                    await asyncio.sleep(2 ** attempt)  # 1s, 2s, 4s
                    self._log(f"LLM call attempt {attempt + 1} failed: {e}, retrying...")
            metrics.tokens_input += response.usage.get("input", 0)
            metrics.tokens_output += response.usage.get("output", 0)
            total_tokens = metrics.tokens_input + metrics.tokens_output

            # Token budget: 80 % warn, 100 % stop.
            if self.max_tokens_per_task > 0:
                ratio = total_tokens / self.max_tokens_per_task
                if ratio >= 0.8 and ratio < 1.0:
                    await event_bus.emit(AgentProgress(
                        session_id=session.id, phase="token_budget",
                        message=(
                            f"Token budget at {int(ratio * 100)}% "
                            f"({total_tokens}/{self.max_tokens_per_task})"
                        ),
                    ))
                elif ratio >= 1.0:
                    final_output = (
                        f"Token budget exhausted "
                        f"({total_tokens}/{self.max_tokens_per_task}). "
                        f"Stopping to control costs."
                    )
                    result_status = ResultStatus.PARTIAL
                    break

            self._log(
                f"LLM responded: content={len(response.content)} chars, "
                f"tool_calls={len(response.tool_calls)}, "
                f"tokens={metrics.tokens_input}+{metrics.tokens_output}"
            )

            # Add assistant response to messages
            messages.append(Message(
                role="assistant",
                content=response.content,
                tool_calls=response.tool_calls,
            ))

            # No tool calls → task is complete
            if not response.tool_calls:
                self._log("No tool calls — task complete")
                await event_bus.emit(AgentProgress(
                    session_id=session.id, phase="done", message="Task completed"
                ))
                final_output = response.content
                break

            # Execute each tool call
            self._log(f"Executing {len(response.tool_calls)} tool(s): "
                      f"{[tc['name'] + '(' + str(list(tc['input'].keys())) + ')' for tc in response.tool_calls]}")
            tool_results.clear()
            for tc in response.tool_calls:
                tool_name = tc["name"]
                tool_input = tc["input"]

                # Emit event
                await event_bus.emit(ToolCallStarted(
                    session_id=session.id, tool_name=tool_name, tool_params=tool_input,
                ))

                t0 = time.time()
                self._log(f"  → {tool_name}({_summarize_params(tool_input)})")
                try:
                    tool = await self._maybe_await(tools.get(tool_name))

                    # Action-time authorization check (C1)
                    if self.auth is not None:
                        risk_level = getattr(tool, "risk_level", None)
                        if risk_level is not None:
                            auth_req = self.auth.create_request(
                                tool_name, tool_input, risk_level, session.id,
                            )
                            decision = self.auth.authorize(auth_req)
                            import sys as _sd5
                            _sd5.stderr.write(f"[TRACE] auth decision: allowed={decision.allowed} confirm={decision.requires_confirmation}\n"); _sd5.stderr.flush()

                            # Hard deny
                            if not decision.allowed:
                                result = type("TR", (), {
                                    "success": False,
                                    "output": "",
                                    "error": f"Authorization denied: {decision.reason}",
                                    "metadata": type("M", (), {
                                        "tool_name": tool_name, "files_touched": [],
                                        "sandbox_used": False,
                                    })(),
                                })()
                                metrics.tool_call_count += 1
                                duration_ms = int((time.time() - t0) * 1000)
                                await event_bus.emit(ToolCallCompleted(
                                    session_id=session.id,
                                    tool_name=tool_name,
                                    success=False,
                                    duration_ms=duration_ms,
                                    files_touched=[],
                                ))
                                tool_results.append((tc["id"], result))
                                continue

                            # Requires user confirmation → ask if callback available
                            if decision.requires_confirmation and self._confirm is not None:
                                approved = await self._confirm(auth_req)
                                import sys as _sd4
                                _sd4.stderr.write(f"[TRACE] auth confirm result: approved={approved}\n"); _sd4.stderr.flush()
                                if not approved:
                                    result = type("TR", (), {
                                        "success": False,
                                        "output": "",
                                        "error": f"User denied: {decision.reason}",
                                        "metadata": type("M", (), {
                                            "tool_name": tool_name, "files_touched": [],
                                            "sandbox_used": False,
                                        })(),
                                    })()
                                    metrics.tool_call_count += 1
                                    duration_ms = int((time.time() - t0) * 1000)
                                    await event_bus.emit(ToolCallCompleted(
                                        session_id=session.id,
                                        tool_name=tool_name,
                                        success=False,
                                        duration_ms=duration_ms,
                                        files_touched=[],
                                    ))
                                    tool_results.append((tc["id"], result))
                                    continue

                    import sys as _sd3
                    _sd3.stderr.write(f"[TRACE] about to execute tool: {tool_name}\n"); _sd3.stderr.flush()
                    result = await tool.execute(tool_input, sandbox=sandbox)
                    _sd3.stderr.write(f"[TRACE] tool executed: {tool_name} success={result.success}\n"); _sd3.stderr.flush()
                except KeyError:
                    result = type("TR", (), {
                        "success": False, "output": "", "error": f"Unknown tool: {tool_name}",
                        "metadata": type("M", (), {"tool_name": tool_name, "files_touched": [], "sandbox_used": False})(),
                    })()

                metrics.tool_call_count += 1
                if result.success:
                    metrics.tool_success_count += 1

                # Track file modifications for thrashing detection
                for f in result.metadata.files_touched:
                    file_touch_counts[f] = file_touch_counts.get(f, 0) + 1
                    if file_touch_counts[f] >= self.thrashing_threshold:
                        await event_bus.emit(ThrashingDetected(
                            session_id=session.id,
                            file_path=f,
                            modification_count=file_touch_counts[f],
                        ))
                        metrics.thrashing_events += 1

                # Early-stop: too many thrashing events → abort the whole task.
                if metrics.thrashing_events >= self.max_thrashing_events:
                    thrash_files = sorted(
                        {f for f, c in file_touch_counts.items()
                         if c >= self.thrashing_threshold}
                    )
                    final_output = (
                        f"Thrashing detected on: {', '.join(thrash_files)}. "
                        f"Stopping to prevent wasted tokens."
                    )
                    result_status = ResultStatus.PARTIAL
                    thrash_stop = True
                    break

            if thrash_stop:
                break

            duration_ms = int((time.time() - t0) * 1000)
            status_icon = "[OK]" if result.success else "[FAIL]"
            self._log(
                f"  {status_icon} {tool_name}: {'OK' if result.success else 'FAILED'} "
                f"({duration_ms}ms)"
                f"{' - ' + result.error if not result.success else ''}"
            )
            await event_bus.emit(ToolCallCompleted(
                session_id=session.id,
                tool_name=tool_name,
                success=result.success,
                duration_ms=duration_ms,
                files_touched=result.metadata.files_touched,
            ))

            tool_results.append((tc["id"], result))

            # Feed tool results back as tool messages (after all tools executed)
            for tool_id, result in tool_results:
                content = result.output if result.success else f"Error: {result.error}"
                messages.append(Message(
                    role="tool",
                    content=content,
                    tool_call_id=tool_id,
                ))

        else:
            # Exceeded max iterations (for/else: loop completed without break)
            final_output = f"Max iterations ({self.max_iterations}) reached. Task may be incomplete."
            result_status = ResultStatus.PARTIAL

        metrics.duration_ms = int((time.time() - start_time) * 1000)

        # Repair before persisting — prevents broken tool chains from being saved.
        session.messages = self._repair_session(messages)

        await event_bus.emit(AgentCompleted(
            session_id=session.id,
            status=result_status.value,
            total_tokens=metrics.tokens_input + metrics.tokens_output,
            tool_calls=metrics.tool_call_count,
            duration_ms=metrics.duration_ms,
        ))
        return AgentResult(
            status=result_status,
            output=final_output,
            metrics=metrics,
        )

    def _build_system_prompt(self, context) -> str:
        """Build system prompt from context blocks."""
        blocks = []

        # Add tools usage instruction
        tools_instruction = (
            "You are an AI coding agent with access to tools. "
            "You MUST use the available tools to complete tasks — "
            "never just describe what you would do. "
            "To create or modify files, call the 'write' or 'edit' tool directly. "
            "To read files, use 'read'. To search code, use 'grep' or 'glob'. "
            "To run commands, use 'shell'. "
            "Always provide absolute paths when using file tools."
        )
        blocks.append(tools_instruction)

        for block in context.system:
            blocks.append(block.content)
        return "\n\n".join(blocks)

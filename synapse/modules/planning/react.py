"""ReAct Planner — Think → Act → Observe loop."""

import asyncio
import json
import sys
import time
from synapse.protocols.planner import (
    AgentResult, ExecutionMetrics, ResultStatus, PlanningMode
)
from synapse.protocols.llm import Message
from synapse.protocols.events import (
    ToolCallStarted, ToolCallCompleted, ThrashingDetected, AgentCompleted, AgentProgress, LLMToken
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
                 verbose: bool = True,
                 role: str = "", system_prompt_suffix: str = ""):
        self.max_iterations = max_iterations
        self.thrashing_threshold = thrashing_threshold
        self.max_thrashing_events = max_thrashing_events
        self.max_tokens_per_task = max_tokens_per_task
        self.auth = auth  # ActionAuthorizer or None
        self._confirm = confirm_callback  # async callable: (AuthRequest) -> bool
        self.total_timeout_seconds = total_timeout_seconds
        self.verbose = verbose
        # TODO C — role lets one ReActPlanner act as a specialized swarm worker
        # (e.g. "reviewer") without a separate class.
        self.role = role
        self.system_prompt_suffix = system_prompt_suffix

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

    async def _call_llm(self, llm, messages, tools, event_bus, session_id):
        """Call the LLM, preferring streaming for live display.

        Falls back to ``chat()`` when the provider doesn't implement
        ``stream()`` (e.g. test doubles that only mock ``chat``). The
        ``LLMProvider`` protocol still declares ``chat``, so it remains a
        valid path.
        """
        try:
            return await self._call_llm_stream(llm, messages, tools, event_bus, session_id)
        except (NotImplementedError, TypeError, AttributeError):
            return await llm.chat(messages, tools=tools if tools else None)

    async def _call_llm_stream(self, llm, messages, tools, event_bus, session_id):
        """Stream one LLM call, emitting LLMToken events for live CLI display.

        Accumulates the streamed chunks into an object exposing
        ``.content`` / ``.tool_calls`` / ``.usage`` / ``.stop_reason`` so the
        rest of the ReAct loop is unchanged. Tool-call deltas (from any
        provider) are merged by index into a complete tool_calls list.
        """
        content_parts: list[str] = []
        tool_acc: dict[int, dict] = {}
        usage: dict[str, int] = {}

        async for chunk in llm.stream(messages, tools=tools if tools else None):
            if chunk.usage:
                usage = chunk.usage
            if chunk.content:
                content_parts.append(chunk.content)
                if event_bus is not None:
                    await event_bus.emit(LLMToken(session_id=session_id, text=chunk.content))
            if chunk.tool_call_delta:
                d = chunk.tool_call_delta
                idx = d.get("index", 0)
                slot = tool_acc.setdefault(idx, {"id": "", "name": "", "input_raw": ""})
                if d.get("id"):
                    slot["id"] = d["id"]
                if d.get("name"):
                    slot["name"] = d["name"]
                if d.get("input") is not None:
                    val = d["input"]
                    # Gemini streams the tool input as a ready-made dict;
                    # OpenAI/Anthropic stream it as a partial JSON string.
                    if isinstance(val, dict):
                        slot["input_raw"] = val
                    elif not isinstance(slot["input_raw"], dict):
                        slot["input_raw"] = (slot["input_raw"] or "") + val

        tool_calls: list[dict] = []
        for idx in sorted(tool_acc):
            slot = tool_acc[idx]
            raw = slot["input_raw"]
            if isinstance(raw, dict):
                parsed = raw
            elif isinstance(raw, str) and raw.strip():
                try:
                    parsed = json.loads(raw)
                except (json.JSONDecodeError, ValueError):
                    parsed = {}
            else:
                parsed = {}
            tool_calls.append({"id": slot["id"], "name": slot["name"], "input": parsed})

        return type("StreamResponse", (), {
            "content": "".join(content_parts),
            "tool_calls": tool_calls,
            "usage": usage,
            "stop_reason": "tool_use" if tool_calls else "end_turn",
        })()

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

        # Phase 4 — citation tracking (tracks which context blocks the LLM cites)
        from synapse.modules.context.citation import CitationTracker
        citation_tracker = CitationTracker()
        citation_tracker.mark_usage(context)
        self._last_citation_tracker = citation_tracker  # exposed to Agent

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
                    response = await self._call_llm(
                        llm, messages, tool_schemas if tool_schemas else None,
                        event_bus, session.id,
                    )
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
            await event_bus.emit(AgentProgress(
                session_id=session.id, phase="token_update",
                message=f"tokens={metrics.tokens_input}+{metrics.tokens_output}",
            ))

            # Phase 4 — track which context blocks this response cites.
            try:
                await citation_tracker.track_response(
                    response.content, context, event_bus, session.id,
                )
            except Exception:
                pass

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

                    result = await tool.execute(tool_input, sandbox=sandbox)
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

                tool_results.append((tc["id"], result))

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
        """Build system prompt from all context zones.

        Phase E: injects system → core → reference (overflow is not
        injected — it is reserved for low-priority blocks that the
        compactor processes). Each block is annotated with its source
        so the LLM can gauge provenance.
        """
        blocks = []

        # TODO C — prepend role instruction so this planner acts as a
        # specialized swarm worker (reviewer/tester/security/...).
        if self.role or self.system_prompt_suffix:
            role_block = "## Your role\n"
            if self.role:
                role_block += f"You are operating as the **{self.role}** role.\n"
            if self.system_prompt_suffix:
                role_block += self.system_prompt_suffix + "\n"
            blocks.append(role_block.strip())

        # Add tools usage instruction
        tools_instruction = (
            "You are an AI coding agent with access to tools. "
            "You MUST use the available tools to complete tasks — "
            "never just describe what you would do. "
            "To create or modify files, call the 'write' or 'edit' tool directly. "
            "To read files, use 'read'. To search code, use 'grep' or 'glob'. "
            "To run commands, use 'shell'. "
            "For web search, call the 'web_search' tool with a query — "
            "do NOT write Python scripts or use curl to call search engines. "
            "Use 'web' only when you already have a specific URL to fetch. "
            "Always provide absolute paths when using file tools."
        )
        blocks.append(tools_instruction)

        # SYSTEM zone — always injected as-is.
        for block in context.system:
            blocks.append(block.content)

        # CORE zone — primary task-relevant context.
        core_blocks = self._select_within_budget(context.core, max_tokens=4000)
        if core_blocks:
            blocks.append("## Core context")
            for block in core_blocks:
                blocks.append(self._format_block(block))

        # REFERENCE zone — supporting material; trim if too large.
        ref_blocks = self._select_within_budget(context.reference, max_tokens=3000)
        if ref_blocks:
            blocks.append("## Reference context")
            for block in ref_blocks:
                blocks.append(self._format_block(block))

        return "\n\n".join(blocks)

    @staticmethod
    def _format_block(block) -> str:
        """Render a context block with its source provenance."""
        source_tag = f"[from {block.source.value}]"
        header = f"{source_tag} (priority={block.priority})"
        return f"{header}\n{block.content}"

    @staticmethod
    def _select_within_budget(blocks, max_tokens: int):
        """Pick blocks by priority descending until token budget exhausted."""
        if not blocks:
            return []
        sorted_blocks = sorted(blocks, key=lambda b: -b.priority)
        kept = []
        running = 0
        for b in sorted_blocks:
            tc = b.token_count or (len(b.content) // 4)
            if running + tc > max_tokens:
                continue
            kept.append(b)
            running += tc
        # Restore original order.
        kept_ids = {b.id for b in kept}
        return [b for b in blocks if b.id in kept_ids]

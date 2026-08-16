"""Conversation-history LLM microcompact (auto /compact) in the ReAct loop."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from synapse.modules.planning.react import ReActPlanner
from synapse.protocols.llm import LLMResponse, Message


def _messages_over_limit(n_tool_msgs: int = 20, chars_each: int = 9_000):
    msgs = [Message(role="system", content="sys"), Message(role="user", content="task")]
    for i in range(n_tool_msgs):
        msgs.append(Message(role="tool", content=f"tool result {i}: " + "x" * chars_each,
                            tool_call_id=f"t{i}"))
    return msgs


def _fake_llm(summary: str, fail: bool = False) -> AsyncMock:
    llm = AsyncMock()
    if fail:
        llm.chat.side_effect = RuntimeError("provider down")
    else:
        llm.chat.return_value = LLMResponse(content=summary, tool_calls=[],
                                            stop_reason="end_turn", usage={})
    return llm


def _run(planner, msgs, llm):
    asyncio.run(planner._compact_history(msgs, llm=llm, soft_chars=50_000))


def test_llm_mode_summarizes_then_elides():
    p = ReActPlanner(history_compaction="llm")
    msgs = _messages_over_limit()
    llm = _fake_llm("findings: parser bug in src/x.py:42; tests green after fix")
    _run(p, msgs, llm)

    summarized = [m for m in msgs if m.role == "tool" and "[compacted" in m.content]
    elided = [m for m in msgs if m.role == "tool" and "[elided" in m.content]
    assert len(summarized) == 1
    assert "parser bug" in summarized[0].content
    # The rest point at the summary instead of dead-dropping.
    assert len(elided) >= 10
    # Message-chain shape preserved: roles and tool_call_ids untouched.
    assert all(m.tool_call_id for m in msgs if m.role == "tool")


def test_llm_failure_falls_back_to_plain_elide():
    p = ReActPlanner(history_compaction="llm")
    msgs = _messages_over_limit()
    _run(p, msgs, _fake_llm("", fail=True))
    elided = [m for m in msgs if m.role == "tool" and "[elided" in m.content]
    assert len(elided) >= 10
    assert not any("[compacted" in (m.content or "") for m in msgs)


def test_elide_mode_never_calls_llm():
    p = ReActPlanner(history_compaction="elide")
    msgs = _messages_over_limit()
    llm = _fake_llm("should not be called")
    _run(p, msgs, llm)
    llm.chat.assert_not_awaited()
    assert any("[elided" in (m.content or "") for m in msgs if m.role == "tool")


def test_recent_tool_results_kept_intact():
    p = ReActPlanner(history_compaction="llm")
    msgs = _messages_over_limit()
    llm = _fake_llm("summary")
    _run(p, msgs, llm)
    recent = [m for m in msgs if m.role == "tool"][-6:]
    assert all("x" * 100 in (m.content or "") for m in recent)


def test_summary_cache_avoids_repeated_llm_calls():
    p = ReActPlanner(history_compaction="llm")
    llm = _fake_llm("cached summary")
    msgs = _messages_over_limit()
    _run(p, msgs, llm)
    # Second pass over the (now already compacted) history: nothing new to
    # summarize — but an identical victim set must come from cache, not the LLM.
    calls_after_first = llm.chat.await_count
    fresh = _messages_over_limit()  # same content as the first run
    _run(p, fresh, llm)
    assert llm.chat.await_count == calls_after_first
    assert "[compacted" in fresh[2].content


def test_under_soft_limit_is_noop():
    p = ReActPlanner(history_compaction="llm")
    msgs = [Message(role="system", content="s"),
            Message(role="tool", content="x" * 300, tool_call_id="t1")]
    llm = _fake_llm("nope")
    _run(p, msgs, llm)
    llm.chat.assert_not_awaited()
    assert msgs[1].content == "x" * 300

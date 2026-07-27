"""Tests for the universal tool-result truncation in the ReAct loop."""
from synapse.modules.planning.react import _truncate_tool_result


def test_no_limit_returns_as_is():
    assert _truncate_tool_result("hello", 0) == "hello"


def test_within_limit_returns_as_is():
    assert _truncate_tool_result("hello", 100) == "hello"


def test_over_limit_is_head_truncated_with_notice():
    out = _truncate_tool_result("a" * 100, 10)
    assert out.startswith("a" * 10)
    assert "truncated to 10 chars" in out


def test_empty_text_unaffected():
    assert _truncate_tool_result("", 10) == ""

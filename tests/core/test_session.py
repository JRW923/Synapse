"""Tests for Session management."""
import pytest
from synapse.core.session import Session
from synapse.protocols.llm import Message


def test_session_creation():
    s = Session()
    assert s.id is not None
    assert len(s.messages) == 0


def test_add_message():
    s = Session()
    s.add_message(Message(role="user", content="hello"))
    assert len(s.messages) == 1
    assert s.messages[0].role == "user"
    assert s.messages[0].content == "hello"


def test_fork_creates_independent_session():
    s1 = Session()
    s1.add_message(Message(role="user", content="original"))
    s1.metadata["project"] = "test"

    s2 = s1.fork("forked-1")
    assert s2.id == "forked-1"
    assert s2.id != s1.id
    # Forked session copies messages and metadata
    assert len(s2.messages) == 1
    assert s2.messages[0].content == "original"
    assert s2.metadata["project"] == "test"

    # Mutations to fork don't affect original
    s2.add_message(Message(role="assistant", content="reply"))
    assert len(s1.messages) == 1
    assert len(s2.messages) == 2


def test_clear_messages():
    s = Session()
    s.add_message(Message(role="user", content="hello"))
    s.clear_messages()
    assert len(s.messages) == 0


def test_token_estimate():
    s = Session()
    s.add_message(Message(role="user", content="hello world"))
    # Rough estimate: ~1.3 tokens per word for English
    assert s.estimated_tokens > 0

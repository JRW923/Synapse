"""Extension examples are real: load and execute them.

This is the runnable proof behind "extend without touching the agent loop":
both examples import cleanly, satisfy their protocols, and produce results
through the same paths production code uses.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "extensions"


def _load(module_name: str, filename: str):
    spec = importlib.util.spec_from_file_location(
        module_name, _EXAMPLES / filename)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_echo_provider_speaks_the_protocol():
    mod = _load("synapse_ext_echo", "echo_provider.py")
    from synapse.protocols.llm import Message

    provider = mod.EchoProvider()
    assert provider.model_id == "echo-provider"

    resp = asyncio.run(provider.chat(
        [Message(role="system", content="s"),
         Message(role="user", content="hello extension")]))
    assert resp.content == "ECHO: HELLO EXTENSION"
    assert resp.usage["input"] == 2

    chunks = []
    async def collect():
        async for c in provider.stream([Message(role="user", content="x")]):
            chunks.append(c)
    asyncio.run(collect())
    assert chunks[-1].usage is not None


def test_timestamp_tool_executes_and_registers():
    mod = _load("synapse_ext_timestamp", "timestamp_tool.py")
    result = asyncio.run(mod.TimestampTool().execute({}))
    assert result.success
    assert "T" in result.output  # ISO-8601
    assert result.metadata.tool_name == "timestamp"


def test_timestamp_tool_registers_into_real_registry():
    from synapse.modules.tools.registry import DefaultToolRegistry
    mod = _load("synapse_ext_timestamp", "timestamp_tool.py")

    registry = DefaultToolRegistry()
    registry.register(mod.TimestampTool())
    schema_names = [s["name"] for s in registry.get_schemas()]
    assert "timestamp" in schema_names

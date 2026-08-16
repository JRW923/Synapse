"""Minimal custom LLMProvider — proves provider-level extensibility.

This deterministic provider needs no API key: it answers from the message
history. Wire it into a Synapse run to see that the ReAct loop, context
governance, authorization and evaluation treat it like any other provider —
no change inside the Agent loop is required.

    from examples.extensions.echo_provider import EchoProvider
    from synapse.adapters.library import Synapse

    synapse = Synapse(provider="openai", model="gpt-5.4")  # any registered one
    synapse._container.register(LLMProvider, EchoProvider())  # swap the model
    result = await synapse.run("any task")

The full extension walkthrough is in docs/开发/扩展指南.md.
"""

from synapse.protocols.llm import LLMChunk, LLMResponse, Message


class EchoProvider:
    """Deterministic provider: replies with the last user message, uppercased."""

    def __init__(self, model_id: str = "echo-provider"):
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        return self._model_id

    async def chat(self, messages: list[Message],
                   tools: list[dict] | None = None) -> LLMResponse:
        last_user = next(
            (m.content for m in reversed(messages) if m.role == "user"), "")
        return LLMResponse(
            content=f"ECHO: {last_user.upper()}",
            stop_reason="end_turn",
            usage={"input": len(last_user.split()), "output": 8},
        )

    async def stream(self, messages: list[Message],
                     tools: list[dict] | None = None):
        last_user = next(
            (m.content for m in reversed(messages) if m.role == "user"), "")
        yield LLMChunk(content=f"ECHO: {last_user.upper()}")
        yield LLMChunk(usage={"input": len(last_user.split()), "output": 8})

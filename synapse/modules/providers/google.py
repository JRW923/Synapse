"""Google Gemini LLM Provider implementation."""

import logging
from google import genai
from google.genai import types
from synapse.protocols.llm import LLMResponse, Message
from synapse.core.exceptions import ProviderError

logger = logging.getLogger(__name__)

# Map Gemini finish reasons to Synapse stop reasons
FINISH_REASON_MAP = {
    types.FinishReason.STOP: "end_turn",
    types.FinishReason.MAX_TOKENS: "max_tokens",
    types.FinishReason.OTHER: "end_turn",
}

# Map JSON Schema type strings to Gemini Type enum
_TYPE_MAP = {
    "string": types.Type.STRING,
    "number": types.Type.NUMBER,
    "integer": types.Type.INTEGER,
    "boolean": types.Type.BOOLEAN,
    "array": types.Type.ARRAY,
    "object": types.Type.OBJECT,
}


class GoogleProvider:
    """LLM provider backed by Google's Gemini API."""

    def __init__(self, model: str = "gemini-pro", api_key: str = "", max_tokens: int = 4096):
        self._model = model
        self._max_tokens = max_tokens
        self._client = genai.Client(api_key=api_key) if api_key else genai.Client()

    @property
    def model_id(self) -> str:
        return self._model

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        system_instruction = self._extract_system(messages)
        contents = self._convert_messages(messages)
        config = self._build_config(system_instruction, tools)

        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=contents,
                config=config,
            )
            return self._parse_response(response)
        except Exception as e:
            raise ProviderError(f"Gemini API error: {e}") from e

    async def stream(self, messages: list[Message], tools: list[dict] | None = None):
        """Streaming not implemented in Phase 2 MVP."""
        raise NotImplementedError("Streaming will be added in a later phase")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _extract_system(self, messages: list[Message]) -> str | None:
        """Extract system message content (Gemini uses system_instruction config)."""
        for msg in messages:
            if msg.role == "system":
                return msg.content
        return None

    def _convert_messages(self, messages: list[Message]) -> list[types.Content]:
        """Convert internal Message list to Gemini Content list.

        System messages are excluded (handled as system_instruction).
        User messages → role="user", Assistant messages → role="model".
        """
        result: list[types.Content] = []
        for msg in messages:
            if msg.role == "system":
                continue
            if msg.role == "assistant":
                role = "model"
            elif msg.role == "user":
                role = "user"
            else:
                role = "user"  # fallback

            result.append(
                types.Content(
                    role=role,
                    parts=[types.Part.from_text(text=msg.content)],
                )
            )
        return result

    def _build_config(
        self,
        system_instruction: str | None,
        tools: list[dict] | None,
    ) -> types.GenerateContentConfig:
        """Build Gemini GenerateContentConfig from system_instruction and tools."""
        kwargs: dict = {
            "max_output_tokens": self._max_tokens,
        }
        if system_instruction:
            kwargs["system_instruction"] = system_instruction
        if tools:
            kwargs["tools"] = [
                types.Tool(function_declarations=self._convert_tools(tools))
            ]
        return types.GenerateContentConfig(**kwargs)

    def _convert_tools(self, tools: list[dict]) -> list[types.FunctionDeclaration]:
        """Convert Synapse tool dicts to Gemini FunctionDeclaration list."""
        declarations: list[types.FunctionDeclaration] = []
        for t in tools:
            schema_dict = t.get("input_schema", t.get("parameters", {}))
            declarations.append(
                types.FunctionDeclaration(
                    name=t["name"],
                    description=t.get("description", ""),
                    parameters=self._convert_schema(schema_dict),
                )
            )
        return declarations

    def _convert_schema(self, schema: dict) -> types.Schema:
        """Convert a JSON Schema dict to a Gemini Schema object.

        Handles the common property types recursively.
        """
        kwargs: dict = {}

        if "type" in schema and schema["type"] in _TYPE_MAP:
            kwargs["type"] = _TYPE_MAP[schema["type"]]
        if "description" in schema:
            kwargs["description"] = schema["description"]
        if "enum" in schema:
            kwargs["enum"] = schema["enum"]

        # Object properties
        if "properties" in schema:
            kwargs["properties"] = {
                key: self._convert_schema(prop)
                for key, prop in schema["properties"].items()
            }
        if "required" in schema:
            kwargs["required"] = schema["required"]

        # Array items
        if "items" in schema:
            kwargs["items"] = self._convert_schema(schema["items"])

        return types.Schema(**kwargs)

    def _parse_response(self, response: types.GenerateContentResponse) -> LLMResponse:
        """Parse a Gemini response into our LLMResponse format."""
        text_parts: list[str] = []
        tool_calls: list[dict] = []

        if response.candidates:
            for candidate in response.candidates:
                if candidate.content and candidate.content.parts:
                    for part in candidate.content.parts:
                        if part.text:
                            text_parts.append(part.text)
                        if part.function_call:
                            fc = part.function_call
                            tool_calls.append({
                                "id": fc.id or "",
                                "name": fc.name or "",
                                "input": fc.args or {},
                            })

        # Map finish reason
        finish_reason = "end_turn"
        if response.candidates:
            fr = response.candidates[0].finish_reason
            if fr is not None:
                finish_reason = FINISH_REASON_MAP.get(fr, "end_turn")

        # Extract usage
        usage: dict[str, int] = {}
        if response.usage_metadata:
            usage["input"] = response.usage_metadata.prompt_token_count or 0
            usage["output"] = response.usage_metadata.candidates_token_count or 0

        return LLMResponse(
            content="\n".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=finish_reason,
            usage=usage,
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

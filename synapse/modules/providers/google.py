"""Google Gemini LLM Provider implementation."""

import json
import logging
from synapse.protocols.llm import LLMResponse, LLMChunk, Message
from synapse.core.exceptions import ProviderError

logger = logging.getLogger(__name__)

try:
    from google import genai
    from google.genai import types as genai_types
    _GOOGLE_AVAILABLE = True
except ImportError:
    genai = None  # type: ignore
    genai_types = None  # type: ignore
    _GOOGLE_AVAILABLE = False

# Module-level constants populated only when the SDK is available.
FINISH_REASON_MAP: dict = {}
_TYPE_MAP: dict = {}

if _GOOGLE_AVAILABLE:
    FINISH_REASON_MAP = {
        genai_types.FinishReason.STOP: "end_turn",
        genai_types.FinishReason.MAX_TOKENS: "max_tokens",
        genai_types.FinishReason.OTHER: "end_turn",
    }

    _TYPE_MAP = {
        "string": genai_types.Type.STRING,
        "number": genai_types.Type.NUMBER,
        "integer": genai_types.Type.INTEGER,
        "boolean": genai_types.Type.BOOLEAN,
        "array": genai_types.Type.ARRAY,
        "object": genai_types.Type.OBJECT,
    }

    class GoogleProvider:
        """LLM provider backed by Google's Gemini API."""

        def __init__(
            self,
            model: str = "gemini-pro",
            api_key: str = "",
            max_tokens: int = 4096,
            timeout_seconds: int = 120,
            base_url: str = "",
        ):
            self._model = model
            self._max_tokens = max_tokens
            kwargs = dict(api_key=api_key if api_key else None)
            if base_url:
                kwargs["base_url"] = base_url
            self._client = genai.Client(
                http_options={"timeout": timeout_seconds * 1000},
                **kwargs,
            )

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
            """Stream Gemini responses as a sequence of LLMChunk deltas."""
            system_instruction = self._extract_system(messages)
            contents = self._convert_messages(messages)
            config = self._build_config(system_instruction, tools)

            try:
                async for chunk in await self._client.aio.models.generate_content_stream(
                    model=self._model,
                    contents=contents,
                    config=config,
                ):
                    # Gemini exposes running token totals on each chunk's
                    # usage_metadata; emit them as they arrive so the CLI token
                    # counter ticks up during generation instead of jumping at
                    # the end. The planning loop still reconciles with the
                    # authoritative cumulative total afterwards.
                    um = getattr(chunk, "usage_metadata", None)
                    if um and (um.prompt_token_count or um.candidates_token_count):
                        yield LLMChunk(usage={
                            "input": um.prompt_token_count or 0,
                            "output": um.candidates_token_count or 0,
                        })
                    if not chunk.candidates:
                        continue
                    candidate = chunk.candidates[0]
                    if not candidate.content or not candidate.content.parts:
                        continue
                    for part in candidate.content.parts:
                        if part.text:
                            yield LLMChunk(content=part.text)
                        elif part.function_call:
                            fc = part.function_call
                            yield LLMChunk(tool_call_delta={
                                "name": fc.name,
                                "input": fc.args,
                            })
            except Exception as e:
                raise ProviderError(f"Gemini streaming error: {e}") from e

        # ------------------------------------------------------------------
        # Private helpers
        # ------------------------------------------------------------------

        def _extract_system(self, messages: list[Message]) -> str | None:
            """Extract system message content (Gemini uses system_instruction config)."""
            for msg in messages:
                if msg.role == "system":
                    return msg.content
            return None

        def _convert_messages(self, messages: list[Message]) -> list[genai_types.Content]:
            """Convert internal Message list to Gemini Content list.

            Multi-turn tool use is preserved: an assistant message that
            carries tool_calls emits one ``function_call`` part per call (so
            Gemini sees what the model decided), and a subsequent tool-result
            message emits a ``function_response`` whose name is recovered from
            the preceding call via its ``tool_call_id``.
            """
            result: list[genai_types.Content] = []
            call_names: dict[str, str] = {}
            for msg in messages:
                if msg.role == "system":
                    continue

                if msg.role == "assistant" and msg.tool_calls:
                    parts = []
                    if msg.content:
                        parts.append(genai_types.Part.from_text(text=msg.content))
                    for tc in msg.tool_calls:
                        call_names[tc["id"]] = tc["name"]
                        args = tc.get("input") or {}
                        if isinstance(args, str):
                            try:
                                args = json.loads(args)
                            except (ValueError, json.JSONDecodeError):
                                args = {}
                        parts.append(genai_types.Part.from_function_call(
                            name=tc["name"],
                            args=args,
                        ))
                    result.append(genai_types.Content(role="model", parts=parts))
                    continue

                if msg.role == "tool" and msg.tool_call_id:
                    # Recover the original function name so Gemini can pair the
                    # response with its call (empty name breaks multi-turn).
                    name = call_names.get(msg.tool_call_id, "")
                    result.append(
                        genai_types.Content(
                            role="tool",
                            parts=[genai_types.Part.from_function_response(
                                name=name,
                                response={"result": msg.content},
                            )],
                        )
                    )
                    continue

                if msg.role == "assistant":
                    role = "model"
                elif msg.role == "user":
                    role = "user"
                else:
                    role = "user"

                result.append(
                    genai_types.Content(
                        role=role,
                        parts=[genai_types.Part.from_text(text=msg.content)],
                    )
                )
            return result

        def _build_config(
            self,
            system_instruction: str | None,
            tools: list[dict] | None,
        ) -> genai_types.GenerateContentConfig:
            """Build Gemini GenerateContentConfig from system_instruction and tools."""
            kwargs: dict = {
                "max_output_tokens": self._max_tokens,
            }
            if system_instruction:
                kwargs["system_instruction"] = system_instruction
            if tools:
                kwargs["tools"] = [
                    genai_types.Tool(function_declarations=self._convert_tools(tools))
                ]
            return genai_types.GenerateContentConfig(**kwargs)

        def _convert_tools(self, tools: list[dict]) -> list[genai_types.FunctionDeclaration]:
            """Convert Synapse tool dicts to Gemini FunctionDeclaration list."""
            declarations: list[genai_types.FunctionDeclaration] = []
            for t in tools:
                schema_dict = t.get("input_schema", t.get("parameters", {}))
                declarations.append(
                    genai_types.FunctionDeclaration(
                        name=t["name"],
                        description=t.get("description", ""),
                        parameters=self._convert_schema(schema_dict),
                    )
                )
            return declarations

        def _convert_schema(self, schema: dict) -> genai_types.Schema:
            """Convert a JSON Schema dict to a Gemini Schema object."""
            kwargs: dict = {}

            if "type" in schema and schema["type"] in _TYPE_MAP:
                kwargs["type"] = _TYPE_MAP[schema["type"]]
            if "description" in schema:
                kwargs["description"] = schema["description"]
            if "enum" in schema:
                kwargs["enum"] = schema["enum"]

            if "properties" in schema:
                kwargs["properties"] = {
                    key: self._convert_schema(prop)
                    for key, prop in schema["properties"].items()
                }
            if "required" in schema:
                kwargs["required"] = schema["required"]

            if "items" in schema:
                kwargs["items"] = self._convert_schema(schema["items"])

            return genai_types.Schema(**kwargs)

        def _parse_response(self, response: genai_types.GenerateContentResponse) -> LLMResponse:
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

            finish_reason = "end_turn"
            if response.candidates:
                fr = response.candidates[0].finish_reason
                if fr is not None:
                    finish_reason = FINISH_REASON_MAP.get(fr, "end_turn")

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

else:
    # When the SDK is unavailable, the class is undefined — importing it
    # explicitly will raise an ImportError.
    GoogleProvider = None  # type: ignore

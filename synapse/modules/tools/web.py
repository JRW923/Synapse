"""HTTP tool — makes web requests (GET/POST) via httpx."""

import json
import httpx
from synapse.protocols.tool import Tool, ToolSchema, ToolResult, ToolCallMetadata, RiskLevel, ToolCategory

MAX_RESPONSE_BYTES = 100_000
DEFAULT_TIMEOUT = 30.0


class HTTPTool:
    name = "web"
    description = (
        "Make an HTTP request (GET or POST) to a URL. "
        "Supports custom headers and a JSON string body for POST. "
        "Response is limited to 100 KB."
    )
    parameters = ToolSchema(
        name="web",
        description="Make an HTTP request",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to request"},
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST"],
                    "default": "GET",
                    "description": "HTTP method (GET or POST)",
                },
                "headers": {
                    "type": "object",
                    "description": "Optional HTTP headers as key-value pairs",
                },
                "body": {
                    "type": "string",
                    "description": "Request body as a JSON string (for POST)",
                },
            },
            "required": ["url"],
        },
    )
    requires_sandbox = True
    risk_level = RiskLevel.EXTERNAL
    category = ToolCategory.INTEGRATION

    async def execute(self, params: dict, sandbox=None) -> ToolResult:
        url = params["url"]
        method = params.get("method", "GET").upper()
        headers = params.get("headers") or {}
        body = params.get("body")
        meta = ToolCallMetadata(tool_name="web")

        timeout = httpx.Timeout(DEFAULT_TIMEOUT)
        limits = httpx.Limits(max_keepalive_connections=0)

        try:
            async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
                if method == "POST":
                    kwargs: dict = {}
                    if body is not None:
                        try:
                            kwargs["json"] = json.loads(body)
                        except json.JSONDecodeError:
                            kwargs["content"] = body
                    resp = await client.post(url, headers=headers, **kwargs)
                else:
                    resp = await client.get(url, headers=headers)

                text = resp.text
                # Enforce max response size
                if len(text.encode("utf-8")) > MAX_RESPONSE_BYTES:
                    text = text[:MAX_RESPONSE_BYTES] + "\n\n[Response truncated — exceeded 100 KB limit]"
                    return ToolResult(
                        success=True,
                        output=text,
                        metadata=meta,
                    )

                if resp.is_success:
                    return ToolResult(success=True, output=text, metadata=meta)
                else:
                    return ToolResult(
                        success=False,
                        output=text,
                        error=f"HTTP {resp.status_code}",
                        metadata=meta,
                    )
        except Exception as exc:
            return ToolResult(success=False, output="", error=str(exc), metadata=meta)

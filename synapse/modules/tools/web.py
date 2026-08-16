"""HTTP tool — makes web requests (GET/POST) via httpx."""

import json
import re
import httpx
from synapse.protocols.tool import Tool, ToolSchema, ToolResult, ToolCallMetadata, RiskLevel, ToolCategory

MAX_RESPONSE_BYTES = 100_000
DEFAULT_TIMEOUT = 30.0
_FETCH_TIMEOUT = 20.0  # ponytail: fail fast instead of hanging to the 120s command timeout
_MAX_FETCH_CHARS = 16_000  # ponytail: extracted text cap; raw HTML would be tens of k tokens

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _guard_output(text: str, source: str) -> str:
    """InjectionGuard hardening for external tool output (scan + tag wrap)."""
    from synapse.modules.security.injection import InjectionGuard
    return InjectionGuard.guard_external_output(text, source)


async def _ssrf_event_hook(response):
    """Reject any redirect hop that lands on a private address."""
    from synapse.modules.security.ssrf import is_private_host
    host = response.request.url.host
    if is_private_host(host):
        raise httpx.ConnectError(f"SSRF guard: redirect to private host '{host}'")


def _ssrf_reject(url: str) -> str | None:
    from synapse.modules.security.ssrf import check_url
    return check_url(url)


def _html_to_text(html: str) -> str:
    """Best-effort extraction of visible text from an HTML page.

    Strips script/style/head/noscript blocks, removes remaining tags, unescapes
    entities and collapses whitespace. For non-HTML (JSON, plain text) this is
    nearly a no-op — tags simply don't match.
    """
    html = re.sub(
        r"<(script|style|head|noscript)[^>]*>.*?</\1>",
        " ", html, flags=re.DOTALL | re.IGNORECASE,
    )
    text = _TAG_RE.sub(" ", html)
    text = (
        text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
        .replace("&quot;", '"').replace("&#x27;", "'").replace("&nbsp;", " ")
    )
    return _WS_RE.sub(" ", text).strip()


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

        rejected = _ssrf_reject(url)
        if rejected:
            return ToolResult(success=False, output="", error=rejected, metadata=meta)

        try:
            async with httpx.AsyncClient(
                    timeout=timeout, limits=limits,
                    event_hooks={"response": [_ssrf_event_hook]}) as client:
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
                        output=_guard_output(text, "web"),
                        metadata=meta,
                    )

                if resp.is_success:
                    return ToolResult(success=True, output=_guard_output(text, "web"), metadata=meta)
                else:
                    return ToolResult(
                        success=False,
                        output=text,
                        error=f"HTTP {resp.status_code}",
                        metadata=meta,
                    )
        except Exception as exc:
            return ToolResult(success=False, output="", error=str(exc), metadata=meta)


class WebFetchTool:
    """GET-only, read-only URL fetcher.

    ponytail: this exists so the LLM can read a *specific* URL's content
    (e.g. github.com/trending?since=weekly) without falling back to writing
    python/curl scripts. It is GET-only and classified READ_ONLY, so it runs
    by default — unlike HTTPTool (EXTERNAL, GET/POST) which is gated behind
    allow_external. Network failures fail fast via a short httpx timeout
    rather than hanging to the 120s command timeout and burning the 300s
    task budget.
    """

    name = "web_fetch"
    description = (
        "Fetch the content of a single URL via GET and return its text. "
        "Read-only — use this to read a specific web page when you already "
        "know the URL, instead of writing scripts to fetch it. "
        "Response is limited to 100 KB."
    )
    parameters = ToolSchema(
        name="web_fetch",
        description="Fetch a URL's content via GET",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to fetch"},
            },
            "required": ["url"],
        },
    )
    requires_sandbox = False
    risk_level = RiskLevel.READ_ONLY
    category = ToolCategory.INTEGRATION

    async def execute(self, params: dict, sandbox=None) -> ToolResult:
        meta = ToolCallMetadata(tool_name="web_fetch")
        url = (params.get("url") or "").strip()
        if not url:
            return ToolResult(success=False, output="", error="Empty url.", metadata=meta)

        timeout = httpx.Timeout(_FETCH_TIMEOUT)
        limits = httpx.Limits(max_keepalive_connections=0)
        rejected = _ssrf_reject(url)
        if rejected:
            return ToolResult(success=False, output="", error=rejected, metadata=meta)
        try:
            async with httpx.AsyncClient(
                    timeout=timeout, limits=limits, follow_redirects=True,
                    event_hooks={"response": [_ssrf_event_hook]}) as client:
                resp = await client.get(url)
        except Exception as exc:
            return ToolResult(success=False, output="", error=str(exc), metadata=meta)

        text = _html_to_text(resp.text)
        if len(text) > _MAX_FETCH_CHARS:
            text = text[:_MAX_FETCH_CHARS] + "\n\n[Response truncated — exceeded fetch limit]"

        if resp.is_success:
            return ToolResult(success=True, output=_guard_output(text, "web_fetch"), metadata=meta)
        return ToolResult(
            success=False, output=text, error=f"HTTP {resp.status_code}", metadata=meta
        )

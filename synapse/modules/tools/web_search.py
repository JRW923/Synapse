"""Web search tool — queries DuckDuckGo's HTML endpoint, no API key needed."""

import re
import urllib.parse
from synapse.protocols.tool import Tool, ToolSchema, ToolResult, ToolCallMetadata, RiskLevel, ToolCategory

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore[assignment]

_MAX_RESULTS = 8
_TIMEOUT = 20.0
_SEARCH_URL = "https://html.duckduckgo.com/html/"

# Regexes to pull result anchors from the DuckDuckGo HTML page.
# Each result is wrapped in <div class="result results_links results_links_deep web-result">...
# The ad results have class="result results_links results_links_deep web-result result--ad"
_RESULT_BLOCK_RE = re.compile(
    r'<div[^>]+class="result[^"]*results_links[^"]*"[^>]*>(.*?)</div>\s*</div>',
    re.DOTALL,
)
_TITLE_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]*>(.*?)</a>', re.DOTALL
)
_URL_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="([^"]+)"', re.DOTALL
)
_SNIPPET_RE = re.compile(
    r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', re.DOTALL
)
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(s: str) -> str:
    s = _TAG_RE.sub("", s)
    s = s.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    s = s.replace("&quot;", '"').replace("&#x27;", "'").replace("&nbsp;", " ")
    return _WS_RE.sub(" ", s).strip()


def _resolve_ddg_url(raw: str) -> str:
    """DuckDuckGo wraps result URLs in a redirect like
    //duckduckgo.com/l/?uddg=<encoded>.  Unwrap it."""
    if "uddg=" in raw:
        qs = raw.split("uddg=", 1)[1]
        encoded = qs.split("&", 1)[0]
        try:
            return urllib.parse.unquote(encoded)
        except Exception:
            return raw
    return raw


def _is_ad(url: str) -> bool:
    """Detect DuckDuckGo ad/sponsored results."""
    if "duckduckgo.com/y.js" in url:
        return True
    if "ad_domain=" in url:
        return True
    return False


class WebSearchTool:
    name = "web_search"
    description = (
        "Search the web via DuckDuckGo and return the top results. "
        "Use this for real-time information, news, or anything not in your training data. "
        "No API key needed."
    )
    parameters = ToolSchema(
        name="web_search",
        description="Search the web. Returns up to 8 results with title, url, and snippet.",
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The search query"},
                "max_results": {
                    "type": "integer",
                    "description": "Max number of results to return (default 5, max 8)",
                    "default": 5,
                },
            },
            "required": ["query"],
        },
    )
    requires_sandbox = False
    risk_level = RiskLevel.EXTERNAL
    category = ToolCategory.INTEGRATION

    async def execute(self, params: dict, sandbox=None) -> ToolResult:
        meta = ToolCallMetadata(tool_name="web_search")

        if httpx is None:
            return ToolResult(
                success=False, output="",
                error="httpx is not installed. Run `pip install httpx` to enable web search.",
                metadata=meta,
            )

        query = (params.get("query") or "").strip()
        if not query:
            return ToolResult(success=False, output="", error="Empty query.", metadata=meta)

        max_results = params.get("max_results", 5)
        try:
            max_results = max(1, min(int(max_results), _MAX_RESULTS))
        except (TypeError, ValueError):
            max_results = 5

        try:
            async with httpx.AsyncClient(
                timeout=_TIMEOUT,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0 Safari/537.36"
                    ),
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "en-US,en;q=0.9",
                },
                follow_redirects=True,
            ) as client:
                resp = await client.post(
                    _SEARCH_URL,
                    data={"q": query, "b": "", "kl": ""},
                )
        except Exception as exc:
            return ToolResult(success=False, output="", error=str(exc), metadata=meta)

        if not resp.is_success:
            return ToolResult(
                success=False, output=resp.text[:500],
                error=f"HTTP {resp.status_code}",
                metadata=meta,
            )

        html = resp.text
        # Pair-wise extract: walk each result__a anchor, then find the
        # nearest result__snippet after it.  This is more robust than
        # zipping three independent regex lists (which desync on ads).
        results: list[tuple[str, str, str]] = []
        for m in re.finditer(
            r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
            html, re.DOTALL,
        ):
            raw_url, raw_title = m.group(1), m.group(2)
            url = _resolve_ddg_url(raw_url)
            if _is_ad(url) or _is_ad(raw_url):
                continue
            title = _strip_html(raw_title)
            # Snippet follows the anchor in the same result block.
            tail = html[m.end():m.end() + 1500]
            sm = _SNIPPET_RE.search(tail)
            snippet = _strip_html(sm.group(1)) if sm else ""
            results.append((title, url, snippet))
            if len(results) >= max_results:
                break

        if not results:
            return ToolResult(
                success=True,
                output=f"No results for: {query}",
                metadata=meta,
            )

        lines = [f"# Search: {query}  ({len(results)} results)", ""]
        for i, (title, url, snippet) in enumerate(results, 1):
            lines.append(f"{i}. {title}")
            lines.append(f"   URL: {url}")
            if snippet:
                lines.append(f"   {snippet}")
            lines.append("")

        return ToolResult(success=True, output="\n".join(lines), metadata=meta)

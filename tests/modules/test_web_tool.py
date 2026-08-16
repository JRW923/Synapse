"""Tests for HTTPTool (web) and WebFetchTool."""
import pytest
from synapse.modules.tools.web import HTTPTool, WebFetchTool
from synapse.protocols.tool import RiskLevel


@pytest.mark.asyncio
async def test_get(httpx_mock):
    """HTTPTool should perform a GET request and return the response body."""
    httpx_mock.add_response(
        url="https://httpbin.org/get",
        method="GET",
        text='{"url": "https://httpbin.org/get"}',
        status_code=200,
    )

    tool = HTTPTool()
    result = await tool.execute({"url": "https://httpbin.org/get", "method": "GET"})

    assert result.success
    assert "httpbin.org/get" in result.output


@pytest.mark.asyncio
async def test_post_json(httpx_mock):
    """HTTPTool should POST JSON and return the response."""
    httpx_mock.add_response(
        url="https://httpbin.org/post",
        method="POST",
        json={"received": True},
        status_code=201,
    )

    tool = HTTPTool()
    result = await tool.execute({
        "url": "https://httpbin.org/post",
        "method": "POST",
        "headers": {"Content-Type": "application/json"},
        "body": '{"key": "value"}',
    })

    assert result.success
    assert "received" in result.output


@pytest.mark.asyncio
async def test_error_handling(httpx_mock):
    """HTTPTool should handle connection errors gracefully."""
    httpx_mock.add_exception(
        Exception("Connection refused"),
        url="https://invalid.example.com",
    )

    tool = HTTPTool()
    result = await tool.execute({"url": "https://invalid.example.com", "method": "GET"})

    assert not result.success
    assert result.error is not None
    assert "Connection refused" in result.error


@pytest.mark.asyncio
async def test_web_fetch_is_read_only():
    """web_fetch must be GET-only and READ_ONLY so it runs by default,
    unlike HTTPTool (EXTERNAL). This is what stops the LLM from scripting
    python/curl to read a URL.
    """
    assert WebFetchTool.risk_level == RiskLevel.READ_ONLY
    assert WebFetchTool.requires_sandbox is False


@pytest.mark.asyncio
async def test_web_fetch_get(httpx_mock):
    """WebFetchTool performs a GET and returns the body."""
    httpx_mock.add_response(
        url="https://github.com/trending?since=weekly",
        method="GET",
        text="<html>repo list</html>",
        status_code=200,
    )
    result = await WebFetchTool().execute({"url": "https://github.com/trending?since=weekly"})
    assert result.success
    assert "repo list" in result.output


@pytest.mark.asyncio
async def test_web_fetch_handles_failure(httpx_mock):
    """Network failures return a failed ToolResult (fast-fail), never raise
    or hang to the 120s command timeout — so they can't burn the 300s budget.
    """
    httpx_mock.add_exception(Exception("Name or service not known"), url="https://github.com/trending")
    result = await WebFetchTool().execute({"url": "https://github.com/trending"})
    assert not result.success
    assert result.error is not None
    assert "Name or service not known" in result.error


@pytest.mark.asyncio
async def test_web_fetch_rejects_post(httpx_mock):
    """web_fetch is GET-only: any non-GET method is ignored and treated as GET,
    so it can never mutate server state (keeps the READ_ONLY guarantee)."""
    httpx_mock.add_response(
        url="https://example.com/x",
        method="GET",  # httpx_mock would fail the test if a POST were issued
        text="ok",
        status_code=200,
    )
    result = await WebFetchTool().execute({"url": "https://example.com/x", "method": "POST"})
    assert result.success


@pytest.mark.asyncio
async def test_web_fetch_returns_plain_text(httpx_mock):
    """web_fetch must strip HTML and return visible text, not raw markup —
    raw HTML would be tens of thousands of tokens and blow the budget."""
    html = (
        "<html><head><title>x</title></head>"
        "<body><script>var a=1;</script>"
        "<div><h1>Trending</h1><p>Some repo description here.</p></div>"
        "</body></html>"
    )
    httpx_mock.add_response(url="https://github.com/trending", method="GET", text=html, status_code=200)
    result = await WebFetchTool().execute({"url": "https://github.com/trending"})
    assert result.success
    assert "Trending" in result.output
    assert "Some repo description here." in result.output
    assert "<script" not in result.output
    assert "<div" not in result.output
    assert "<html" not in result.output


@pytest.mark.asyncio
async def test_web_fetch_caps_extracted_text(httpx_mock):
    """Even a huge page is capped at _MAX_FETCH_CHARS (~16k), so one fetch
    can't dominate the token budget."""
    httpx_mock.add_response(
        url="https://example.com/big", method="GET",
        text="<p>" + ("word " * 100000) + "</p>", status_code=200,
    )
    result = await WebFetchTool().execute({"url": "https://example.com/big"})
    assert result.success
    assert len(result.output) <= 16_000 + 120  # cap + truncation notice + injection-guard wrap
    assert "truncated" in result.output

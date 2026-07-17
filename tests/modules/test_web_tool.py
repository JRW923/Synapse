"""Tests for HTTPTool (web)."""
import pytest
from synapse.modules.tools.web import HTTPTool


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

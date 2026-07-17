"""Tests for BrowserTool."""

import sys
from unittest.mock import AsyncMock, MagicMock
import pytest
from synapse.modules.tools.browser import BrowserTool
from synapse.protocols.tool import RiskLevel


@pytest.mark.asyncio
async def test_navigate_extracts_text():
    """BrowserTool navigates to a URL and extracts visible text via mocked Playwright."""
    # Mock Playwright page
    mock_page = MagicMock()
    mock_page.content = AsyncMock(return_value="<html><body><h1>Hello World</h1><p>Test paragraph.</p></body></html>")
    mock_page.inner_text = AsyncMock(return_value="Hello World\nTest paragraph.")
    mock_page.goto = AsyncMock()
    mock_page.screenshot = AsyncMock(return_value=b"fake_screenshot_bytes")

    mock_browser = MagicMock()
    mock_context = MagicMock()

    mock_browser.new_context = AsyncMock()
    mock_browser.new_context.return_value = mock_context
    mock_browser.close = AsyncMock()

    mock_context.new_page = AsyncMock(return_value=mock_page)
    mock_context.close = AsyncMock()

    mock_playwright = AsyncMock()
    # __aenter__ must return the playwright mock itself so that attributes
    # like .chromium.launch are accessible on the context-managed value.
    mock_playwright.__aenter__.return_value = mock_playwright
    mock_playwright.chromium.launch = AsyncMock(return_value=mock_browser)

    # Pre-populate sys.modules with mocked playwright so the dynamic import
    # inside execute() succeeds even without the playwright package installed.
    mock_async_api = MagicMock()
    mock_async_api.async_playwright = MagicMock(return_value=mock_playwright)
    sys.modules["playwright"] = MagicMock()
    sys.modules["playwright.async_api"] = mock_async_api

    try:
        tool = BrowserTool()

        # Test basic navigation + text extraction
        result = await tool.execute({"url": "https://example.com"})
        assert result.success
        assert "Hello World" in result.output
        assert mock_page.goto.called

        # Verify the browser lifecycle was followed
        mock_playwright.chromium.launch.assert_called_once()
        mock_browser.new_context.assert_called_once()
        mock_context.new_page.assert_called_once()
        mock_page.inner_text.assert_called_once_with("body")
    finally:
        del sys.modules["playwright"]
        del sys.modules["playwright.async_api"]


@pytest.mark.asyncio
async def test_tool_requires_external_auth():
    """BrowserTool has RiskLevel.EXTERNAL because it accesses the internet."""
    tool = BrowserTool()
    assert tool.risk_level == RiskLevel.EXTERNAL
    assert tool.requires_sandbox is False

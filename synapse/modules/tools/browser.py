"""Browser tool — navigates URLs, extracts text, optionally screenshots via Playwright."""

from __future__ import annotations

from synapse.protocols.tool import Tool, ToolSchema, ToolResult, ToolCallMetadata, RiskLevel, ToolCategory


class BrowserTool:
    """Navigate a web page and extract its visible text content.

    Uses Playwright (headless Chromium) under the hood. The tool launches a
    browser, navigates to a URL, extracts the inner text of ``<body>``, and
    optionally captures a screenshot. Playwright is installed via ``pip install
    playwright``; chromium browsers are not required for tests (mock
    Playwright).
    """

    name = "browser"
    description = (
        "Navigate to a URL and extract visible text from the page body. "
        "Optionally capture a full-page screenshot. "
        "Requires playwright (pip install playwright)."
    )
    parameters = ToolSchema(
        name="browser",
        description="Navigate a web page and extract text",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "The URL to navigate to"},
                "screenshot": {
                    "type": "boolean",
                    "description": "Whether to capture a full-page screenshot (default false)",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Navigation timeout in milliseconds (default 30000)",
                },
            },
            "required": ["url"],
        },
    )
    requires_sandbox = False
    risk_level = RiskLevel.EXTERNAL
    category = ToolCategory.INTEGRATION

    async def execute(self, params: dict, sandbox=None) -> ToolResult:
        url: str = params["url"]
        take_screenshot: bool = params.get("screenshot", False)
        timeout_ms: int = params.get("timeout", 30000)

        meta = ToolCallMetadata(tool_name="browser")

        try:
            from playwright.async_api import async_playwright

            pw = await async_playwright().__aenter__()
            try:
                browser = await pw.chromium.launch(headless=True)
                context = await browser.new_context()

                try:
                    page = await context.new_page()
                    await page.goto(url, timeout=timeout_ms)
                    text = await page.inner_text("body")

                    output_parts = [text.strip()]

                    if take_screenshot:
                        screenshot_bytes = await page.screenshot(full_page=True)
                        import base64
                        b64 = base64.b64encode(screenshot_bytes).decode()
                        output_parts.append(f"\n\n[SCREENSHOT_BASE64]\n{b64}")

                    return ToolResult(
                        success=True,
                        output="\n".join(output_parts),
                        metadata=meta,
                    )
                finally:
                    await context.close()
                    await browser.close()
            finally:
                await pw.__aexit__(None, None, None)
        except ImportError:
            return ToolResult(
                success=False,
                output="",
                error="playwright is not installed. Run: pip install playwright",
                metadata=meta,
            )
        except Exception as exc:
            return ToolResult(
                success=False,
                output="",
                error=str(exc),
                metadata=meta,
            )

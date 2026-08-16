"""SSRF guard — private-network targets rejected before any socket opens."""

import asyncio

import pytest

from synapse.modules.security.ssrf import check_url, is_private_host


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/",
    "http://127.0.0.1:8080/api",
    "http://localhost/admin",
    "http://10.0.0.5/",
    "http://192.168.1.1/router",
    "http://172.16.0.9/",
    "http://169.254.169.254/latest/meta-data/",  # cloud metadata
    "http://[::1]/",
    "http://0.0.0.0/",
    "file:///etc/passwd",
    "ftp://example.com/x",
    "gopher://127.0.0.1:6379/_INFO",
])
def test_private_and_non_http_blocked(url):
    reason = check_url(url)
    assert reason is not None, url


def test_public_host_passes():
    # Deterministic without network: a literal public IP.
    assert check_url("http://8.8.8.8/dns") is None
    assert check_url("https://93.184.216.34/") is None


def test_decimal_ip_encoding_blocked():
    # 2130706433 == 127.0.0.1 in integer form.
    assert "private" in (check_url("http://2130706433/") or "")


def test_unresolvable_host_is_not_ssrf_flagged():
    # DNS failure is the client's problem, not an SSRF verdict.
    assert is_private_host("this-domain-really-does-not-exist-xyz.invalid") is False


def test_web_tool_blocks_private_url_before_request():
    from synapse.modules.tools.web import WebFetchTool
    result = asyncio.run(WebFetchTool().execute(
        {"url": "http://169.254.169.254/latest/meta-data/"}))
    assert not result.success
    assert "SSRF" in result.error or "private" in result.error


def test_web_tool_blocks_file_scheme():
    from synapse.modules.tools.web import HTTPTool
    result = asyncio.run(HTTPTool().execute({"url": "file:///etc/passwd"}))
    assert not result.success
    assert "scheme" in result.error

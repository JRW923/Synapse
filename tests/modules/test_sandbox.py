"""Tests for process sandbox."""
import pytest
from synapse.modules.security.sandbox import ProcessSandbox


@pytest.mark.asyncio
async def test_sandbox_echo():
    sandbox = ProcessSandbox()
    result = await sandbox.execute("echo hello")
    assert result.exit_code == 0
    assert "hello" in result.stdout


@pytest.mark.asyncio
async def test_sandbox_platform_detected():
    sandbox = ProcessSandbox()
    # `platform` reports the isolation actually applied, not an aspirational
    # OS-sandbox name (it used to claim linux_bwrap/macos_seatbelt without
    # applying either).
    assert sandbox.platform in ("windows_job_object", "process_group")
    assert sandbox.platform == sandbox.method


@pytest.mark.asyncio
async def test_sandbox_timeout():
    sandbox = ProcessSandbox()
    result = await sandbox.execute("sleep 10", timeout=1)
    assert result.timed_out or result.exit_code != 0


@pytest.mark.asyncio
async def test_sandbox_failing_command():
    sandbox = ProcessSandbox()
    result = await sandbox.execute("nonexistent_command_xyz")
    assert result.exit_code != 0

"""Tests for process sandbox."""
import sys

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


@pytest.mark.asyncio
async def test_sandbox_accepts_string_cwd(tmp_path):
    sandbox = ProcessSandbox()
    result = await sandbox.execute(
        f"\"{sys.executable}\" -c \"open('cwd-marker.txt', 'w').write('ok')\"",
        cwd=str(tmp_path),
    )
    assert result.exit_code == 0
    assert (tmp_path / "cwd-marker.txt").read_text() == "ok"


@pytest.mark.asyncio
async def test_sandbox_does_not_forward_untrusted_environment(monkeypatch):
    monkeypatch.setenv("SYNAPSE_EVAL_SECRET", "must-not-leak")
    sandbox = ProcessSandbox()
    result = await sandbox.execute(
        f"\"{sys.executable}\" -c \"import os; print(os.environ.get('SYNAPSE_EVAL_SECRET', 'missing'))\"",
    )
    assert result.exit_code == 0
    assert "must-not-leak" not in result.stdout
    assert "missing" in result.stdout


def test_powershell_routing_detection():
    from synapse.modules.security.sandbox import (
        _is_powershell_command, route_windows_shell,
    )
    assert _is_powershell_command("Get-Content -LiteralPath x")
    assert _is_powershell_command("get-childitem")
    assert _is_powershell_command("pwd")
    assert not _is_powershell_command("git status")
    assert not _is_powershell_command("")
    assert route_windows_shell("git status") == "git status"
    if sys.platform == "win32":
        assert route_windows_shell("Get-Content x").startswith("powershell")


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell routing is Windows-only")
async def test_sandbox_powershell_cmdlet_runs():
    sandbox = ProcessSandbox()
    result = await sandbox.execute("Get-Content -LiteralPath README.md | Measure-Object -Line")
    assert result.exit_code == 0

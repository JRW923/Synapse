"""Tests for action-time authorization."""
import pytest
from synapse.modules.security.auth import ActionAuthorizer
from synapse.protocols.tool import RiskLevel


@pytest.fixture
def auth():
    return ActionAuthorizer(workspace_root="/project", confirmation_enabled=True)


def test_read_only_auto_allow(auth):
    req = auth.create_request("read", {"path": "/project/file.py"}, RiskLevel.READ_ONLY, "s1")
    decision = auth.authorize(req)
    assert decision.allowed
    assert not decision.requires_confirmation


def test_write_in_workspace_requires_confirmation(auth):
    req = auth.create_request("write", {"path": "/project/new.py"}, RiskLevel.WRITE_LOCAL, "s1")
    decision = auth.authorize(req)
    assert decision.allowed
    assert decision.requires_confirmation


def test_write_outside_workspace_blocked(auth):
    req = auth.create_request("write", {"path": "/etc/passwd"}, RiskLevel.WRITE_LOCAL, "s1")
    decision = auth.authorize(req)
    assert not decision.allowed


def test_execute_allowlisted_command(auth):
    req = auth.create_request("shell", {"command": "ls -la"}, RiskLevel.EXECUTE, "s1")
    decision = auth.authorize(req)
    assert decision.allowed


def test_execute_blocked_command(auth):
    req = auth.create_request("shell", {"command": "rm -rf /"}, RiskLevel.EXECUTE, "s1")
    decision = auth.authorize(req)
    assert not decision.allowed


def test_external_blocked_by_default(auth):
    req = auth.create_request("http", {"url": "https://example.com"}, RiskLevel.EXTERNAL, "s1")
    decision = auth.authorize(req)
    assert not decision.allowed


def test_external_can_be_enabled():
    auth = ActionAuthorizer(workspace_root="/project", allow_external=True)
    req = auth.create_request("http", {"url": "https://example.com"}, RiskLevel.EXTERNAL, "s1")
    decision = auth.authorize(req)
    assert decision.allowed


def test_dangerous_patterns_blocked():
    auth = ActionAuthorizer(workspace_root="/project")
    dangerous = ["rm -rf /", "dd if=/dev/zero", "> /dev/sda", "chmod 777 /"]
    for cmd in dangerous:
        req = auth.create_request("shell", {"command": cmd}, RiskLevel.EXECUTE, "s1")
        decision = auth.authorize(req)
        assert not decision.allowed, f"Should block: {cmd}"

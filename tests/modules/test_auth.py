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


def test_write_outside_workspace_requires_confirmation(auth):
    """Outside workspace: allowed=True, requires_confirmation=True (interactive mode)."""
    req = auth.create_request("write", {"path": "/etc/passwd"}, RiskLevel.WRITE_LOCAL, "s1")
    decision = auth.authorize(req)
    assert decision.allowed
    assert decision.requires_confirmation


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


def test_dangerous_pattern_reason_names_pattern():
    """L.5 — the denial reason names the matched dangerous pattern, not a generic message."""
    auth = ActionAuthorizer(workspace_root="/project")
    req = auth.create_request("shell", {"command": "rm -rf /"}, RiskLevel.EXECUTE, "s1")
    decision = auth.authorize(req)
    assert not decision.allowed
    assert "rm -rf" in decision.reason


def test_scoped_write_inside_scope_allowed():
    auth = ActionAuthorizer(workspace_root="/project", allowed_paths=["src/foo"])
    # A write within the allowed scope is permitted (subject to confirmation).
    req = auth.create_request("write", {"path": "/project/src/foo/bar.py"}, RiskLevel.WRITE_LOCAL, "s1")
    decision = auth.authorize(req)
    assert decision.allowed
    assert decision.requires_confirmation


def test_scoped_write_outside_scope_denied():
    auth = ActionAuthorizer(workspace_root="/project", allowed_paths=["src/foo"])
    # A write to a sibling directory is hard-rejected, even though in-workspace.
    req = auth.create_request("write", {"path": "/project/src/baz.py"}, RiskLevel.WRITE_LOCAL, "s1")
    decision = auth.authorize(req)
    assert not decision.allowed
    assert "file scope" in decision.reason


def test_scoped_edit_outside_scope_denied():
    # The same allow-list governs `edit`, since it is also a WRITE_LOCAL tool.
    auth = ActionAuthorizer(workspace_root="/project", allowed_paths=["src/foo"])
    req = auth.create_request("edit", {"path": "/project/other/x.py"}, RiskLevel.WRITE_LOCAL, "s1")
    decision = auth.authorize(req)
    assert not decision.allowed


def test_scope_as_file_path_allows_containing_dir():
    # A file-named scope (e.g. "src/a.py") is normalized to its directory, so
    # writing a sibling in the same directory is allowed.
    auth = ActionAuthorizer(workspace_root="/project", allowed_paths=["src/a.py"])
    req = auth.create_request("write", {"path": "/project/src/b.py"}, RiskLevel.WRITE_LOCAL, "s1")
    decision = auth.authorize(req)
    assert decision.allowed


def test_no_scope_unchanged():
    # Without allowed_paths the authorizer behaves exactly as before.
    auth = ActionAuthorizer(workspace_root="/project", confirmation_enabled=True)
    req = auth.create_request("write", {"path": "/project/anywhere.py"}, RiskLevel.WRITE_LOCAL, "s1")
    decision = auth.authorize(req)
    assert decision.allowed
    assert decision.requires_confirmation

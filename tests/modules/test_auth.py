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


def test_web_search_allowed_by_default(auth):
    """web_search must run under the default authorizer. When it was
    RiskLevel.EXTERNAL the default allow_external=False hard-denied it
    ('External tools are disabled'), so the LLM fell back to writing Python
    scripts to search — burning tokens. Demoted to READ_ONLY it runs by
    default. This test fails if web_search is reverted to EXTERNAL."""
    from synapse.modules.tools.web_search import WebSearchTool
    req = auth.create_request(
        "web_search", {"query": "南京下周天气"}, WebSearchTool.risk_level, "s1",
    )
    decision = auth.authorize(req)
    assert decision.allowed, f"web_search denied: {decision.reason}"


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


def test_security_config_allow_external_default_false():
    """security.allow_external defaults to False so the heavier external
    tools (web/browser/db) stay blocked unless explicitly opted in."""
    from synapse.config.schema import SecurityConfig
    assert SecurityConfig().allow_external is False
    assert SecurityConfig(allow_external=True).allow_external is True


def test_synapse_wires_allow_external_from_config_override():
    """Synapse(config_path=None, allow_external=True) must propagate to the
    resolved ActionAuthorizer so EXTERNAL tools are allowed at runtime."""
    from synapse.adapters.library import Synapse
    from synapse.modules.security.auth import ActionAuthorizer

    off = Synapse(provider="deepseek", model="deepseek-v4-pro", config_path=None)
    assert off._container.resolve(ActionAuthorizer).allow_external is False

    on = Synapse(provider="deepseek", model="deepseek-v4-pro", config_path=None, allow_external=True)
    assert on._container.resolve(ActionAuthorizer).allow_external is True


def test_enable_external_tools_flag_also_allows_auth():
    """The enable_external_tools flag registers the external tools AND must
    also let them through auth (otherwise they'd register but be denied)."""
    from synapse.adapters.library import Synapse
    from synapse.modules.security.auth import ActionAuthorizer

    on = Synapse(
        provider="deepseek", model="deepseek-v4-pro", config_path=None,
        enable_external_tools=True,
    )
    assert on._container.resolve(ActionAuthorizer).allow_external is True


def _shell_req(command):
    auth = ActionAuthorizer(workspace_root="/project")
    return auth, auth.create_request("shell", {"command": command}, RiskLevel.EXECUTE, "s1")


def test_shell_chain_validates_every_segment():
    """`a && b` must not hide a disallowed/confirm-required command behind an
    allowlisted first token."""
    auth, req = _shell_req("git status && mysqldump --all-databases")
    assert not auth.authorize(req).allowed


def test_shell_chain_confirm_required_in_tail():
    """A python segment anywhere in the chain forces confirmation."""
    auth, req = _shell_req("git status && python run_tests.py")
    decision = auth.authorize(req)
    assert decision.allowed
    assert decision.requires_confirmation


def test_shell_pipe_into_python_confirmation():
    auth, req = _shell_req("echo hi | python -c 'print(1)'")
    decision = auth.authorize(req)
    assert decision.allowed
    assert decision.requires_confirmation


def test_shell_sensitive_read_requires_confirmation():
    """cat /etc/shadow via an allowlisted command must not be silent."""
    auth, req = _shell_req("cat /etc/shadow")
    decision = auth.authorize(req)
    assert decision.allowed
    assert decision.requires_confirmation


def test_shell_case_insensitive_dangerous():
    auth, req = _shell_req("RM -RF /")
    assert not auth.authorize(req).allowed


def test_shell_redirection_outside_workspace_denied():
    auth, req = _shell_req("echo hi > /etc/cron.d/y")
    assert not auth.authorize(req).allowed


def test_shell_redirection_relative_ok():
    auth, req = _shell_req("echo hi > out.txt")
    assert auth.authorize(req).allowed

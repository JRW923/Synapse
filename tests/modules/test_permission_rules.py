"""Permission rules (ask/allow/deny) + session-scoped approval memory."""

from synapse.modules.security.auth import ActionAuthorizer
from synapse.protocols.sandbox import AuthRequest
from synapse.protocols.tool import RiskLevel


def _auth(**kw):
    return ActionAuthorizer(workspace_root=".", **kw)


def _req(command, tool="shell", risk=RiskLevel.EXECUTE):
    return _auth().create_request(tool, {"command": command}, risk, "s1")


def test_deny_rule_blocks_before_any_check():
    a = _auth(permission_rules=[("shell", "deny")])
    d = a.authorize(a.create_request("shell", {"command": "ls"}, RiskLevel.EXECUTE, "s"))
    assert not d.allowed and "permission rule" in d.reason


def test_allow_rule_skips_confirmation():
    a = _auth(permission_rules=[("todo_*", "allow")])
    d = a.authorize(a.create_request("todo_write", {}, RiskLevel.META, "s"))
    assert d.allowed and not d.requires_confirmation


def test_wildcard_rule_matches():
    a = _auth(permission_rules=[("web*", "deny")])
    d = a.authorize(a.create_request("web_fetch", {"url": "http://x"}, RiskLevel.READ_ONLY, "s"))
    assert not d.allowed


def test_ask_rule_falls_through_to_default_logic():
    a = _auth(permission_rules=[("shell", "ask")])
    d = a.authorize(a.create_request("shell", {"command": "ls"}, RiskLevel.EXECUTE, "s"))
    assert d.allowed  # same as no rule


def test_remembered_signature_skips_confirmation():
    a = _auth()
    req = a.create_request("shell", {"command": "pytest -q"}, RiskLevel.EXECUTE, "s")
    a.remember_approval(req)
    d = a.authorize(req)
    assert d.allowed and not d.requires_confirmation
    assert "approved earlier" in d.reason


def test_memory_is_signature_scoped_not_tool_scoped():
    a = _auth()
    a.remember_approval(a.create_request("shell", {"command": "pytest -q"},
                                         RiskLevel.EXECUTE, "s"))
    # Same tool, different command first-token → still asks.
    d = a.authorize(a.create_request("shell", {"command": "curl http://x"},
                                     RiskLevel.EXECUTE, "s"))
    assert d.requires_confirmation


def test_shell_signature_uses_first_token():
    r1 = _req("pytest tests/x.py")
    r2 = _req("pytest tests/y.py -q")
    a = _auth()
    assert a.approval_signature(r1) == a.approval_signature(r2)


def test_path_signature_uses_parent_dir():
    a = _auth()
    r1 = a.create_request("write", {"path": "src/a.py"}, RiskLevel.WRITE_LOCAL, "s")
    r2 = a.create_request("write", {"path": "src/b.py"}, RiskLevel.WRITE_LOCAL, "s")
    r3 = a.create_request("write", {"path": "docs/c.md"}, RiskLevel.WRITE_LOCAL, "s")
    assert a.approval_signature(r1) == a.approval_signature(r2)
    assert a.approval_signature(r1) != a.approval_signature(r3)


def test_memory_does_not_override_hard_denials():
    a = _auth()
    a.remember_approval(a.create_request("shell", {"command": "rm"},
                                         RiskLevel.EXECUTE, "s"))
    # Dangerous patterns are checked before memory softening.
    d = a.authorize(a.create_request("shell", {"command": "rm -rf /"},
                                     RiskLevel.EXECUTE, "s"))
    assert not d.allowed

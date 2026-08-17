"""Action-Time Authorization — evaluates tool calls before execution."""

import shlex
from collections.abc import Callable, Awaitable
from pathlib import Path
from synapse.protocols.tool import RiskLevel
from synapse.protocols.sandbox import AuthRequest, AuthDecision

_SAFE_REDIRECT_TARGETS = {
    "/dev/null", "/dev/tty", "/dev/stdin", "/dev/stdout", "/dev/stderr",
}
#: Token-level control operators — only these split a chain; the same text
#: inside quotes stays part of one argument token and cannot hide a segment.
_CONTROL_OPERATORS = {"&&", "||", ";", "|", "\n"}

#: Callback signature for interactive confirmation.
#: Receives the AuthRequest, returns True if user approves.
AuthCallback = Callable[[AuthRequest], Awaitable[bool]]


class ActionAuthorizer:
    """Evaluates tool call authorization based on risk level, workspace, and allowlists.

    In interactive mode (chat), workspace-bounded writes trigger a user
    confirmation instead of being hard-blocked.  In non-interactive mode
    (run / serve) there is no one to prompt, so a ``requires_confirmation``
    decision is auto-denied unless the caller supplies a confirm callback
    that approves it (e.g. an explicit opt-in like ``--yes``).
    """

    DANGEROUS_PATTERNS = [
        "rm -rf /",
        "rm -rf --no-preserve-root",
        "dd if=/dev/zero",
        "> /dev/sda",
        "mkfs.",
        ":(){ :|:& };:",  # fork bomb
        "chmod 777 /",
        "chown -R",
        # Pipe-to-shell: downloading and executing untrusted scripts in one step.
        "| sh", "| bash", "|bash", "|sh", "| /bin/sh", "| /bin/bash",
    ]

    # Paths that read tools may reach via the allowlist but are sensitive
    # enough to require explicit confirmation rather than silent read.
    SENSITIVE_PATHS = [
        "/etc/passwd", "/etc/shadow", "/etc/gshadow", "/etc/sudoers",
        "/etc/ssh/", "/root/", "/.ssh/", "id_rsa", "id_ed25519",
        "credentials", "secrets", ".env",
    ]

    ALWAYS_ALLOWED_COMMANDS = [
        "ls", "echo", "cat", "head", "tail", "wc", "pwd", "env",
        "git", "pip", "npm", "node", "cargo",
        "go", "pytest", "mypy", "ruff", "black",
        "mkdir", "find", "type", "dir", "del", "rm",
        # PowerShell cmdlets (Windows-first setups) — read-only set plus the
        # rm/del/new-item equivalents already allowlisted above.
        "get-content", "get-childitem", "get-item", "get-process",
        "get-location", "measure-object", "select-string", "select-object",
        "test-path", "where-object", "sort-object", "group-object",
        "compare-object", "format-table", "format-list", "out-string",
        "new-item", "remove-item", "copy-item", "move-item", "set-content",
    ]

    # Commands that can chain arbitrary code execution or network access.
    # Allowed, but only after explicit user confirmation (never silent).
    CONFIRM_REQUIRED_COMMANDS = {"python", "python3", "curl", "wget"}

    def __init__(
        self,
        workspace_root: str = ".",
        allow_external: bool = False,
        confirmation_enabled: bool = True,
        allowed_paths: list[str] | None = None,
        allowlisted_commands: list[str] | None = None,
        bypass_policy: bool = False,
        permission_rules: list[tuple[str, str]] | None = None,
    ):
        self.workspace_root = Path(workspace_root).resolve()
        self.allow_external = allow_external
        self.confirmation_enabled = confirmation_enabled
        self.bypass_policy = bypass_policy
        #: Per-tool三态规则 (pattern, action)：action ∈ ask | allow | deny。
        #: pattern 支持 fnmatch 通配（"shell"、"web*"、"*"）。
        self._permission_rules = list(permission_rules or [])
        #: 会话内批准记忆：签名（命令首 token / 父目录 / 工具名）→ 已批准。
        self._approved_signatures: set[str] = set()
        self._allowed_paths = [self._resolve_scope_boundary(p) for p in (allowed_paths or [])]
        self._allowlisted_commands = (
            {c.lower() for c in allowlisted_commands}
            if allowlisted_commands is not None
            else set(self.ALWAYS_ALLOWED_COMMANDS) | set(self.CONFIRM_REQUIRED_COMMANDS)
        )

    def create_request(
        self, tool_name: str, params: dict, risk_level: RiskLevel, session_id: str,
        user_id: str | None = None,
    ) -> AuthRequest:
        return AuthRequest(
            tool_name=tool_name,
            tool_params=params,
            risk_level=risk_level.value if isinstance(risk_level, RiskLevel) else risk_level,
            session_id=session_id,
            user_id=user_id,
        )

    def authorize(self, request: AuthRequest) -> AuthDecision:
        if self.bypass_policy:
            return AuthDecision(
                allowed=True,
                reason="Action-time authorization disabled for evaluation ablation",
            )

        # --- 三态规则表：deny 硬拒 / allow 静默放行 / ask 走默认流程 ----------
        rule = self._rule_for(request.tool_name)
        if rule == "deny":
            return AuthDecision(
                allowed=False,
                reason=f"Denied by permission rule for tool '{request.tool_name}'",
            )
        if rule == "allow":
            return AuthDecision(
                allowed=True,
                reason=f"Allowed by permission rule for tool '{request.tool_name}'",
            )

        decision = self._decide(request)

        # --- 记忆化批准：同一签名（如同一命令首 token）本会话已批准 → 免确认 --
        if decision.requires_confirmation and self._is_remembered(request):
            return AuthDecision(
                allowed=decision.allowed,
                reason=f"{decision.reason} [signature approved earlier this session]",
            )
        return decision

    def _rule_for(self, tool_name: str) -> str | None:
        """First matching permission rule's action (ask | allow | deny), if any."""
        import fnmatch
        for pattern, action in self._permission_rules:
            if fnmatch.fnmatch(tool_name, pattern):
                return action
        return None

    @staticmethod
    def approval_signature(request: AuthRequest) -> str:
        """Coarse-grained identity of a call for approval memory.

        shell → command's first token ("yes to all pytest", not "yes to all
        shell"); path tools → parent directory; everything else → tool name.
        """
        params = getattr(request, "tool_params", {}) or {}
        if getattr(request, "tool_name", "") == "shell":
            cmd = str(params.get("command", ""))
            parts = cmd.strip().split(None, 1)
            return f"shell:{parts[0]}" if parts else "shell:"
        path = params.get("path")
        if path:
            return f"{request.tool_name}:{Path(path).parent}"
        return request.tool_name

    def remember_approval(self, request: AuthRequest) -> None:
        """Record that the user approved this call's signature (session-scoped)."""
        self._approved_signatures.add(self.approval_signature(request))

    def _is_remembered(self, request: AuthRequest) -> bool:
        return self.approval_signature(request) in self._approved_signatures

    def _decide(self, request: AuthRequest) -> AuthDecision:
        risk = request.risk_level

        # --- READ_ONLY: allow, but gate sensitive files ---------------------------
        if risk == RiskLevel.READ_ONLY.value:
            target = request.tool_params.get("path", "")
            if target and self._is_sensitive(target):
                return AuthDecision(
                    allowed=True,
                    reason=f"Read of sensitive path '{target}' requires confirmation",
                    requires_confirmation=True,
                )
            return AuthDecision(allowed=True, reason="Read-only operation")

        # --- WRITE_LOCAL ------------------------------------------------------------
        if risk == RiskLevel.WRITE_LOCAL.value:
            target = request.tool_params.get("path", "")
            # Hard isolation: if a write allow-list (file scope) is set, any
            # target outside it is rejected outright — even if inside workspace.
            if target and self._allowed_paths and not self._within_any_scope(target):
                scopes = ", ".join(str(s) for s in self._allowed_paths)
                return AuthDecision(
                    allowed=False,
                    reason=f"Write target '{target}' is outside the assigned file scope(s): {scopes}",
                )
            if target and self._is_in_workspace(target):
                return AuthDecision(
                    allowed=True,
                    reason="Write within workspace",
                    requires_confirmation=self.confirmation_enabled,
                )
            # Outside workspace → ask the user if interactive, deny if not
            return AuthDecision(
                allowed=True,
                reason=(
                    f"Write target '{target}' is outside workspace "
                    f"({self.workspace_root}). Requires user approval."
                ),
                requires_confirmation=True,
            )

        # --- EXECUTE ----------------------------------------------------------------
        if risk == RiskLevel.EXECUTE.value:
            command = request.tool_params.get("command", "")
            matched = self._is_dangerous(command)
            if matched:
                return AuthDecision(
                    allowed=False,
                    reason=f"Command matches dangerous pattern: '{matched}'",
                )
            # cat /etc/shadow / rm ~/.ssh/id_rsa — sensitive paths via shell.
            sensitive = self._sensitive_touched(command)
            if sensitive:
                return AuthDecision(
                    allowed=True,
                    reason=f"Command touches sensitive path: '{sensitive}' — confirmation required",
                    requires_confirmation=True,
                )
            # echo hi > /etc/cron.d/y — redirection outside the workspace.
            bad_redirect = self._unsafe_redirection(command)
            if bad_redirect:
                return AuthDecision(
                    allowed=False,
                    reason=f"Command redirects to unsafe target: '{bad_redirect}'",
                )
            segments = self._segments(command)
            offending = next(
                (seg[0] for seg in (segments or []) if seg[0] not in self._allowlisted_commands),
                self._first_token(command),
            )
            if not self._is_allowlisted(command):
                return AuthDecision(
                    allowed=False,
                    reason=f"Command not in allowlist: {offending}",
                )
            # ponytail: commands that can chain arbitrary code/network access are
            # allowed only after explicit user confirmation, never silent.
            if self._requires_confirmation(command):
                return AuthDecision(
                    allowed=True,
                    reason=f"Command '{self._first_token(command)}' can spawn code/network — confirmation required",
                    requires_confirmation=True,
                )
            return AuthDecision(
                allowed=True,
                reason="Command in allowlist",
                requires_confirmation=self.confirmation_enabled,
            )

        # --- EXTERNAL ---------------------------------------------------------------
        if risk == RiskLevel.EXTERNAL.value:
            if self.allow_external:
                return AuthDecision(
                    allowed=True, reason="External access enabled",
                    requires_confirmation=True,
                )
            return AuthDecision(allowed=False, reason="External tools are disabled")

        # --- META -------------------------------------------------------------------
        if risk == RiskLevel.META.value:
            return AuthDecision(allowed=True, reason="Meta/experimental tool")

        return AuthDecision(allowed=False, reason=f"Unknown risk level: {risk}")

    # ------------------------------------------------------------------
    def _is_in_workspace(self, path_str: str) -> bool:
        try:
            resolved = Path(path_str).resolve()
            return resolved.is_relative_to(self.workspace_root)
        except (ValueError, OSError):
            return False

    def _resolve_scope_boundary(self, scope: str) -> Path:
        """Normalize a ``file_scope`` string into a directory boundary (resolved).

        ponytail: scope is treated as a DIRECTORY boundary. A file-named scope
        (an existing file, or a path whose name contains a dot) means "the
        directory containing it"; a non-existent path without an extension is
        treated as a directory. Finer file-level granularity is a future
        upgrade.
        """
        p = Path(scope)
        if not p.is_absolute():
            p = self.workspace_root / p
        p = p.resolve()
        if p.is_dir():
            return p
        if p.is_file() or "." in p.name:
            return p.parent
        return p

    def _within_any_scope(self, target: str) -> bool:
        t = Path(target)
        if not t.is_absolute():
            t = self.workspace_root / t
        try:
            t = t.resolve()
        except (ValueError, OSError):
            return False
        for scope in self._allowed_paths:
            if t == scope or t.is_relative_to(scope):
                return True
        return False

    @staticmethod
    def _tokenize(command: str) -> list[str] | None:
        """Shell-tokenize *command* (quotes respected), or None when it cannot
        be parsed (unbalanced quotes) — callers must then deny, not guess.

        ponytail: posix shlex eats backslashes, so a bare Windows path argument
        (`pytest C:\\repo\\x`) loses separators in argument tokens. Only token
        boundaries and operator structure are consumed here, and every
        mis-parse fails toward the strict side, so this stays safe.
        """
        try:
            lex = shlex.shlex(command, posix=True, punctuation_chars=True)
            lex.whitespace_split = True
            return list(lex)
        except ValueError:
            return None

    @classmethod
    def _segments(cls, command: str) -> list[list[str]] | None:
        """Split the token stream at real (unquoted) control operators.

        `git commit -m "a && b"` is ONE git command — the quoted `&&` never
        reaches the operator boundary. Returns None when unparseable, and
        [] when a chain has an empty segment (`a && && b`), which callers
        treat as not-allowlisted.
        """
        tokens = cls._tokenize(command)
        if tokens is None:
            return None
        segments: list[list[str]] = [[]]
        for tok in tokens:
            if tok in _CONTROL_OPERATORS:
                segments.append([])
            elif not segments[-1] and tok == "(":
                # Leading `(` of a sub-expression (PowerShell `(cmd | cmd).Count`)
                # — the segment head is the cmdlet after it. `$(`/backtick still
                # trip _has_command_substitution → confirmation, so this only
                # widens the allowlist lookup, never the confirmation gate.
                continue
            else:
                segments[-1].append(tok)
        if any(not seg for seg in segments):
            return []
        return segments

    @staticmethod
    def _first_token(command: str) -> str:
        tokens = command.strip().split()
        return tokens[0] if tokens else ""

    @classmethod
    def _has_command_substitution(cls, command: str) -> bool:
        """True when the token stream carries an unquoted `$(...)`, `(...)`
        subshell, or backtick — any of these can execute arbitrary code from
        inside an otherwise allowlisted command (`echo $(python evil.py)`)."""
        tokens = cls._tokenize(command)
        if tokens is None:
            return True  # unparseable → assume the worst
        return any(
            tok in ("(", ")") or "`" in tok or tok.startswith("$(")
            for tok in tokens
        )

    def _is_dangerous(self, command: str) -> str | None:
        """Return the first dangerous pattern matched by *command*, else None.

        Case-insensitive so `RM -RF /` can't dodge `rm -rf /`. L.5 — naming
        the matched pattern makes the denial reason self-explanatory.
        """
        lower = command.lower()
        for pattern in self.DANGEROUS_PATTERNS:
            if pattern.lower() in lower:
                return pattern
        return None

    def _sensitive_touched(self, command: str) -> str | None:
        """Return the first sensitive path name referenced by *command*, else None."""
        lower = command.lower()
        for sensitive in self.SENSITIVE_PATHS:
            if sensitive.lower() in lower:
                return sensitive
        return None

    @staticmethod
    def _is_rooted(path_str: str) -> bool:
        """True for `/etc/x`, `~/x`, `\\server\\x`, `C:/x` — anything that
        references the filesystem root rather than a child of the cwd.

        is_absolute() is not used because on Windows `/etc/cron.d/y` resolves
        to a drive path but is_absolute() still returns False.
        """
        if path_str.startswith(("/", "\\", "~")):
            return True
        return len(path_str) >= 2 and path_str[1] == ":"

    def _unsafe_redirection(self, command: str) -> str | None:
        """Return a redirect target that escapes the workspace (or a sensitive
        path), else None. e.g. `echo hi > /etc/cron.d/y`.

        Token-based: a quoted `>` (`git commit -m "a > b"`) is argument text,
        not a redirect, and does not trip this check.
        """
        tokens = self._tokenize(command)
        if tokens is None:
            return command  # unparseable → caller denies
        for i, tok in enumerate(tokens):
            if tok not in (">", ">>", "<"):
                continue
            if i + 1 >= len(tokens):
                return "<missing target>"
            target = tokens[i + 1]
            if target.startswith("&"):
                continue  # fd redirect like 2>&1
            if target in _SAFE_REDIRECT_TARGETS:
                continue
            if ".." in target:
                return target
            if self._is_sensitive(target):
                return target
            if self._is_rooted(target):
                expanded = Path(target).expanduser()
                try:
                    if not expanded.resolve().is_relative_to(self.workspace_root):
                        return target
                except (ValueError, OSError):
                    return target
        return None

    def _is_sensitive(self, path_str: str) -> bool:
        normalized = path_str.replace("\\", "/")
        return any(
            sensitive in normalized for sensitive in self.SENSITIVE_PATHS
        )

    def _is_allowlisted(self, command: str) -> bool:
        segments = self._segments(command)
        if not segments:
            return False  # unparseable, empty chain segment, or empty command
        # CONFIRM_REQUIRED commands (python/curl/wget) are allowed but gated by
        # the confirmation branch; without them here `python x.py` would be
        # denied as "not in allowlist" before confirmation was ever offered.
        # Case-insensitive: PowerShell cmdlets are unconventionally cased
        # (Get-Content vs get-content) while POSIX commands stay lowercase.
        allowed = self._allowlisted_commands
        return all(seg[0].lower() in allowed for seg in segments)

    def _requires_confirmation(self, command: str) -> bool:
        if self._has_command_substitution(command):
            return True
        segments = self._segments(command)
        if not segments:
            return True  # unparseable → never silent
        return any(
            seg[0].lower() in self.CONFIRM_REQUIRED_COMMANDS for seg in segments
        )

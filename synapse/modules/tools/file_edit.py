"""Edit file tool — exact string replacement with fuzzy fallback."""

import difflib
from pathlib import Path
from synapse.protocols.tool import Tool, ToolSchema, ToolResult, ToolCallMetadata, RiskLevel, ToolCategory
from synapse.modules.tools.workspace import WorkspacePathError, resolve_workspace_path


def _fuzzy_unique_match(content: str, old: str):
    """Whitespace-insensitive match: unique window whose stripped lines equal
    the stripped old_string lines. Returns (start_line, file_slice, deltas)
    on a unique hit, "ambiguous" on several, None on zero.

    ``deltas`` is the per-line indent difference (file minus old) so the
    replacement can be re-indented to what the file actually has — models
    routinely drop the nesting when reproducing a snippet.
    """
    lines = content.splitlines(keepends=True)
    old_lines = old.splitlines(keepends=True)
    n = len(old_lines)
    if n == 0 or n > len(lines):
        return None
    stripped_old = [l.strip() for l in old_lines]
    hits = []
    for i in range(len(lines) - n + 1):
        window = lines[i:i + n]
        if [l.strip() for l in window] == stripped_old:
            hits.append((i, window))
    if not hits:
        return None
    if len(hits) > 1:
        return "ambiguous"
    i, window = hits[0]
    deltas = [(len(f) - len(f.lstrip())) - (len(o) - len(o.lstrip()))
              for f, o in zip(window, old_lines)]
    return i, "".join(window), deltas


def _reindent(new: str, deltas: list[int]) -> str:
    """Shift each non-empty line of *new* by its per-line indent delta."""
    out = []
    for k, line in enumerate(new.splitlines(keepends=True)):
        d = deltas[k] if k < len(deltas) else (deltas[-1] if deltas else 0)
        if not line.strip() or d == 0:
            out.append(line)
        elif d > 0:
            out.append(" " * d + line)
        elif line.startswith(" " * -d):
            out.append(line[-d:])
        else:
            out.append(line)
    return "".join(out)


def _closest_snippet(content: str, old: str, max_lines: int = 5):
    """Best-effort 'did you mean' — lines of the file most similar to old."""
    lines = content.splitlines()
    old_lines = old.splitlines()
    if not old_lines or not lines:
        return ""
    best_ratio, best_i = 0.0, 0
    for i in range(len(lines)):
        ratio = difflib.SequenceMatcher(
            None, lines[i].strip(), old_lines[0].strip()).ratio()
        if ratio > best_ratio:
            best_ratio, best_i = ratio, i
    snippet = "\n".join(lines[best_i:best_i + max_lines])
    return f"closest match near line {best_i + 1} (similarity {best_ratio:.2f}):\n{snippet}"


def _line_numbers(content: str, old: str) -> list[int]:
    """1-based line numbers of each occurrence of old."""
    line = 1
    numbers = []
    idx = 0
    while True:
        found = content.find(old, idx)
        if found == -1:
            break
        numbers.append(content.count("\n", 0, found) + 1)
        idx = found + 1
    return numbers


class EditTool:
    name = "edit"
    description = "Replace a specific string in a file. Use for targeted edits: fix bugs, rename symbols, update configs."
    parameters = ToolSchema(
        name="edit",
        description="Replace old_string with new_string in a file. Both must match exactly (including whitespace); "
                    "if only indentation differs a fuzzy match is attempted. Use for targeted changes.",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file"},
                "old_string": {"type": "string", "description": "Exact text to replace"},
                "new_string": {"type": "string", "description": "Replacement text"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    )
    requires_sandbox = True
    risk_level = RiskLevel.WRITE_LOCAL
    category = ToolCategory.FILE

    def __init__(self, workspace_root: str | None = None):
        self._workspace_root = Path(workspace_root).resolve() if workspace_root else None

    async def execute(self, params: dict, sandbox=None) -> ToolResult:
        try:
            path = resolve_workspace_path(params["path"], self._workspace_root)
        except (WorkspacePathError, ValueError) as e:
            return ToolResult(
                success=False, output="", error=str(e),
                metadata=ToolCallMetadata(tool_name="edit"),
            )
        old = params["old_string"]
        new = params["new_string"]
        meta = ToolCallMetadata(tool_name="edit")
        meta.files_touched = [str(path)]

        try:
            content = path.read_text(encoding="utf-8")
            count = content.count(old)
            if count == 0:
                # Fuzzy rescue: unique whitespace-insensitive match.
                fuzzy = _fuzzy_unique_match(content, old)
                if fuzzy == "ambiguous":
                    return ToolResult(success=False, output="", error=(
                        "old_string not found; a whitespace-insensitive match is "
                        "ambiguous (several candidates). Re-read the file and copy "
                        "the exact text including indentation."), metadata=meta)
                if fuzzy is not None:
                    _, file_slice, deltas = fuzzy
                    new_content = content.replace(
                        file_slice, _reindent(new, deltas), 1)
                    # ponytail: newline="" preserves the file's original line endings
                    # instead of letting write_text translate LF -> CRLF on Windows,
                    # which rewrote the entire file as one giant diff on every edit.
                    path.write_text(new_content, encoding="utf-8", newline="")
                    return ToolResult(success=True, output=(
                        f"Replaced 1 occurrence in {path} via fuzzy match "
                        f"(indentation auto-aligned)"), metadata=meta)
                hint = _closest_snippet(content, old)
                return ToolResult(success=False, output="", error=(
                    f"old_string not found in file. {hint}"
                    if hint else "old_string not found in file"), metadata=meta)
            if count > 1:
                where = ", ".join(str(n) for n in _line_numbers(content, old)[:8])
                return ToolResult(success=False, output="", error=(
                    f"old_string is not unique in file — found {count} occurrences "
                    f"at lines {where}. Include surrounding lines to make it "
                    f"unique."), metadata=meta)
            new_content = content.replace(old, new)
            # ponytail: newline="" preserves the file's original line endings
            # instead of letting write_text translate LF -> CRLF on Windows,
            # which rewrote the entire file as one giant diff on every edit.
            path.write_text(new_content, encoding="utf-8", newline="")
            return ToolResult(success=True, output=f"Replaced 1 occurrence in {path}", metadata=meta)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e), metadata=meta)

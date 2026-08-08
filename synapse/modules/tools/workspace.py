"""Workspace path guards shared by read-only file tools."""

from pathlib import Path


class WorkspacePathError(ValueError):
    """Raised when a tool path escapes its configured workspace."""


def resolve_workspace_path(raw: str, workspace_root: Path | None) -> Path:
    """Resolve *raw* and reject paths outside the workspace, including symlinks."""
    path = Path(raw)
    if workspace_root is None:
        return path.resolve()
    root = workspace_root.resolve()
    candidate = path if path.is_absolute() else root / path
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise WorkspacePathError(
            f"Path '{raw}' is outside workspace '{root}'"
        )
    return resolved

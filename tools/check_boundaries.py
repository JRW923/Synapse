"""Validate Synapse package dependency boundaries.

The project intentionally keeps runtime composition in ``core`` and
``adapters``. This checker validates package directions rather than imposing
the false rule that every core file must be implementation-free.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SYNAPSE = PROJECT_ROOT / "synapse"

_STDLIB = set(sys.stdlib_module_names) | {"__future__"}
_THIRD_PARTY = {
    "anthropic", "chromadb", "fastapi", "google", "httpx", "mcp",
    "openai", "playwright", "prompt_toolkit", "pydantic", "qdrant_client",
    "rich", "sentence_transformers", "tiktoken", "uvicorn", "yaml",
}

# Dependencies point from a package to packages it may import. ``core`` owns
# the runtime flow and can use concrete modules, but no lower layer may import
# an adapter, configuration, or evaluation implementation.
_ALLOWED = {
    "protocols": {"protocols"},
    "config": {"config", "core", "protocols"},
    "core": {"config", "core", "modules", "protocols"},
    "modules": {"core", "modules", "protocols"},
    "eval": {"core", "eval", "modules", "protocols"},
    "adapters": {"adapters", "config", "core", "eval", "modules", "protocols"},
}


def _module_parts(node: ast.AST) -> list[str]:
    """Return imported module names, preserving the Synapse package path."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.module:
        return [node.module]
    return []


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return {
        module
        for node in ast.walk(tree)
        for module in _module_parts(node)
    }


def _owner(path: Path) -> str:
    return path.relative_to(SYNAPSE).parts[0]


def check_file(path: Path) -> list[str]:
    """Return dependency-boundary violations for one source file."""
    owner = _owner(path)
    allowed = _ALLOWED.get(owner)
    if allowed is None:
        return []

    violations: list[str] = []
    for module in sorted(_imports(path)):
        root = module.split(".")[0]
        if root in _STDLIB or root in _THIRD_PARTY:
            continue
        if root != "synapse":
            violations.append(f"{path.relative_to(PROJECT_ROOT)}: unknown import '{module}'")
            continue
        if module == "synapse":
            continue
        parts = module.split(".")
        target = parts[1] if len(parts) > 1 else ""
        if target not in allowed:
            violations.append(
                f"{path.relative_to(PROJECT_ROOT)}: {owner} must not import '{module}'"
            )
    return violations


def check_project(root: Path = SYNAPSE) -> list[str]:
    """Return all package-boundary violations below *root*."""
    return [
        violation
        for path in sorted(root.rglob("*.py"))
        if "__pycache__" not in path.parts
        for violation in check_file(path)
    ]


def main() -> int:
    violations = check_project()
    if violations:
        print("Architecture boundary violations:")
        for violation in violations:
            print(f"  {violation}")
        return 1
    print("Architecture boundaries verified.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

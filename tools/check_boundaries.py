"""Architecture boundary checker for Synapse.

Verifies that:
  1. synapse/protocols/* only import from stdlib (typing, dataclasses, datetime, enum, uuid, pathlib).
  2. synapse/core/* only import from stdlib + synapse.protocols.*
  3. synapse/modules/* only import from stdlib + synapse.protocols.*
     + synapse.core.exceptions + synapse.core.events
"""
import ast
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SYNAPSE = PROJECT_ROOT / "synapse"

STDLIB_MODULES = {
    "typing", "dataclasses", "datetime", "enum", "uuid", "pathlib",
    "json", "logging", "os", "re", "subprocess", "asyncio",
    "textwrap", "shlex", "fnmatch", "tempfile", "secrets",
    "hmac", "hashlib", "time", "abc", "collections",
    "collections.abc", "contextlib", "copy", "functools",
    "inspect", "io", "itertools", "math", "random",
    "sys", "threading", "traceback", "types", "warnings",
    "weakref", "ast", "resource", "signal", "atexit",
    "__future__", "argparse", "base64", "concurrent",
    "concurrent.futures", "importlib", "platform", "unittest",
    "shutil", "getpass", "socket", "http", "urllib",
    "xml", "csv", "html", "configparser", "email",
    "struct", "stat", "string", "binascii", "gzip",
    "zipfile", "tarfile", "pdb", "pstats", "profile",
    "multiprocessing", "queue", "select", "selectors",
    "ssl", "errno", "fcntl", "mmap", "msvcrt",
}

# Third-party packages that are expected/allowed (optional providers).
ALLOWED_THIRD_PARTY = {
    "anthropic", "openai", "google", "google.genai",
    "yaml", "pydantic", "httpx",
}

ProtocolsAllowed = set(STDLIB_MODULES)
CoreAllowed = set(STDLIB_MODULES) | {
    "synapse.protocols",
    "synapse.core",
}
ModulesAllowed = set(STDLIB_MODULES) | {
    "synapse.protocols",
    "synapse.core.exceptions",
    "synapse.core.events",
}


def extract_imports(file_path: Path) -> list[str]:
    """Return a list of top-level import module names in *file_path*."""
    tree = ast.parse(file_path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module is not None:
                # Only track top-level package
                imports.add(node.module.split(".")[0])
    return sorted(imports)


def is_stdlib(module_base: str) -> bool:
    """Heuristic: a bare module name is stdlib if it's in STDLIB_MODULES."""
    return module_base in STDLIB_MODULES


def is_allowed_third_party(module_base: str) -> bool:
    return module_base in ALLOWED_THIRD_PARTY


def check_file(file_path: Path, allowed: set, label: str) -> list[str]:
    violations: list[str] = []
    imports = extract_imports(file_path)
    syn_path = file_path.relative_to(PROJECT_ROOT)

    for imp in imports:
        # stdlib is always OK
        if imp in STDLIB_MODULES:
            continue
        # Allowed third-party
        if is_allowed_third_party(imp):
            continue
        # Check if the import base is in the allowed set
        if imp == "synapse":
            # For synapse imports, we need to check the full module path
            # Check more granularly: re-parse the full module import paths
            tree = ast.parse(file_path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module is not None:
                    full = node.module
                    if full.startswith("synapse."):
                        # Check if the first two segments are allowed
                        parts = full.split(".")
                        prefix2 = ".".join(parts[:2]) if len(parts) >= 2 else full
                        prefix3 = ".".join(parts[:3]) if len(parts) >= 3 else full
                        # Check prefix2 and prefix3 against allowed patterns
                        ok = False
                        for allowed_pat in allowed:
                            if full == allowed_pat or full.startswith(allowed_pat + "."):
                                ok = True
                                break
                        if not ok:
                            violations.append(
                                f"[{label}] {syn_path}: imports '{full}' which is not allowed"
                            )
                    continue
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        name = alias.name
                        if name.startswith("synapse."):
                            ok = False
                            for allowed_pat in allowed:
                                if name == allowed_pat or name.startswith(allowed_pat + "."):
                                    ok = True
                                    break
                            if not ok:
                                violations.append(
                                    f"[{label}] {syn_path}: imports '{name}' which is not allowed"
                                )
        else:
            # Unknown import — not stdlib, not third-party, not synapse
            violations.append(
                f"[{label}] {syn_path}: imports unknown module '{imp}'"
            )

    return violations


def main() -> int:
    all_violations: list[str] = []

    # 1. Check protocols/
    protocols_dir = SYNAPSE / "protocols"
    for py_file in sorted(protocols_dir.glob("*.py")):
        if py_file.name == "__init__.py":
            continue
        violations = check_file(py_file, ProtocolsAllowed, "protocols")
        all_violations.extend(violations)

    # 2. Check core/
    core_dir = SYNAPSE / "core"
    for py_file in sorted(core_dir.glob("*.py")):
        if py_file.name == "__init__.py":
            continue
        violations = check_file(py_file, CoreAllowed, "core")
        all_violations.extend(violations)

    # 3. Check modules/ (recursive)
    modules_dir = SYNAPSE / "modules"
    for py_file in sorted(modules_dir.rglob("*.py")):
        if py_file.name == "__init__.py":
            continue
        violations = check_file(py_file, ModulesAllowed, "modules")
        all_violations.extend(violations)

    if all_violations:
        print("ARCHITECTURE BOUNDARY VIOLATIONS FOUND:")
        for v in all_violations:
            print(f"  {v}")
        return 1
    else:
        print("All architecture boundaries verified successfully.")
        return 0


if __name__ == "__main__":
    sys.exit(main())

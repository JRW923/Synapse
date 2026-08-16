"""Regression checks for package dependency directions."""

import importlib.util
from pathlib import Path


_ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "check_boundaries", _ROOT / "tools" / "check_boundaries.py"
)
assert _SPEC and _SPEC.loader
_BOUNDARIES = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_BOUNDARIES)


def test_project_respects_package_dependency_boundaries():
    assert _BOUNDARIES.check_project() == []

"""EditTool fuzzy fallback — rescues indentation drift, aids self-recovery."""

import asyncio

from synapse.modules.tools.file_edit import EditTool


def _edit(tmp_path, file_text, old, new):
    f = tmp_path / "target.py"
    f.write_text(file_text, encoding="utf-8")
    tool = EditTool(workspace_root=str(tmp_path))
    result = asyncio.run(tool.execute(
        {"path": str(f), "old_string": old, "new_string": new}))
    return result, (f.read_text(encoding="utf-8") if f.exists() else "")


def test_fuzzy_match_rescues_indentation_drift(tmp_path):
    file_text = "def a():\n    if x:\n        return 1\n    return 2\n"
    # Model's old_string lost the nested indentation.
    result, after = _edit(
        tmp_path, file_text,
        old="if x:\nreturn 1\n", new="if x:\nreturn 42\n")
    assert result.success, result.error
    assert "fuzzy" in result.output.lower()
    # File keeps its original indentation; replacement re-indented to match.
    assert after == "def a():\n    if x:\n        return 42\n    return 2\n"


def test_fuzzy_ambiguous_reports_and_rejects(tmp_path):
    # Two identical stripped blocks → fuzzy match cannot pick one.
    file_text = "if a:\n    pass\n\nif a:\n    pass\n"
    result, after = _edit(
        tmp_path, file_text, old="if a:\npass\n", new="if a:\nreturn\n")
    assert not result.success
    assert "ambiguous" in result.error
    assert after == file_text  # untouched


def test_not_found_error_names_closest_match(tmp_path):
    file_text = "def compute_total(items):\n    total = 0\n    return total\n"
    result, _ = _edit(
        tmp_path, file_text,
        old="def compute_sum(items):\n    total = 0\n", new="x")
    assert not result.success
    assert "line" in result.error and "similarity" in result.error


def test_non_unique_error_lists_line_numbers(tmp_path):
    file_text = "foo()\nbar()\nfoo()\nbaz()\n"
    result, _ = _edit(tmp_path, file_text, old="foo()\n", new="qux()\n")
    assert not result.success
    assert "lines 1, 3" in result.error
    assert "unique" in result.error


def test_exact_match_still_works(tmp_path):
    file_text = "alpha\nbeta\n"
    result, after = _edit(tmp_path, file_text, old="beta\n", new="gamma\n")
    assert result.success and "fuzzy" not in result.output.lower()
    assert after == "alpha\ngamma\n"


def test_multiline_fuzzy_with_blank_lines(tmp_path):
    file_text = ("class A:\n\n    def m(self):\n\n        pass\n")
    result, after = _edit(
        tmp_path, file_text,
        old="class A:\n\ndef m(self):\n\npass\n",
        new="class A:\n\ndef m(self):\n\nreturn 1\n")
    assert result.success, result.error
    assert "        return 1" in after

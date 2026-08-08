"""Regression coverage for UTF-8 Git paths on Windows."""

import subprocess

from synapse.modules.context.retriever import BasicContextRetriever


def test_git_file_listing_handles_cjk_paths(tmp_path):
    (tmp_path / "中文文档.md").write_text("内容", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    paths = BasicContextRetriever._git_files(tmp_path)
    assert paths is not None
    assert "中文文档.md" in paths

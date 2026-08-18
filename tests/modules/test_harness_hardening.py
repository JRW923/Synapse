import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import subprocess

import pytest

from synapse.adapters.library import Synapse
from synapse.core.events import EventBus
from synapse.eval.benchmarks.repo_pytest import RepoPytestBenchmark
from synapse.eval.benchmarks.swebench import SWEBenchAdapter
from synapse.modules.context.retriever import BasicContextRetriever
from synapse.modules.plugins import DefaultPluginRegistry
from synapse.modules.providers.routing import FallbackLLMProvider
from synapse.modules.planning.worktree import WorktreeManager
from synapse.modules.tools.file_glob import GlobTool
from synapse.modules.tools.file_read import ReadTool
from synapse.modules.tools.search_grep import GrepTool
from synapse.protocols.events import AgentProgress
from synapse.protocols.llm import LLMResponse
from synapse.protocols.memory import MemoryEntry, MemoryLevel
from synapse.protocols.planner import AgentResult, ExecutionMetrics, ResultStatus
from synapse.protocols.planner import Planner
from synapse.protocols.retriever import ContextBudget
from synapse.protocols.sandbox import Sandbox
from synapse.protocols.tool import ToolRegistry


@pytest.mark.asyncio
async def test_readonly_file_tools_reject_workspace_escape(tmp_path: Path):
    outside = tmp_path.parent / "outside-synapse.txt"
    outside.write_text("secret", encoding="utf-8")
    for tool, params in [
        (ReadTool(str(tmp_path)), {"path": str(outside)}),
        (GlobTool(str(tmp_path)), {"pattern": "*", "path": str(outside.parent)}),
        (GrepTool(str(tmp_path)), {"pattern": "secret", "path": str(outside)}),
    ]:
        result = await tool.execute(params)
        assert not result.success
        assert "outside workspace" in (result.error or "")


@pytest.mark.asyncio
async def test_read_rejects_symlink_escape(tmp_path: Path):
    outside = tmp_path.parent / "outside-target.txt"
    outside.write_text("secret", encoding="utf-8")
    link = tmp_path / "link.txt"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlink creation is unavailable on this platform")
    result = await ReadTool(str(tmp_path)).execute({"path": str(link)})
    assert not result.success
    assert "outside workspace" in (result.error or "")


def test_tools_enabled_and_retry_config_are_wired():
    synapse = Synapse(
        provider="deepseek", model="deepseek-v4-pro", config_path=None,
        enabled=["read"], max_retries=7,
    )
    registry = synapse._container.resolve(ToolRegistry)
    assert [tool.name for tool in registry.list_all()] == ["read"]
    planner = synapse._container.resolve(Planner)
    assert planner.max_llm_retries == 7


def test_sandbox_enforce_fails_closed_but_warn_degrades():
    with patch("synapse.adapters.library.ProcessSandbox", side_effect=OSError("missing")):
        with pytest.raises(RuntimeError, match="enforce"):
            Synapse(provider="deepseek", model="deepseek-v4-pro", sandbox_mode="enforce")
        warn = Synapse(provider="deepseek", model="deepseek-v4-pro", sandbox_mode="warn")
        assert warn._container.resolve(Sandbox) is None


@pytest.mark.asyncio
async def test_event_bus_adds_trace_chain():
    bus = EventBus()
    seen = []
    bus.subscribe("agent_progress", lambda event: _append(seen, event))
    bus.configure_run("run-1", "trace-1")
    first = AgentProgress(session_id="s", phase="one")
    second = AgentProgress(session_id="s", phase="two")
    await bus.emit(first)
    await bus.emit(second)
    assert first.run_id == second.run_id == "run-1"
    assert first.trace_id == second.trace_id == "trace-1"
    assert second.parent_event_id == first.event_id


async def _append(target, value):
    target.append(value)


@pytest.mark.asyncio
async def test_retriever_includes_user_memory(tmp_path: Path):
    user_entry = MemoryEntry(id="u1", content="prefer pytest", level=MemoryLevel.USER)

    class Memory:
        async def retrieve(self, query, level, top_k=5):
            return [user_entry] if level == MemoryLevel.USER else []

    context = await BasicContextRetriever().retrieve(
        "pytest", tmp_path, tools=None, memory=Memory(), budget=ContextBudget(),
    )
    assert any(block.content == "prefer pytest" for block in context.reference)


def test_worktree_conflict_is_reported_without_overwrite(tmp_path: Path):
    mgr = WorktreeManager(tmp_path)
    a = mgr.create("a")
    b = mgr.create("b")
    (a / "same.txt").write_text("A", encoding="utf-8")
    (b / "same.txt").write_text("B", encoding="utf-8")
    conflicts = mgr.merge_all()
    assert conflicts == ["same.txt"]
    assert (tmp_path / "same.txt").read_text(encoding="utf-8") == "A"
    mgr.remove_all()


@pytest.mark.asyncio
async def test_repo_pytest_benchmark_runs_real_git_fixture():
    async def fix(_task, root):
        (root / "calculator.py").write_text(
            "def add(a: int, b: int) -> int:\n    return a + b\n",
            encoding="utf-8",
        )
        return AgentResult(
            status=ResultStatus.SUCCESS, output="fixed", metrics=ExecutionMetrics(),
        )

    result = await RepoPytestBenchmark().run(fix, trusted_host_execution=True)
    assert result.baseline_failed
    assert result.tests_passed
    assert any("calculator.py" in path for path in result.changed_files)


def test_incremental_index_refreshes_only_changed_content(tmp_path: Path):
    source = tmp_path / "main.py"
    source.write_text("def old(): pass\n", encoding="utf-8")
    retriever = BasicContextRetriever()
    assert "old" in retriever._read(source)
    cached = retriever._content_cache[source.resolve()]
    assert retriever._read(source) == cached[2]
    source.write_text("def new_name(): pass\n", encoding="utf-8")
    assert "new_name" in retriever._read(source)
    assert retriever._content_cache[source.resolve()] != cached


def test_plugin_manifest_semver_and_api_version(tmp_path: Path):
    manifest = tmp_path / "synapse-plugin.yaml"
    manifest.write_text(
        "name: demo\nversion: 1.2.3\napi_version: '1'\n"
        "capabilities: [tools]\nentry_point: demo:register\n",
        encoding="utf-8",
    )
    registry = DefaultPluginRegistry()
    loaded = registry.discover([str(tmp_path)])
    assert loaded[0].name == "demo"
    assert loaded[0].version == "1.2.3"


@pytest.mark.asyncio
async def test_provider_fallback_uses_second_provider():
    first = SimpleNamespace(model_id="first", chat=AsyncMock(side_effect=RuntimeError("down")))
    second = SimpleNamespace(
        model_id="second",
        chat=AsyncMock(return_value=LLMResponse(content="ok")),
    )
    routed = FallbackLLMProvider([first, second])
    result = await routed.chat([])
    assert result.content == "ok"
    assert routed.model_id == "second"


def test_swebench_executes_patch_and_private_tests(tmp_path: Path):
    repo = tmp_path / "source"
    repo.mkdir()
    (repo / "calculator.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@synapse.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Synapse Test"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=repo, check=True)
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    patch_text = (
        "diff --git a/calculator.py b/calculator.py\n"
        "--- a/calculator.py\n+++ b/calculator.py\n"
        "@@ -1,2 +1,2 @@\n def add(a, b):\n-    return a - b\n+    return a + b\n"
    )
    result = SWEBenchAdapter.execute(
        str(repo), commit, patch_text,
        {"test_private.py": "from calculator import add\n\ndef test_add(): assert add(2, 3) == 5\n"},
        timeout=60,
        trusted_host_execution=True,
    )
    assert result.applied
    assert result.passed, result.output


@pytest.mark.asyncio
async def test_sse_generator_close_cancels_agent_run():
    from synapse.adapters.server import RunRequest, create_app

    bus = EventBus()
    cancelled = asyncio.Event()

    class FakeSynapse:
        _container = SimpleNamespace(resolve=lambda _type: bus)

        async def run(self, task, session=None, confirm_callback=None):
            try:
                await bus.emit(AgentProgress(session_id=session.id, phase="started"))
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        def get_run_score(self):
            return None

    app = create_app(FakeSynapse())
    endpoint = next(route.endpoint for route in app.routes if route.path == "/run/stream")
    response = await endpoint(RunRequest(task="wait"))
    iterator = response.body_iterator
    assert "agent_progress" in await anext(iterator)
    await iterator.aclose()
    await asyncio.wait_for(cancelled.wait(), timeout=1)

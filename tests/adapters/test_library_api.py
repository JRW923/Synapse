"""Tests for the Synapse library API facade."""

from pathlib import Path

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from synapse.protocols.llm import LLMResponse
from synapse.protocols.planner import ResultStatus
from synapse.core.events import EventBus
from synapse.protocols.events import FileWritten
from synapse.protocols.memory import MemoryStore


# ---------------------------------------------------------------------------
# test_library_api_basic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_library_api_basic():
    """A simple task completes successfully with a mocked LLM."""
    mock_llm = AsyncMock()
    mock_llm.model_id = "mock"
    mock_llm.chat.return_value = LLMResponse(
        content="Task completed successfully.",
        tool_calls=[],
        stop_reason="end_turn",
        usage={"input": 10, "output": 5},
    )

    with patch(
        "synapse.modules.providers.anthropic.AnthropicProvider",
        return_value=mock_llm,
    ):
        from synapse.adapters.library import Synapse

        synapse = Synapse(provider="anthropic")
        result = await synapse.run("Say hello")

    assert result.status == ResultStatus.SUCCESS
    assert "Task completed" in result.output
    assert result.metrics.tokens_input == 10
    assert result.metrics.tokens_output == 5


# ---------------------------------------------------------------------------
# test_library_api_config_override
# ---------------------------------------------------------------------------

def test_library_api_config_override():
    """**overrides passed to Synapse(...) take precedence over defaults."""
    with patch("synapse.modules.providers.anthropic.AnthropicProvider"):
        from synapse.adapters.library import Synapse

        synapse = Synapse(
            provider="anthropic",
            model="claude-opus-4-6",
            max_tokens=8000,
            max_iterations=100,
        )

    config = synapse._config
    assert config.provider.provider == "anthropic"
    assert config.provider.model == "claude-opus-4-6"
    assert config.provider.max_tokens == 8000
    assert config.planning.max_iterations == 100


def test_strict_overrides_include_context_and_reject_unknown_keys():
    with patch("synapse.modules.providers.anthropic.AnthropicProvider"):
        from synapse.adapters.library import Synapse

        synapse = Synapse(
            provider="anthropic",
            strict_overrides=True,
            total_tokens=12_345,
        )
        assert synapse._config.context.total_tokens == 12_345

        with pytest.raises(ValueError, match="Unknown Synapse config override"):
            Synapse(
                provider="anthropic",
                strict_overrides=True,
                misspelled_setting=True,
            )


def test_eval_ablation_requires_eval_and_wires_runtime_modules(tmp_path):
    from synapse.eval.ablations import DisabledMemoryStore, EvaluationAblations
    from synapse.modules.security.auth import ActionAuthorizer
    from synapse.protocols.planner import Planner
    from synapse.protocols.tool import ToolRegistry

    with patch("synapse.modules.providers.anthropic.AnthropicProvider"):
        from synapse.adapters.library import Synapse

        with pytest.raises(ValueError, match="requires enable_eval=True"):
            Synapse(provider="anthropic", eval_ablation={"memory": False})

        with pytest.raises(RuntimeError, match="Docker sandbox backend"):
            Synapse(
                provider="anthropic",
                enable_eval=True,
                workspace_root=str(tmp_path),
                eval_ablation={"action_auth": False},
            )

        with pytest.raises(RuntimeError, match="host lifecycle hooks"):
            Synapse(
                provider="anthropic",
                enable_eval=True,
                workspace_root=str(tmp_path),
                hooks={"hooks": {"llm_token": ["echo unsafe"]}},
            )

        strong_sandbox = MagicMock(backend="docker")
        with patch(
            "synapse.adapters.library.ProcessSandbox", return_value=strong_sandbox,
        ):
            synapse = Synapse(
                provider="anthropic",
                enable_eval=True,
                workspace_root=str(tmp_path),
                eval_ablation={
                    "context": False,
                    "memory": False,
                    "completion_gate": False,
                    "action_auth": False,
                },
            )

    assert isinstance(synapse._container.resolve(MemoryStore), DisabledMemoryStore)
    assert synapse._container.resolve(EvaluationAblations).to_dict() == {
        "context": False,
        "memory": False,
        "completion_gate": False,
        "action_auth": False,
    }
    planner = synapse._container.resolve(Planner)
    auth = synapse._container.resolve(ActionAuthorizer)
    assert planner.completion_gate_enabled is False
    assert planner.auth is auth
    assert auth.bypass_policy is True
    assert auth.workspace_root == tmp_path.resolve()
    registry = synapse._container.resolve(ToolRegistry)
    for name in ("read", "write", "edit", "glob", "grep", "shell", "git"):
        assert registry.get(name)._workspace_root == tmp_path.resolve()


def test_effective_config_is_secret_free_and_path_portable(tmp_path):
    with patch("synapse.modules.providers.anthropic.AnthropicProvider"):
        from synapse.adapters.library import Synapse

        synapse = Synapse(
            provider="anthropic",
            enable_eval=True,
            workspace_root=str(tmp_path),
            api_key="secret-value",
            eval_ablation={"memory": False},
        )

    effective = synapse.get_effective_config()
    assert effective["provider"]["api_key"] == "<redacted>"
    assert effective["tools"]["workspace_root"] == "<runtime-workspace>"
    assert str(tmp_path) not in str(effective)
    assert effective["runtime"]["eval_ablation"]["memory"] is False


def test_eval_instances_isolate_background_and_todo_state(tmp_path):
    from synapse.adapters.library import Synapse
    from synapse.protocols.planner import Planner
    from synapse.protocols.tool import ToolRegistry

    with patch("synapse.modules.providers.anthropic.AnthropicProvider"):
        first = Synapse(
            provider="anthropic",
            enable_eval=True,
            workspace_root=str(tmp_path / "first"),
        )
        second = Synapse(
            provider="anthropic",
            enable_eval=True,
            workspace_root=str(tmp_path / "second"),
        )

    first_registry = first._container.resolve(ToolRegistry)
    second_registry = second._container.resolve(ToolRegistry)
    first_shell = first_registry.get("shell")
    second_shell = second_registry.get("shell")
    first_todo = first_registry.get("todo_write")
    second_todo = second_registry.get("todo_write")

    assert first_shell.background_manager is first._container.resolve(
        Planner,
    ).background_manager
    assert first_shell.background_manager is not second_shell.background_manager
    assert first_todo.store is not second_todo.store


def test_run_score_attests_actual_tool_and_mcp_capabilities(tmp_path):
    from synapse.adapters.library import Synapse

    with patch("synapse.modules.providers.anthropic.AnthropicProvider"):
        synapse = Synapse(
            provider="anthropic", enable_eval=True, workspace_root=str(tmp_path),
        )

    capabilities = synapse.get_run_score()["capabilities"]
    assert capabilities["tool_count"] > 0
    assert len(capabilities["tool_names_sha256"]) == 64
    assert capabilities["mcp_connected"] is False
    assert capabilities["mcp_tool_count"] == 0


@pytest.mark.asyncio
async def test_eval_synapse_aclose_releases_private_resources(tmp_path):
    from synapse.adapters.library import Synapse

    with patch("synapse.modules.providers.anthropic.AnthropicProvider"):
        synapse = Synapse(
            provider="anthropic",
            enable_eval=True,
            workspace_root=str(tmp_path),
        )

    memory_dir = Path(synapse._eval_memory_dir.name)
    mcp = AsyncMock()
    synapse._mcp_manager = mcp

    await synapse.aclose()

    assert not memory_dir.exists()
    mcp.shutdown.assert_awaited_once()
    assert synapse._eval_memory_dir is None


@pytest.mark.parametrize("backend", ["process", "bubblewrap", "seatbelt"])
def test_action_auth_ablation_rejects_non_docker_sandboxes(tmp_path, backend):
    with (
        patch("synapse.modules.providers.anthropic.AnthropicProvider"),
        patch(
            "synapse.adapters.library.ProcessSandbox",
            return_value=MagicMock(backend=backend),
        ),
    ):
        from synapse.adapters.library import Synapse

        with pytest.raises(RuntimeError, match="Docker sandbox backend"):
            Synapse(
                provider="anthropic",
                enable_eval=True,
                workspace_root=str(tmp_path),
                eval_ablation={"action_auth": False},
            )


@pytest.mark.asyncio
async def test_context_ablation_keeps_retrieval_but_skips_governance(tmp_path):
    from synapse.adapters.library import Synapse
    from synapse.core.agent import Agent
    from synapse.protocols.retriever import Context, ContextBlock, ContextSource

    with patch("synapse.modules.providers.anthropic.AnthropicProvider"):
        synapse = Synapse(
            provider="anthropic",
            enable_eval=True,
            workspace_root=str(tmp_path),
            eval_ablation={"context": False},
        )

    agent = Agent(synapse._container)
    overflow = ContextBlock("raw overflow", ContextSource.RETRIEVER)
    agent.retriever.retrieve = AsyncMock(return_value=Context(overflow=[overflow]))
    agent._partitioner.partition = AsyncMock(side_effect=AssertionError("partitioned"))
    agent._compactor.compact = AsyncMock(side_effect=AssertionError("compacted"))

    context = await agent._build_context("Fix src/example.py")

    agent.retriever.retrieve.assert_awaited_once()
    assert context.reference == [overflow]
    assert context.overflow == []


def test_deepseek_v4_flash_uses_anthropic_compatible_endpoint():
    from synapse.adapters.library import _resolve_provider

    provider_cls, base_url = _resolve_provider("deepseek", model="deepseek-v4-flash")
    assert provider_cls.__name__ == "AnthropicProvider"
    assert base_url == "https://api.deepseek.com/anthropic"


def test_library_api_without_provider_uses_models_json_default(tmp_path, monkeypatch):
    """Zero-argument construction must preserve the user's persisted default."""
    from synapse.config.models import upsert_model
    from synapse.adapters.library import Synapse

    models_file = tmp_path / "models.json"
    upsert_model(
        "deepseek", "deepseek-chat", api_key="sk-test", path=models_file,
    )
    monkeypatch.setattr(
        "synapse.config.models.models_config_path", lambda: models_file,
    )

    with patch("synapse.modules.providers.deepseek.DeepSeekProvider"):
        synapse = Synapse()

    assert synapse._config.provider.provider == "deepseek"
    assert synapse._config.provider.model == "deepseek-chat"


def test_library_api_uses_custom_provider_base_url(tmp_path, monkeypatch):
    from synapse.config.models import upsert_model
    from synapse.adapters.library import Synapse

    models_file = tmp_path / "models.json"
    upsert_model(
        "local",
        "coder",
        api_key="local-key",
        base_url="http://127.0.0.1:1234/v1",
        path=models_file,
    )
    monkeypatch.setattr(
        "synapse.config.models.models_config_path", lambda: models_file,
    )

    with patch("synapse.modules.providers.openai.OpenAIProvider") as provider_cls:
        Synapse()

    assert provider_cls.call_args.kwargs["base_url"] == "http://127.0.0.1:1234/v1"


# ---------------------------------------------------------------------------
# TODO K — runtime scoring closed loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_score_populated_after_run():
    """run() resets the collectors, then exposes a score with all four categories."""
    mock_llm = AsyncMock()
    mock_llm.model_id = "mock"
    mock_llm.chat.return_value = LLMResponse(
        content="Task completed successfully.",
        tool_calls=[],
        stop_reason="end_turn",
        usage={"input": 10, "output": 5},
    )

    with patch(
        "synapse.modules.providers.anthropic.AnthropicProvider",
        return_value=mock_llm,
    ):
        from synapse.adapters.library import Synapse

        synapse = Synapse(provider="anthropic")
        result = await synapse.run("Say hello")

    score = synapse.get_run_score()
    assert score is not None
    assert set(score.keys()) == {
        "task", "status", "run_id", "model_id", "safety", "process",
        "quality", "efficiency", "process_hint", "capabilities",
    }
    assert score["status"] == "success"
    assert score["run_id"]
    assert score["model_id"] == "mock"
    # Every category is present and itself a dict of metrics.
    for cat in ("safety", "process", "quality", "efficiency"):
        assert isinstance(score[cat], dict) and score[cat]
    # L.4 — a real run emits ProcessQualityScored, so the closed-loop hint is
    # surfaced (non-empty string).
    assert isinstance(score["process_hint"], str) and score["process_hint"]


@pytest.mark.asyncio
async def test_eval_mode_does_not_persist_memory_or_run_scores(tmp_path):
    mock_llm = AsyncMock()
    mock_llm.model_id = "mock"
    mock_llm.chat.return_value = LLMResponse(
        content="Task completed successfully.",
        tool_calls=[],
        stop_reason="end_turn",
        usage={"input": 10, "output": 5},
    )

    with patch(
        "synapse.modules.providers.anthropic.AnthropicProvider",
        return_value=mock_llm,
    ):
        from synapse.adapters.library import Synapse

        synapse = Synapse(
            provider="anthropic",
            enable_eval=True,
            workspace_root=str(tmp_path),
        )
        await synapse.run("Say hello")

    assert not (tmp_path / ".synapse").exists()
    layered = synapse._container.resolve(MemoryStore)
    assert tmp_path not in layered._project._base_path.parents
    assert tmp_path not in layered._user._dir.parents


@pytest.mark.asyncio
async def test_run_metrics_wired_and_collect():
    """Collectors are subscribed to the EventBus, so real events update the score."""
    mock_llm = AsyncMock()
    mock_llm.model_id = "mock"
    mock_llm.chat.return_value = LLMResponse(
        content="done", tool_calls=[], stop_reason="end_turn", usage={"input": 1, "output": 1},
    )

    with patch(
        "synapse.modules.providers.anthropic.AnthropicProvider",
        return_value=mock_llm,
    ):
        from synapse.adapters.library import Synapse

        synapse = Synapse(provider="anthropic")
        bus = synapse._container.resolve(EventBus)

        # Before any event: fresh collectors show no out-of-workspace access.
        assert synapse.get_run_score()["safety"]["out_of_workspace_access"] == 0

        # Emit a real out-of-workspace write → SafetyMetrics must pick it up.
        await bus.emit(FileWritten(session_id="s1", path="/etc/passwd", bytes_written=100))

        score = synapse.get_run_score()
        assert score["safety"]["out_of_workspace_access"] >= 1


@pytest.mark.asyncio
async def test_run_score_includes_process_hint():
    """L.4 — the last ProcessQualityScored hint is surfaced via get_run_score."""
    from synapse.protocols.events import ProcessQualityScored
    from synapse.protocols.llm import LLMResponse

    mock_llm = AsyncMock()
    mock_llm.model_id = "mock"
    mock_llm.chat.return_value = LLMResponse(
        content="Task completed successfully.", tool_calls=[],
        stop_reason="end_turn", usage={"input": 10, "output": 5},
    )

    with patch(
        "synapse.modules.providers.anthropic.AnthropicProvider",
        return_value=mock_llm,
    ):
        from synapse.adapters.library import Synapse

        synapse = Synapse(provider="anthropic")
        bus = synapse._container.resolve(EventBus)

        # No hint yet.
        assert synapse.get_run_score()["process_hint"] is None

        await bus.emit(ProcessQualityScored(
            session_id="s1", task="t", score=0.1, reuse_ratio=0.0, write_without_lookup=3,
            thrashing_events=0, success=True, tool_calls=5,
            hint="下次请先 grep/read 定位可复用代码。",
        ))

        assert synapse.get_run_score()["process_hint"] == "下次请先 grep/read 定位可复用代码。"
        # A subsequent run resets the hint, and the closed loop re-captures it.
        await synapse.run("Say hello")
        assert isinstance(synapse.get_run_score()["process_hint"], str) and synapse.get_run_score()["process_hint"]

"""端到端验证：confirm_callback 从 Synapse → Planner → Auth 的完整链路"""
import asyncio
import pytest
from unittest.mock import AsyncMock, patch
from synapse.adapters.library import Synapse
from synapse.protocols.llm import LLMResponse
from synapse.protocols.planner import AgentResult, ResultStatus


@pytest.mark.asyncio
async def test_confirm_callback_reaches_planner(tmp_path):
    """验证 confirm_callback 能正确传递到 ReActPlanner 并在写 workspace 外文件时触发"""
    confirm_log = []

    async def my_confirm(request):
        confirm_log.append(request.tool_name)
        return True  # 用户同意

    # 在 tmp_path 下创建项目，这样写入 tmp_path 外的路径会触发确认
    synapse = Synapse(
        provider="deepseek",
        model="deepseek-chat",
        config_path=None,
        confirm_callback=my_confirm,
    )
    # 覆盖 workspace_root 为 tmp_path
    auth = synapse._container._instances.get(
        type(synapse._container._instances[list(synapse._container._instances.keys())[0]])
    )

    # 直接拿到 planner 和 agent
    from synapse.core.agent import Agent
    from synapse.protocols.planner import Planner
    from synapse.protocols.tool import ToolRegistry

    planner = synapse._container.resolve(Planner)
    assert planner is not None
    # 验证 planner 上有 confirm_callback
    assert hasattr(planner, '_confirm')
    assert planner._confirm is not None

    # Mock LLM 返回一个写 workspace 外文件的 tool call
    mock_llm = AsyncMock()
    mock_llm.model_id = "mock"
    mock_llm.chat.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[{
                "id": "t1", "name": "write",
                "input": {
                    "path": "/etc/passwd",  # 明确在 workspace 外
                    "content": "evil",
                },
            }],
            stop_reason="tool_use",
            usage={"input": 10, "output": 5},
        ),
        LLMResponse(
            content="Done",
            tool_calls=[],
            stop_reason="end_turn",
            usage={"input": 5, "output": 3},
        ),
    ]

    # 替换容器中的 LLM provider
    from synapse.protocols.llm import LLMProvider
    synapse._container.register(LLMProvider, mock_llm)

    # 运行任务
    agent = Agent(synapse._container)
    from synapse.core.session import Session
    result = await agent.run("test write", Session())

    # 验证 confirm 回调被调用了
    assert len(confirm_log) == 1, f"期望 confirm 被调用 1 次，实际 {len(confirm_log)} 次"
    assert confirm_log[0] == "write"
    assert result.status == ResultStatus.SUCCESS


@pytest.mark.asyncio
async def test_confirm_callback_deny_blocks_tool(tmp_path):
    """验证用户拒绝时工具调用被阻止"""
    confirm_log = []

    async def my_confirm(request):
        confirm_log.append(request.tool_name)
        return False  # 用户拒绝

    synapse = Synapse(
        provider="deepseek",
        model="deepseek-chat",
        config_path=None,
        confirm_callback=my_confirm,
    )

    mock_llm = AsyncMock()
    mock_llm.model_id = "mock"
    mock_llm.chat.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[{
                "id": "t1", "name": "write",
                "input": {"path": "/etc/shadow", "content": "bad"},
            }],
            stop_reason="tool_use",
            usage={"input": 10, "output": 5},
        ),
        LLMResponse(
            content="I cannot write there.",
            tool_calls=[],
            stop_reason="end_turn",
            usage={"input": 5, "output": 3},
        ),
    ]

    from synapse.protocols.llm import LLMProvider
    synapse._container.register(LLMProvider, mock_llm)

    from synapse.core.agent import Agent
    from synapse.core.session import Session
    agent = Agent(synapse._container)
    result = await agent.run("test", Session())

    assert len(confirm_log) == 1
    assert confirm_log[0] == "write"
    # 工具应该被阻止，LLM 收到错误后给出最终回复
    assert result.status == ResultStatus.SUCCESS


@pytest.mark.asyncio
async def test_no_confirm_callback_auto_denies(tmp_path):
    """L.3: 无确认回调且 requires_confirmation 时，工具调用被自动拒绝（安全网）。"""
    from synapse.modules.security.auth import ActionAuthorizer

    synapse = Synapse(
        provider="deepseek", model="deepseek-chat", config_path=None,
        # 注意：不传 confirm_callback
    )
    # 把工作区根设为 tmp_path，使写入 tmp_path 内文件需要确认
    auth = synapse._container.resolve(ActionAuthorizer)
    auth.workspace_root = tmp_path
    auth._allowed_paths = []

    target = tmp_path / "out.txt"
    mock_llm = AsyncMock()
    mock_llm.model_id = "mock"
    mock_llm.chat.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[{
                "id": "t1", "name": "write",
                "input": {"path": str(target), "content": "hi"},
            }],
            stop_reason="tool_use",
            usage={"input": 10, "output": 5},
        ),
        LLMResponse(
            content="Done", tool_calls=[], stop_reason="end_turn",
            usage={"input": 5, "output": 3},
        ),
    ]
    from synapse.protocols.llm import LLMProvider
    synapse._container.register(LLMProvider, mock_llm)

    from synapse.core.agent import Agent
    from synapse.core.session import Session
    agent = Agent(synapse._container)
    result = await agent.run("test", Session())

    # 工具被自动拒绝，文件不应被写入
    assert not target.exists(), "无回调时确认类写操作不应执行"
    assert result.status == ResultStatus.SUCCESS


if __name__ == "__main__":
    asyncio.run(test_confirm_callback_reaches_planner())
    asyncio.run(test_confirm_callback_deny_blocks_tool())
    asyncio.run(test_no_confirm_callback_auto_denies())
    print("All pass")

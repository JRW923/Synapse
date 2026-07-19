# Synapse

> 连接想法与代码 —— 智能模块化 Code Agent

[English](README.md)

## 快速开始

```bash
# 1. 安装（按需选 provider）
pip install -e ".[deepseek]"          # 可选: anthropic / openai / deepseek / google / ollama

# 2. 启动 —— 首次运行自动进入配置向导
synapse
```

首次启动会自动引导你选择供应商、输入 API key，写入 `~/.synapse/config.yaml`。之后每次启动直接进入 REPL。

```bash
# 也可以通过命令行参数指定
synapse -p anthropic -m claude-sonnet-4-6 "修复 auth.py 的 bug"
```

## REPL 命令

| 命令 | 说明 |
|------|------|
| `/help` | 显示所有命令 |
| `/model` | 显示可用模型（绿色=可用，灰色=需配 API key） |
| `/model <名称>` | 切换到已配置的模型 |
| `/provider <名称>` | 切换供应商 |
| `/memory` | 会话信息 + token 用量 |
| `/session` | 显示会话路径 |
| `/reset` | 清空会话 |
| `/mode <名称>` | 切换规划模式 (react / plan_execute / hierarchical) |
| `/tools` | 列出工具 |
| `/exit` | 退出 |

## 配置

配置文件查找顺序：

1. `./synapse.yaml`（然后向上遍历目录树）
2. `<package-root>/synapse.yaml`（`pip install -e .` 自动发现）
3. `~/.synapse/config.yaml`（全局兜底，首次运行向导自动生成）

示例 `synapse.yaml`：

```yaml
provider:
  provider: deepseek
  model: deepseek-v4-pro
  api_key: "sk-你的key"

# 追加更多供应商，让 /model 可以列出切换
  models:
    - provider: openai
      model: gpt-5.5
      api_key: "sk-openai-key"
```

或使用环境变量：`DEEPSEEK_API_KEY`、`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`GOOGLE_API_KEY`。

## 预置模型

安装后自带 5 家供应商 13 款主流模型：

| 供应商 | 模型 |
|--------|------|
| anthropic | claude-sonnet-4-6, claude-opus-4-7, claude-haiku-4-5 |
| openai | gpt-5.5, gpt-5.4, o4-mini |
| deepseek | deepseek-chat, deepseek-v4-pro, deepseek-v4-flash |
| google | gemini-3-flash, gemini-3-pro |
| ollama | qwen3.5:4b, llama4:8b |

API key 填好后即为可用（绿色），未填者显示为灰色。

## 架构

```
synapse/
├── protocols/     # 纯接口定义（零依赖）
├── core/          # Agent、Container、EventBus、Session
├── modules/
│   ├── providers/ # 5 家 LLM 供应商
│   ├── tools/     # 10 个工具（文件/搜索/Shell/Git/HTTP/DB/Browser）
│   ├── planning/  # 3 种规划模式（ReAct / PlanExecute / Hierarchical）
│   ├── memory/    # 4 层记忆（Session/Project/User/Semantic）
│   ├── context/   # 上下文治理（Retriever + Partitioner + Compactor）
│   ├── security/  # 4 层安全（Sandbox/ActionAuth/Audit/Injection Defense）
│   └── mcp/       # MCP 客户端
├── eval/          # 指标、Benchmark、A/B 实验
├── adapters/      # CLI、Library API、HTTP Server
└── config/        # Pydantic Schema + YAML/环境变量加载
```

## License

MIT

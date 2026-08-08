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
| `/resume [id]` | 恢复已保存的会话（省略 id 恢复最近一次） |
| `/sessions` | 列出已保存的会话 |
| `/mode <名称>` | 切换规划模式 (react / plan_execute / hierarchical) |
| `/tools` | 列出工具 |
| `/context-report` | 显示 context 区块引用 / 使用热力图 |
| `/score` | 显示运行时评分（safety / process / quality / efficiency）+ 过程质量 hint |
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

## CLI

```bash
synapse                              # 主 REPL
synapse setup                        # 安装启动器脚本
synapse chat                         # 聊天会话
synapse run "任务"                   # 一次性任务（实时流式展示进度）
synapse serve                        # HTTP API（端口 8000）
synapse version                      # 显示版本
```

```
-c, --config  PATH     指定 synapse.yaml 路径
-p, --provider NAME    指定 LLM 供应商
-m, --model    NAME    指定模型 ID
--mode         NAME    规划模式
--resume [ID]          恢复已保存的会话（省略 ID 恢复最近一次；run/chat/REPL 均支持）
-y, --yes             （run）自动批准需要确认的操作（无交互场景下的显式放行）
```

会话会在每次任务结束后自动持久化到 `~/.synapse/sessions/<id>.json`，
退出后用 `synapse --resume` 或 REPL 内 `/resume` 可继续之前的对话。

`run` 与 `chat` 会以实时面板流式展示进度（工具调用、Swarm 生命周期、token），
不再静默阻塞。当某工具需要确认而终端无人应答时，默认**自动拒绝**，除非你加
`--yes` 显式放行。

交互界面会根据终端能力自动降级：宽终端显示完整工作区首页，窄终端使用 compact 布局，
重定向/非 TTY 环境输出无 ANSI 的纯文本。任务面板统一显示当前 phase、最近 5 个工具步骤、
elapsed 与 token；结束后给出耗时、工具成功率，以及 `partial`/`failed` 的继续操作提示。

## HTTP API

`/run` 与 `/run/stream`（SSE）是 `run` 的程序化等价物，二者均接受 `RunRequest`：

```json
{ "task": "重构 auth.py", "auto_approve": false }
```

- `auto_approve` —— 显式放行需确认的工具（等价于 `--yes`）。
- `/run/stream` 会推送 `agent_progress`、`llm_token`、`tool_call_*` 以及 Swarm
  事件（`worker_spawned`、`worker_completed`、`review_submitted`、`vote_cast`、
  `swarm_verified`），让外部调用者看到与 CLI 一致的实时进度。
- 响应（以及流式最终 `done` 事件）包含 `run_score` —— 运行时评分
  （`safety` / `process` / `quality` / `efficiency`）及供下一次任务参考的
  `process_hint`。

错误以友好的「原因 / 建议」文案返回，绝不向用户暴露原始 traceback。

## 架构

```
synapse/
├── protocols/     # 纯接口定义（零依赖）
├── core/          # Agent、Container、EventBus、Session
├── modules/
│   ├── providers/ # 5 家 LLM 供应商
│   ├── tools/     # 10 个工具（文件/搜索/Shell/Git/HTTP/DB/Browser）
│   ├── planning/  # 3 种规划模式（ReAct / PlanExecute / Hierarchical）
│   ├── memory/    # 记忆：Session/Project/User 为磁盘持久化；Semantic 向量层为可选后端
│   ├── context/   # 上下文治理（Retriever + Partitioner + Compactor）
│   ├── security/  # 4 层安全（Sandbox/ActionAuth/Audit/Injection Defense）
│   └── mcp/       # MCP 客户端
├── eval/          # 指标、Benchmark、A/B 实验
├── adapters/      # CLI、Library API、HTTP Server
└── config/        # Pydantic Schema + YAML/环境变量加载
```

## 已知限制（Known Limitations）

为避免误导，以下是当前**尚未真正接线**或仅停留在脚手架阶段的能力，使用前请知悉：

- **Semantic 记忆层**：向量后端（ChromaDB/Qdrant）为可选依赖。任务结束后会把摘要写入向量层，后续任务按相似度召回；未安装后端时自动降级为仅 Session/Project/User 记忆。
- **Eval / Benchmark**：`eval/` 下的指标管道与 redteam 框架可运行，但 `swebench` **未连接真实数据集**（无 clone/docker/打补丁/跑测试），`process_bench` 为针对虚构仓库的示例任务；二者属于评估脚手架，请勿当作真实基准结果。
- **Security Sandbox**：`ProcessSandbox` 现为进程树隔离——Windows 用 Job Object（`KILL_ON_JOB_CLOSE`）、Unix 用进程组 + `killpg`，超时/退出时整棵子进程树被杀，孙进程不再逃逸成孤儿。它保证的是「资源/生命周期不失控」，**不是文件系统/网络的强隔离**（无 bubblewrap/Seatbelt/namespace）；文件写入的真正安全边界在 `ActionAuthorizer` 命令审批闸门。
- **Swarm + Worktree**：worker 结果已在清理前 merge 回主工作区，但并行 worker 对同文件的写冲突为「后写覆盖」，无真正三方合并。

## License

MIT

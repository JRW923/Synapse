<p align="center">
  <img src="docs/资源/图标/synapse_icon_1_neural-S.png" width="120" alt="Synapse">
</p>

<h1 align="center">Synapse</h1>

<p align="center">
  <b>模块化的 Agent Harness</b> —— 把想法编排成代码
</p>

<p align="center">
  <a href="README_en.md">English</a> &nbsp;·&nbsp; MIT License
</p>

---

## 设计思想

Synapse 不是又一个 LLM 命令行封装，而是一套可组合的 **Agent Harness**：它把 Agent 的运行时、规划、工具、记忆、安全、Swarm 编排拆成清晰的接口与模块，方便你在上面实验、替换和扩展自己的 Agent 策略。

### 协议优先，零依赖边界

`protocols/` 定义 Agent、Tool、Provider、Memory、Security 等纯接口。核心只依赖接口，具体实现下沉到 `modules/`。这意味着你可以替换任一模块——接入新的模型供应商、增加自定义工具、换一种记忆实现——而不用重写整个系统。

### Agent + Container：任务执行容器

`core/` 中的 `Agent` 与 `Container` 负责任务的完整生命周期：接收输入、维护会话状态、协调规划器与工具调用、收集运行指标。`Container` 是运行时的上下文边界，也是后续更强隔离能力的挂载点。

### 事件总线：模块解耦与可观测

`EventBus` 贯穿 CLI、HTTP Server 与 Agent 内部。工具调用、LLM token、规划阶段切换、Swarm 事件都通过事件发布，外部监听者可以实时重放整个执行过程，也便于构建可视化编排界面。

### 规划模式可插拔

`modules/planning/` 提供 ReAct / Plan-Execute / Hierarchical 三种规划器，通过统一接口接入 Agent。`/mode` 命令或 `--mode` 参数可动态切换，便于在不同任务类型上实验最合适的策略。

### 工具 + MCP：能力扩展点

内置工具覆盖文件读写、搜索、Shell、Git、HTTP、数据库、浏览器等。工具接口也是 `protocols/` 的一部分；MCP 客户端（stdio + Streamable HTTP）允许接入外部工具服务，无需改动核心。

### 记忆与上下文治理

记忆分层为 Session / Project / User 三层磁盘持久化，语义记忆层（向量召回）为可选后端。`context/` 中的 Retriever + Partitioner + Compactor 负责把历史上下文裁剪到合适大小，避免长任务下的 token 爆炸。

### 安全：审批闸门 + 生命周期隔离

真正的写入安全边界是 `ActionAuthorizer` 命令审批闸门；`security/` 中的 Sandbox 负责进程树生命周期隔离（Windows Job Object / Unix 进程组），确保子任务超时或退出时不会留下孤儿进程。

### 运行时评分

每次任务产出 `run_score`：safety / process / quality / efficiency 四个维度，并附带 `process_hint`，为下一次迭代提供可量化的改进建议。

### Swarm：多 Worker 协作

`core/` 支持多 worker 并行执行，结果经评审投票后合并回主工作区。事件流暴露 `worker_spawned`、`worker_completed`、`review_submitted`、`vote_cast`、`swarm_verified` 等状态，便于可视化编排与审计。

## 快速开始

```bash
pip install -e ".[deepseek]"   # anthropic / openai / deepseek / google / ollama
synapse
```

首次运行进入配置向导，写入 `~/.synapse/models.json`。之后直接 `synapse` 进入 REPL，或 `synapse run "任务"` 执行一次性任务。会话自动持久化到 `~/.synapse/sessions/`，可用 `--resume` 或 REPL 的 `/resume` 续接。

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

文档导航：[docs/文档索引.md](docs/文档索引.md)

## 未来可拓展方向

- **更强的安全隔离**：在现有进程树隔离基础上，引入 bubblewrap / Seatbelt / namespace，实现文件系统与网络的真正沙箱。
- **真实基准接入**：`swebench` 已支持隔离 checkout、patch/private-test grader；新增 `terminal_smoke` / `terminal_bench` 适配层。详见 `docs/评测/evaluation-harness-research-2026-08-08.md`。
- **Swarm 三方合并**：并行 worker 对同文件的写冲突从「后写覆盖」演进为真正的三方合并。
- **语义记忆默认化**：把 ChromaDB/Qdrant 向量召回作为默认记忆层，提升长程上下文关联能力。
- **MCP 生态接入**：通过 MCP 协议热插拔更多外部工具与模型 provider，保持核心稳定。
- **可视化编排**：基于事件总线，构建任务执行时序图与依赖 DAG 编辑器。

## License

MIT

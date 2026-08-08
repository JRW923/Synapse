<p align="center">
  <img src="docs/资源/图标/synapse_icon_1_neural-S.png" width="120" alt="Synapse">
</p>

<h1 align="center">Synapse</h1>

<p align="center">
  <b>一个可观测、可扩展、可评测的 Code Agent Harness</b>
</p>

<p align="center">
  <a href="README_en.md">English</a> &nbsp;·&nbsp; MIT License
</p>

---

## 给 HR / 面试官的 30 秒摘要

Synapse 是一个由 Python 实现的端到端 Code Agent Harness。它不是把 LLM API 包一层的聊天机器人，而是把 **Agent Loop、规划、上下文、记忆、工具、权限、沙箱、事件流和评测** 组织成可替换的运行时。

这个项目适合展示应届生对 Agent 系统的完整理解：既能从 CLI 直接完成代码任务，也能沿着协议边界替换 Provider、Tool、Memory 和 Planner；同时保留了真实的测试、Red Team、Git fixture、SWE-bench/Terminal-Bench 适配和 HTML/CSV 评测证据。

| 面试关注点 | 可以在项目中看到的证据 |
| --- | --- |
| 是否理解 Agent Harness，而不只是 Prompt | `protocols/` 接口、`core/` 生命周期、`modules/` 可替换实现 |
| 是否能把模型接入做成产品 | 首次启动向导、`~/.synapse/models.json`、多 Provider 与 fallback/routing |
| 是否考虑安全 | Action-time authorization、敏感路径/命令检查、进程树回收、HMAC Audit |
| 是否能验证效果 | `repo_pytest`、`terminal_smoke`、`terminal_bench`、SWE-bench 适配、Red Team、重复实验 |
| 是否关注工程质量 | 事件驱动可观测性、运行时四维评分、原子 Session、434 passed / 1 skipped 的测试快照 |

## 核心特色

### 1. 协议优先的 Harness 结构

`protocols/` 定义 `LLMProvider`、`Tool`、`Memory`、`Planner`、`Sandbox`、`MCP` 等边界，`core/` 负责 Agent/Container/Session/EventBus 生命周期，`modules/` 放具体策略。替换模型供应商或工具实现，不需要把业务逻辑散落到 CLI 中。

### 2. 真正的 Agent Loop，而不是单轮函数调用

ReAct 支持 streaming、tool call、timeout、retry、授权和最小验证 gate；Plan-Execute、Hierarchical、Swarm 共享 Planner 接口。默认从简单的 ReAct 开始，复杂模式按任务显式选择，不把 Agent 数量当作核心卖点。

### 3. Context Engineering + 分层记忆

Retriever 使用 Git-aware 文件发现、相关性排序、AST symbol、预算分区与 compaction；Session / Project / User / Semantic 四层记忆分别服务于会话恢复、项目规则、用户偏好和可选向量召回。外部内容带 trust annotation，降低 prompt injection 中“数据被当成指令”的风险。

### 4. Action-time 安全边界

每次工具调用都会结合风险等级、工作区路径、命令链、敏感路径和 MCP 配置重新授权。默认进程沙箱保证子进程生命周期回收；Docker、bubblewrap、Seatbelt 是可选的更强执行后端。这里明确区分 **process containment** 与完整的文件/网络 sandbox。

### 5. 事件驱动的可观测性

Token、工具、授权、Agent、Swarm、过程质量等事件统一进入 EventBus。每次运行产生 `run_score`，包含 `safety / process / quality / efficiency` 四个维度；CLI、HTTP/SSE、Audit、评测报告共用同一事件来源，便于追踪一次任务到底做了什么。

### 6. 可复现的评测闭环

评测不是只看模型最后一句 `SUCCESS`：

- `repo_pytest`：临时 Git 仓库、真实修改、pytest grader。
- `terminal_smoke`：离线终端 fixture，验证文件状态和命令结果。
- `terminal_bench`：兼容常见 JSON/JSONL 任务字段、隔离 workspace 和命令 grader。
- `swebench`：本地数据集驱动的 clone、checkout、patch、private tests 执行路径。
- `--repeat N`：保存每次 attempt，汇总 Pass@k、Wilson 95% CI、Token、cost 和 tool success rate。
- 每个 JSON 报告自动生成双语 HTML Dashboard 与双语 CSV；CSV 保留原英文 machine key，并在每个字段旁附上中文别名，兼顾脚本兼容性和人工阅读。

## 快速开始

### 安装

```bash
# 选择一个模型供应商：anthropic / openai / deepseek / google / ollama
pip install -e ".[deepseek]"
```

### 首次启动与模型配置

```bash
synapse
```

第一次启动会主动进入配置向导，配置保存到 `~/.synapse/models.json`。后续启动自动使用默认 Provider/Model；在 REPL 中执行 `/model add` 可以继续添加模型，`/model` 可以切换并保存默认项。命令行 `--provider` / `--model` 只用于临时覆盖，不要求每次输入。

### 常用工作流

```bash
# 交互式任务
synapse

# 一次性代码任务
synapse run "修复 src/parser.py 的边界条件，并运行相关测试"

# 指定规划模式或恢复会话
synapse run --mode plan_execute "重构认证模块并补充测试"
synapse --resume

# 启动 HTTP API / SSE
synapse serve --host 127.0.0.1 --port 8000
```

REPL 常用命令：`/help`、`/model`、`/model add`、`/mode`、`/resume`、`/score`。会话自动持久化到 `~/.synapse/sessions/`。

## 评测与可视化

```bash
# 本地功能基准，适合第一次验证 Harness
python -m synapse eval repo_pytest --repeat 3 \
  --provider deepseek --model deepseek-v4-flash \
  --report eval-results/repo.json

# 离线 Terminal-Bench 风格 smoke
python -m synapse eval terminal_smoke --repeat 3 \
  --report eval-results/terminal-smoke.json

# 本地 JSON/JSONL 数据集适配
python -m synapse eval terminal_bench --dataset path/to/tasks.jsonl --max-tasks 10
python -m synapse eval swebench --dataset path/to/swebench.jsonl --max-tasks 10

# 对已有 JSON 重新生成报告
python -m synapse.eval.visualize eval-results/repo.json
```

评测完成后会得到：

```text
eval-results/repo.json   # 机器可读的完整报告
eval-results/repo.html   # 自包含、离线可打开的中文 / English Dashboard
eval-results/repo.csv    # 逐任务数据，英文 machine key 与中文别名相邻
```

HTML 展示通过率、Pass@k、置信区间、平均得分、耗时、输入/输出 Token、cost、工具成功率、过程得分、安全事件、分类通过率和逐任务 grader 结果。报告中的 `official_runner=external` 表示这是适配层，不冒充官方榜单分数。

## Harness 架构

```text
CLI / HTTP / Library
          │
          ▼
Container ── Agent Loop ── Planner (ReAct / Plan-Execute / Hierarchical / Swarm)
    │              │                         │
    │              ├── Context Retriever ────┤
    │              ├── Memory Layers         │
    │              └── EventBus              ▼
    │                              Tool Registry + MCP + Skills
    ├── ActionAuthorizer + Audit + ProcessSandbox
    └── RunScore (safety / process / quality / efficiency)
```

```text
synapse/
├── protocols/     # LLM、Tool、Memory、Planner、Sandbox、MCP 等接口
├── core/          # Agent、Container、EventBus、Session
├── modules/
│   ├── providers/ # Anthropic、OpenAI-compatible、Google、Ollama 等
│   ├── tools/     # 文件、搜索、Shell、Git、Web/HTTP、DB、Browser、Todo
│   ├── planning/  # ReAct、Plan-Execute、Hierarchical、Swarm
│   ├── memory/    # Session / Project / User / Semantic
│   ├── context/   # Retriever、Partitioner、Compactor、Citation
│   ├── security/  # ActionAuth、Sandbox、Audit、Injection Defense
│   └── mcp/       # stdio / Streamable HTTP MCP client
├── eval/          # Metrics、Benchmarks、Red Team、A/B experiments、visualize
├── adapters/      # CLI、Library API、HTTP Server
└── config/        # Pydantic schema、YAML、env、models.json
```

## 工程边界与未来优化

项目刻意保留了几项诚实边界，这些也是面试时值得讨论的地方：

- 默认 `process` sandbox 主要做进程树隔离，不等于默认 Docker 文件系统隔离。
- SWE-bench / Terminal-Bench 是本地数据集适配层；官方镜像、数据版本和完整 runner 仍由外部提供。
- Plugin 当前是 manifest discovery/version gate，不会任意 import 第三方代码。
- Swarm 支持 worktree、review、vote 和冲突保护，但还不是完整 Git three-way merge。
- 评测分数区分模型能力、Harness 能力和 grader 质量，不能用一次 smoke run 推断通用模型排名。

优先路线：Git checkpoint/rollback、typed retry classifier、HTTP SSRF policy、跨平台强沙箱 CI、SWE-bench 小样本可复现实验，以及基于 EventBus 的时序/DAG 可视化。当前没有全量 TypeScript 重构计划：Python 更适合保留 Agent runtime，TypeScript 只作为 IDE/API client 边界。

## 面试答辩切入点

1. **为什么授权必须发生在 action-time？** Tool schema 只能描述能力，不能证明本次参数没有副作用；风险应在每次调用时结合路径、命令和外部服务配置判断。
2. **为什么 Swarm 不是默认模式？** 分解、重复上下文、冲突合并和评审 token 成本可能超过并行收益，单 Agent 在小任务上更稳定。
3. **process containment 与 filesystem sandbox 有什么区别？** Windows Job Object/Unix process group 能回收子进程，但不自动限制文件和网络；强隔离必须显式选择后端。
4. **如何证明 Agent 真的完成了任务？** 用功能 grader、测试证据和 runtime score 交叉判断，而不是接受模型文本中的“已完成”。

## 文档与质量检查

- 文档入口：[docs/文档索引.md](docs/文档索引.md)
- Harness 审查：[docs/架构审查/harness-review-2026-08-08.md](docs/架构审查/harness-review-2026-08-08.md)
- 评测调研：[docs/评测/evaluation-harness-research-2026-08-08.md](docs/评测/evaluation-harness-research-2026-08-08.md)
- 产品体验：[docs/产品体验/ux-review-and-plan-2026-08-08.md](docs/产品体验/ux-review-and-plan-2026-08-08.md)

```bash
pytest -q
python -m compileall -q synapse
```

当前验证快照：`434 passed, 1 skipped`。可选 Provider、向量库、浏览器和强沙箱按需安装，核心评测可在无外部数据集时先运行离线 fixture。

## License

MIT

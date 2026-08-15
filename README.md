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

## 这是什么

Synapse 是一个 Python 实现的端到端 Code Agent Harness。它不是把 LLM API 包一层的聊天机器人，而是把 **Agent Loop、规划、上下文、记忆、工具、权限、沙箱、事件流和评测** 组织成可替换的运行时。

你可以从 CLI 直接把一个代码任务交给它，也可以沿着协议边界替换 Provider、Tool、Memory 和 Planner，把它当成自己 Agent 的底座。每次运行都会留下事件流和四维评分，所以「这次任务到底做了什么、凭什么算完成」是可以回答的。

| 能力 | 实现位置 |
| --- | --- |
| 协议优先的 Harness 结构 | `protocols/` 接口、`core/` 生命周期、`modules/` 可替换实现 |
| 多 Provider 接入与热切换 | 首次启动向导、`~/.synapse/models.json`、fallback / routing |
| Action-time 安全边界 | 逐次授权、敏感路径与命令链检查、进程树回收、HMAC Audit |
| 可复现评测 | `repo_pytest`、`terminal_smoke`、`terminal_bench`、SWE-bench 适配、Red Team、重复实验 |
| 可观测性 | EventBus 事件驱动、运行时四维评分、原子写 Session 持久化 |

评测实现、验证快照和结论边界统一记录在[评测体系方案](docs/评测/评测体系方案.md)；工程回归只证明评测链路行为，不代表模型或 Harness 能力提升。

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
- `--repeat N`：保存每次 attempt，区分 attempt 通过率与 task success@N，并输出 Pass@k、Pass^k 及各自 95% CI。
- 数据集包含完整 `repository_id` 时，正式区间自动按 repository cluster 重采样，避免同一仓库多题被当成独立证据。
- 报告同时记录仅基于有效外部 grader 的成功误报率、延迟 median/P95、Token 覆盖率与每次成功的预估成本、dataset manifest、配置/任务集/Git 指纹及成本单价来源；runner 提供证据时再记录实际 model/run ID。
- `governance`：冻结数据集与 split/tombstone、预注册主指标和样本量、校准 grader golden cases、归档失败产物、登记不可变 run，并分析同一 series 的趋势与漂移。
- `experiment`：同轮 A/B 随机交错，支持多指标独立方向、配对 bootstrap CI、随机化检验和 `agent_reported_success`/安全 guardrail；提供 `--dataset` 时按任务独立 workspace、同 baseline 和外部 grader 做多任务配对，并持久化脱敏配置、配置指纹和运行顺序。无 dataset 的命令只用于运行时诊断。
- `synapse eval` 输出 JSON、双语 HTML Dashboard 与 CSV；多任务 `experiment` 输出 JSON 与自包含 HTML。JSON 是事实来源，HTML 不改变统计口径。

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
# 内置单题 fixture，只验证 Agent -> workspace -> grader -> report 链路
# 不代表模型或 Harness 的能力成绩
python -m synapse eval repo_pytest --repeat 3 \
  --provider deepseek --model deepseek-v4-flash \
  --trusted-host-execution \
  --report eval-results/repo.json

# 离线 Terminal-Bench 风格 smoke
python -m synapse eval terminal_smoke --repeat 3 \
  --report eval-results/terminal-smoke.json

# 本地 JSON/JSONL 数据集适配；只有确认数据与 grader 可信时才允许宿主执行
python -m synapse eval terminal_bench --dataset path/to/tasks.jsonl --max-tasks 10 \
  --trusted-host-execution
python -m synapse eval swebench --dataset path/to/swebench.jsonl --max-tasks 10 \
  --trusted-host-execution

# 对已有 JSON 重新生成报告
python -m synapse.eval.visualize eval-results/repo.json

# 冻结数据集；已有 manifest 不允许原地覆盖
python -m synapse.eval.governance freeze path/to/tasks.jsonl path/to/manifest.json \
  --name internal-code --version 1.0.0 --source internal --license MIT \
  --grader-version grader-v1 --image-digest sha256:...

# 预注册、run 登记、完整性校验与趋势分析
python -m synapse.eval.governance preregister prereg-spec.json prereg-frozen.json
python -m synapse.eval.governance register eval-registry eval-results/repo.json \
  --report-id run-2026-001 --series harness-v1 --role baseline \
  --status complete --approval approved
python -m synapse.eval.governance verify eval-registry eval-results
python -m synapse.eval.governance trend eval-registry --series harness-v1

# 同一模型下比较两组 Harness 配置；seed 控制配对顺序与统计重采样
python -m synapse experiment --name context-ablation \
  --config-a '{"eval_ablation":{"context":false}}' \
  --config-b '{"eval_ablation":{"context":true}}' \
  --allowed-config-diff runtime.eval_ablation.context \
  --task "Fix the repository bug and verify it" --runs 6 \
  --primary-metric duration_ms --direction lower --seed 42

# 多任务功能对比；同一数据集、外部 grader、预算和权限，只替换 Harness 配置
python -m synapse experiment --name harness-functional-ab \
  --config-a '{"eval_ablation":{"context":false}}' \
  --config-b '{"eval_ablation":{"context":true}}' \
  --allowed-config-diff runtime.eval_ablation.context \
  --dataset path/to/tasks.jsonl --max-tasks 20 --runs 3 \
  --primary-metric functional_success --seed 42 \
  --trusted-host-execution --report eval-results/harness-functional-ab.json
```

`experiment` 中的 `agent_reported_success` 是 Harness 状态，不等同于外部功能判分；跨 Harness 能力对比需固定同一模型、任务、权限与 grader，并使用多任务配对报告。

HTTP `/eval/experiment` 只接受受限的 Harness 配置差异，拒绝请求覆盖 `api_key`、`base_url`、hooks、plugins、MCP、外部工具、workspace 与 sandbox/Auth 边界；需要这些能力的正式实验应由可信 runner 或固定容器编排，不从网络请求注入宿主执行配置。

使用 `synapse eval` 完成 Benchmark 后会得到：

```text
eval-results/repo.json   # 机器可读的完整报告
eval-results/repo.html   # 自包含、离线可打开的中文 / English Dashboard
eval-results/repo.csv    # 逐任务数据，英文 machine key 与中文别名相邻
```

HTML 展示 attempt 通过率、task success@N、Pass@k/Pass^k 曲线、95% CI、验证状态与成功误报率、延迟 median/P95、输入/输出 Token 覆盖、预估成本与单价来源、工具成功率、过程/安全诊断、dataset manifest、复现指纹和逐任务 grader 结果。报告中的 `official_runner=external` 表示这是适配层，不冒充官方榜单分数。

失败 attempt 可显式传入 `ArtifactStore` 写入内容寻址的受控目录，JSON 报告只保留 `artifact://sha256/...`、摘要和字节数。仓库内提供的 Memory follow-up 与 grader golden-case 文件只是冻结的离线契约 fixture，不是正式模型成绩。

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

以下边界是刻意保留并显式标注的，使用前请先确认它们符合你的场景：

- 默认 `process` sandbox 主要做进程树隔离，不等于默认 Docker 文件系统隔离。
- SWE-bench / Terminal-Bench 是本地数据集适配层；官方镜像、数据版本和完整 runner 仍由外部提供。
- Plugin 当前是 manifest discovery/version gate，不会任意 import 第三方代码。
- Swarm 支持 worktree、review、vote 和冲突保护，但还不是完整 Git three-way merge。
- 评测分数区分模型能力、Harness 能力和 grader 质量，不能用一次 smoke run 推断通用模型排名。

优先路线：Git checkpoint/rollback、typed retry classifier、HTTP SSRF policy、跨平台强沙箱 CI、SWE-bench 小样本可复现实验，以及基于 EventBus 的时序/DAG 可视化。当前没有全量 TypeScript 重构计划：Python 更适合保留 Agent runtime，TypeScript 只作为 IDE/API client 边界。

## 设计取舍

**为什么授权发生在 action-time，而不是工具注册时？** Tool schema 只能描述能力，不能证明本次调用的参数是安全的。同一个 shell 工具，`ls` 和 `curl x | bash` 风险完全不同，所以风险要在每次调用时结合参数、路径、命令链和外部服务配置重新判断。

**为什么 Swarm 不是默认模式？** 任务分解、重复上下文、冲突合并和评审的 token 开销，在小任务上经常超过并行收益，单 Agent 反而更稳定。默认是 ReAct，Swarm 需要显式 `--mode swarm`。

**process containment 和 filesystem sandbox 有什么区别？** Windows Job Object / Unix process group 能保证子进程被回收，但不限制文件和网络访问。需要强隔离必须显式选择 Docker、bubblewrap 或 Seatbelt 后端。

**怎么判断任务真的完成了？** 运行时 gate 看的是工具返回的 exit code，不是模型文本里的「已完成」；评测层的 grader 在 Agent 跑完之后由 Harness 独立执行。正式数据集还必须在入库 preflight 中确认 baseline 失败、目标状态可通过，避免「测试本来就过」的假阳性。

## 文档

- 文档入口：[docs/文档索引.md](docs/文档索引.md)
- 架构审查：[docs/架构审查/架构审查与改造记录.md](docs/架构审查/架构审查与改造记录.md)
- 评测说明：[docs/评测/评测基线与接入.md](docs/评测/评测基线与接入.md)
- 产品体验：[docs/产品体验/交互体验评审与改造.md](docs/产品体验/交互体验评审与改造.md)

## 本地验证

```bash
pytest -q
python -m compileall -q synapse
```

可选 Provider、向量库、浏览器和强沙箱按需安装；没有外部数据集时可以先跑离线 fixture（`terminal_smoke`）验证 Harness 链路。

## License

MIT

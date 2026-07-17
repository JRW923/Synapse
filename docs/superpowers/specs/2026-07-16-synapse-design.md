# Synapse Design Spec

> 一个以"过程质量"为核心差异点的模块化 Code Agent

## 1. 项目定位

Synapse 是一个简化版的 Code Agent（参考 Claude Code / Codex），核心差异化在于**过程质量**——不仅关注任务是否完成，更关注完成过程中的行为质量：是否复用了现有实现、是否定位了根因、是否持久化了测试、是否始终遵循项目指令。

### 核心原则

1. **模块解耦**：每个模块独立、可替换、可独立测试。参考 pico 的 Protocol + IoC 思想，但不照搬
2. **设计可解释**：每处设计都有明确的 trade-off 分析
3. **实验驱动**：通过 benchmark 和评测证明价值，不靠信仰而是靠数据

### 技术栈选定

- **语言**：Python 3.11+
- **异步**：asyncio 原生
- **Protocol**：typing.Protocol（接口定义）
- **DI 容器**：自建轻量 IoC（避免引入重量级框架）
- **配置**：YAML + env vars → Pydantic model
- **向量数据库**：Chroma（默认）/ Qdrant（可选）
- **CLI 框架**：rich + argparse
- **HTTP**：FastAPI

---

### MVP 阶段划分

| 阶段 | 内容 | 优先级 |
|------|------|--------|
| **Phase 1: Core + ReAct** | IoC 容器、Agent 主循环、ReActPlanner、单 Provider(Anthropic)、基础工具(File/Shell/Grep/Git)、会话记忆、基础上下文治理、Process Sandbox、Action-Time 授权 | 必须 |
| **Phase 2: 完整模块** | PlanExecutePlanner、HierarchicalPlanner、其他 Provider、项目/用户记忆、完整四区上下文治理、审计日志、CLI + Library 入口 | 必须 |
| **Phase 3: 评测 + 安全增强** | 评测框架(metrics + benchmarks + A/B 实验)、语义记忆、HTTP API、Prompt Injection 防御、SWE-bench 防污染措施 | 必须 |
| **Phase 4: 外部集成** | 数据库/浏览器/HTTP 工具、完整 benchmark 套件跑分、Qdrant 后端支持 | 后续 |

> 安全层按 Phase 逐步启用：Phase 1 至少 Sandbox + Action-Time Auth；审计日志 Phase 2；注入防御 Phase 3。

---

## 2. 架构总览

### 核心模式：分层 Protocol + EventBus 横切

```
┌─────────────────────────────────────────────────┐
│                  Adapters                        │
│         CLI  │  Python API  │  HTTP Server       │
├─────────────────────────────────────────────────┤
│                   Core                            │
│    Container (IoC)  │  Agent  │  EventBus        │
├─────────────────────────────────────────────────┤
│                 Protocols                         │
│  LLMProvider │ Tool │ Planner │ Memory │ ...     │
├──────────────┼──────┼─────────┼────────┼────────┤
│                 Modules                           │
│  Providers │ Tools │ Planning │ Memory │ Context  │
│            │       │          │        │ Security │
├─────────────────────────────────────────────────┤
│              Eval (EventBus consumers)           │
│     Metrics Collectors  │  Benchmark Runner      │
└─────────────────────────────────────────────────┘
```

**边界规则**：
- `protocols/` 是根依赖，不能 import 其他模块
- `core/` 只依赖 `protocols/`
- `modules/` 实现 protocols，可依赖 core 的 exceptions 和 events
- `eval/` 通过 EventBus 消费事件，不侵入核心路径
- `adapters/` 依赖 core，做薄适配

**Protocol vs EventBus 的边界**：
- **Protocol（同步依赖）**：Agent 主循环依赖的核心模块——LLM、Tool、Planner、Memory、Retriever、Sandbox
- **EventBus（异步横切）**：不与主循环产生同步依赖的横切关注点——审计日志、指标采集、安全通知

---

## 3. 项目目录结构

```
synapse/
├── protocols/              # 纯接口定义
│   ├── __init__.py
│   ├── llm.py              # LLMProvider
│   ├── tool.py             # Tool + ToolRegistry
│   ├── planner.py          # Planner
│   ├── memory.py           # MemoryStore
│   ├── retriever.py        # ContextRetriever
│   ├── sandbox.py          # Sandbox
│   └── events.py           # Event 类型定义
│
├── core/                   # IoC 容器 + Agent 主循环
│   ├── __init__.py
│   ├── container.py        # DI 容器
│   ├── agent.py            # Agent.run() 主循环
│   ├── events.py           # EventBus 实现
│   ├── session.py          # Session 状态
│   └── exceptions.py       # 核心异常
│
├── modules/                # 各模块默认实现
│   ├── providers/          # Anthropic / OpenAI / Google / DeepSeek / Ollama
│   ├── tools/              # File / Shell / Git / Search / Web / DB
│   ├── planning/           # ReActPlanner / PlanExecutePlanner / HierarchicalPlanner
│   ├── memory/             # SessionMemory / ProjectMemory / UserMemory / SemanticMemory
│   ├── context/            # Partitioner / Compactor / DeterministicRetriever
│   └── security/           # ProcessSandbox / ActionAuthorizer / AuditLogger / InjectionGuard
│
├── eval/                   # 评测框架
│   ├── __init__.py
│   ├── runner.py           # Benchmark 运行器
│   ├── metrics/            # process / quality / efficiency / safety
│   ├── benchmarks/         # swebench / process_bench / custom
│   └── experiments.py      # A/B 实验追踪
│
├── adapters/               # 入口层
│   ├── cli.py              # CLI（argparse + rich）
│   ├── library.py          # Python API
│   └── server.py           # HTTP API（FastAPI）
│
└── config/                 # 配置管理
    ├── schema.py           # Pydantic schema
    └── loader.py           # YAML + env vars 加载
```

---

## 4. Agent 主循环

```python
class Agent:
    """依赖注入的装配器，自身不实现循环逻辑"""

    def __init__(self, container: Container):
        self.llm: LLMProvider = container.resolve(LLMProvider)
        self.planner: Planner = container.resolve(Planner)
        self.tools: ToolRegistry = container.resolve(ToolRegistry)
        self.memory: MemoryStore = container.resolve(MemoryStore)
        self.retriever: ContextRetriever = container.resolve(ContextRetriever)
        self.sandbox: Sandbox = container.resolve(Sandbox)
        self.event_bus: EventBus = container.resolve(EventBus)

    async def run(self, task: str, session: Session) -> AgentResult:
        # 1. 构建上下文（memory + retriever 协作）
        context = await self._build_context(task, session)

        # 2. 选择规划策略并执行
        result = await self.planner.execute(
            task=task, context=context, tools=self.tools,
            llm=self.llm, sandbox=self.sandbox, session=session,
        )

        # 3. 持久化记忆
        await self._persist_memory(session, result)

        return result
```

**为什么 Agent 不持有循环逻辑？** 三种规划模式（ReAct / Plan-then-Execute / Hierarchical）的循环结构完全不同。Agent 只做装配，循环交给 Planner 各自实现。新增规划模式不需要修改 Agent。

---

## 5. 核心 Protocol 设计

```python
class LLMProvider(Protocol):
    @property
    def model_id(self) -> str: ...
    async def chat(self, messages: list[Message], tools: list[ToolSchema] | None) -> LLMResponse: ...
    async def stream(self, messages: list[Message], tools: list[ToolSchema] | None) -> AsyncIterator[LLMChunk]: ...

class Planner(Protocol):
    mode: PlanningMode  # REACT | PLAN_EXECUTE | HIERARCHICAL
    async def execute(self, task: str, context: Context, tools: ToolRegistry,
                       llm: LLMProvider, sandbox: Sandbox, session: Session) -> AgentResult: ...

class Tool(Protocol):
    name: str
    description: str
    parameters: ToolSchema
    requires_sandbox: bool
    risk_level: RiskLevel  # READ_ONLY | WRITE_LOCAL | EXECUTE | EXTERNAL | META
    async def execute(self, params: dict, sandbox: Sandbox | None) -> ToolResult: ...

class ToolRegistry(Protocol):
    def register(self, tool: Tool) -> None: ...
    def get(self, name: str) -> Tool: ...
    def list_all(self) -> list[Tool]: ...
    def get_schemas(self) -> list[ToolSchema]: ...

class MemoryStore(Protocol):
    async def store(self, entry: MemoryEntry, level: MemoryLevel) -> None: ...
    async def retrieve(self, query: str, level: MemoryLevel, top_k: int) -> list[MemoryEntry]: ...
    async def forget(self, entry_id: str) -> None: ...

class ContextRetriever(Protocol):
    async def retrieve(self, query: str, project_root: Path,
                        tools: ToolRegistry, max_tokens: int) -> Context: ...

class Sandbox(Protocol):
    async def execute(self, command: str, cwd: Path | None, env: dict,
                       timeout: int, network: bool) -> SandboxResult: ...
    @property
    def platform(self) -> str: ...  # windows_job | macos_seatbelt | linux_bwrap
```

**设计决策**：
- 使用 `typing.Protocol` 而非 ABC：不需要显式继承，mock 更简单
- Memory 统一接口 + level 参数：减少核心路径的分支逻辑
- Planner 持有循环：三种模式差异大，各自实现更清晰

---

## 6. 上下文治理（Context Governance）

### 三阶段流程

**BUILD（构建）**：从多个来源收集候选上下文
- MemoryStore.retrieve() → 项目记忆 + 用户记忆
- DeterministicRetriever.retrieve() → grep/glob/AST 精确匹配
- MemoryStore.retrieve(level=SEMANTIC) → 语义相似（最低优先级）

**ORGANIZE（组织）**：四区制优先级分配

| 分区 | 预算占比 | 内容 | 是否可压缩 |
|------|---------|------|-----------|
| SYSTEM | ~15% | 项目指令、安全规则、用户偏好 | 否 |
| CORE | ~50% | 当前文件、直接相关代码、依赖定义 | 否（当前文件部分） |
| REFERENCE | ~25% | 项目文档、历史记忆 | 紧急时可压缩 |
| OVERFLOW | ~10% | 语义搜索结果、低优先级辅助信息 | 是 |

**MAINTAIN（维护）**：对话进行中持续维护
- **Compaction**：token 超阈值时自动摘要压缩 overflow/reference 区
- **Phase Clearing**：Planner 发出 phase_change 事件时清除过期 block
- **Drift Detection**：定期检测 agent 行为是否符合 SYSTEM 指令

### ContextBudget 可配置

```python
@dataclass
class ContextBudget:
    total_tokens: int = 100_000
    system_pct: float = 0.15
    core_pct: float = 0.50
    reference_pct: float = 0.25
    overflow_pct: float = 0.10
```

### Trade-offs

| 决策 | 选择 | 理由 |
|------|------|------|
| 检索策略 | Grep/AST 优先，语义 fallback | RAG 常返回"像但不精确"的结果，确定性工具保证 100% 召回 |
| 分区模型 | 四区制 | 保证 SYSTEM 指令不被压缩冲掉，解决"对话变长后遗忘指令"的痛点 |
| 压缩触发 | 按 token 使用量 | 不同任务 token 消耗差异巨大（可达 7M），固定轮数触发不可靠 |
| 语义记忆位置 | OVERFLOW 区 | 语义检索准确性不够，仅作辅助参考 |

---

## 7. 记忆系统

### 四层记忆

| 层次 | 后端 | 作用域 | 职责 |
|------|------|--------|------|
| SESSION | 内存 dict | 单会话 | 上下文窗口管理，会话结束释放 |
| PROJECT | `.synapse/memory/` | 单项目 | 项目约定、架构决策、已知坑位 |
| USER | `~/.synapse/memory/` | 跨项目 | 用户偏好、编码风格、全局配置 |
| SEMANTIC | Chroma/Qdrant | 跨所有 | 向量化相似问题检索 |

### 文件结构

```
.synapse/memory/
├── MEMORY.md              # 索引
├── architecture.md
├── conventions.md
├── pitfalls.md
└── decisions/
    └── YYYY-MM-DD-xxx.md
```

### MemoryEntry 结构

```python
@dataclass
class MemoryEntry:
    id: str
    content: str
    level: MemoryLevel
    metadata: MemoryMetadata

@dataclass
class MemoryMetadata:
    project: str | None
    timestamp: datetime
    tags: list[str]
    priority: int = 5         # 检索排序权重
    source_task: str | None   # 产生这条记忆的任务
    access_count: int = 0     # 淘汰依据
    embedding: list[float] | None  # 仅 SEMANTIC
```

### Trade-offs

| 决策 | 选择 | 理由 |
|------|------|------|
| 存储格式 | Markdown 文件 | 人类可读、git 可追踪、agent 可直接编辑 |
| 语义记忆写入 | Agent 自动写入（可配置关闭） | Agent 应在任务完成后主动记录经验 |
| 语义记忆触发 | 仅确定性工具无结果时 | 避免不精确的语义匹配污染 CORE 区 |

---

## 8. 规划系统（Planning）

### 三种模式

```
任务复杂度判断 → 自动选择或用户指定

ReActPlanner         → 简单任务（单文件修改、查询、安装依赖）
PlanExecutePlanner   → 中等任务（多文件修改、特性实现、重构）
HierarchicalPlanner  → 大型任务（多模块变更、跨仓库修改）
```

### ReActPlanner

经典 Think → Act → Observe 循环，严格单线程顺序执行。
- 最大循环次数 50（可配置）
- 检测 thrashing：同一文件被修改 > 3 次 → 发出事件

### PlanExecutePlanner

```
Phase 1 - Plan:    llm 生成执行计划 → emit PlanCreated
Phase 2 - Execute: 每个 step 前 phase_clear → ReAct 执行 → 更新计划状态
Phase 3 - Verify:  检测关键步骤是否被跳过
```

关键：每个 step 开始前清理旧上下文（phase_clear），防止"对话变长指令遗忘"。

### HierarchicalPlanner

```
Orchestrator: 分解任务 → 为每个子任务 fork 独立 session →
              自动选择 ReAct/PlanExecute → 串行执行 → LLM 汇总
```

- 子任务 session 隔离（避免上下文污染）
- 编排者不直接执行代码（权限最小化）
- 串行执行（调研结论：并行多 agent 错误会几何级放大）

### Trade-offs

| 决策 | 选择 | 理由 |
|------|------|------|
| 子任务并行 vs 串行 | 串行 | 现有 agent 并行执行会放大错误 |
| 计划确认 vs 自动 | 可配置，默认交互式 | PlanExecute 默认等待确认减少漂移 |
| 子 session 隔离 | 独立 fork | 避免子任务间上下文污染 |
| thrashing 检测 | 告警但不中断 | 可能是正常迭代；持续发出事件供观察 |

---

## 9. 工具系统（Tool System）

### 工具层次

```
文件层:        ReadTool / WriteTool / EditTool / GlobTool
代码理解层:    GrepTool / ASTTool / GitTool (log/blame/diff)
执行层:        ShellTool / GitWriteTool / NotebookTool
外部集成层:    HTTPTool / DBTool / BrowserTool
评测层:        BenchmarkTool / EvalTool (META, 实验模式)
```

### 安全分级

| 风险等级 | 工具 | 沙箱需求 | 授权需求 |
|---------|------|---------|---------|
| READ_ONLY | Read, Glob, Grep, AST, Git(log) | 否 | 自动通过 |
| WRITE_LOCAL | Write, Edit | 是 | 项目外需确认 |
| EXECUTE | Shell, GitWrite, Notebook | 是 | 白名单 + 确认 |
| EXTERNAL | HTTP, DB, Browser | 是 | 需显式启用 |
| META | Benchmark, Eval | 否 | 实验模式 |

### 工具扩展

实现 Tool Protocol + 注册即可。零侵入新增自定义工具。

### Trade-offs

| 决策 | 选择 | 理由 |
|------|------|------|
| ToolSchema 格式 | 裸 dict | 兼容所有 LLM Provider 的 function calling 格式 |
| 风险声明 | Tool 声明 + Registry 可覆盖 | 部署环境可能调整（生产禁止 EXTERNAL） |
| EditTool | diff 匹配（字符串替换） | AST 操作理论上更准确但 tree-sitter 跨语言质量参差不齐 |

---

## 10. 安全层（Security）

### 四层防护

**Layer 1: Prompt Injection 检测**
- 对 EXTERNAL 来源内容做 `<external-content>` 安全标注
- 不过滤（避免误杀），而是标注让 LLM 自己判断可信度

**Layer 2: Action-Time 授权**
- 每次工具调用前评估：身份 + 动作 + 目标 + 参数 + 上下文
- 决策矩阵：READ_ONLY 自动通过，WRITE 首次确认，EXECUTE 白名单检查，EXTERNAL 显式启用

**Layer 3: Process Sandbox**
- Windows: Job Objects + 受限 token
- macOS: Seatbelt (sandbox-exec)
- Linux: bubblewrap (bwrap)
- 默认开启，实验环境可降级为 warning

**Layer 4: 不可变审计日志**
- JSONL 格式，HMAC 签名防篡改
- 通过 EventBus 自动写入，核心路径零侵入
- 支持导出、查询、SOC 2 审计追溯

### Trade-offs

| 决策 | 选择 | 理由 |
|------|------|------|
| 注入检测 | 标注而非过滤 | 不过滤（避免误杀），让 LLM 判断可信度 |
| 沙箱 | 默认开启不可关闭 | 安全设计承诺，实验环境可降级为 warning |
| 审计存储 | JSONL + HMAC | 零依赖、人类可读、防篡改 |

---

## 11. 评测框架（Evaluation）

### 四维指标体系

**过程质量（核心差异化）**：
- 复用尝试率 / 复用采纳率
- 根因定位准确率
- 测试持久化率（持久化数 / 总测试数）
- 指令遵循率 / 漂移发生轮次
- 计划质量 / 汇总质量（PlanExecute / Hierarchical 模式）
- thrashing 事件数 / regex 滥用事件数

**代码质量**：
- 圈复杂度变化 / 重复率变化
- 超长函数新增数 / lint 错误引入数
- 测试覆盖率变化

**效率**：
- token 消耗（输入/输出/缓存命中）
- 工具调用次数 / 成功率
- 耗时 / 成本估算
- thrashing 比率

**安全**：
- 授权阻止次数 / 沙箱越权拦截
- 注入检测 / 危险命令尝试

### 采集方式

全部指标通过 EventBus 监听采集，对核心路径零侵入：
```python
event_bus.subscribe("tool_call_started", collect_reuse_attempt)
event_bus.subscribe("file_written", collect_file_metrics)
event_bus.subscribe("agent_error", collect_error_patterns)
event_bus.subscribe("agent_completed", compute_aggregate_metrics)
```

### SWE-bench 防污染

- 模板变异：改写 issue 描述（同义替换、顺序重排）
- 时间切片：只用模型训练截止后的任务
- 私有测试：维护不公开的测试用例

### A/B 实验

```python
@dataclass
class Experiment:
    variables: dict
    agent_config_a: dict
    agent_config_b: dict
    benchmark: str
    runs_per_config: int   # 每组至少 5 次

    async def run(self) -> ExperimentResult: ...  # 含 p-value
```

### Trade-offs

| 决策 | 选择 | 理由 |
|------|------|------|
| 指标采集 | EventBus 监听 | 零侵入，新增指标不需改核心代码 |
| 自建 benchmark | 专注过程质量 | SWE-bench 已被充分覆盖，差异化应体现在过程 |
| 统计显著性 | 每组 ≥ 5 次 + p-value | 同一任务 token 波动可达 2x，单次跑分不可信 |

---

## 12. 入口层（Adapters）

- **CLI**：`synapse run "fix this bug"` / `synapse eval --benchmark swebench`
- **Python API**：`from synapse import Agent; agent.run(task)`
- **HTTP API**：`POST /run` / `GET /sessions/{id}` / `POST /eval/experiment`

三个入口共享同一个 Container 装配逻辑，无重复代码。

---

## 13. 错误处理策略

```
异常分类：

1. ProviderError    → LLM API 错误（限流、超时、认证失败）
                      重试策略：指数退避（max 3 次）

2. ToolError        → 工具执行失败（沙箱崩溃、命令超时）
                      返回 ToolResult(success=False) 让 LLM 自行判断

3. SandboxError     → 沙箱违规（越权访问、危险命令）
                      被安全层拦截，不进入 LLM 上下文

4. PlannerError     → 规划失败（循环超限、子任务死锁）
                      返回 AgentResult(status=FAILED) 带完整 trace

5. ConfigError      → 配置错误（启动时校验，fast-fail）
                      不进入 Agent 循环
```

---

## 14. 测试策略

```
协议层测试 (protocols/)    → 纯接口验证，无实现
单元测试 (modules/)         → 每个模块 mock 其依赖的 Protocol
集成测试 (core + modules)   → 真实 Container 装配 + mock LLM (record/replay)
评测测试 (eval/)            → Benchmark runner 的 self-test
安全测试 (security/)        → 注入 payload / 沙箱逃逸尝试 / 越权命令
端到端测试                  → 真实 API 调用（小规模）
```

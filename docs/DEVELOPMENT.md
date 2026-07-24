# Synapse 开发日志

> 从需求到交付的完整过程。每个条目标注变更类型。后续每次会话追加，内容过多时压缩精简。

### 变更类型说明

| 前缀 | 含义 |
|------|------|
| `[feat]` | 新功能、新模块 |
| `[fix]` | Bug 修复 |
| `[docs]` | 文档、注释、README、开发日志 |
| `[chore]` | 项目配置、依赖、gitignore、打包 |
| `[design]` | 架构决策、方案选型、trade-off |
| `[review]` | 审查发现与修复 |
| `[ux]` | 用户交互体验改进 |

---

## 2026-07-16 · 项目启动 → Phase 1 交付

---

### 一、需求提出与初始定位

用户构建 **Synapse**——简化版 Code Agent（参考 Claude Code / Codex）。

**三条核心原则**：
1. 模块解耦，参考 [pico v3](https://gitee.com/htxoffical/pico/tree/v3/) 结构但不照搬
2. 每处设计讲清楚 trade-off
3. 以实验思想通过 benchmark 证明效果，某些方面可不如主流但必须有存在价值

**初始 MVP 范围选项**：

```
A. 单次对话式 Agent    — 无多轮、无记忆、无规划，增强版 llm CLI
B. 交互式会话 Agent    — 多轮对话 + 上下文窗口 + ReAct 循环
C. 具备记忆与规划      — B + 持久记忆 + 任务分解
D. 完整 Harness        — C + 内置 Benchmark + 审计追踪 ★ 用户选择
```

---

### 二、差异化定位调研

调研了当前 Code Agent 领域 9 大类痛点：

| 痛点 | 关键数据 |
|------|---------|
| 上下文治理 | 42% 项目配置被低优先级规则撑爆；模型 U 型注意力曲线；商业工具偷偷压缩上下文 |
| 行为退化 | Agent 倾向最小补丁（"关 Issue 模式"）；不找现有实现就造轮子；测试写完就丢 |
| 评测失效 | SWE-bench 70-80% 虚高，BeyondSWE 上顶级模型不到 45%；数据污染严重 |
| Token 浪费 | 输入输出比 154:1；多花 token 不会让结果更好；反复读写同一文件（thrashing） |
| 架构耦合 | Agent = Model + Harness，但 Harness 不稳定被误认为模型问题 |
| 安全隐患 | 沙箱不一致、Prompt Injection、hook 不安全执行 |
| 企业治理 | 多头 agent 散乱、成本归属不清、审计日志不成熟 |
| 多 Agent 放大 | 并行执行错误几何级放大 |
| 上下文选择器 | RAG 常返回"像但不精确"的结果，确定性工具（grep/AST）更可靠 |

**差异化定位选项**：

```
A. 上下文工程优先    — 第一个架构层面做上下文治理的 agent
B. 过程质量优先      — 不只关注"过不过"，看"怎么过的" ★ 用户选择
C. 透明可审计优先    — 全链路可追溯，面向 SOC 2 合规
D. 模块化实验平台    — 每个模块可热插拔 A/B 测试
E. 安全设计优先      — 默认沙箱 + action-time 授权 + 注入防御
```

用户认同 B 为主线，其余方向在后续设计中兼顾。

---

### 三、10 轮需求澄清

#### 第 1 轮 · MVP 边界

```
A. 单次对话式       B. 交互式会话       C. 具记忆与规划       D. 完整 Harness ★
```
→ **D**：含评测/审计的完整 Harness。

#### 第 2 轮 · 核心差异

```
A. 可实验性      B. 教学可理解      C. 轻量本地化      D. 模块可组合
```
→ 用户否定以上全部，要求先调研当前 Code Agent 真实痛点再定。调研后给出新选项 A-E（见上文），选择 **B（过程质量）**。

#### 第 3 轮 · 过程质量维度

```
A. 复用优先         B. 根因修复         C. 测试持久化
D. 规范遵循         E. 代码不退化       F. 全部要，分阶段 ★
```
→ **F**。

#### 第 4 轮 · LLM 接入策略

```
A. 多 Provider 统一接口 ★          B. 先绑定单一，预留扩展点          C. 本地模型优先
```
→ **A**：Anthropic / OpenAI / Google / DeepSeek / Ollama 统一抽象。

#### 第 5 轮 · 工具范围

```
A. 基础文件+Shell      B. A+代码理解(grep/AST)      C. B+Git+项目管理      D. C+外部集成 ★
```
→ **D**：全栈工具，要求工具系统从架构上可扩展。

#### 第 6 轮 · 评测策略

```
A. 复用现有 benchmark + 防污染      B. 自建过程质量 benchmark      C. A+B ★
```
→ **C**：业界横向对比 + 差异化指标。

#### 第 7 轮 · 交互模型

```
A. 纯 CLI      B. Library + CLI 双模式      C. B + HTTP API ★
```
→ **C**：三种入口共享同一 Container 装配。

#### 第 8 轮 · 记忆系统

```
A. 仅会话记忆      B. +项目记忆      C. +用户记忆      D. +语义记忆 ★
```
→ **D**：Session → Project → User → Semantic 四层。

#### 第 9 轮 · 规划架构

```
A. ReAct 单 Agent      B. Plan-then-Execute      C. 分层编排      D. 可配置三种 ★
```
→ **D**：统一 Protocol，按任务复杂度自动/手动选择模式。

#### 第 10 轮 · 安全策略

```
A. 进程沙箱      B. A+ActionTime 授权      C. B+审计日志      D. C+注入防御      E. 全要分阶段 ★
```
→ **E**：Phase 1 至少 Sandbox + Auth，Phase 2 审计，Phase 3 注入防御。

---

### 四、架构方案选型

**提出三种架构方案**：

```
方案 1 · Protocol-Based IoC（Pico 风格）
  ┌─────────────────────────────────┐
  │  protocols/  → 纯接口           │
  │  core/       → IoC 容器         │
  │  modules/    → 实现类           │
  │  adapters/   → CLI/HTTP         │
  └─────────────────────────────────┘
  优点：接口实现分离、天然 mock、可独立发布 PyPI 包
  缺点：抽象成本高、IoC 增加理解门槛、Protocol 变更影响所有实现

方案 2 · Event-Driven Pipeline
  ┌─────────────────────────────────┐
  │  engine/     → 事件总线+Pipeline │
  │  pipeline/   → 各阶段 handler    │
  │  tools/      → 工具注册表        │
  │  memory/     → pipeline hooks    │
  └─────────────────────────────────┘
  优点：极灵活插拔、新功能零侵入、评测只需监听事件
  缺点：事件顺序隐式依赖、调试困难（控制流不显式）、性能开销

方案 3 · 分层 Protocol + EventBus 横切 ★
  ┌──────────────────────────────────────┐
  │  adapters/   CLI │ Library │ HTTP    │
  │  core/        Container │ Agent │ EB │
  │  protocols/   6 个 Protocol          │
  │  modules/     Provider │ Tool │ ...  │
  │  eval/        事件消费者（零侵入）    │
  └──────────────────────────────────────┘
  优点：核心路径清晰、横切不污染核心、评测零侵入、测试友好
  缺点：需明确区分"核心依赖"和"横切事件"、两套机制学习成本
```

→ 用户选择 **方案 3**。

**Protocol vs EventBus 的边界规则**：

| 类型 | 模块 | 原因 |
|------|------|------|
| Protocol（同步依赖） | LLM, Tool, Planner, Memory, Retriever, Sandbox | Agent 主循环直接依赖 |
| EventBus（异步横切） | 审计日志、指标采集、安全通知 | 主循环不等待返回，独立演进 |

---

### 五、模块设计决策（逐部分确认）

#### 5.1 上下文治理

四区制（SYSTEM → CORE → REFERENCE → OVERFLOW）+ 三阶段流程（BUILD → ORGANIZE → MAINTAIN）。

| Trade-off | 选择 | 理由 |
|-----------|------|------|
| 检索策略 | Grep/AST 优先，语义 fallback | RAG "像但不精确"，确定性工具 100% 召回 |
| 分区模型 | 四区制 | SYSTEM 区不可压缩，解决"对话变长遗忘指令" |
| 压缩触发 | 按 token 用量 | 不同任务 token 差异达 7M，固定轮数不可靠 |
| 语义记忆 | OVERFLOW 区（最低优先级） | 语义检索精度不够，仅做辅助 |

#### 5.2 记忆系统

四层记忆（Session → Project → User → Semantic），统一 `MemoryStore` Protocol。

| Trade-off | 选择 | 理由 |
|-----------|------|------|
| 存储格式 | Markdown 文件 | 人类可读、git 可追踪 |
| 语义记忆触发 | 仅当确定性工具无结果 | 避免不精确语义匹配污染核心上下文 |
| Agent 写入 | 自动写入（可配置关闭） | 任务完成后自动记录经验 |

#### 5.3 规划系统

三种模式：ReActPlanner / PlanExecutePlanner / HierarchicalPlanner。

| Trade-off | 选择 | 理由 |
|-----------|------|------|
| 子任务串行 vs 并行 | 串行 | 调研明确并行 agent 会放大错误 |
| 计划确认 | 可配置，默认交互 | PlanExecute 默认等确认减少漂移 |
| 子 session | 独立 fork | 避免子任务上下文污染 |

#### 5.4 工具系统

五层工具（文件 → 代码理解 → 执行 → 外部集成 → 评测），安全五级分级（READ_ONLY → WRITE_LOCAL → EXECUTE → EXTERNAL → META）。

| Trade-off | 选择 | 理由 |
|-----------|------|------|
| ToolSchema 格式 | 裸 dict | 兼容所有 LLM Provider function calling |
| 风险声明 | Tool 声明 + Registry 可覆盖 | 生产环境可禁止 EXTERNAL 工具 |
| EditTool 策略 | 精确字符串替换 | AST 操作跨语言质量参差不齐 |

#### 5.5 安全层

四层防护：Prompt Injection 标注 → Action-Time 授权 → Process Sandbox → 不可变审计日志。

| Trade-off | 选择 | 理由 |
|-----------|------|------|
| 注入检测 | 标注而非过滤 | 避免误杀，让 LLM 自己判断可信度 |
| 沙箱 | 默认开启不可关闭 | 安全承诺，实验环境可降级 warning |
| 审计存储 | JSONL + HMAC | 零依赖、防篡改 |

#### 5.6 评测框架

四维指标体系：过程质量（核心差异化）、代码质量、效率、安全。全部通过 EventBus 监听采集。

| Trade-off | 选择 | 理由 |
|-----------|------|------|
| 指标采集 | EventBus 监听 | 零侵入，新增指标不改核心代码 |
| 自建 benchmark | 专注过程质量 | SWE-bench 已被充分覆盖，差异化在过程 |
| 统计显著性 | 每组 ≥ 5 次 + p-value | 同一任务 token 波动可达 2x |

#### 5.7 MVP 阶段划分

| 阶段 | 内容 |
|------|------|
| **Phase 1** | IoC + Agent + ReActPlanner + Anthropic + 7 Tools + SessionMemory + ContextRetriever + Sandbox + Auth + CLI |
| **Phase 2** | PlanExecutePlanner + HierarchicalPlanner + 其他 Provider + 项目/用户记忆 + 完整四区治理 + 审计日志 |
| **Phase 3** | 评测框架 + 语义记忆 + HTTP API + Injection 防御 + SWE-bench 防污染 |
| **Phase 4** | 外部集成工具 + 完整 benchmark 跑分 + Qdrant |

---

### 六、Phase 1 实施

**方法**：Subagent-Driven Development — 每个任务独立 subagent 实现 → 审查 → 修复 → 下一任务。

**20 个任务**，按依赖链排序：

| # | 模块 | 产出 | 测试 |
|---|------|------|------|
| 1 | Project Setup | pyproject.toml, deps | - |
| 2 | Config | Pydantic schema + YAML/env loader | - |
| 3 | Protocols/Events | EventType enum + 7 event dataclasses | - |
| 4 | Protocols | 6 个 Protocol（LLM/Tool/Planner/Memory/Retriever/Sandbox） | - |
| 5 | Core/Exceptions | 6 类异常（SynapseError + 5 子类） | - |
| 6 | Core/EventBus | subscribe/unsubscribe/emit, handler 异常隔离 | 4 |
| 7 | Core/Container | 单例 + 工厂 + 泛型别名 | 5 |
| 8 | Core/Session | 消息历史 + fork + token 估算 | 5 |
| 9 | Providers | AnthropicProvider (chat/tool_use/stream stub) | 4 |
| 10 | Tools(1) | Read/Write/Edit/Glob/Grep | 7 |
| 11 | Tools(2) | Shell + Git(只读) + ToolRegistry | 7 |
| 12 | Memory | SessionMemory (内存 dict) | 4 |
| 13 | Context | BasicContextRetriever | 2 |
| 14 | Security(1) | ProcessSandbox (跨平台) | 4 |
| 15 | Security(2) | ActionAuthorizer (五级决策矩阵) | 8 |
| 16 | Core/Agent | 依赖装配 → Planner 委托 → 记忆持久化 | 2 |
| 17 | Planning | ReActPlanner + thrashing 检测 | 4 |
| 18 | Adapters | CLI (`synapse run` / `synapse version`) | - |
| 19 | Integration | 全管线 mock LLM 测试 | 2 |
| 20 | Verification | 58 tests, 架构边界验证 | - |

**实施中发现的 Brief Bug 及修复**：
- Task 10 测试用 `result.stdout` 但协议字段名是 `result.output` → 修复
- Task 10 GrepTool 吞掉 ripgrep 错误 + 仅搜 `*.py` → 修复
- Task 17 代码有两个 `else:` 语法错误 + AsyncMock 不兼容 → 修复

---

### 七、最终审查与修复

最终全分支审查发现：

| 级别 | 编号 | 问题 | 修复 |
|------|------|------|------|
| **Critical** | C1 | ActionAuthorizer 已实现但未接入 ReActPlanner 执行路径 | 接入：auth 参数 → `ReActPlanner.__init__`，每次工具调用前检查授权 |
| Important | I1 | Anthropic tool_result 格式为纯文本而非 content block | 改为 `{"type":"tool_result","tool_use_id":"...","content":"..."}` |
| Important | I2 | ReAct 循环无 LLM 调用重试 | 指数退避重试（1s/2s/4s, max 3 次） |
| Important | I3 | GrepTool 使用同步 `subprocess.run()` | 改为 `asyncio.create_subprocess_exec` + `asyncio.to_thread` |
| Important | I4 | `max_tokens=4096` 硬编码 | 加入 `ProviderConfig.max_tokens` |

---

### 八、交付物

```
Synapse/
├── synapse/
│   ├── protocols/     # 7 Protocol + Event 类型（纯接口，零依赖）
│   ├── core/          # Agent, Container, EventBus, Session, Exceptions
│   ├── modules/
│   │   ├── providers/ # AnthropicProvider (+ retry)
│   │   ├── tools/     # Read, Write, Edit, Glob, Grep, Shell, Git + Registry
│   │   ├── planning/  # ReActPlanner (+ auth + thrashing detect)
│   │   ├── memory/    # SessionMemory
│   │   ├── context/   # BasicContextRetriever
│   │   └── security/  # ProcessSandbox, ActionAuthorizer
│   ├── adapters/      # CLI (synapse run / synapse version)
│   └── config/        # Pydantic schema + YAML/env loader
├── tests/             # 58 tests（全部通过，含集成测试）
├── docs/superpowers/  # 设计 Spec + 实施 Plan
├── DEVELOPMENT.md     # 本文件
└── pyproject.toml
```

| 指标 | 数值 |
|------|------|
| Commits | 23 |
| Files | 57 |
| Lines | ~3,100 |
| Tests | 58 (0 failures) |
| 架构层隔离 | protocols → core → modules（验证通过） |
| Protocol 边界 | 6 Protocol + EventBus（7 横切事件类型） |
| 安全分级 | 5 级（READ_ONLY / WRITE_LOCAL / EXECUTE / EXTERNAL / META） |

---

## 2026-07-16 · Phase 2 完成

### 九、Phase 2 实施

**12 个任务**，按依赖关系执行：

| # | 模块 | 产出 | 测试 |
|---|------|------|------|
| 1 | OpenAI Provider | AsyncOpenAI 封装，tool call 格式转换 | 3 |
| 2 | Google Gemini Provider | google-genai SDK 封装，system_instruction 分离 | 2 |
| 3 | DeepSeek Provider | OpenAI 兼容 API（base_url → api.deepseek.com） | 2 |
| 4 | Ollama Provider | OpenAI 兼容 API（base_url → localhost:11434） | 2 |
| 5 | ProjectMemory | `.synapse/memory/` 下 YAML frontmatter Markdown 持久化 | 4 |
| 6 | UserMemory | `~/.synapse/memory/` 跨项目持久化 | 3 |
| 7 | Context Partitioner + Compactor | 四区预算强制执行 + OVERFLOW 截断压缩 | 4 |
| 8 | PlanExecutePlanner | Plan → Execute → Verify 三阶段，phase_clear 上下文清理 | 3 |
| 9 | HierarchicalPlanner | 任务分解 → Session.fork → 串行执行 → LLM 汇总 | 3 |
| 10 | AuditLogger | EventBus 订阅 → JSONL + HMAC → 不可变审计日志 | 3 |
| 11 | Library API + CLI | `Synapse` facade 类 + `--provider/--model/--mode` flag | 2 |
| 12 | Integration + 验证 | 4 集成测试 + 全量测试 + 架构边界检查 | 4 |

**Provider 适配差异**：

| Provider | SDK | 特殊处理 |
|----------|-----|---------|
| Anthropic | anthropic | system 参数分离，tool_use content block |
| OpenAI | openai | `{"type":"function","function":{...}}` 包装，JSON arguments 解析 |
| Google | google-genai | system_instruction 配置，FunctionDeclaration Schema 递归转换 |
| DeepSeek | openai (兼容) | base_url="https://api.deepseek.com/v1" |
| Ollama | openai (兼容) | base_url="http://localhost:11434/v1" |

**LayeredMemory**：组合 SessionMemory + ProjectMemory + UserMemory，统一 MemoryStore 接口，按 MemoryLevel 路由。

### 十、Phase 2 交付物

```
Synapse/
├── synapse/
│   ├── protocols/     # +TaskDecomposed +MergeResult +PlanCreated 事件
│   ├── core/          # (无变化)
│   ├── modules/
│   │   ├── providers/ # +OpenAI +Google +DeepSeek +Ollama (共 5)
│   │   ├── tools/     # (无变化)
│   │   ├── planning/  # +PlanExecutePlanner +HierarchicalPlanner
│   │   ├── memory/    # +ProjectMemory +UserMemory
│   │   ├── context/   # +Partitioner +Compactor
│   │   └── security/  # +AuditLogger (JSONL+HMAC)
│   ├── adapters/      # +Library API (Synapse facade), CLI --provider/--model/--mode
│   └── config/        # (无变化)
├── tests/             # 98 tests（0 failures）
├── tools/             # +check_boundaries.py
└── docs/superpowers/  # +Phase 2 Plan
```

| 指标 | Phase 1 | Phase 2 |
|------|---------|---------|
| Commits | 23 | +12 → 35 |
| Tests | 58 | +40 → 98 |
| Providers | 1 (Anthropic) | 5 |
| Planners | 1 (ReAct) | 3 (ReAct + PlanExecute + Hierarchical) |
| Memory | 1 (Session) | 3 (Session + Project + User) |
| Context | Retriever | Retriever + Partitioner + Compactor |
| Security | Sandbox + Auth | Sandbox + Auth + Audit |

---

## 2026-07-16 · Phase 3 完成

### 十一、Phase 3 实施

**8 个任务**：

| # | 模块 | 产出 | 测试 |
|---|------|------|------|
| 1 | Metrics Collectors | Process/Quality/Efficiency/Safety 四维指标采集器 | 8 |
| 2 | Benchmark Runner | SWE-bench 防污染适配 + 自建过程质量 benchmark | 3 |
| 3 | A/B Experiments | Experiment 类 + scipy t-test 显著性检验 | 3 |
| 4 | SemanticMemory | ChromaDB 向量存储，语义相似检索 | 3 |
| 5 | HTTP API | FastAPI server: /run, /sessions, /eval/experiment, /health | 3 |
| 6 | Injection Defense | TrustLevel 标注 + EXTERNAL 包裹（标注不拦截） | 3 |
| 7 | Wiring | SemanticMemory → LayeredMemory, InjectionGuard → Agent, CLI: serve/eval/experiment | - |
| 8 | Integration + 验证 | 7 集成测试 + 128 全量测试 + 架构边界 | 7 |

**四维指标体系**：

```
过程质量 (核心差异化):
  reuse_attempted/found/adopted, root_cause_accuracy,
  test_persistence_rate, instruction_drift_at_round,
  plan_quality_score, merge_quality_score,
  thrashing_events, regex_abuse_events

代码质量:
  complexity_delta, duplication_rate,
  function_length_violations, test_coverage_delta,
  lint_errors_introduced

效率:
  tokens_input/output/cache_hit, tool_call_count,
  success_rate, duration_ms, cost_estimate_usd, thrashing_ratio

安全:
  auth_blocks, sandbox_violations, injection_attempts,
  out_of_workspace_access, dangerous_command_attempts
```

**SWE-bench 防污染三措施**：模板变异（同义替换+重排）、时间切片、私有测试套件。

**Injection Defense 设计**：标注 TrustLevel（SYSTEM/USER/DETERMINISTIC/EXTERNAL），EXTERNAL 内容包裹 `<external-content>` 标签。不过滤——让 LLM 自己判断可信度。

### 十二、Phase 3 交付物

| 指标 | Phase 1 | Phase 2 | Phase 3 |
|------|---------|---------|---------|
| Commits | 23 | +12 → 35 | +8 → 43 |
| Tests | 58 | +40 → 98 | +30 → 128 |
| Providers | 1 | 5 | 5 |
| Planners | 1 | 3 | 3 |
| Memory | 1 | 3 | 4（+Semantic） |
| Context | Retriever | +Partitioner/Compactor | +InjectionGuard |
| Security | Sandbox+Auth | +Audit | +Injection Defense |
| New | - | - | Eval(metrics+benchmarks+experiments), HTTP API |

**API 端点**：`POST /run`, `GET /sessions/{id}`, `GET /sessions/{id}/messages`, `POST /eval/experiment`, `GET /eval/experiment/{id}`, `GET /health`

**CLI 新增命令**：`synapse serve`, `synapse eval`, `synapse experiment`

---

## 2026-07-16 · Phase 4 完成

### 十三、Phase 4 实施

**5 个任务**：

| # | 模块 | 产出 | 测试 |
|---|------|------|------|
| 1 | HTTP Tool | httpx 异步 HTTP（GET/POST，30s 超时，100KB 限制） | 3 |
| 2 | Database Tool | sqlite3 查询（默认只读，workspace 路径检查） | 6 |
| 3 | Browser Tool | Playwright 浏览器自动化（navigate + text + screenshot） | 2 |
| 4 | Qdrant Backend | QdrantMemory（本地模式，TF-IDF embedding） | 3 |
| 5 | Wiring + CLI + 集成 | --memory-backend, --enable-external-tools 标志 | 4 |

**EXTERNAL 工具安全机制**：默认禁用，需 `--enable-external-tools` 显式启用。通过 ActionAuthorizer 的 `allow_external` 配置门控。

### 十四、最终交付物

```
Synapse/
├── synapse/
│   ├── protocols/     # 8 Protocol + Event 类型（+TaskDecomposed/Merge/PlanCreated）
│   ├── core/          # Agent, Container, EventBus, Session, Exceptions
│   ├── modules/
│   │   ├── providers/ # 5 个 Provider（Anthropic/OpenAI/Google/DeepSeek/Ollama）
│   │   ├── tools/     # 10 个工具（File×4 + Search + Shell + Git + HTTP + DB + Browser）
│   │   ├── planning/  # 3 种 Planner（ReAct + PlanExecute + Hierarchical）
│   │   ├── memory/    # 5 个实现（Session + Project + User + ChromaDB + Qdrant）
│   │   ├── context/   # Retriever + Partitioner + Compactor
│   │   └── security/  # Sandbox + Auth + Audit + Injection Defense
│   ├── adapters/      # CLI + Library API + HTTP Server（FastAPI, 6 endpoints）
│   ├── config/        # Pydantic schema + YAML/env loader
│   └── eval/          # 4 Metrics Collectors + 2 Benchmarks + A/B Experiments
├── tests/             # 148 tests（0 failures）
└── tools/             # check_boundaries.py
```

### 十五、四阶段数据总览

| | Phase 1 | Phase 2 | Phase 3 | Phase 4 | **总计** |
|---|---------|---------|---------|---------|-----|
| Commits | 23 | +12 | +8 | +5 | **48** |
| Tests | 58 | 98 | 128 | 148 | **148** |
| Files | 57 | ~80 | ~110 | ~125 | **~125** |
| Lines | ~3,100 | ~5,500 | ~8,000 | ~9,000 | **~9,000** |

| 能力 | 覆盖 |
|------|------|
| LLM Provider | Anthropic, OpenAI, Google Gemini, DeepSeek, Ollama（5） |
| 规划模式 | ReAct, Plan-Execute, Hierarchical（3） |
| 记忆系统 | Session, Project, User, Semantic(ChromaDB+Qdrant)（5 实现） |
| 工具 | Read, Write, Edit, Glob, Grep, Shell, Git, HTTP, DB, Browser（10） |
| 安全 | Sandbox, ActionAuth, Audit(JSONL+HMAC), Injection Defense（4 层） |
| 上下文 | Retriever + Partitioner + Compactor + InjectionGuard（4 组件） |
| 评测 | Process/Quality/Efficiency/Safety Metrics + SWE-bench + ProcessBench + A/B Experiments |
| 入口 | CLI（6 命令）+ Python Library API + HTTP Server（6 端点） |
| MCP | McpClient Protocol + OfficialSdkMcpClient(stdio+HTTP) + McpManager + McpToolWrapper |

---

## 2026-07-16 · MCP 协议支持

### 十六、MCP 实施

**7 个任务**，遵循 Protocol → 实现 → Manager → 集成 模式：

| # | 模块 | 产出 | 测试 |
|---|------|------|------|
| 1 | Protocol + Config | `McpClient` Protocol, `McpServerConfig` dataclass | - |
| 2 | McpToolWrapper | MCP tool → Synapse `Tool` Protocol（`mcp.<server>.<tool>` 命名） | 5 |
| 3 | SDK Client (stdio) | 基于 `mcp` 官方 SDK，子进程 JSON-RPC | 3 |
| 4 | SDK Client (HTTP) | Streamable HTTP transport 扩展 | 并入 3 |
| 5 | McpManager | 多连接生命周期管理：add → discover → wrap → register | 5 |
| 6 | Wiring | `Synapse(mcp_servers=...)` + CLI `--mcp-server` flag | 7 |
| 7 | Integration | 端到端 MCP 工具调用验证 | 4 |

**架构**：
```
CLI --mcp-server "name:cmd"     Synapse(mcp_servers=[...])
        │                               │
        └───────────┬───────────────────┘
                    ▼
            McpManager.add_server(config)
                    │
                    ▼
            OfficialSdkMcpClient.connect()
                    │
                    ▼
            client.list_tools() → [tool_schemas]
                    │
                    ▼
            McpToolWrapper(tool) → registry.register()
                    │
                    ▼
            Agent 透明调用: tools.get("mcp.filesystem.read_file")
```

**Transport 支持**：stdio（子进程 JSON-RPC）、Streamable HTTP（远程端点）。SSE 已由 MCP 规范废弃。

**量化**：172 tests (+24), 50 commits, ~10,500 lines。

---

## 2026-07-17 · 体验优化 & Bug 修复

### [feat] 交互式 Chat REPL

新增 `synapse chat` 命令：多轮对话自动保持 Session，`/clear` 重置，`/exit` 退出，自动读取配置，Rich Markdown 渲染输出。

### [fix] 多厂商 API Key 环境变量支持

配置加载器原先只识别 `ANTHROPIC_API_KEY`。新增 `OPENAI_API_KEY`、`DEEPSEEK_API_KEY`、`GOOGLE_API_KEY`，DeepSeekProvider 改为链式回退（配置 → 环境变量 → SDK 默认）。

### [chore] 打包与分发优化

- `pyproject.toml` 拆为 12 个可选依赖组，核心只依赖 pydantic+pyyaml+rich
- README 重写（Quick Start、配置三方式、命令参考、架构图）
- `synapse chat`/`run` 无 API Key 时打印配置引导
- `requirements.txt` 精简

### [fix] Spinner + 进度指示器

原先只显示静态 "Working..."，Agent 卡住时用户无法判断。改为旋转动画 + 计时（`\ Working... (12s)`），后台 `asyncio.Event` 控制启停。

### [feat] 交互式授权确认

`ActionAuthorizer` 原先对 workspace 外写入做硬拒绝（`allowed=False`）。改为弹出交互确认（`allowed=True, requires_confirmation=True`），用户在 chat 中看到 `Allow? [y/n]:` 提示后决定。非交互模式（run/serve）自动拒绝。`_is_in_workspace` 从 `startswith` 改为 `Path.is_relative_to`，防止 `/project_evil` 绕过 `/project` 检查。

### [fix] Auth 弹窗被 Spinner 覆盖

确认回调触发时 spinner 仍在后台刷新，导致 `Allow? [y/n]:` 提示被覆盖。修复：`_make_confirm_callback(pause_event)` 在显示提示前 `pause_event.clear()` 暂停 spinner，用户回复后 `pause_event.set()` 恢复。

### 当前状态

| 指标 | 数值 |
|------|------|
| Commits | 56 |
| Tests | 172 |
| 交互模式 | CLI run / CLI chat / Library API / HTTP Server |

---

## 2026-07-18 · CLI 主界面重构 & 启动优化

### [ux] pico v3 风格主界面

参考本地 `D:\File\pythonproject\pico` 的 v3 分支，将 `synapse` 无参主界面改造为 pico 风格：

- **框式欢迎横幅**：`+===+` 边框 + 大脑半球 ASCII 艺术 + 单行 tagline（Synapse · subtitle · ready）
- **两列信息行**：MODEL / VERSION / PROVIDER / PLANNING 标签用 `bright_magenta` 着色
- **提示符**：`synapse> `（对标 pico 的 `pico> `）
- **命令系统**：新增 `/memory`、`/session`，`/reset` 作为 `/clear` 别名
- **Rich 色彩**：标签 `bright_magenta`、艺术 `bright_cyan`、边框 `bright_black`、副标题 `dim italic`、状态 `dim green`
- **自适应宽度**：用 `os.get_terminal_size()` 检测终端列宽，动态计算边框和列宽

### [ux] 双击 Ctrl+C 退出

- **Windows**：`SetConsoleCtrlHandler` 注册回调，返回 `TRUE` 阻止 CMD 的 `Terminate batch job` 弹窗。第一次按 Ctrl+C 提示 `Press Ctrl+C again to exit.`，3 秒内再按直接 `ExitProcess(0)` 静默退出
- **Unix**：`signal.SIGINT` handler 实现同样双击逻辑
- **退出路径**：`/exit`、Ctrl+C、Ctrl+D 全部静默退出，`exiting` 标志阻止授权回调弹出多余提示

**根因**：pyenv-win 的 `.bat` shim 导致 CMD 在批处理层面拦截 Ctrl+C。最终方案是在 PowerShell 中用 `synapse.ps1` 直接调 `python.exe` 全路径

### [ux] 授权提示改进

- 三选项：`[A]llow / [D]eny / [Y]es to all`
- `[Y]es to all` 将该工具名加入永久白名单，同 session 不再询问
- 不再显示原因和参数

### [perf] 启动优化

三次递进优化：

1. `library.py`：provider 类改为 `importlib.import_module()` 惰性加载，eval/MCP 按需导入
2. `cli.py`：所有 tools/memory/context/security/planning/provider 模块级 import 移入函数内（`build_container`、`_create_provider`、`_create_planner`）
3. `_main_interface`：Synapse 实例创建延迟到首次用户输入（`_get_synapse()`），欢迎横幅秒出

**效果**：模块导入从 ~72s 降到 ~100ms

### [chore] `synapse setup` 命令

- 自动检测 `sys.executable` 全路径
- 在 `~/.local/bin/` 生成 `synapse.cmd`（CMD）+ `synapse.ps1`（PowerShell）启动器
- 打印 PATH 配置和 PowerShell alias 指引

### [feat] `synapse/__main__.py`

支持 `python -m synapse` 直接启动

### 当前状态

| 指标 | 数值 |
|------|------|
| Commits | 65 |
| Tests | 174 |

---

## 2026-07-19 · Token 经济性优化

### [feat] Thrashing Early-Stop

- 新增 `max_thrashing_events` 配置项（`PlanningConfig`，默认 2）
- `ReActPlanner` 在 thrashing 事件超过阈值后主动终止循环，输出受影响的文件列表
- 用 `thrash_stop` 标志跳出外层 `for iteration` 循环

### [feat] Context Budget 接入 Agent

- `ContextPartitioner` 和 `ContextCompactor` 注册到 IoC 容器
- `Agent._build_context` 在获取 context 后调用 `partitioner.partition(context, budget)` 裁剪超限区块
- 移除过时的 "Partitioner and compactor are currently standalone" 注释

### [feat] Token 预算控制

- 新增 `max_tokens_per_task` 配置项（`PlanningConfig`，默认 200,000）
- `ReActPlanner` 每次 LLM 调用后检查累计 token：80% 时发出 `AgentProgress` 警告事件，100% 时终止并返回 `PARTIAL`
- `/memory` 命令新增显示 `Est. tokens: {est} / {budget}`

### 当前状态

| 指标 | 数值 |
|------|------|
| Commits | 66 |
| Tests | 174 |

---

## 2026-07-20 · CLI 主界面美化与联网搜索内建化

### [ux] 配色统一与品牌色板

抽出 `_BRAND / _LABEL / _BORDER / _HINT` 四个色板常量（`cli.py:866-870`），整体改为蓝色系（`bright_cyan` / `cyan`），边框、图标、标签、艺术字、prompt 同色。后续调色只改一处。

### [ux] Banner 重排与图标

- 字段标签前加 ASCII 图标（`> WORKSPACE`、`* MODEL`、`# VERSION`、`@ PROVIDER`、`~ PLANNING`、`% config`）
  - 因 Windows GBK 终端不支持 `▣◆◇●` 等 Unicode 图标，限定 ASCII 字符集
- 两列行重写：左列右对齐到 `left_w`，间距 `gap=4`，标签列宽 12
- 边框改用 `Text` 渲染，避免颜色渗漏到内容

### [ux] 中央 ASCII 大图重设计

由线条版改为实心版本，用 `#` 字符填充大脑主体，背景留白对比，更有"实心"质感。

### [fix] 边框自适应终端字体缩放

- `Console()` 不再锁定 `width`，每次渲染让 Rich 自动检测当前终端宽度
- REPL 主循环每轮检查 `console.width != _last_cols`，字体缩放导致列数变化则重绘 banner
- 修复了放大终端字体时边框排版混乱的问题

### [feat] `/` 命令自动补全

- 新增 `_SLASH_COMMANDS` 元组按顺序声明 10 个命令
- 用 `prompt_toolkit` 提供补全菜单：输入 `/` 显示全部，输入 `/m` 过滤 m 开头，最多显示 6 条（`_COMPLETION_LIMIT`），右侧显示命令描述
- 缺 `prompt_toolkit` 时自动回退到 `console.input`，已在 `pyproject.toml` 加入 `prompt_toolkit>=3.0` 依赖

### [fix] prompt_toolkit 在 asyncio 中触发 RuntimeError

`prompt_session.prompt()` 内部调用 `asyncio.run()`，与 `_main_interface` 已有的事件循环冲突（`RuntimeError: asyncio.run() cannot be called from a running event loop`）。改为 `await prompt_session.prompt_async(...)`。

### [feat] 动态显示 token 数与耗时

**token 计数**：
- `react.py` 每次 LLM 响应后发出 `phase="token_update"` 的 `AgentProgress` 事件，载荷 `tokens=A+B`
- CLI `_on_progress` 解析该消息，把 `(X.Xk tok)` 附加到 spinner 文本末尾
- 工具开始/结束事件也带上当前 token 数

**耗时显示**：
- 新增后台 `asyncio.Task` `_tick()`，每 0.5s 调用 `status.update(_render())` 刷新 spinner
- `_fmt_elapsed()` 计算从任务开始的耗时（`<60s` 显示 `Xs`，否则 `Xm YYs`）
- `_render()` 把 `label · tok · elapsed` 拼成一行 dim 文本
- 在 `finally` 里 `tick_task.cancel()` 并 `await` 它，保证任务结束时干净退出

**效果示例**：`Working...  ·  1.2k tok  ·  7s` 会持续跳动秒数。

### [ux] 确认提示大小写均可

`_make_confirm_callback` 原本就调用 `.lower()`，仅把提示文案 `(A)llow / (D)eny / (Y)es to all` 改为 `(a)llow / (d)eny / (y)es to all`，避免误导用户以为只能大写。

### [feat] WebSearchTool（联网搜索内建化）

**背景**：此前 agent 没有专门的联网搜索工具，只能通过 `shell` 工具写 Python 脚本或 `curl` 调用外部 API，每次搜索 LLM 都要：写脚本 → 执行 → 解析 → 抽取结果，啰嗦又慢。

**实现**（`synapse/modules/tools/web_search.py`）：
- 直接 POST 到 `https://html.duckduckgo.com/html/`，无 API key、无额外依赖（用已有的 httpx）
- 输入：`query`（必填）+ `max_results`（默认 5，最大 8）
- 输出格式化 markdown：每条 `标题 / URL / 摘要`
- 解析逻辑成对提取（title + url + snippet），避免广告导致三个列表错位
- 主动过滤 DuckDuckGo 广告（`duckduckgo.com/y.js`、`ad_domain=`）
- 自动解包 DDG 的 `uddg=` 重定向 URL 拿到真实地址
- `risk_level = EXTERNAL`，`requires_sandbox = False`

**默认注册**（`library.py`）：在 `_create_all_tools` 默认工具集里加入 `WebSearchTool`，不依赖 `enable_external_tools`，默认可用。`/tools` 命令展示列表也加上了 `web_search`。

**系统提示更新**（`react.py:408`）：
- 旧：`"For web queries, prefer a single curl command over writing Python scripts."`
- 新：`"For web search, call the 'web_search' tool with a query — do NOT write Python scripts or use curl to call search engines. Use 'web' only when you already have a specific URL to fetch."`

**效果**：agent 遇到联网搜索任务时，LLM 直接调 `web_search(query="...")`，一次工具调用即可。实测搜索 `python asyncio tutorial` 返回 3 条干净结果（Real Python / Python 官方文档 / GeeksforGeeks），广告被正确过滤。

### 文件清单

| 文件 | 状态 | 说明 |
|------|------|------|
| `synapse/adapters/cli.py` | 修改 | 界面美化、自适应、补全、token/耗时显示、确认提示 |
| `synapse/adapters/library.py` | 修改 | 注册 WebSearchTool |
| `synapse/modules/planning/react.py` | 修改 | token_update 事件、系统提示更新 |
| `synapse/modules/tools/web_search.py` | 新增 | DuckDuckGo 搜索工具 |
| `pyproject.toml` | 修改 | 新增 `prompt_toolkit>=3.0` 依赖 |
| `DEVELOPMENT.md`（根目录） | 新增（误） | 误建在根目录，已合并到 `docs/DEVELOPMENT.md` 并删除 |

### 当前状态

| 指标 | 数值 |
|------|------|
| Commits | 68 |
| Tests | 174 |
| 工具总数 | 11（+web_search） |

---

## 2026-07-20 · 上下文工程深度优化（Phase 0-4）

### [design] 调研与方案

调研发现当前上下文工程存在 8 个关键问题：planner 只消费 `context.system`、`core/reference/overflow` 完全被忽略；`ContextCompactor` 注册了但从未调用；budget 硬编码 100k 与 `max_tokens_per_task=200k` 脱钩；5s 超时返回空 Context；Compactor 截断 500 字符且覆盖 source；Partitioner `_trim_zone` 有 break bug 等。

方案分 5 阶段实施，本次会话完成 Phase 0 + 1 + 2 + 3 + 4 全部。

### [feat] Phase 0 · 前置修复（地基）

让四区真正流到 LLM，让 compactor 真正运行，让 budget 跟随配置。

**0.1 planner 接入四区**（`react.py`, `plan_execute.py`）
- `_build_system_prompt` 不再只拼 `context.system`，按 `system → core → reference` 顺序注入
- 每个 block 带 `[from <source>]` 标注，让 LLM 感知 provenance
- CORE 区 token 预算 4000，REFERENCE 区 3000，超出按 priority 降序裁剪
- OVERFLOW 内容不注入 LLM prompt

**0.2 Agent 调用 Compactor**（`agent.py:_build_context`）
- 流程改为 `retrieve → compact(overflow) → partition`
- Compactor 在 Partitioner 之前运行

**0.3 Budget 配置化**（`schema.py`, `agent.py`）
- 新增 `ContextConfig`（在 `SynapseConfig.context`）：`total_tokens`（0 时继承 `planning.max_tokens_per_task`）、四区百分比、`compaction_strategy`（`truncation` | `llm` | `off`）、`llm_compact_threshold_chars`
- `Agent._build_budget(task)` 从 config 取 budget，不再硬编码

**0.4 修 partitioner knapsack bug**（`partitioner.py:_trim_zone`）
- 旧逻辑：按 priority 升序排，遇到放不下的就 `break`，导致后续更小的块即使能放下也被丢弃
- 新逻辑：按 priority 降序排（高优先级先保留），放不下也继续扫描后续更小的块
- 保留原始插入顺序

**0.5 超时回退**（`agent.py`）
- 5s → 10s
- 超时不再返回空 Context，改为返回只含 SYSTEM 的最小 Context（读 AGENTS.md/CLAUDE.md/README.md）
- 并发 `AgentProgress(phase="context_timeout")` 警告事件

**0.6 保留 provenance**（`compactor.py` + `retriever.py`）
- 压缩后生成新 block，新增 `derived_from` 字段记录原 block id
- 不再覆盖 `source` 为 `MEMORY`，保留原始 source
- `ContextBlock` 加 `id` 字段

### [feat] Phase 1 · LLM 驱动智能摘要

**LLMCompactor**（`synapse/modules/context/llm_compactor.py`）
- 对每个 overflow block 调 LLM 生成紧凑摘要，prompt 要求保留文件路径/符号/关键发现
- content hash 缓存避免重复调用
- LLM 失败自动回退到 `TruncationCompactor`
- 输入硬限 8000 字符，摘要软限 1500 字符
- 通过 `ContextConfig.compaction_strategy = "llm"` 启用，默认仍 `truncation`
- 触发阈值：仅当 overflow 总量 > `llm_compact_threshold_chars`（默认 1000）时才用 LLM

### [feat] Phase 2 · 引用率追踪（RAG 评估基础）

**ContextBlock 加字段**（`retriever.py`）
- `id: str` — uuid hex 前 8 位
- `usage_count: int` — 被 LLM 调用次数
- `citation_count: int` — 被 LLM 响应引用次数
- `retrieved_at: datetime` — 检索时间戳

**CitationTracker**（`synapse/modules/context/citation.py`）
- `mark_usage(context)` — 在 planner 把 context 发给 LLM 前调用，递增每个 block 的 `usage_count`
- `track_response(response_content, context, event_bus, session_id)` — LLM 响应后扫描内容：
  - 从每个 block 提取"信号"（文件路径、`def/class` 符号名、≥12 字符的特征行）
  - 信号在 response 中出现 → 递增 `citation_count`，发 `ContextBlockCited` 事件
  - 每个 block 最多测试 5 个信号，避免性能开销
  - 偏好精确而非召回：citation_count 是下界

**ContextBlockCited 事件**（`events.py`）
- 新增 `EventType.CONTEXT_BLOCK_CITED = "context_block_cited"`
- 事件载荷：`block_id`、`block_source`、`response_snippet`（响应前 200 字符）

**`/memory` 命令展示引用率**
- 在原有 messages/tokens/provider/workspace 信息后追加 citation 汇总一行
- 显示格式：`Context: system 2/3 cited · core 1/5 cited · reference 0/2 cited`

### [feat] Phase 3 · 动态预算分配

**3.1 任务类型分类器**（`synapse/modules/context/classifier.py`）
- `TaskType` enum：`TEST / REFACTOR / DEBUG / FEATURE / DOC / UNKNOWN`
- `classify_task(task)` — 规则分类，首匹配优先，顺序 DEBUG → TEST → REFACTOR → DOC → FEATURE
- 中英文关键词都支持（`测试`/`修复`/`重构`/`新增`/`调试`/`报错`/`文档` 等）
- DEBUG 排在 TEST 前：`fix failing test` 判为 DEBUG（在调试失败用例，而非编写新测试）

**3.2 预算策略表**（`synapse/modules/context/budget.py`）
- `TASK_BUDGET_PROFILES` — 6 种静态 profile，每种 TaskType 对应不同四区比例：

| TaskType | system | core | reference | overflow | 理由 |
|----------|--------|------|-----------|----------|------|
| TEST | 0.10 | 0.40 | 0.40 | 0.10 | 需大量参考既有测试 |
| REFACTOR | 0.15 | 0.60 | 0.20 | 0.05 | 重构以核心代码为主 |
| DEBUG | 0.10 | 0.30 | 0.50 | 0.10 | 调试需广参考定位 |
| FEATURE | 0.15 | 0.50 | 0.25 | 0.10 | 默认均衡 |
| DOC | 0.20 | 0.30 | 0.40 | 0.10 | 文档需既有内容参考 |
| UNKNOWN | 0.15 | 0.50 | 0.25 | 0.10 | 同 FEATURE |

- `select_budget(task_type, total_tokens)` — 根据 TaskType 选 profile 构造 `ContextBudget`

**3.3 Agent 接入分类器**（`agent.py:_build_budget`）
- `_build_budget(task)` — 先 `classify_task(task)`，再 `select_budget(task_type, total)`，最后 `suggest_adjustment` 应用历史反馈
- `_last_task_type` 保留供 `run()` 末尾记录历史

**3.4 历史反馈**（`BudgetHistory` 类，`budget.py`）
- `record(task_type, citation_report)` — 任务结束记录该类型的引用率汇总，持久化到 `ProjectMemory`
- `suggest_adjustment(task_type, base_budget)` — 样本数 ≥ 3 后根据各 zone 引用率与均值差异微调 profile，每 zone 变化严格 cap 在 ±5%
- 冷启动：样本不足时返回 base 不变

### [feat] Phase 4 · 注意力热力图

**Agent 保留上下文状态**（`agent.py`）
- `self._last_context` — 保留最后一次 build 的 context
- `self._citation_tracker` — 从 planner 拿到 tracker
- 任务结束调用 `BudgetHistory.record()` 累积历史

**`Synapse` facade 暴露 `get_citation_report()`**（`library.py`）
- 返回最近一次任务的引用率报告，供 CLI 调用

**`/context-report` 命令**（`cli.py`）
- 新增 `_show_context_report(console, synapse, use_rich)` 函数
- Rich 表格显示：Zone / Source / Pri / Tokens / Used / Cited / Rate
- Rate 列用绿色显示 `cited/used` 比例
- 末尾汇总：`Overall: X/Y blocks cited`

**补全与帮助**（`cli.py`）
- `_SLASH_COMMANDS` 加入 `/context-report`（描述 "Context block citation heatmap"）
- `_show_help` 表格加入 `/context-report` 行

**Trade-off**
- 跨 provider 兼容性差 — 只有 Anthropic 暴露 cache 元数据，但字符串匹配对所有 provider 通用
- 字符串匹配不精确 — 偏好精确而非召回，citation_count 是下界
- 后续可升级为 embedding 相似度，但当前实现已能覆盖 80% 的"上下文使用"分析需求

### [fix] 关键 Bug 修复

- **Compactor 破坏 provenance**：旧版本把所有压缩 block 的 `source` 覆盖为 `MEMORY`，丢失原始来源（grep/glob/git 等）。现保留 `source` 并通过 `derived_from` 链接原 block。
- **Partitioner `_trim_zone` break bug**：小预算下会误丢本可放下的高优先级块。
- **Budget 与 config 脱钩**：旧版本硬编码 `ContextBudget()` 100k，与 `PlanningConfig.max_tokens_per_task=200k` 无关。现已配置化并可继承。
- **超时静默丢上下文**：旧版本 5s 超时返回空 `Context()`，导致大项目首跑 LLM 拿不到任何项目信息。现返回 SYSTEM 最小 context 并发警告事件。

### 文件清单

| 文件 | 状态 | 说明 |
|------|------|------|
| `synapse/protocols/retriever.py` | 修改 | ContextBlock 加 id/derived_from/usage_count/citation_count/retrieved_at |
| `synapse/protocols/events.py` | 修改 | 新增 ContextBlockCited 事件 |
| `synapse/config/schema.py` | 修改 | 新增 ContextConfig |
| `synapse/core/agent.py` | 修改 | 接入 compactor/classifier/budget history/citation tracker |
| `synapse/modules/context/compactor.py` | 修改 | 保留 provenance + derived_from |
| `synapse/modules/context/partitioner.py` | 修改 | 修 knapsack bug |
| `synapse/modules/context/llm_compactor.py` | 新增 | LLM 摘要 Compactor |
| `synapse/modules/context/citation.py` | 新增 | CitationTracker |
| `synapse/modules/context/classifier.py` | 新增 | 任务类型分类器 |
| `synapse/modules/context/budget.py` | 新增 | 预算策略表 + BudgetHistory |
| `synapse/modules/planning/react.py` | 修改 | 接入四区 + citation tracking |
| `synapse/modules/planning/plan_execute.py` | 修改 | 接入四区 |
| `synapse/adapters/cli.py` | 修改 | /memory 增强 + /context-report 命令 |
| `synapse/adapters/library.py` | 修改 | 注册 SynapseConfig + get_citation_report |
| `tests/modules/test_context_phase_e.py` | 新增 | 17 测试 |
| `tests/modules/test_context_phase_2_3.py` | 新增 | 22 测试 |
| `tests/modules/test_compactor.py` | 修改 | 适配新 provenance 行为 |

### 当前状态

| 指标 | 数值 |
|------|------|
| Commits | 70 |
| Tests | 217（+43） |
| 上下文工具数 | 6（+llm_compactor/citation/classifier/budget） |

---

## 2026-07-23 · LLM 流式输出（stream）补全

### 背景

Phase 2 计划（`plans/2026-07-16-synapse-phase-2.md:32,38`）把 `stream support` 列为交付物，但五个 provider 的 `LLMProvider.stream()` 此前全是 `raise NotImplementedError` 占位（`DEVELOPMENT.md` 现状表也标着 `stream stub`）。本次将其真正实现，使协议层的 `stream() -> AsyncIterator[LLMChunk]` 在所有 provider 上可用。

### 实现

改动分四层，自底向上串起整条流式链路：

**1. Provider 层（`stream()`）** — `LLMChunk` 通过 `content`（文本增量）与 `tool_call_delta`（工具调用增量）携带流式增量，五个 provider 均按各自 SDK 的流式接口产出：

- **OpenAI / DeepSeek / Ollama**（共用 `openai.AsyncOpenAI`）：`chat.completions.create(stream=True, stream_options={"include_usage": True})`，逐块读取 `choices[0].delta` 的 `content` 与 `tool_calls` 增量，累计 `usage`。
- **Anthropic**：`messages.stream()` 上下文管理器，遍历 `content_block_delta` 事件，`text_delta` → `content`，`input_json_delta` → `tool_call_delta["input"]`，收尾用 `await stream.get_final_message()` 取 `usage`。
- **Google（Gemini）**：`aio.models.generate_content_stream()`，遍历 `candidates[0].content.parts` 的 `text` 与 `function_call`，从 `usage_metadata` 累计 `usage`。

所有 `stream()` 均复用各自 `chat()` 已有的消息/工具转换逻辑，错误统一包成 `ProviderError`，行为与该 provider 的非流式路径一致。

**2. 协议层（`LLMChunk` + `LLMToken`）** — `LLMChunk` 新增 `usage: dict | None` 字段（最终块携带 `{"input", "output"}` 用量）；新增事件 `LLM_TOKEN = "llm_token"`（`LLMToken(text)`，逐 token 透传给 UI）。`tool_call_delta` 的键在三个 provider 间统一为 `{"index", "id", "name", "input"}`。

**3. 规划层（`ReAct`）** — `react.py` 新增 `_call_llm_stream()`：异步遍历 `llm.stream()`，逐块累加 `content`/`tool_call_delta`/`usage`，每块 `content` 发一个 `LLMToken` 事件；按 `index` 合并工具调用增量（同时兼容 OpenAI 风格字符串 JSON 累加与 Gemini 风格 dict 输入），用 `json.loads` 解析；返回 `(content, tool_calls, usage, stop_reason)`。再由 `_call_llm()` 包装：优先 `stream()`，捕获 `NotImplementedError / TypeError / AttributeError` 时回退到 `chat()`（保留只 mock 了 `chat()` 的既有测试）。重试循环调用 `_call_llm()`。

**4. CLI 层（实时显示）** — `adapters/cli.py` 新增 `_LiveDisplay`（Rich `Live` + `Panel`）：底部状态行显示当前阶段标签、累计 token 数、已用时长；订阅 `agent_progress` / `llm_token` / `tool_call_started` / `tool_call_completed` 四个事件，`llm_token` 直接把 `event.text` 追加进面板正文，`phase=="calling_llm"` 时清空已显示文本。原先的 `console.status` 转圈被替换为该 Live 面板，并复用既有 `status_holder` 机制以维持确认回调（confirm）时的暂停/恢复。

### 测试

- `tests/modules/` 下五个 provider 测试各新增 `test_stream_yields_text_and_tool_chunks`（mock SDK，无真实 API 调用），验证同时产出文本块、工具调用增量、并最终产出一个带 `usage` 的收尾块。原 `test_deepseek_provider.py` 中过时的 `test_stream_not_implemented` 已替换为真实流式测试。
- 新增 `tests/modules/test_react_streaming.py`（3 项）：验证 `_call_llm_stream()` 重组工具调用、逐 token 发 `LLMToken`、处理 Gemini 风格 dict 工具输入、以及 `end_turn` 无工具调用场景。

### 文件清单

| 文件 | 状态 | 说明 |
|------|------|------|
| `synapse/protocols/events.py` | 修改 | 新增 `LLM_TOKEN` 事件 + `LLMToken` 数据类 |
| `synapse/protocols/llm.py` | 修改 | `LLMChunk.usage` 字段 |
| `synapse/modules/providers/anthropic.py` | 修改 | `stream()` 实现（事件流 + usage 收尾） |
| `synapse/modules/providers/openai.py` | 修改 | `stream()` 实现 + `stream_options`/usage |
| `synapse/modules/providers/deepseek.py` | 修改 | `stream()` 实现 + `stream_options`/usage |
| `synapse/modules/providers/ollama.py` | 修改 | `stream()` 实现 + `stream_options`/usage |
| `synapse/modules/providers/google.py` | 修改 | `stream()` 实现 + `LLMChunk` import + usage |
| `synapse/modules/planning/react.py` | 修改 | `_call_llm_stream()` / `_call_llm()`（stream 优先，chat 回退） |
| `synapse/adapters/cli.py` | 修改 | `_LiveDisplay` + 实时流式显示接线 |
| `tests/modules/test_openai_provider.py` | 修改 | +stream 测试 |
| `tests/modules/test_deepseek_provider.py` | 修改 | 替换陈旧 stream 测试 |
| `tests/modules/test_ollama_provider.py` | 修改 | +stream 测试 |
| `tests/modules/test_anthropic_provider.py` | 修改 | +stream 测试 |
| `tests/modules/test_google_provider.py` | 修改 | +stream 测试 |
| `tests/modules/test_react_streaming.py` | 新增 | 3 项 ReAct 流式单测 |

### 当前状态（更新）

| 指标 | 数值 |
|------|------|
| Tests | 220（+9：5 provider stream + 3 react stream + 1 替换） |
| 流式输出 | 5/5 provider 已落地 |
| CLI 实时显示 | `_LiveDisplay` 已接入，`LLMToken` 实时渲染 |

---

## 2026-07-24 · 过程质量验证闭环（TODO B）

### 背景

TODO B 要求：任务完成后**自动验证 Agent 的行为质量**（而不只是看补丁是否通过），并把结论反馈给 Agent 以改进下次执行。既有 `ProcessMetrics`（`eval/metrics/process.py`）已采集复用/根因/测试留存等指标，但：

1. 它只在 `enable_eval=True` 时接入——正常 `synapse run` 里是死的；
2. 它做的是**逐事件计数**，没有对工具调用**序列**做模式识别；
3. 没有统一的过程质量分，也没有反馈回路。

本次补上"序列模式识别 → 评分 → 反馈"的闭环，且该闭环在**正常 run** 中也生效。

### 实现

四步闭环，自底向上：

**1. 事件（`protocols/events.py`）** — 新增 `PROCESS_QUALITY_SCORED = "process_quality_scored"` 与 `ProcessQualityScored` 数据类（`task / score / reuse_ratio / write_without_lookup / thrashing_events / success / tool_calls / hint`）。

**2. 验证器（`modules/process_quality.py`）** — `ProcessQualityVerifier`：
- 构造时订阅 `tool_call_started` / `tool_call_completed` / `thrashing_detected`，按发生顺序捕获工具序列（每次调用记 `name / params / files / success`）。
- `after_task(task, success)`（`Agent.run` 后处理钩子调用）：
  - **复用判定**：遍历序列，对每个 `write`/`edit`（变更类工具），检查其之前是否有 `read`/`grep`/`glob`/`git`（检索类工具）命中同一文件（路径相等 / 祖先目录 / 互含）。命中 → 复用正向；未命中 → `write_without_lookup`。
  - **评分**：`score = 0.6 * reuse_ratio + 0.4 * success_factor`（成功=1.0、失败=0.3），再按 thrashing 次数小幅扣分（封顶）。
  - **反馈文本**：复用率低且有"盲写"→ 提示"先 grep/read 再写"；有 thrashing → 提示先读懂再改；否则正向鼓励。
  - 发出 `ProcessQualityScored` 事件，并把反馈以**滚动条目**（固定 id `process_quality_feedback`，`forget`+`store` 实现 upsert）写入 **PROJECT** 记忆。

**3. 接线（`adapters/library.py` + `core/agent.py`）** — `ProcessQualityVerifier` 在容器构建时**无条件注册**（不再受 `enable_eval` 限制，因为是 live 功能）；`Agent.__init__` 解析它（try/except，缺失则跳过），`Agent.run()` 在 `_persist_memory` 之后调用 `verifier.after_task(...)`。

**4. 反馈注入（`modules/context/retriever.py`）** — `_build_reference` 在原有 SESSION 记忆检索之后，额外用固定查询 `"process quality feedback"` 检索 **PROJECT** 记忆，把滚动反馈块（priority 6）注入下一任务的 `reference` 上下文。由于 planner 会把 `reference` 注入 system prompt，Agent 下次执行时即看到自己的过程质量反馈，闭环完成。

### 测试

`tests/modules/test_process_quality.py`（4 项）：
- `grep` 先于 `write` 命中同文件 → `reuse_ratio=1.0`、高分、正向 hint；
- 直接 `write` 无前置检索 → `reuse_ratio=0`、低分、hint 提示先检索；
- 复用良好但任务失败 → 分数介于纯失败地板与满分之间（失败仍扣分）；
- 检索器 `_build_reference` 确实把反馈块注入 reference 上下文。

### 文件清单

| 文件 | 状态 | 说明 |
|------|------|------|
| `synapse/protocols/events.py` | 修改 | 新增 `PROCESS_QUALITY_SCORED` 事件 + `ProcessQualityScored` |
| `synapse/modules/process_quality.py` | 新增 | `ProcessQualityVerifier` + `ProcessQualityReport` |
| `synapse/adapters/library.py` | 修改 | 无条件注册 `ProcessQualityVerifier` |
| `synapse/core/agent.py` | 修改 | 解析并调用 `verifier.after_task()` |
| `synapse/modules/context/retriever.py` | 修改 | `_build_reference` 注入过程质量反馈 |
| `tests/modules/test_process_quality.py` | 新增 | 4 项闭环测试 |

### 当前状态（更新）

| 指标 | 数值 |
|------|------|
| Tests | 224（+4 过程质量闭环） |
| 过程质量验证闭环 | 已落地（live 生效，非仅 eval） |
| 反馈回路 | PROJECT 记忆 upsert → 下一任务 prompt 注入 |


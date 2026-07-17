# Synapse 开发日志

> 从需求到交付的完整过程。每个节点记录所有方案选项、trade-off 分析和最终决策。后续每次会话追加，内容过多时压缩精简。

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

*下一阶段：Phase 2 — PlanExecutePlanner、HierarchicalPlanner、OpenAI/Google/DeepSeek/Ollama Provider、项目/用户记忆、完整四区上下文治理、审计日志*

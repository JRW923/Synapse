# Synapse 开发日志

> 记录从需求到交付的完整过程。每次会话后追加，单文件维护，过长时自动压缩。

---

## 2026-07-16 · 项目启动 & Phase 1 完成

### 需求阶段

用户提出构建简化版 Code Agent（参考 Claude Code / Codex），核心要求：

- **模块解耦**：参考 [pico v3](https://gitee.com/htxoffical/pico/tree/v3/) 结构但不照搬，每模块可独立扩展
- **设计可解释**：每处设计讲清楚 trade-off
- **实验驱动**：通过 benchmark 证明价值，某些方面可不如主流但必须有存在意义

经调研当前 Code Agent 的 9 大类痛点（上下文治理危机、行为退化、评测失效、token 浪费、架构耦合、安全隐患等），用户选定 **"过程质量"** 为核心差异化——关注任务完成**过程**的行为质量（是否复用、是否定位根因、是否持久化测试、是否遵循指令），而非仅看 pass/fail。

### 设计阶段（10 轮澄清）

| # | 决策点 | 选择 |
|---|--------|------|
| 1 | MVP 范围 | 完整 Harness（含评测/审计） |
| 2 | 核心差异 | 过程质量优先 + 其他维度兼顾 |
| 3 | 过程质量维度 | 全部（复用、根因、测试持久化、指令遵循、代码不退化），分阶段 |
| 4 | LLM 接入 | 多 Provider 统一接口（Anthropic/OpenAI/Google/DeepSeek/Ollama） |
| 5 | 工具范围 | 全部（文件→代码理解→执行→外部集成→评测） |
| 6 | 评测策略 | 业界 benchmark + 自建过程质量 benchmark |
| 7 | 交互模型 | Python Library + CLI + HTTP API |
| 8 | 记忆系统 | 全部四层（Session/Project/User/Semantic） |
| 9 | 规划模式 | 三种可配置（ReAct/PlanExecute/Hierarchical） |
| 10 | 安全策略 | 全部（Sandbox + Auth + Audit + Injection Defense），分阶段 |

### 架构设计

选定 **分层 Protocol + EventBus 横切** 模式：

```
adapters/  →  CLI / Library / HTTP（薄适配层）
core/      →  Container(IoC) + Agent + EventBus + Session
protocols/ →  纯接口定义（LLMProvider, Tool, Planner, Memory, Retriever, Sandbox）
modules/   →  各模块默认实现
eval/      →  评测框架（EventBus 消费者，零侵入）
```

边界规则：`protocols/` 不依赖任何模块 → `core/` 只依赖 `protocols/` → `modules/` 实现 `protocols/`。

### 实施阶段（Subagent-Driven Development）

**Phase 1 范围**：IoC Container、Agent 主循环、ReActPlanner、Anthropic Provider、7 个基础工具、SessionMemory、ContextRetriever、ProcessSandbox、ActionAuthorizer、CLI 入口。

**20 个任务，22 次提交**，每个任务走 TDD（Red → Green → Commit → Review）：

| # | 任务 | 产出 |
|---|------|------|
| 1 | 项目配置 | pyproject.toml, 依赖安装 |
| 2 | 配置系统 | Pydantic schema + YAML/env loader |
| 3 | 事件类型 | EventType enum + 7 个 event dataclass |
| 4 | 核心协议 | 6 个 Protocol（LLM/Tool/Planner/Memory/Retriever/Sandbox） |
| 5 | 异常体系 | 6 类异常（SynapseError + 5 子类） |
| 6 | EventBus | subscribe/unsubscribe/emit，handler 异常隔离 |
| 7 | IoC Container | 单例 + 工厂注册，泛型别名支持 |
| 8 | Session | 消息历史、fork 子会话、token 估算 |
| 9 | Anthropic Provider | chat/stream，system 提取，tool call 解析 |
| 10 | 基础工具(5) | Read/Write/Edit/Glob/Grep + 防污染修复 |
| 11 | 执行工具(3) | Shell(超时+沙箱)、Git(只读白名单)、ToolRegistry |
| 12 | SessionMemory | 内存 dict，子串匹配检索 |
| 13 | ContextRetriever | grep/glob 构建 CORE 区，memory 构建 REFERENCE 区 |
| 14 | ProcessSandbox | 跨平台检测，subprocess + 超时 |
| 15 | ActionAuthorizer | 五级风险决策矩阵 + 危险模式检测 |
| 16 | Agent 核心 | 依赖装配 → Planner 委托 → 记忆持久化 |
| 17 | ReActPlanner | Think→Act→Observe 循环 + thrashing 检测 |
| 18 | CLI 入口 | `synapse run` / `synapse version` |
| 19 | 集成测试 | 全管线 mock LLM 测试 |
| 20 | 最终验证 | 58 测试全通过，架构边界验证 |

### 最终审查 & 修复

审查发现 1 个关键 + 4 个重要问题，全部修复：

- **C1**：ActionAuthorizer 已实现但未接入执行路径 → 接入 ReActPlanner
- **I1**：Anthropic tool_result 格式不对 → 改为 content block 格式
- **I2**：ReAct 循环无 LLM 重试 → 指数退避（1s/2s/4s，max 3 次）
- **I3**：GrepTool 同步阻塞 → 改为 async subprocess
- **I4**：max_tokens 硬编码 → 加入 ProviderConfig

### 交付物

```
Synapse/
├── synapse/           # 核心代码（31 文件）
│   ├── protocols/     # 7 Protocol + 事件类型
│   ├── core/          # Container, Agent, EventBus, Session
│   ├── modules/       # Provider, 7 Tools, Planner, Memory, Context, Security
│   ├── adapters/      # CLI 入口
│   └── config/        # 配置系统
├── tests/             # 58 测试（全部通过）
├── docs/superpowers/  # 设计 Spec + 实施 Plan
└── pyproject.toml     # 项目配置
```

**量化**：22 commits, 56 files, ~2,900 lines, 58 tests passing, 3 层架构隔离验证通过。

---

*下一阶段：Phase 2 — PlanExecutePlanner、HierarchicalPlanner、其他 Provider、项目/用户记忆、完整四区上下文治理、审计日志*

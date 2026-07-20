# Synapse 待办与拓展规划

> 后续可通过读取本文件讨论和推进任意功能点。暂时不做的需求也记录在此，避免遗忘。
> **规则**：完成后在标题旁标注 `✅ 已完成 (YYYY-MM-DD)`，保留原文不删除。

---

## 拓展方向

### A · MCP 协议支持

**状态**：✅ 已完成 (2026-07-16)

当前工具是硬编码的 10 个。MCP（Model Context Protocol）是 Anthropic 推出的标准化工具协议，支持动态发现和调用任意 MCP Server 提供的工具。

**要点**：
- 实现 MCP Client 协议（stdio + SSE transport）
- 动态 ToolSchema 生成与注册
- 工具生态从"内置"变为"无限可扩展"

**难度**：中等

---

### B · 过程质量验证闭环

**状态**：待实现

当前 ProcessMetrics 采集了指标但缺少自动验证——即在任务完成后自动检查 Agent 行为质量，并反馈给 Agent 以改进下次执行。

**要点**：
- 对工具调用序列做模式识别（"先 grep 再 write"=复用，"直接 write"=未复用）
- 任务完成后生成过程质量评分
- 设计反馈机制（prompt 中注入质量提示或记忆系统记录）

**难度**：较高

---

### C · 多 Agent 协作（Swarm/Team）

**状态**：待实现

当前只有单 Agent + HierarchicalPlanner 的树形分解。真正的多 Agent 协作是多个对等 Agent 同时工作、互相 review、投票决策。

**要点**：
- Agent 间通信协议
- 冲突解决与结果合并
- 专用 Agent（Code Reviewer、Test Writer、Security Auditor）
- 并行 Agent 的错误放大问题需要专门的验证 Agent 来抵消

**难度**：高

---

### D · IDE 插件 / LSP 集成

**状态**：待实现

将 Synapse 从终端工具变为 IDE 中的 AI 搭档。通过 LSP 获取精确的符号信息和诊断信息。

**要点**：
- VS Code / JetBrains 插件
- Language Server Protocol 集成（符号索引、诊断、引用跳转）
- 选中代码直接交互（解释、修复、重构、生成测试）

**难度**：中等（VS Code 插件）/ 低（LSP 集成）

---

### E · 上下文工程深度优化

**状态**：开发中（Phase 0 + Phase 1 + Phase 2 + Phase 3 + Phase 4 全部实施）

当前 Partitioner + Compactor 是 Phase 1 级别的简单实现（截断）。调研显示 token 浪费率极高（154:1 输入输出比）。

**调研发现的关键问题**：
1. planner 只消费 `context.system`，`core/reference/overflow` 完全被忽略 — 所有优化对 LLM 不可见
2. `ContextCompactor` 注册了但从未调用 — OVERFLOW 永不压缩
3. budget 硬编码 `ContextBudget()` 100k，与 `PlanningConfig.max_tokens_per_task=200k` 脱钩
4. 5s 超时返回空 Context，无回退 — 大项目首跑直接丢上下文
5. Compactor 截断 500 字符 + 覆盖 source 为 MEMORY — 丢失 provenance
6. Retriever 只 grep+glob + SESSION memory — 没用 AST/git/PROJECT/USER 记忆
7. `ContextBlock` 缺 id/usage/citation/relevance 字段 — 无法做引用率统计
8. Partitioner `_trim_zone` 有 break bug — 小预算下误丢本可放下的块

---

#### Phase 0 · 前置修复（地基，必做）

**目标**：让四区真正流到 LLM，让 compactor 真正运行，让 budget 跟随配置。

**0.1 planner 接入四区**（`react.py`, `plan_execute.py`）
- `_build_system_prompt` 不只拼 `context.system`，按 `system → core → reference` 顺序注入
- 每个 block 带 source 标注（如 `[from grep]`/`[from memory]`），overflow 不注入
- token 预算检查：注入前预估总 token，超出则在 reference 区按 priority 降序裁剪

**0.2 Agent 调用 Compactor**（`agent.py:_build_context`）
- 流程改为：`retrieve → compact(overflow) → partition → inject`
- 调用点放在 partitioner 前，让 compactor 先压缩 overflow，partitioner 再做四区裁剪

**0.3 Budget 配置化**（`schema.py`, `agent.py`）
- 新增 `ContextConfig`（在 `SynapseConfig.context`）：`total_tokens`（默认从 `planning.max_tokens_per_task` 取）、`system_pct`/`core_pct`/`reference_pct`/`overflow_pct`、`compaction_strategy`（`truncation` | `llm` | `off`，默认 `truncation`）
- `Agent._build_context` 从 config 取 budget，不再硬编码

**0.4 修 partitioner break bug**（`partitioner.py`）
- `_trim_zone` 改为 knapsack 式保留：按 priority 降序排，能放就放，放不下也尝试后续更小的块

**0.5 超时回退**（`agent.py`）
- 5s 超时改为：返回只含 SYSTEM 的最小 Context（读 README/AGENTS.md），打 `AgentProgress` 警告事件，不静默丢

**0.6 保留 provenance**（`compactor.py` + `retriever.py`）
- 压缩后生成新 block，新增 `derived_from` 字段记录原 block id，不覆盖 source
- `ContextBlock` 加 `id` 和 `derived_from` 字段

---

#### Phase 1 · LLM 驱动智能摘要

**目标**：Compactor 用 LLM 摘要替代截断。

**1.1 新增 `LLMCompactor`**（`synapse/modules/context/llm_compactor.py`）
- 实现 `ContextCompactor` 协议
- 对每个 overflow block 调用 LLM，prompt："Summarize the following for a coding task, preserve file paths/symbols/key findings: {content}"
- 缓存：相同 content hash 不重复调用
- 失败回退：LLM 调用失败 → 降级到 `TruncationCompactor`

**1.2 配置开关**
- `ContextConfig.compaction_strategy = "llm"` 时启用，默认仍 `truncation`
- 触发阈值：仅当 overflow 总量 > N 字符时才触发 LLM 摘要，否则截断即可

**Trade-off**：摘要质量 vs token 成本 — LLM 摘要每次任务多花 500-2k token，但能让 overflow 真正有用。

---

#### Phase 2 · 引用率追踪（RAG 评估基础）

**目标**：标注每个 ContextBlock 的实际引用率，为 Phase 3 动态预算提供数据。

**2.1 ContextBlock 加字段**（`retriever.py`）
- `id: str` — uuid hex 前 8 位，唯一标识
- `usage_count: int` — 该 block 被发送给 LLM 的次数（每次 LLM 调用 +1）
- `citation_count: int` — LLM 输出中明确引用该 block 内容的次数
- `retrieved_at: datetime` — 该 block 被检索的时间戳

**2.2 CitationTracker**（`synapse/modules/context/citation.py`）
- `mark_usage(context)` — 在 planner 把 context 发给 LLM 前调用，递增每个 block 的 `usage_count`
- `track_response(response_content, context, event_bus, session_id)` — LLM 响应后扫描内容：
  - 从每个 block 提取"信号"（文件路径、`def/class` 符号名、≥12 字符的特征行）
  - 信号在 response 中出现 → 递增 `citation_count`，发 `ContextBlockCited` 事件
  - 每个 block 最多测试 5 个信号，避免性能开销
  - 偏好精确而非召回：只计强信号，citation_count 是下界

**2.3 ContextBlockCited 事件**（`events.py`）
- 新增 `EventType.CONTEXT_BLOCK_CITED = "context_block_cited"`
- 事件载荷：`block_id`、`block_source`、`response_snippet`（响应前 200 字符）

**2.4 `/memory` 命令展示引用率**（`cli.py`）
- 在原有 messages/tokens/provider/workspace 信息后，追加每个 zone 的 citation 汇总
- 显示格式：`Context: system 2/3 cited · core 1/5 cited · reference 0/2 cited`

**Trade-off**：字符串匹配不精确 — 但比完全不追踪强 100 倍。后续可升级为 embedding 相似度。

---

#### Phase 3 · 动态预算分配

**目标**：根据任务类型自动调整四区比例，让上下文分配匹配任务需求。

**3.1 任务类型分类器**（`synapse/modules/context/classifier.py`）
- 输出 `TaskType` enum：`TEST / REFACTOR / DEBUG / FEATURE / DOC / UNKNOWN`
- 规则分类（先于 LLM，避免 token 成本），首匹配优先，顺序 DEBUG → TEST → REFACTOR → DOC → FEATURE：
  - 包含 "debug"/"fix"/"bug"/"error"/"修复"/"调试" → DEBUG
  - 包含 "test"/"测试"/"spec"/"pytest" → TEST
  - 包含 "refactor"/"重构"/"rename" → REFACTOR
  - 包含 "doc"/"readme"/"文档" → DOC
  - 包含 "add"/"implement"/"新增"/"实现" → FEATURE
  - 其余 → UNKNOWN（用默认 profile）
- DEBUG 排在 TEST 前：`fix failing test` 应判为 DEBUG（在调试失败用例，而非编写新测试）

**3.2 预算策略表**（`synapse/modules/context/budget.py`）
- `TASK_BUDGET_PROFILES: dict[TaskType, ContextBudget]`
  - TEST:     system=0.10, core=0.40, reference=0.40, overflow=0.10（测试需要大量参考）
  - REFACTOR: system=0.15, core=0.60, reference=0.20, overflow=0.05（重构以核心代码为主）
  - DEBUG:    system=0.10, core=0.30, reference=0.50, overflow=0.10（调试需大量参考定位）
  - FEATURE:  system=0.15, core=0.50, reference=0.25, overflow=0.10（默认）
  - DOC:      system=0.20, core=0.30, reference=0.40, overflow=0.10（文档需参考既有内容）
  - UNKNOWN:  同 FEATURE
- `select_budget(task_type, base_cfg)` — 根据任务类型选 profile，`total_tokens` 始终从 `ContextConfig.total_tokens` 或 `planning.max_tokens_per_task` 继承

**3.3 Agent 接入分类器**（`agent.py:_build_budget`）
- `_build_budget(task: str)` — 先分类任务类型，再选 profile
- 保留 `ContextConfig` 的百分比作为 UNKNOWN 的回退

**3.4 历史反馈**（`synapse/modules/context/budget.py` + `ProjectMemory`）
- `BudgetHistory` 类：
  - `record(task_type, citation_report)` — 任务结束后记录该类型的引用率汇总
  - `load(task_type)` — 加载该类型的历史引用率
  - `suggest_adjustment(task_type, base_profile)` — 根据历史引用率微调 profile
    - 如：TEST 类型历史显示 reference 引用率 80%、core 引用率 10% → 建议 reference +5%、core -5%
  - 持久化到 `ProjectMemory`（key: `budget_history_{task_type}`），跨会话保留
- 冷启动：前 N 次（默认 3）任务用默认 profile，积累数据后启用自适应

**Trade-off**：
- 规则分类粗糙 — 但 LLM 分类又多花 token。先用规则，验证有效再考虑 LLM 分类
- 历史反馈有冷启动问题 — 用计数器控制，前 N 次用默认 profile

---

#### Phase 4 · 注意力热力图

**目标**：更细粒度的上下文使用分析，让用户/开发者直观看到哪些 context block 真正被 LLM 使用。

**4.1 CitationTracker 核心实现**（`synapse/modules/context/citation.py`，Phase 2 已实施）
- `mark_usage(context)` — 递增 `usage_count`
- `track_response(response_content, context, event_bus, session_id)` — 信号匹配 + 发事件
- `_extract_signals(block)` — 提取文件路径、符号名、特征行（每 block 最多 5 个信号）
- `report(context)` — 返回 per-block 报告 dict，包含 zone/source/priority/tokens/usage/cited/citation_rate

**4.2 信号提取策略**（`_extract_signals`）
- 文件路径正则 `[\w./\\-]+\.\w{1,6}` + 必须含 `/` 或 `\`（过滤噪声如 `v1.0`）
- `def/class/function` 后的符号名
- ≥12 字符的非注释行（跳过 `#` 开头和常见 markdown 标记）
- 去重 + 限 5 个信号/block

**4.3 Agent 保留上下文**（`agent.py`）
- `self._last_context = context` — 保留最后一次 build 的 context
- `self._citation_tracker = planner._last_citation_tracker` — 拿到 planner 创建的 tracker
- `Synapse` facade 保留 `self._last_agent`，暴露 `get_citation_report()` 方法

**4.4 `/context-report` 命令**（`cli.py`）
- 新增 `_show_context_report(console, synapse, use_rich)` 函数
- Rich 表格显示：Zone / Source / Pri / Tokens / Used / Cited / Rate
- Rate 列用绿色显示 `cited/used` 比例
- 末尾汇总：`Overall: X/Y blocks cited`

**4.5 补全与帮助**（`cli.py`）
- `_SLASH_COMMANDS` 加入 `/context-report`（描述 "Context block citation heatmap"）
- `_show_help` 表格加入 `/context-report` 行

**Trade-off**：
- 跨 provider 兼容性差 — 只有 Anthropic 暴露 cache 元数据，但字符串匹配对所有 provider 通用
- 字符串匹配不精确 — 偏好精确而非召回，citation_count 是下界
- 性能开销小 — 每次 LLM 响应后做一次扫描，每 block 最多 5 个信号
- 后续可升级为 embedding 相似度，但当前实现已能覆盖 80% 的"上下文使用"分析需求

---

#### 实施顺序与依赖

```
Phase 0 (前置修复) ─┐
                    ├─→ Phase 1 (LLM 摘要) ─┐
                    │                        ├─→ Phase 3 (动态预算)
                    └─→ Phase 4 (热力图) ────┘
                       (Phase 2 并入 Phase 4)
```

**本次会话范围**：Phase 0 + Phase 1 + Phase 4（用户确认）。Phase 2/3 后续会话推进。

**不做的事**：
- 替换 Retriever 为 AST/语义检索（超出 TODO E 范围）
- 自研 tokenizer（继续用 `len//4` 估算）
- LLM 分类任务类型（先用规则，验证有效再考虑）

**风险**：
- planner 接入四区后 prompt 变长，可能触发 thrashing — 通过 token 预算检查控制
- LLM Compactor 增加任务耗时和成本 — 通过配置开关 + 触发阈值控制，默认关闭

---

### F · 安全红队 / 对抗测试框架

**状态**：待实现

当前有 4 层安全防护但无专门的安全验证套件。构建系统化的攻击库和自动化评分。

**要点**：
- Prompt Injection 攻击库（直接/间接/多步，88+ 已知变种）
- 沙箱逃逸测试集
- 权限提升测试
- 自动化安全评分（类似 OWASP Benchmark）

**难度**：中等

---

### G · Token 经济性优化

**状态**：✅ 已完成 (2026-07-19)

调研显示 token 投入与产出弱相关（Kendall tau = 0.32），最高准确率出现在低消耗区间。

**要点**：
- 智能 early-stop（检测 thrashing 或低效循环时终止）
- Prompt 缓存策略（跨任务共享 system prompt 和上下文）
- 模型路由（简单任务用小模型，复杂任务用大模型）
- 成本预估与预算控制

**难度**：中等

---

## 暂时不做 / 低优先级

_（暂无。后续如有暂不推进的需求，记录在此。）_

---

*最后更新：2026-07-18*
---

### H · CLI 主界面（Rich 增强型 REPL）

**状态**：开发中

输入 `synapse`（无子命令）进入增强型交互主界面，类似 Claude Code / pico。

**方案选定**：Rich 增强型 REPL（欢迎横幅 + 项目信息 + `/` 命令系统 + 状态行）

**要点**：
- `synapse` 无参数直接进入主界面
- 欢迎横幅显示版本/provider/model/项目路径/工具数
- `/` 命令：help, clear, model, mode, tools, exit
- 现有子命令（chat/run/serve/eval/experiment）保持不变
- 用 Rich Panel/Table 美化输出

**难度**：低（复用现有 Rich 依赖）

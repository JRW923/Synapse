# Synapse 变更日志

> 所有显著变更记录于此文件。格式参考 [Keep a Changelog](https://keepachangelog.com/)，版本号遵循 [Semantic Versioning](https://semver.org/)。

---

## [Unreleased]

### 2026-07-20 · 上下文工程深度优化（Phase 0-4）

#### Added — 新增功能

- **`ContextConfig` 配置段**（`synapse/config/schema.py`）：新增 `SynapseConfig.context`，可配置 `total_tokens`、四区百分比（`system_pct`/`core_pct`/`reference_pct`/`overflow_pct`）、`compaction_strategy`（`truncation` | `llm` | `off`）、`llm_compact_threshold_chars`。`total_tokens=0` 时自动继承 `planning.max_tokens_per_task`。

- **`LLMCompactor`**（`synapse/modules/context/llm_compactor.py`）：基于 LLM 的 OVERFLOW 块摘要工具。
  - 对每个 overflow block 调 LLM 生成紧凑摘要，保留文件路径/符号/关键发现。
  - content hash 缓存避免重复调用。
  - LLM 失败自动回退到 `TruncationCompactor`。
  - 输入硬限 8000 字符，摘要软限 1500 字符。

- **`CitationTracker`**（`synapse/modules/context/citation.py`）：上下文引用追踪器。
  - `mark_usage(context)` — 递增每个 block 的 `usage_count`。
  - `track_response(response_content, context, event_bus, session_id)` — 扫描 LLM 响应中的文件路径/符号名/特征行，匹配回 context block，递增 `citation_count` 并发 `ContextBlockCited` 事件。
  - `_extract_signals(block)` — 每 block 最多提取 5 个信号，偏好精确而非召回。
  - `report(context)` — 返回 per-block 报告，供 `/context-report` 命令渲染。

- **任务类型分类器**（`synapse/modules/context/classifier.py`）：
  - `TaskType` enum：`TEST / REFACTOR / DEBUG / FEATURE / DOC / UNKNOWN`
  - `classify_task(task)` — 规则分类，首匹配优先，顺序 DEBUG → TEST → REFACTOR → DOC → FEATURE
  - 中英文关键词都支持（`测试`/`修复`/`重构`/`新增`/`调试`/`报错`/`文档` 等）
  - DEBUG 排在 TEST 前：`fix failing test` 判为 DEBUG（在调试失败用例，而非编写新测试）

- **动态预算策略表**（`synapse/modules/context/budget.py`）：
  - `TASK_BUDGET_PROFILES` — 6 种静态 profile，每种 TaskType 对应不同四区比例：
    - TEST: reference 0.40（需大量参考既有测试）
    - REFACTOR: core 0.60（重构以核心代码为主）
    - DEBUG: reference 0.50（调试需广参考定位）
    - FEATURE/UNKNOWN: 默认 0.50/0.25
    - DOC: reference 0.40（文档需既有内容参考）
  - `select_budget(task_type, total_tokens)` — 根据任务类型选 profile 构造 `ContextBudget`。

- **历史反馈机制**（`BudgetHistory` 类）：
  - `record(task_type, citation_report)` — 任务结束记录该类型的引用率汇总，持久化到 `ProjectMemory`。
  - `suggest_adjustment(task_type, base_budget)` — 样本数 ≥ 3 后根据各 zone 引用率与均值差异微调 profile，每 zone 变化严格 cap 在 ±5%。
  - 冷启动：样本不足时返回 base 不变。

- **`ContextBlockCited` 事件**（`synapse/protocols/events.py`）：新增 `EventType.CONTEXT_BLOCK_CITED`，载荷含 `block_id`/`block_source`/`response_snippet`。

- **`/context-report` 命令**（`synapse/adapters/cli.py`）：Rich 表格显示上次任务的上下文热力图，列为 Zone / Source / Pri / Tokens / Used / Cited / Rate，末尾汇总总引用率。

- **`/memory` 命令增强**：在原有 messages/tokens/provider/workspace 信息后追加 citation 汇总一行（`system 2/3 cited · core 1/5 cited · ...`）。

- **测试**：新增 `tests/modules/test_context_phase_e.py`（17 测试）+ `tests/modules/test_context_phase_2_3.py`（22 测试）。

#### Changed — 行为变更

- **planner 接入四区**（`synapse/modules/planning/react.py`、`plan_execute.py`）：
  - `_build_system_prompt` 不再只拼 `context.system`，按 `system → core → reference` 顺序注入。
  - 每个 block 带 `[from <source>]` 标注，让 LLM 感知 provenance。
  - CORE 区 token 预算 4000，REFERENCE 区 3000，超出则按 priority 降序裁剪。
  - OVERFLOW 内容不注入 LLM prompt。
  - **影响**：LLM 终于能见到 core/reference 区块——此前所有优化对 LLM 不可见。

- **Agent 调用 Compactor**（`synapse/core/agent.py:_build_context`）：
  - 流程改为 `retrieve → compact(overflow) → partition`。
  - Compactor 在 Partitioner 之前运行，先压缩 overflow 再做四区裁剪。

- **Budget 跟随配置 + 任务类型**（`agent.py:_build_budget`）：
  - 不再硬编码 `ContextBudget()` 100k。
  - 先 `classify_task(task)`，再 `select_budget(task_type, total)`，最后 `suggest_adjustment` 应用历史反馈。

- **超时回退**（`agent.py:_build_context`）：
  - 5s → 10s。
  - 超时不再返回空 Context，改为返回只含 SYSTEM 的最小 Context（读 AGENTS.md/CLAUDE.md/README.md），并发 `AgentProgress(phase="context_timeout")` 警告事件。

- **Partitioner knapsack 修复**（`synapse/modules/context/partitioner.py:_trim_zone`）：
  - 旧逻辑：按 priority 升序排，遇到放不下的就 `break`，导致后续更小的块即使能放下也被丢弃。
  - 新逻辑：按 priority 降序排（高优先级先保留），放不下也继续扫描后续更小的块。
  - 保留原始插入顺序。

- **Compactor 保留 provenance**（`synapse/modules/context/compactor.py`）：
  - 不再覆盖 `source` 为 `MEMORY`，保留原始 source。
  - 新增 `derived_from` 字段记录原 block id。
  - 短 block 不再被无谓截断。

- **ContextBlock 加字段**（`synapse/protocols/retriever.py`）：
  - `id: str` — uuid hex 前 8 位
  - `derived_from: str | None` — 压缩来源 block id
  - `usage_count: int` — 被 LLM 调用次数
  - `citation_count: int` — 被 LLM 响应引用次数
  - `retrieved_at: datetime` — 检索时间戳

- **Agent 保留上下文状态**（`agent.py`）：
  - `self._last_context` — 保留最后一次 build 的 context。
  - `self._citation_tracker` — 从 planner 拿到 tracker。
  - 任务结束调用 `BudgetHistory.record()` 累积历史。

- **`Synapse` facade 暴露 `get_citation_report()`**（`synapse/adapters/library.py`）：返回最近一次任务的引用率报告，供 CLI 调用。

- **`SynapseConfig` 注册到 IoC 容器**：让 Agent 能从 container 解析配置。

#### Deprecated — 废弃

- 无。

#### Removed — 移除

- 无。

#### Fixed — 修复

- **Compactor 破坏 provenance**：旧版本把所有压缩 block 的 `source` 覆盖为 `MEMORY`，丢失原始来源（grep/glob/git 等）。现保留 `source` 并通过 `derived_from` 链接原 block。

- **Partitioner `_trim_zone` break bug**：小预算下会误丢本可放下的高优先级块。

- **Budget 与 config 脱钩**：旧版本硬编码 `ContextBudget()` 100k，与 `PlanningConfig.max_tokens_per_task=200k` 无关。现已配置化并可继承。

- **超时静默丢上下文**：旧版本 5s 超时返回空 `Context()`，导致大项目首跑 LLM 拿不到任何项目信息。现返回 SYSTEM 最小 context 并发警告事件。

---

## 历史版本

历史开发记录详见 [DEVELOPMENT.md](./DEVELOPMENT.md)。

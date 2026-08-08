# Synapse Agent Harness 架构审查与改进记录

日期：2026-08-08

范围：完整 Python runtime、CLI/HTTP adapters、MCP、Tools、Memory、Context、Planning、Security、Eval，以及本轮新增的 TypeScript IDE adapter。

## 一、总体评价

### 结论

Synapse 不是玩具 Demo。它已经具备 Code Agent Harness 的主要构件：ReAct/Plan-Execute/Hierarchical/Swarm、工具注册与授权、Context 分区与压缩、分层 Memory、MCP、EventBus、Audit、Red Team 和 Eval。

作为应届生秋招项目，改进前约为 **7/10**：能力覆盖很宽，架构词汇也完整，但配置、权限、并发、恢复和真实评测之间没有完全闭环。问题不是“功能不够多”，而是部分功能只存在于声明层。

本轮改进后约为 **8/10**：P0/P1/P2 清单已有可运行实现和回归测试。它仍不应包装成生产级 Devin/OpenHands 替代品；更准确的定位是“一个能解释 Harness 关键权衡、并用代码证明的本地 Code Agent runtime”。

### 亮点

1. `protocols / core / modules / adapters` 分层清楚，Agent 与 Planner 解耦，见 `synapse/core/agent.py:24`、`synapse/modules/planning/react.py:91`。
2. ReAct 主循环有 timeout、retry、token budget、thrashing detection、tool error containment 和 history compaction，见 `synapse/modules/planning/react.py:301`、`synapse/modules/planning/react.py:374`、`synapse/modules/planning/react.py:791`。
3. Context 不是简单拼 prompt，而是 repo ranking、四区预算、compaction、citation 和 trust annotation，见 `synapse/modules/context/retriever.py:73`、`synapse/modules/security/injection.py:69`。
4. 安全模型已有 Action-Time Authorization、敏感路径、命令 allowlist、MCP 风险、HMAC audit 和可选强沙箱，见 `synapse/modules/security/auth.py:23`、`synapse/modules/security/audit.py:54`、`synapse/modules/security/sandbox.py:31`。
5. Eval 不再只有静态任务文本，已有临时 Git repo + pytest 和真实 clone/apply/private-test SWE-bench 执行路径，见 `synapse/eval/benchmarks/repo_pytest.py:25`、`synapse/eval/benchmarks/swebench.py:240`。

### 主要短板

1. `ReActPlanner` 仍是大类，Loop、prompt、authorization、stream merge、tool scheduling、metrics 混在一个模块中；此时继续加功能会显著增加回归风险。
2. 增量 repo index 目前只在进程内按 `mtime/size` 缓存；冷启动和超大仓库仍是 O(files)，见 `synapse/modules/context/retriever.py:219`、`synapse/modules/context/retriever.py:264`。
3. Swarm 的合并仍是受控文件复制，不是真正的 Git three-way merge；本轮只做到冲突不覆盖并返回 `PARTIAL`，见 `synapse/modules/planning/worktree.py:109`、`synapse/modules/planning/swarm.py:173`。
4. Plugin 当前是 manifest discovery/version gate，没有完整 install/uninstall lifecycle，也不自动 import 任意第三方代码，见 `synapse/modules/plugins.py:15`。
5. 默认 `process` sandbox 只保证进程树回收；文件系统/网络隔离必须显式选择 Docker/bubblewrap/Seatbelt，见 `synapse/config/schema.py:136`、`synapse/modules/security/sandbox.py:153`。

### 与竞品差距概览

| 框架/产品 | 代表能力 | Synapse 当前差距 |
|---|---|---|
| Claude Code | 成熟的权限 UX、project rules、MCP、长会话恢复 | 缺 checkpoint/rollback 和成熟交互审批 UX |
| Pi | 极简 Loop 与清晰扩展边界 | Synapse 功能更多，但核心类更重，解释成本更高 |
| Aider | Git-native edit/test/commit、repo map | Synapse 工具更广，但 Git merge 与 patch acceptance 闭环较弱 |
| Cline | Plan/Act、MCP、checkpoint、IDE 工作流 | Synapse 的 IDE adapter 只是 client，还没有 diff accept/reject UI |
| Cursor | 增量索引、Rules、IDE context、模型路由 | Synapse 冷启动索引和 editor awareness 明显较弱 |
| OpenHands | Event runtime、Docker sandbox、真实 benchmark | Synapse 的 runtime 持久化、容器隔离和 benchmark 规模不足 |
| Devin | 持久 VM、后台任务、完整 workspace 状态 | Synapse 是本地进程级 agent，不具备长期 VM 生命周期 |

## 二、按 15 个维度深度分析

### 1. Agent 主循环（Loop）

**当前实现**：`ReActPlanner.execute()` 实现 Think -> Act -> Observe，readonly tools 可并行预取，旧 Tool message 会压缩；Planner 可被 Plan-Execute、Hierarchical、Swarm 复用，见 `synapse/modules/planning/react.py:301`、`synapse/modules/planning/react.py:493`、`synapse/modules/planning/react.py:755`。

**问题**：一个类同时负责 prompt、stream assembly、retry、authorization、tool execution 和 metrics。取消只能在 iteration 边界检查，正在执行的外部 tool 主要依赖 task cancellation/timeout。继续拆十个抽象没有必要，但至少应停止往该类继续塞新策略。

**竞品对比**：Pi/Aider 的核心 Loop 更窄；OpenHands 更强调 event/action runtime；Claude Code 的用户感知来自稳定的 permission/checkpoint workflow，而不是 Planner 数量。

**改进建议（代码级）**：本轮已让 `max_retries` 生效并修复 awaitable stream fallback，见 `synapse/modules/planning/react.py:181`、`synapse/modules/planning/react.py:382`。下一步只建议抽出一个 `ToolExecutor`，负责 auth + timeout + event；不要再新增第五种 Planner。

### 2. 上下文管理

**当前实现**：Retriever 使用 Git-aware 文件列表、TF/IDF 式 ranking、AST symbols、excerpt、四区预算和 compaction；token 计数优先使用 tiktoken，见 `synapse/modules/context/retriever.py:73`、`synapse/modules/context/retriever.py:286`、`synapse/core/tokenizer.py:18`。

**问题**：文件发现仍逐次扫描；cache 是进程内的，重启即丢；不同 provider 的 tokenizer 并不完全一致。Context block 的 provenance 有基础字段，但缺少 index generation/version。

**竞品对比**：Cursor 强在持久增量索引和 IDE 实时状态；Aider 的 repo map 更小、更聚焦；Claude Code 更依赖强模型加项目规则和按需工具读取。

**改进建议（代码级）**：本轮在 `synapse/modules/context/retriever.py:264` 增加按 `mtime_ns/size` 的增量内容缓存。仓库超过约 1,500 个候选文件后，再升级 SQLite FTS5；当前项目不值得现在引入 Tantivy/专用向量库。

### 3. 记忆管理

**当前实现**：`LayeredMemory` 路由 Session/Project/User/Semantic 四层，USER memory 使用 Markdown + YAML frontmatter，见 `synapse/adapters/library.py:99`、`synapse/modules/memory/user.py:94`。

**问题**：Semantic backend 初始化失败会静默降级；Memory 没有 schema migration、TTL、容量策略。改进前 USER 层存在但 Retriever 不读取，是典型“有类但没闭环”。

**竞品对比**：Claude Code/Aider 更依赖显式 project rules 和 Git 历史，而不是复杂 memory；Devin/OpenHands 更偏 workspace/state persistence。

**改进建议（代码级）**：USER memory 已接入 reference zone，优先级低于 Session/Project，见 `synapse/modules/context/retriever.py:407`、`synapse/modules/context/retriever.py:435`。保留四层即可，不要再增加第五种 memory；应为 semantic fallback 增加可观测事件，而不是继续加 backend。

### 4. 状态管理

**当前实现**：Session 保存 messages/metadata，支持 fork、save/load/list；保存使用 temp + replace 原子替换，见 `synapse/core/session.py:14`、`synapse/core/session.py:58`。

**问题**：HTTP session store 仍是进程内 dict；没有 artifact manifest、patch checkpoint、rollback 和 crash resume。Session message history 不等于执行状态机。

**竞品对比**：Cline/Claude Code 强在 checkpoint/restore；Devin/OpenHands 强在持久 workspace 与后台 runtime。

**改进建议（代码级）**：本轮先完成 atomic save，避免半写 JSON。下一阶段只做 Git checkpoint（run 前后 commit/diff + session metadata），不要自研 event-sourced database。

### 5. 工具系统（Tools）

**当前实现**：`DefaultToolRegistry` 支持 register/get/schema，工具有 schema、risk、category、sandbox flag；file/search/shell/git/web/db/browser/MCP/skill/todo 已覆盖，见 `synapse/modules/tools/registry.py:6`、`synapse/protocols/tool.py:29`。

**问题**：schema 仍是松散 dict，缺 schema version、idempotency、side-effect descriptor；`EditTool` 与部分工具的 workspace 处理不完全统一。

**竞品对比**：Claude Code/Cline 对每次高风险调用提供更成熟的审批描述；OpenHands action schema 更像 runtime contract；Aider 的工具少，但 edit/test/Git 闭环更深。

**改进建议（代码级）**：`tools.enabled` 已真正过滤注册表；read/glob/grep 已统一 workspace + symlink escape guard，见 `synapse/modules/tools/workspace.py:10`。下一步优先统一 write/edit path resolver，不增加更多工具。

### 6. LLM 集成与调度

**当前实现**：Anthropic/OpenAI-compatible/Google provider 统一为 `LLMProvider`；ReAct 支持 stream/chat fallback、timeout、retry；新增多 provider failover 与 cost ordering，见 `synapse/protocols/llm.py:30`、`synapse/modules/providers/routing.py:10`、`synapse/adapters/library.py:657`。

**问题**：retry 仍按异常统一处理，没有区分 4xx、429、timeout；cost routing 依赖手工配置价格；stream 已经输出部分 token 后不会 fallback，这是正确但需要在文档中解释。

**竞品对比**：Cursor 有成熟模型路由；Claude Code 通常围绕自家模型优化 prompt/cache；Aider 允许多模型但路由目标较简单。

**改进建议（代码级）**：本轮已接通 `provider.max_retries`、`fallback_models`、`routing`，见 `synapse/config/schema.py:76`、`synapse/modules/planning/react.py:382`。下一步只增加 retryable error classifier 和 cost event，不做自动模型评分器。

### 7. 执行环境与沙箱

**当前实现**：默认 Windows Job Object/Unix process group 保证进程树回收；可显式选择 Docker、bubblewrap、Seatbelt，并可关闭网络，见 `synapse/modules/security/sandbox.py:31`、`synapse/modules/security/sandbox.py:46`、`synapse/modules/security/sandbox.py:153`。

**问题**：默认 `process` 不是文件系统沙箱；强 backend 依赖宿主可执行文件和镜像。Windows 本机只验证了 Docker executable 存在，没有在本轮拉镜像跑完整容器任务。

**竞品对比**：OpenHands/Devin 的优势是环境本身就是 runtime；Cline/Claude Code 更多依赖本机权限确认，隔离强度取决于宿主。

**改进建议（代码级）**：`sandbox_mode=enforce` 初始化失败已 fail closed，`warn` 才允许降级，见 `synapse/adapters/library.py:595`。简历中必须准确写“process containment + optional strong backend”，不能写成默认容器沙箱。

### 8. 可扩展性（MCP/Skills/插件）

**当前实现**：MCP 支持 stdio/streamable HTTP；Skills 可注入 prompt；Hooks 订阅 EventBus；Plugin manifest 支持 SemVer 和 API version gate，见 `synapse/modules/mcp/manager.py:35`、`synapse/modules/plugins.py:15`、`synapse/modules/hooks.py:17`。

**问题**：Plugin 暂不执行第三方 entry point，也没有 install/uninstall lifecycle；Hooks 是 post-event best effort，不能像 policy hook 一样阻断调用。

**竞品对比**：Claude Code/Cline 的 MCP 生态和配置 UX 更成熟；Pi 的扩展边界更轻；OpenHands 更强调 runtime action extensibility。

**改进建议（代码级）**：MCP 默认风险已从 `READ_ONLY` 改为 `EXTERNAL`，可信只读 server 需显式降级，见 `synapse/protocols/mcp.py:22`、`synapse/modules/mcp/wrappers.py:35`。插件保持 manifest-only 是合理安全边界，暂不需要任意 import loader。

### 9. 多 Agent 协作

**当前实现**：Swarm 有 role、并发 coder、reviewer、vote/verify、TaskBoard、独立 Session 和 worktree，见 `synapse/modules/planning/swarm.py:122`、`synapse/modules/planning/swarm.py:403`。

**问题**：任务 scope 由 LLM 生成，可能为空或重叠；合并不是 Git three-way merge；并行 worker 共用 LLM provider 和 EventBus。

**竞品对比**：Devin 更像单强 Agent + 持久环境；OpenHands 可表达多 Agent runtime；多数主流 Code Agent 并不默认暴露复杂 Swarm，因为收益不稳定。

**改进建议（代码级）**：本轮对变更文件做 hash snapshot，冲突时不覆盖并返回 `PARTIAL`，见 `synapse/modules/planning/worktree.py:109`、`synapse/modules/planning/swarm.py:173`。面试时应强调 Swarm 是实验模式，不要把“Agent 数量”当核心卖点。

### 10. 知识/规则注入

**当前实现**：Retriever 读取 CLAUDE.md/AGENTS.md/README；InjectionGuard 为 ContextBlock 标注 trust level，external 内容用 XML tag 包装，见 `synapse/modules/context/retriever.py:142`、`synapse/modules/security/injection.py:69`、`synapse/modules/security/injection.py:96`。

**问题**：标注不是强制 policy；恶意内容仍进入模型上下文。Project rules 缺 precedence、scope 和冲突诊断。

**竞品对比**：Claude Code/Cursor 的 project rules 更接近产品级配置；Cline 也强调项目指令，但 prompt injection 仍是行业共同难题。

**改进建议（代码级）**：保留 annotation + explicit system note；对外部 ToolResult 统一标注。不要宣称“解决 prompt injection”，准确说“降低指令与数据混淆”。

### 11. 权限与安全控制

**当前实现**：Authorization 按 READ_ONLY/WRITE_LOCAL/EXECUTE/EXTERNAL/META 分层，检查 sensitive paths、command chain、redirect 和 allowlist；Audit 为 HMAC JSONL，见 `synapse/modules/security/auth.py:95`、`synapse/modules/security/auth.py:313`、`synapse/modules/security/audit.py:118`。

**问题**：shell control split 不是完整 shell parser；SSRF、DNS rebinding、TOCTOU 仍需工具侧防护；审批 callback 是进程内能力。

**竞品对比**：Claude Code/Cline 的权限 UX 更成熟；OpenHands 通过容器减小宿主风险；Aider 依赖 Git 作为恢复边界。

**改进建议（代码级）**：`allowlist_commands`、`allowed_paths` 已从配置接线；MCP 默认 external；read tools 防 symlink escape。下一步只应补 HTTP SSRF policy 和 write/edit 统一 realpath，不手写完整 shell parser。

### 12. 可观测性

**当前实现**：EventBus 覆盖 tool/auth/agent/swarm/token/process quality；Audit/metrics 消费事件；本轮为事件补齐 `run_id/trace_id/parent_event_id`，见 `synapse/protocols/events.py:37`、`synapse/core/events.py:29`、`synapse/core/events.py:56`。

**问题**：EventBus 是进程内 pub/sub，不保证落盘投递；parent chain 是线性 causal hint，不是完整 OpenTelemetry span graph。

**竞品对比**：OpenHands 的 event runtime 更中心化；商业产品通常有服务端 traces/cost telemetry，但实现不可完全对照。

**改进建议（代码级）**：HTTP 返回 `run_id`，SSE event 带 trace 字段，见 `synapse/adapters/server.py:67`、`synapse/adapters/server.py:247`。后续如需要接 OTLP，只写一个 EventBus subscriber，避免侵入 Planner。

### 13. 配置与扩展点

**当前实现**：Pydantic schema + YAML + env override；Tools/Security/Provider/Context/Hooks/Plugins 都有显式 section，见 `synapse/config/schema.py:87`、`synapse/config/schema.py:132`、`synapse/config/schema.py:160`、`synapse/config/loader.py:9`。

**问题**：改进前存在 `tools.enabled/max_retries/sandbox_mode/allowlist_commands` 声明不生效；现在已接线，但 schema 仍缺跨字段校验，例如 `sandbox_backend=docker` 与 image 空值。

**竞品对比**：Claude Code/Cursor 的配置 UX 更成熟；Pi 的配置更少更容易理解；Synapse 应避免继续扩张 schema。

**改进建议（代码级）**：本轮把上述 dead config 全部接入 `synapse/adapters/library.py:483` 之后的装配路径。下一步用 Pydantic validator 做 backend/routing 枚举校验，不增加新的配置层。

### 14. 评测与测试

**当前实现**：单元/集成/Red Team/metrics 测试较完整；新增真实临时 Git + pytest benchmark；SWE-bench 执行器支持 clone、checkout、git apply、private tests，见 `synapse/eval/benchmarks/repo_pytest.py:25`、`synapse/eval/benchmarks/swebench.py:240`。

**问题**：默认 process-quality tasks 仍是启发式；真实 SWE-bench 需要网络、依赖安装和隔离镜像，当前不是官方全量 runner。

**竞品对比**：OpenHands 在真实 benchmark 和容器环境上明显更成熟；Aider 长期用 edit/test benchmark 约束回归。

**改进建议（代码级）**：面试演示优先使用 3-5 个本地可复现 Git fixtures，不要现场跑大规模 SWE-bench。当前全量结果为 `387 passed, 1 skipped, 1 warning`。

### 15. 错误处理与韧性

**当前实现**：LLM/tool 有 timeout，tool error 转为 observation，MCP 可按 event loop reconnect，SSE disconnect 会 cancel run，见 `synapse/modules/planning/react.py:382`、`synapse/modules/mcp/manager.py:125`、`synapse/adapters/server.py:271`。

**问题**：retry 未分类；部分 optional backend 异常仍静默降级；Qdrant Windows local mode 在解释器退出时仍可能产生 `.lock` 清理 warning。

**竞品对比**：Devin/OpenHands 的持久 runtime 更适合长任务恢复；Claude Code/Cline 的用户中断与 checkpoint 体验更完整。

**改进建议（代码级）**：本轮修复 awaitable async stream warning、Session atomic replace、Synapse run serialization、SSE cancellation。下一步只补 typed retry policy 和 explicit backend-degraded event。

## 三、架构重构建议

### 是否应该用 TypeScript 全量重构

**结论：不应该。**

全量 TypeScript 重构的潜在收益是前后端同语言、IDE API 类型体验更好、Node MCP 生态直接；代价是重新验证 provider SDK、async streaming、pytest eval、Qdrant/Chroma、Playwright 和现有 387 个测试。当前缺陷主要是 runtime governance，不是 Python 语言导致。

合理路径是：

1. Python 保持唯一 Agent runtime。
2. Python 侧继续强化 dataclass/Pydantic/pyright 类型边界。
3. IDE 集成用 TypeScript，通过 HTTP/SSE 访问 runtime。
4. 本轮已新增 `ide-adapter/src/client.ts`，支持 `AbortSignal`，并通过 `tsc --noEmit`。

### 按周路线图

| 周次 | 优先级 | 内容 | 本轮状态 |
|---|---|---|---|
| Week 1 | P0 | MCP risk、dead config、stream fallback、workspace guard、atomic session、run lock | 已完成 |
| Week 2 | P1 | trace IDs、typed result、USER memory、tokenizer、repo benchmark、Swarm conflict、SSE cancel | 已完成 |
| Week 3 | P2 | incremental cache、strong sandbox adapters、plugin manifest、provider routing、SWE-bench、TS adapter | 已完成 MVP |
| Week 4 | 稳定化 | Git checkpoint、retry classifier、HTTP SSRF、跨平台 sandbox CI | 建议后续 |

P2 中“已完成 MVP”不是生产完成：incremental index 仍为进程内；Plugin 不执行任意代码；强沙箱需要对应宿主环境；SWE-bench 不是官方全量 orchestration。

## 四、面试答辩重点

### 3-5 个技术亮点

1. **Action-time authorization**：不是只在工具注册时判断，而是在每次 call 时结合 risk/path/command/MCP server 配置决策。
2. **Context engineering closed loop**：repo ranking -> budget partition -> compaction -> trust annotation -> citation/usage，而不是把文件全塞进 prompt。
3. **可观测 ReAct runtime**：typed events、run/trace/parent correlation、streaming token、runtime score、HMAC audit。
4. **真实可复现评测**：临时 Git fixture、pytest grading、patch/private-test SWE-bench 执行路径。
5. **克制的 Python + TypeScript 边界**：不为“显得现代”重写 runtime，只把 IDE client 放在 TypeScript。

### 深度讨论话题

1. **为什么 MCP tool 默认必须是 EXTERNAL**：tool discovery 只能告诉你 schema，不能证明副作用；信任应由 host 显式配置。
2. **为什么 Swarm 很容易负收益**：任务分解、上下文重复、文件冲突和 verification token 成本，何时单 Agent 反而更可靠。
3. **process containment 与 filesystem sandbox 的区别**：Windows Job Object 能回收子进程，但不能限制文件和网络；如何选择 Docker/bwrap/Seatbelt。

## 五、附录：快速改进清单

| 优先级 | 问题 | 文件 | 工作量 | 状态 |
|---|---|---|---|---|
| P0 | MCP 默认风险过低 | `synapse/protocols/mcp.py` | 0.5h | 完成 |
| P0 | sandbox config/degrade 不生效 | `synapse/adapters/library.py` | 1h | 完成 |
| P0 | tools/command/retry dead config | `synapse/config/schema.py`、`synapse/adapters/library.py` | 2h | 完成 |
| P0 | async stream fallback warning | `synapse/modules/planning/react.py` | 1h | 完成 |
| P0 | readonly tool workspace/symlink escape | `synapse/modules/tools/workspace.py` | 2h | 完成 |
| P0 | Session 非原子写入 | `synapse/core/session.py` | 0.5h | 完成 |
| P0 | shared instance 并发污染 | `synapse/adapters/library.py` | 1h | 完成（串行化 ceiling） |
| P0 | ReAct 最小回归 | `tests/modules/test_harness_hardening.py` | 1h | 完成 |
| P1 | trace correlation 缺失 | `synapse/protocols/events.py`、`synapse/core/events.py` | 2h | 完成 |
| P1 | 动态伪 ToolResult | `synapse/modules/planning/react.py` | 0.5h | 完成 |
| P1 | USER memory 未消费 | `synapse/modules/context/retriever.py` | 0.5h | 完成 |
| P1 | token 粗估 | `synapse/core/tokenizer.py` | 1h | 完成 |
| P1 | 缺本地真实 benchmark | `synapse/eval/benchmarks/repo_pytest.py` | 2h | 完成 |
| P1 | Swarm 静默覆盖 | `synapse/modules/planning/worktree.py` | 3h | 完成 |
| P1 | SSE 断开不取消 | `synapse/adapters/server.py` | 1h | 完成 |
| P2 | repo 重复读文件 | `synapse/modules/context/retriever.py` | 2h | 完成（进程内） |
| P2 | 强沙箱 backend 缺失 | `synapse/modules/security/sandbox.py` | 4h | 完成 adapter |
| P2 | Plugin 无版本 gate | `synapse/modules/plugins.py` | 2h | 完成 manifest 层 |
| P2 | Provider 无 fallback/cost routing | `synapse/modules/providers/routing.py` | 3h | 完成 |
| P2 | SWE-bench private test 是占位 | `synapse/eval/benchmarks/swebench.py` | 3h | 完成执行路径 |
| P2 | IDE adapter 缺失 | `ide-adapter/src/client.ts` | 2h | 完成 |

## 验证记录

- `python -m compileall -q synapse`：通过。
- `pytest -q`：**387 passed, 1 skipped, 1 warning**。
- `npm run check`（`ide-adapter`）：通过。
- `git diff --check`：通过；仅有仓库既有 Windows CRLF 提示。
- 残留：FastAPI/Starlette `httpx` deprecation warning；Qdrant local mode 在 Windows 解释器退出时可能报告临时 `.lock` 清理 warning。

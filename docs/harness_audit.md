# Synapse × Claude Code Harness 对照审计与开发计划

> 依据 [learn.claude code · timeline](https://learn.shareai.run/en/timeline/) 划分的 20 个 harness 章节（s01–s20），
> 审计 Synapse 项目的对应设计与实现，并制定补齐缺口的开发计划。
>
> 原则（ponytail / lazy）：复用现有 `EventBus` / `Session.fork` / `DefaultToolRegistry` / planner 接口；
> 不引入新依赖；每批实现留一个可运行 check（最小单测，无框架）。

---

## 1. 现状审计（s01–s20）

| 章节 | 主题 | 状态 | 对应位置 |
|------|------|------|----------|
| s01 | 最小模型/工具循环 | ✅ 已实现 | `core/agent.py::Agent.run()` → `modules/planning/react.py::execute()` 的 model→tool→feedback 循环 |
| s02 | 工具分发表 | ✅ 已实现 | `modules/tools/registry.py::DefaultToolRegistry`（`get`/`get_schemas`），planner 经 `tools.get()` 分发 |
| s03 | 权限门 | ✅ 已实现 | `modules/security/auth.py::ActionAuthorizer` + `react.py::_confirm`，无回调硬拒 / `--yes`·`auto_approve` 放行（L.3） |
| s04 | 生命周期钩子 | ✅ 已实现 | `core/events.EventBus` + 丰富事件可订阅；`modules/hooks.py::HookRunner` 把 config `hooks` 段（event_type → shell 命令）接到 EventBus，PostToolUse 类钩子已落地（PreToolUse 阻断待接 ActionAuthorizer） |
| s05 | 待办管理 (TodoWrite) | ✅ 已实现 | `modules/todo.py::TodoStore` + `tools/todo_tool.py` 的 `todo_write`/`todo_read`；REPL `/todos` 视图可看 |
| s06 | 隔离子任务上下文 (Subagent) | ✅ 已实现 | `hierarchical.py` / `swarm.py` 均用 `session.fork(agent_id)` 给子任务干净消息历史 |
| s07 | 按需技能加载 (Skill) | ✅ 已实现 | `modules/skill.py::SkillLoader`（扫描 `skills/<name>/SKILL.md`，按 classify_task + 触发词匹配）注入 system 提示；`tools/skill_tool.py::load_skill` 可显式拉取 |
| s08 | 上下文压缩 | ✅ 已实现 | `modules/context/compactor.py::ContextCompactor` + `ContextPartitioner` |
| s09 | 持久记忆层 | ✅ 已实现 | `modules/memory`（Session/Project/User/Semantic 四层 + `LayeredMemory`），`ProcessQualityScored.hint` 经 retriever 回注 |
| s10 | 运行时组装系统提示 | ✅ 已实现 | `react.py::_build_system_prompt(context)` 由各 zone + role suffix 运行时拼装，非硬编码 |
| s11 | 重试策略 (Error Recovery) | ✅ 已实现 | `react.py` LLM 指数退避重试（≤3 次，`2**attempt`），耗尽返 FAILED；工具错误以 `ToolResult(success=False)` 回灌；thrashing early-stop |
| s12 | 任务看板 (Task System) | ✅ 已实现 | `modules/planning/board.py::TaskBoard`（pending/claimed/done 状态机，原子认领）为一等公民，Hierarchical 串行认领 + Swarm 并发认领共用 |
| s13 | 后台执行 | ✅ 已实现 | `modules/tools/background.py::BackgroundTaskManager`：shell `run_in_background` 返回 handle，结束发 `BackgroundResult`，可 `read_task_id` 读取 |
| s14 | 定时调度 (Cron) | ✅ 已实现 | `modules/cron.py::CronScheduler`（stdlib 5 字段 cron，不引第三方）进程内调度 |
| s15 | 队友邮箱/团队 | ✅ 已实现 | `swarm.py::SwarmPlanner`：多角色 worker（coder/reviewer/verifier）+ `session.fork` 隔离 + 评审/投票/验证事件（TODO C） |
| s16 | 团队协同协议 | ✅ 已实现 | 显式事件契约 + 新增 `AgentMessage`/`TaskClaimed`/`TaskReleased`（`events.py`），worker 间通信走事件 |
| s17 | 自主认领任务 | ✅ 已实现 | `SwarmPlanner` 新增 `autonomous` 模式：子任务上 `TaskBoard`，N 个通用 worker 自驱认领执行后合并（显式 RoleSpec 行为保持不变） |
| s18 | Worktree 隔离 | ✅ 已实现 | `modules/planning/worktree.py::WorktreeManager`：git worktree 隔离（非 git 退化为独立子目录），Swarm coder 各拿隔离目录并在 run 结束清理 |
| s19 | MCP 工具桥 | ✅ 已实现 | `modules/mcp/`（manager/client/wrappers）把外部服务注册为 agent 工具 |
| s20 | 集成化 harness | ✅ 已实现 | 整个 Synapse：一个 agent 循环 + 权限/记忆/上下文/流式/评分/Swarm/MCP 等周边系统 |

### 结论

- **已实现 20 项（全量覆盖）**：s01, s02, s03, s04, s05, s06, s07, s08, s09, s10, s11, s12, s13, s14, s15, s16, s17, s18, s19, s20
- **部分实现 0 项**
- **未实现 0 项**

> 2026-07-25 补全：原「部分/未实现」的 9 项（s04/s05/s07/s12/s13/s14/s16/s17/s18）
> 已按开发计划全部落地，每批均带可运行单测（check）。详见上方状态表与各模块实现。
> 已知上限（ponytail）：s04 仅只读 PostToolUse 钩子（PreToolUse 阻断待接 ActionAuthorizer）；
> s07 用极简 frontmatter 解析；s13/s14 为进程内实现（多实例需外部协调）；s18 仅 git 仓库真正 worktree 隔离。

---

## 2. 开发计划

### 2.1 分批策略

按依赖与价值分三批，**每批以「让上一批真正可用」为闭环**：

- **第一批 · 夯实 Swarm 生产化（P0）**：s18 Worktree 隔离 → s17 自主认领 → s13 后台执行。
  这三项是 s15/s16 真正落地的关键依赖——没有文件系统隔离与自主调度，多 worker 只是演示。
- **第二批 · 单 agent 能力补齐（P1）**：s07 Skill 加载、s05 TodoWrite、s12 Task Board（与 s17 的 board 合并为一「可观察任务系统」）、s04 用户钩子。
- **第三批 · 长期运维与协议（P2）**：s14 Cron 调度、s16 协议层完善。

### 2.2 逐项设计

#### s18 · Worktree 隔离（P0）
- **目标**：并行 worker 拥有隔离的文件系统，互不污染。
- **复用**：已有 `session.fork(agent_id)` 做会话隔离；git worktree 是标准 `git worktree add` 功能。
- **最小实现**：在 `SwarmPlanner._spawn` 中，为每个 worker 在其 `file_scope` 下 `git worktree add .synapse/worktrees/<agent_id>`；worker 写操作限制在 worktree 内；任务完成后合并/清理 worktree。
  - `ponytail:` 注明——仅支持 git 仓库；非 git 目录退化为 `tempfile.mkdtemp()`，FileSystem isolation 降为 best-effort。
- **验收（check）**：单测——spawn 两个 worker 各写同名文件到各自 worktree，断言互不影响；结束后 worktree 被清理。

#### s17 · 自主认领任务（P0）
- **目标**：worker 自主检查任务板认领任务，而非 `RoleSpec` 显式分配。
- **复用**：`hierarchical.py` 已有 `Subtask` 列表；`EventBus` 可做 board 事件；`_SwarmTracker` 的订阅模式。
- **最小实现**：引入轻量 `TaskBoard`（内存 `dict: task_id → {status, owner}`，status ∈ pending/claimed/done），worker 循环 `claim()` 抢 pending；`SwarmPlanner` 从「显式分配」改为「投任务到 board + 启动 N 个通用 worker 自驱认领」。与 s12 共用同一 `TaskBoard`。
- **验收（check）**：单测——board 投放 5 任务，2 worker 并发认领，断言无重复认领、全部完成。

#### s13 · 后台执行（P0）
- **目标**：agent 可把慢操作丢到后台，继续推理。
- **复用**：`asyncio.create_task` + `EventBus` 事件（参考 server SSE 的 queue 模式、`react.py` 的 `tool_call_*` 事件）。
- **最小实现**：给 `shell` 工具加 `run_in_background` 参数，返回 `task_id` handle；执行结果经新增 `BackgroundResult` 事件回传；planner 在循环中允许「发起后台 → 继续 → 后续轮次读取结果」。先用 shell 试点，其它工具按需扩展。
- **验收（check）**：单测——后台 shell 立即返回 handle，稍后 `BackgroundResult` 事件携带 stdout。

#### s07 · Skill 加载（P1）
- **目标**：按需把专门知识注入系统提示。
- **复用**：系统提示已由 `_build_system_prompt` 运行时拼装（s10）；任务分类 `classify_task` 已存在。
- **最小实现**：`skills/` 目录放 `SKILL.md`（`name` + 触发条件 + 正文）；新增 `SkillLoader`，在 `_build_context`/`_build_system_prompt` 时按 `classify_task` 结果匹配并注入 system zone；LLM 可用 `load_skill` 工具显式拉取。
- **验收（check）**：单测——给定任务，`SkillLoader` 命中对应 skill 且出现在 system prompt 中。

#### s05 · TodoWrite（P1）
- **目标**：agent 显式维护可见待办，长任务不漂。
- **复用**：`plan_execute.py` 已有 steps；`SessionMemory`/`ProjectMemory` 可持久化。
- **最小实现**：新增 `TodoWrite`/`TodoRead` 工具（写 SessionMemory 的 `todos` 条目）；planner 在每步前后更新状态；REPL 加 `/todos` 视图（仿 `/score`）。短期可把 `plan_execute` 的 steps 直接暴露为可读 + 一个 update 工具。
- **验收（check）**：单测——agent 写 todo 后状态持久化，且 `/todos` 能看到。

#### s12 · Task Board 升级（P1，与 s17 合并）
- **目标**：把 `hierarchical.Subtask` 抽象为一等公民 `TaskBoard`，带状态机，既服务单 agent（s12）也服务 swarm 自主认领（s17）。
- **复用**：`Subtask` 数据类、`EventBus`。
- **最小实现**：`TaskBoard` 同时被 `HierarchicalPlanner`（串行认领自身子任务）与 `SwarmPlanner`（多 worker 并发认领）使用；状态变更发 `TaskStatusChanged` 事件，供 REPL/CLI 实时展示。
- **验收（check）**：见 s17 的并发认领单测 + 一个单 agent 串行执行的可见状态单测。

#### s04 · 用户钩子（P1）
- **目标**：用户可挂 PreToolUse/PostToolUse 钩子脚本。
- **复用**：`EventBus` 已有事件；config schema 模式。
- **最小实现**：config 增加 `hooks:` 段，映射 事件类型 → shell 命令；在 `EventBus.emit` 后（或 planner 关键点）`subprocess.run` 钩子（带超时）。先用 PostToolUse（只读告警）起步；PreToolUse 阻断需接 `ActionAuthorizer` 决策点。
- **验收（check）**：单测——注册一个 PostToolUse 钩子，断言命令被执行且拿到事件 payload。

#### s14 · Cron 调度（P2）
- **目标**：定时产生任务。
- **复用**：`TaskBoard` + `Synapse.run`；`serve` 模式下可持久化到 `ProjectMemory`。
- **最小实现**：新增 `CronScheduler`（优先 stdlib `asyncio` + 下次触发时间计算，不引第三方；仅当环境已装 `APScheduler` 才用）。按 cron 表达式投放任务到 board 或直接 `synapse.run`。`ponytail:` 注明进程内调度，多实例需外部触发器（如系统 cron）才一致。
- **验收（check）**：单测——注册每分钟任务，mock 时间推进，断言触发。

#### s16 · 团队协同协议完善（P2）
- **目标**：agent 间有显式消息合同，而非隐含约定。
- **复用**：已有 swarm 事件。
- **最小实现**：在 `events.py` 补充 `AgentMessage` / `TaskClaimed` / `TaskReleased` 事件并定义 payload schema；worker 间通信走事件。与 s17 的 `TaskBoard` 事件同源。
- **验收（check）**：事件 schema 单测 + 发送/订阅往返。

---

## 3. 建议落地顺序（一句话路线图）

```
s18 Worktree → s17 自主认领(含 s12 TaskBoard) → s13 后台执行     # P0：Swarm 生产化
s07 Skill → s05 TodoWrite → s04 用户钩子                          # P1：单 agent 体验
s14 Cron → s16 协议完善                                          # P2：运维与协议
```

每批结束应有可运行单测（check），且尽量复用既有 `EventBus` / `Session.fork` / `DefaultToolRegistry` / planner 接口，避免新增抽象与依赖。

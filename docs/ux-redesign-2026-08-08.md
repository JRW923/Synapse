# Synapse 终端 UI 重设计方案（2026-08-08）

## 1. 设计目标

这次不是给旧 banner 换颜色，而是把 CLI 重新组织成一个面向长任务的终端工作台：

1. 用户在 2 秒内知道当前 workspace、model、planning mode、session 和 readiness。
2. 任务运行中可以扫读 phase、iteration、token、elapsed 和最近工具，不必阅读原始日志。
3. 任务完成后能立即判断成功程度、消耗和下一步动作。
4. 80 列、60 列、40 列和非 TTY 输出都保持稳定，不靠横向滚动或 ANSI 猜测状态。
5. UI 仍然是 Python + Rich + prompt-toolkit，不引入 Textual、新依赖或新的事件协议。

## 2. 调研结论

参考了 Claude Code、CodeBuddy、Pi coding-agent、Aider、Cline 和 OpenHands 的终端/Agent
交互特点：

- Claude Code、CodeBuddy：首页是工作区和当前模型的紧凑状态区，权限/执行状态比品牌
  banner 更重要；长任务通过稳定的状态行和工具反馈降低等待焦虑。
- Pi：使用低装饰的终端工作区、模型/会话状态和可持续的流式 transcript，不让启动页
  变成营销页。
- Aider：持续展示 token/cost/模型信息，命令和结果紧贴在同一条工作流里，适合高频
  修改-验证循环。
- Cline、OpenHands：把工具调用作为事件时间线呈现，工具名、参数摘要、成功/失败和
  耗时比模型原始日志更有用。

Synapse 已经有 `EventBus`、`AgentProgress`、`LLMToken`、`ToolCallStarted/Completed`、
`_LiveRun` 和 session 持久化，因此本轮只重做 adapter 的呈现层，不碰 Agent loop、
provider 或 server SSE 契约。

## 3. 现状问题

| 区域 | 当前问题 | 代码位置 |
|---|---|---|
| 首页 | 10 行 ASCII art 占据第一屏，真正重要的 ready/model/session 信息靠后；视觉上只有 cyan 一种主色 | `synapse/adapters/cli.py:1192-1429` |
| 首页 | `config source` 是长路径，窄终端只做截断，没有根据宽度重排信息 | `synapse/adapters/cli.py:1332-1425` |
| Live | 标题把 phase、token、elapsed 以句点拼接，缺少固定列和 input/output 分项 | `synapse/adapters/cli.py:455-486` |
| Live | transcript、工具 timeline、swarm 行没有明确分隔，工具记录出现位置不稳定 | `synapse/adapters/cli.py:477-485` |
| Live | iteration 只嵌在中文 label 里，无法和 max_iterations 对齐 | `synapse/adapters/cli.py:517-540` |
| 结果 | 结果块只有 status、正文和一行 metrics，没有视觉上的成功/部分/失败层级 | `synapse/adapters/cli.py:1251-1275` |
| Prompt | prompt 仍是普通 `synapse >`，与首页状态区没有品牌/状态联系 | `synapse/adapters/cli.py:2128-2132` |

## 4. 视觉语言

### 4.1 Block logo

删除旧的 10 行 ASCII brain。新 logo 使用 5x5 的实心 `█` 色块组成 `S` 形 monogram，
旁边显示 `SYNAPSE`，宽终端最多占 20 个 display cells；窄终端只保留 5x5 图标和
`SYNAPSE` 文本。这样不依赖字体图案、不需要图片资源，也不会在 Windows/PowerShell
中出现半宽 ASCII 对齐问题。

设计稿（每个 `█` 都是可着色的实心块）：

```text
█████
██
█████
    ██
█████
```

### 4.2 色彩和边框

保持终端默认背景，不主动设置背景色；通过少量语义色建立层级：

| Token | Rich style | 用途 |
|---|---|---|
| `accent` | `bright_cyan` | logo、prompt、当前 phase |
| `info` | `bright_blue` | model、workspace 等稳定信息 |
| `success` | `green` | ready、工具成功、任务完成 |
| `warning` | `yellow` | token 预算、partial、等待确认 |
| `danger` | `red` | 失败、拒绝、错误 |
| `muted` | `grey70` | 次要说明、路径、时间 |
| `border` | `grey35` | 外框和分隔线，避免整屏 cyan |

只使用一个外框包住一组相关信息；不堆叠卡片。首页和任务过程各自只有一个主 Panel，
内部用 Rule/空白分隔，而不是 panel 套 panel。

## 5. 首页规格

### 5.1 宽终端（>= 72 cells）

```text
╭─ █████  SYNAPSE                                      ● READY ─╮
│  workspace  D:\File\Synapse                 session  new       │
│  model      deepseek/deepseek-chat           plan     react     │
│  config     models.json                      tools    15        │
╰─────────────────────────────────────────────────────────────────╯

◆ synapse ›
  Enter 发送 · Esc+Enter 换行 · Tab 补全 · ↑↓/Ctrl+R 历史
```

### 5.2 中等终端（52-71 cells）

隐藏 logo 的大块区域，保留一行品牌 + 状态；信息改为单列两行：

```text
╭─ █████ SYNAPSE · ● READY ─────────────────────────────╮
│ workspace D:\File\Synapse                              │
│ model deepseek/deepseek-chat · plan react · session new │
╰─────────────────────────────────────────────────────────╯
```

### 5.3 窄终端（< 52 cells）

不画外框，避免边框和路径争夺空间；显示三行稳定信息：

```text
█████ SYNAPSE  READY
deepseek/deepseek-chat · react
D:\File\Synapse · new
```

实现要求：所有截断按 Rich `cell_len` 计算；路径使用 middle ellipsis；状态使用固定
短词 `READY / SETUP / RUNNING / PARTIAL / FAILED`，不显示 API key。

## 6. 任务过程面板规格

### 6.1 单一任务 Panel

```text
╭─ ● RUNNING · 调用模型       iter 03/50 ───────────────╮
│  tokens  in 1.2k · out 840 · total 2.0k       00:04  │
│  ━━━━━━━━━━━━━━━━░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░░  │
│                                                       │
│  生成中的 assistant 文本，最多保留可读的最近若干行… │
│                                                       │
│  RECENT TOOLS                                         │
│  ✓ read    src/app.py                         23ms    │
│  ✓ grep    provider                         118ms    │
│  ! shell   pytest                         1.2s       │
╰───────────────────────────────────────────────────────╯
```

### 6.2 固定信息层级

1. **Header**：状态点、短 phase、`iter current/max`。
2. **Stats**：input/output/total token、elapsed；token 未知时显示 `--`，不伪造 0。
3. **Progress**：有 max iteration 时按 iteration 绘制 20 格条；没有上限时不显示假进度条。
4. **Transcript**：保留当前 LLM 流文本，最多 6 行或 1600 display cells，超出显示尾部。
5. **RECENT TOOLS**：最多 5 条，固定列显示状态、工具名、参数摘要、耗时和文件数。
6. **Swarm**：仅在有 worker 时出现独立 `SWARM` 分隔行，不抢占普通 ReAct 面板。

### 6.3 Token 流式统计

沿用 `LLMToken.usage` 的现有语义和 `_LiveRun` 的 baseline reconciliation，不改变
provider 协议。UI 增加三个固定字段：

- `in`：累计输入 token
- `out`：累计输出 token
- `total`：两者相加

格式化规则：小于 1000 显示整数，1000 以上显示一位小数 `1.2k`，超过 1M 显示 `1.2M`。
每 50ms 至多触发一次 token redraw，后台时钟每 200ms 更新一次；统计更新不能阻塞
EventBus 或 assistant 文本追加。

## 7. 结果和命令输出

### 7.1 结果块

结果使用同一主 Panel 的收束态，不再额外打印一条孤立 Rule：

```text
╭─ ✓ TASK COMPLETE ────────────────────────────────────╮
│  任务输出 Markdown                                    │
├─ 1.8s · 2.0k tokens · tools 3/3 · session saved ─────┤
╰───────────────────────────────────────────────────────╯
```

`PARTIAL` 使用 yellow，`FAILED` 使用 red，并在底部给出单行可执行建议。plain 模式使用
同样的顺序：状态 -> 输出 -> metrics -> 下一步，不输出 ANSI 或装饰框。

### 7.2 Prompt 和命令

- prompt 改成 `◆ synapse ›`，`◆` 与 block logo 对应，输入内容保持默认终端颜色。
- `/memory`、`/score`、`/context-report` 统一使用 section heading + 无框 grid，减少
  不同命令之间的视觉跳变。
- `/model` 的当前项使用 `●`，不可用项使用 `·`，不要再依赖绿色/灰色长句解释。

## 8. 代码落地边界

### P0：首页重做

- 替换 `_WELCOME_ART` 为 block logo renderer，新增 `Logo`/header 的 display-cell 测试。
- 重写 `_show_welcome()` 的宽度分支：宽/中/窄三套布局，共用 readiness 数据。
- 更新 `_BRAND/_LABEL/_BORDER` 为语义 palette，prompt 和 bottom toolbar 同步。

### P1：Live 面板重做

- 扩展 `_LiveRun` 保存 `iteration/current_max`、`phase`、token 字段，不新增事件类型。
- 将 `_LiveDisplay._render()` 拆成 header/stats/transcript/tools 三个纯渲染段，最终仍返回
  一个 `Panel`，保留现有锁、刷新线程和 50ms coalesce。
- 工具 timeline 改成固定列，参数按 display cells 截断；token stats 显示 in/out/total。

### P1：结果和 plain fallback

- `_print_result()` 使用统一 status panel/grid；plain 输出顺序与 Rich 完全一致。
- 非 TTY 不调用 Live、不输出 Unicode 边框，保留 ASCII `-` 分隔线和可读字段。

### P2：测试与 QA

- 40/60/80 列截图式字符串测试，验证每行 cell width、不溢出、不出现旧 ASCII art。
- 模拟 10k token stream，检查 token 统计、尾部保留和刷新节流。
- 模拟 5 条工具记录、失败工具、partial、取消和无 iteration 上限。

## 9. 不做什么

- 不引入 Textual、Blessed 或新的 TUI 状态机。
- 不改变 EventBus/SSE 事件名称和 provider 的 token 计数协议。
- 不做全屏 dashboard、鼠标交互、可拖拽布局或 IDE diff 视图。
- 不把图标做成外部 PNG；终端 logo 必须是可控 display-cell 的代码原生色块。

## 10. 完成定义

- 首页没有旧的十行 ASCII brain，第一屏优先显示真实状态。
- 任务面板固定显示 phase、iteration、in/out/total token、elapsed 和最近工具。
- Rich、plain、Windows PowerShell、40 列窄终端均无重叠、无横向溢出。
- `pytest` 全量通过，新增渲染测试能防止旧样式回归。

## 11. 实施结果（2026-08-08）

本轮已按上述边界完成实现：

- `synapse/adapters/cli.py`：首页改为 7 行实心色块 logo（中屏使用 5 行 compact mark），按 92/52 列分档；图标、标签和值使用固定轨道，元数据、readiness、session 和 tools 数量不再动态挤压。
- 首页字段统一使用 `◆` 图标族；labels 使用 bright blue、values 使用 white、brand/prompt 使用 cyan、状态使用 green/yellow/red，外框统一使用 cyan，避免同一信息类别出现多种颜色。
- 输入区增加全宽 `INPUT` 框和多行 continuation 边界，Rich 与 prompt-toolkit 两条路径都能明确显示可输入范围。
- `_LiveDisplay` / `_LiveRun`：任务过程改为圆角 Panel，新增 phase、iteration/max、input/output/total token、elapsed、进度条和最近工具固定列时间线；保留既有 `EventBus`、刷新线程和 token baseline reconciliation。
- `_print_result`：统一为 `TASK COMPLETE/PARTIAL/FAILED` 收束面板，plain fallback 仍保持无 ANSI 的可读顺序。
- prompt 更新为 `◆ synapse ›`，未引入 Textual、新依赖或新的事件协议。
- 新增 40/60/80/100 列、block logo、字段起始列、token breakdown、iteration 和结果面板回归测试；为配置加载测试显式隔离用户级 `models.json`。

本轮追加错误韧性优化：`ReActPlanner` 对 401/403、`authentication_error`、invalid API key 和 permission denied 做 fail-fast，鉴权错误不再进行无意义的指数退避；超时和限流仍保留原有重试策略。

验证记录：`python -m pytest -q` 结果为 **410 passed, 1 skipped**；`python -m compileall -q synapse` 和 `git diff --check` 均通过。测试环境仍有既存的 Starlette/httpx deprecation warning，以及 Qdrant local mode 清理 `.lock` 的 Windows 资源警告，不属于本轮 UI 改动。

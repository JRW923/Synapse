# Synapse 使用体验调研与优化方案（2026-08-08）

## 1. 调研范围与结论

本次调研覆盖 CLI 首次启动、`run`/`chat`/无参 REPL、Rich Live 面板、EventBus 事件、HTTP `/run/stream`、会话恢复和相关回归测试。竞品交互参考：

- [Claude Code common workflows](https://code.claude.com/docs/en/common-workflows.md)：以任务工作区为首页，过程状态和 permission/approval 是主交互，支持中断与会话继续。
- [Aider usage](https://aider.chat/docs/usage.html)：命令行优先，持续显示 token/cost，`/undo`、session history 等命令服务于高频迭代。
- [Cline first task](https://docs.cline.bot/getting-started/your-first-task)：工具调用、审批和结果以任务时间线呈现，长输出可展开查看。
- [OpenHands overview](https://docs.all-hands.dev/usage/overview)：将 agent events/observations 作为统一过程流，前端不依赖模型日志文本猜状态。
- [Cursor Agent modes](https://docs.cursor.com/agent/modes)：把当前模式、权限和执行反馈放在工作区内，不用营销式 landing page 阻断首次任务。
- [Pi coding-agent](https://github.com/badlogic/pi-mono)：终端工作区、session、checkpoint/undo 与可扩展事件流保持简单组合。

**结论**：Synapse 已经有 `EventBus`、`AgentProgress`、`LLMToken`、`ToolCallStarted/Completed`、session 持久化和 Rich Live，基础能力不缺；主要缺陷在 adapters 层的状态编排，而不是再引入 UI 框架。

## 2. 当前用户旅程

1. `synapse` 无参启动：读取配置，缺 key 时进入向导，随后打印 ASCII 欢迎框。
2. 用户在 prompt-toolkit 输入任务；Enter 提交，Esc+Enter 换行，`/` 命令有补全。
3. 首次任务延迟创建 `Synapse`，Rich 面板订阅事件，模型 token 和工具状态在面板中刷新。
4. 任务结束后打印结果并保存 session；`Ctrl+C` 请求 planner 在迭代边界停止。
5. `synapse run` 和 `/run/stream` 分别复用部分同样的事件，但展示和错误收尾不完全一致。

## 3. 问题排序

### P0：影响首次成功和任务可控性

| 问题 | 证据 | 影响 |
|---|---|---|
| 首页是较大的 ASCII banner，ready 状态是静态文本 | `synapse/adapters/cli.py:1184-1195,1317-1410` | 关键信息（workspace、provider/model、session）在窄终端折行，状态不能代表配置是否可用 |
| REPL 与 `_run_task_streamed` 各维护一套事件订阅 | `synapse/adapters/cli.py:690-746,2226-2240` | 修一个状态或清理问题要改两处，容易产生体验漂移 |
| 工具过程只保留当前 label | `synapse/adapters/cli.py:567-580` | 用户看不到刚刚做过什么、耗时和失败上下文 |
| 取消后反馈依赖下一次迭代/异常 | `synapse/adapters/cli.py:106-136,2164-2182`；`synapse/modules/planning/react.py:361-368` | 长任务中用户不知道是否已收到 Ctrl+C，`run` 中断时 session 可能未保存 |
| `_main_interface` 强制 `Console(force_terminal=True)` | `synapse/adapters/cli.py:1928-1930` | 管道、CI、窄终端下可能出现 ANSI/折行问题，plain fallback 失效 |

### P1：影响长任务理解成本

| 问题 | 证据 | 影响 |
|---|---|---|
| phase/iteration 没有稳定展示，只把原始英文 message 放进标题 | `synapse/adapters/cli.py:528-566`；`synapse/modules/planning/react.py:341-456` | “thinking/calling_llm/token_budget”语义不统一，用户无法快速判断当前阶段 |
| Live 每个 token 都触发 Rich update | `synapse/adapters/cli.py:331-452` | 高频 token 流会增加渲染开销，影响流畅度 |
| 结果块缺少耗时/token/tool 摘要和 partial/failed 下一步 | `synapse/adapters/cli.py:1242-1267` | 任务完成后无法快速判断是否值得继续、从哪里排查 |

### P2：暂不值得加入

- 全屏 TUI、多栏可拖拽布局、任务历史浏览器：会引入新的状态和键盘模型，超出周末改造的投入产出比。
- 实时 diff、IDE 图形化面板：应由已有 `ide-adapter`/编辑器承担，CLI 只输出 artifact 摘要。
- 复杂动画和多套主题：不能解决任务可控性问题。

## 4. 方案

### 4.1 首页/首次启动

- 保留现有 Rich banner，但宽度 `>=72` 才显示 ASCII art；窄终端显示一行 compact header。
- 首页显示真实 readiness：`provider/model`、workspace、planning mode、config source、session（new 或恢复的短 ID）。不显示 API key 内容。
- `synapse`、`chat`、`run` 共用同一套 ready 状态和结果摘要；无 Rich 或 stdout 非 TTY 时使用纯文本，不输出 ANSI。
- 首次向导完成后重新加载配置，首页只提示“已就绪/仍需配置”，不重复展示 key。

### 4.2 过程显示

- 新增 CLI 内部 `_LiveRun`，统一构造/订阅/清理 `_LiveDisplay`，三种入口使用同一状态编排。
- 面板标题固定显示：`phase`、`iteration`（若有）、总 token、elapsed。
- phase 映射为稳定中文标签：分析任务、调用模型、执行工具、接近预算、完成、失败；模型原始 message 只作为必要细节。
- 面板保留当前 LLM 流文本，并追加最多 5 条最近工具 timeline：工具名、短参数、`ok/失败`、耗时；长参数和输出继续截断。
- token 刷新节流到约 50ms，后台时钟仍按 200ms 刷新，避免事件风暴导致 Rich 重绘过多。

### 4.3 流畅度、取消与收尾

- 取消信号收到后立即把面板标题切为“正在取消”，planner 在迭代边界返回 `partial`。
- `run` 和 REPL 在中断/partial/failed 后都保存 session，并在结果块给出可执行建议（继续输入、`--resume`、检查 provider/API key）。
- 结果块统一显示 status、耗时、token、工具成功率；不把 traceback 直接暴露给用户。
- SSE 保留现有事件契约；本次只统一 CLI 展示，不新增前端依赖或事件类型。

## 5. 实施清单与验收

| 优先级 | 文件 | 改动 | 工作量 | 验收标准 |
|---|---|---|---:|---|
| P0 | `synapse/adapters/cli.py` | `_LiveRun` 统一订阅；compact/narrow 首页；真实 readiness；非 TTY plain fallback；取消即时反馈与 session 保存 | 3-4h | 三入口无重复事件；40 列不横向溢出；Ctrl+C 后出现 partial 和 resume 提示 |
| P1 | `synapse/adapters/cli.py` | phase/iteration/footer、工具 timeline、渲染节流、结果 metrics/建议 | 3-4h | 10k token 模拟流不明显卡顿；面板可见最近工具状态；结果块包含 metrics |
| P1 | `tests/adapters/test_cli_run.py`、`test_cli_render.py` | 覆盖 timeline、phase、plain fallback、partial 建议、节流上限 | 1-2h | 新增回归测试通过 |
| P2 | `README_zh.md` | 更新真实 CLI 交互截图/录屏说明（不引入新功能） | 1h | 文档与命令一致 |

## 6. 不做什么

本轮不重写 Python runtime、不引入 Textual/Rich TUI 新框架、不增加服务端协议、不做全量 TS 重构。理由是当前瓶颈在状态呈现和清理一致性，额外框架会扩大维护面而不会改善 Agent loop 的正确性。

## 7. 完成定义

- 首页第一屏能回答“我在哪个 workspace、用哪个 model、session 是否可恢复、是否已 ready”。
- 任务执行中能回答“当前阶段、迭代次数、最后几个工具、耗时/token、是否收到取消”。
- 任务结束能回答“成功/部分完成/失败、消耗、下一步怎么做”。
- Rich、plain、Windows PowerShell、窄终端均有可读输出；相关测试和全量 `pytest` 通过。

## 8. 本次执行记录

已落地：

- `synapse/adapters/cli.py`：新增 `_LiveRun` 统一三入口的事件订阅和清理；面板加入 phase、iteration、最近 5 条工具 timeline、工具耗时/文件数；token 驱动刷新节流；Ctrl+C 即时显示“正在取消”；partial/failed 结果显示 metrics 和下一步。
- `synapse/adapters/cli.py`：首页根据 readiness 显示状态，宽度小于 72 列时隐藏 ASCII art 并改为单列；`Console.is_terminal` 控制 Rich，重定向输出自动使用 plain fallback；Ollama 不再错误要求 API key，但不会因此跳过当前 model 的首次配置向导。
- `README_zh.md`：补充窄终端、非 TTY、过程面板和结果收尾行为说明。
- `README.md`：同步英文 CLI 交互说明，避免双语文档对实际行为产生分歧。
- `tests/adapters/test_cli_render.py`、`tests/adapters/test_cli_run.py`：新增 timeline 上限、渲染节流、窄终端首页、取消反馈、Ollama readiness、phase/tool timeline 回归。

验证结果：

- `pytest -q`：`393 passed, 1 skipped, 1 warning`
- `python -m compileall -q synapse`：通过
- `git diff --check`：通过
- `python -m synapse --help` / `python -m synapse version`：通过

已知限制：

- 当前环境是 Windows，无法在本轮对 bubblewrap/Seatbelt 或真实 provider 长流做端到端 UI 测试；新增测试使用 EventBus 和 Rich StringIO 模拟流。
- Qdrant local mode 退出时偶发 `.lock` 清理 warning；FastAPI/Starlette 仍有 `httpx` deprecation warning，均与本次 CLI 改动无关。
- server SSE 事件契约保持不变，尚未新增可视化前端；后续若要做 P2 历史任务浏览，应先复用现有 session API。

## 9. 后续 UI v2 重设计

本文件中的“保留现有 Rich banner/ASCII art”是上一轮优化的历史记录，已由
[ux-redesign-2026-08-08.md](ux-redesign-2026-08-08.md) supersede。UI v2 改为实心色块
block logo、信息优先首页、统一任务 Panel、input/output/total token 统计和三档终端宽度
布局；Agent loop、EventBus 和 SSE 契约保持不变。

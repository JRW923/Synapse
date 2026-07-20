# 开发记录

## 2026-07-20  CLI 主界面美化与联网搜索优化

### 概览

本轮开发聚焦于 CLI 主界面的视觉与交互体验，以及 agent 联网搜索能力的内建化。所有改动保持向后兼容，未引入破坏性变更。

### 1. CLI 主界面美化

涉及文件：`synapse/adapters/cli.py`

- **品牌色统一**：抽出 `_BRAND / _LABEL / _BORDER / _HINT` 四个色板常量，整体改为蓝色系（`bright_cyan` / `cyan`），边框、图标、标签、艺术字、prompt 同色。
- **Banner 重排**：
  - 字段标签前加 ASCII 图标（`> WORKSPACE`、`* MODEL`、`# VERSION`、`@ PROVIDER`、`~ PLANNING`、`% config`），因 Windows GBK 终端不支持 Unicode 图标，限定 ASCII 字符集。
  - 两列行重写：左列右对齐到 `left_w`，间距统一 `gap=4`，标签列宽 12。
  - 边框改用 `Text` 渲染，避免颜色渗漏到内容。
- **中央 ASCII 大图重设计**：由线条版改为实心版，用 `#` 字符填充大脑主体，背景留白对比，更有"实心"质感。
- **`/help` 表格重排**：加标题行 `Commands` + 提示副标，表格去掉 `pad_edge`，命令列加 `no_wrap`，描述文字精简。
- **Prompts 优化**：
  - REPL 提示符改为 `synapse > `（更通透）。
  - 确认提示 `[A]llow` → `(a)llow`，提示文案与大小写均可的实际行为一致。
  - 首次运行向导使用统一品牌色常量。

### 2. 边框自适应终端字体缩放

涉及文件：`synapse/adapters/cli.py`

- `Console()` 不再锁定 `width`，每次渲染时让 Rich 自动检测当前终端宽度。
- REPL 主循环每轮检查 `console.width != _last_cols`，若字体缩放导致列数变化则重绘 banner。
- 修复了放大终端字体时边框排版混乱的问题。

### 3. `/` 命令自动补全

涉及文件：`synapse/adapters/cli.py`、`pyproject.toml`

- 新增 `_SLASH_COMMANDS` 元组按顺序声明所有命令（`/help`、`/memory`、`/session`、`/reset`、`/model`、`/provider`、`/mode`、`/tools`、`/exit`、`/quit`）。
- 用 `prompt_toolkit` 提供补全菜单：
  - 输入 `/` 显示全部命令。
  - 输入 `/m` 过滤显示 m 开头。
  - 最多显示 6 条（`_COMPLETION_LIMIT`）。
  - 右侧显示命令描述。
- 缺 `prompt_toolkit` 时自动回退到原 `console.input`，已在 `pyproject.toml` 加入 `prompt_toolkit>=3.0` 依赖。
- 修复了 `prompt_session.prompt()` 在 `asyncio.run` 内部触发 `RuntimeError: asyncio.run() cannot be called from a running event loop` 的问题——改用 `await prompt_session.prompt_async(...)`。

### 4. 动态显示 token 数与耗时

涉及文件：`synapse/adapters/cli.py`、`synapse/modules/planning/react.py`

- **token 计数**：
  - 在 `react.py` 每次 LLM 响应后额外发出 `phase="token_update"` 的 `AgentProgress` 事件，载荷 `tokens=A+B`。
  - CLI 的 `_on_progress` 解析该消息，把 `(X.Xk tok)` 附加到 spinner 文本末尾。
  - 工具开始/结束事件也带上当前 token 数。
- **耗时显示**：
  - 新增后台 `asyncio.Task` `_tick()`，每 0.5s 调用 `status.update(_render())` 刷新 spinner。
  - `_fmt_elapsed()` 计算从任务开始的耗时（`<60s` 显示 `Xs`，否则 `Xm YYs`）。
  - `_render()` 把 `label · tok · elapsed` 拼成一行 dim 文本。
  - 每次 token / 工具开始 / 工具完成事件更新 label 时也会立刻刷新。
  - 在 `finally` 里 `tick_task.cancel()` 并 `await` 它，保证任务结束时干净退出。
- 效果示例：`Working...  ·  1.2k tok  ·  7s` 会持续跳动秒数。

### 5. 确认提示大小写均可

涉及文件：`synapse/adapters/cli.py`

- `_make_confirm_callback` 原本就调用 `.lower()`，仅是把提示文案 `(A)llow / (D)eny / (Y)es to all` 改为 `(a)llow / (d)eny / (y)es to all`，避免误导用户以为只能大写。

### 6. 新增 WebSearchTool（联网搜索内建化）

涉及文件：`synapse/modules/tools/web_search.py`（新增）、`synapse/adapters/library.py`、`synapse/modules/planning/react.py`、`synapse/adapters/cli.py`

#### 背景

此前 agent 没有专门的联网搜索工具，只能通过 `shell` 工具写 Python 脚本或 `curl` 调用外部 API。每次搜索 LLM 都要：写脚本 → 执行 → 解析 → 抽取结果，啰嗦又慢。

#### 实现

- **新增 `WebSearchTool`**（`synapse/modules/tools/web_search.py`）：
  - 直接 POST 到 `https://html.duckduckgo.com/html/`，无 API key、无额外依赖（用已有的 httpx）。
  - 输入：`query`（必填）+ `max_results`（默认 5，最大 8）。
  - 输出格式化 markdown：每条 `标题 / URL / 摘要`。
  - 解析逻辑成对提取（title + url + snippet），避免广告导致三个列表错位。
  - 主动过滤 DuckDuckGo 广告（`duckduckgo.com/y.js`、`ad_domain=`）。
  - 自动解包 DDG 的 `uddg=` 重定向 URL 拿到真实地址。
  - `risk_level = EXTERNAL`，`requires_sandbox = False`。

- **默认注册**（`synapse/adapters/library.py`）：
  - 在 `_create_all_tools` 默认工具集里加入 `WebSearchTool`，不依赖 `enable_external_tools`，默认可用。
  - `/tools` 命令展示列表也加上了 `web_search`。

- **系统提示更新**（`synapse/modules/planning/react.py:408`）：
  - 旧：`"For web queries, prefer a single curl command over writing Python scripts."`
  - 新：`"For web search, call the 'web_search' tool with a query — do NOT write Python scripts or use curl to call search engines. Use 'web' only when you already have a specific URL to fetch."`

#### 效果

agent 遇到联网搜索任务时，LLM 直接调 `web_search(query="...")`，一次工具调用即可，不再需要写 py 脚本 + curl + 解析。实测搜索 `python asyncio tutorial` 返回 3 条干净结果（Real Python / Python 官方文档 / GeeksforGeeks），广告被正确过滤。

### 文件清单

| 文件 | 状态 | 说明 |
| --- | --- | --- |
| `synapse/adapters/cli.py` | 修改 | 界面美化、自适应、补全、token/耗时显示、确认提示 |
| `synapse/adapters/library.py` | 修改 | 注册 WebSearchTool |
| `synapse/modules/planning/react.py` | 修改 | token_update 事件、系统提示更新 |
| `synapse/modules/tools/web_search.py` | 新增 | DuckDuckGo 搜索工具 |
| `pyproject.toml` | 修改 | 新增 `prompt_toolkit>=3.0` 依赖 |
| `DEVELOPMENT.md` | 新增 | 本开发记录 |

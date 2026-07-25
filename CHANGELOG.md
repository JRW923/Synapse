# CHANGELOG

用户可见的改动记录于此。格式参照 [Keep a Changelog](https://keepachangelog.com/)，
文案默认中文，API / 命令 / 字段名保留英文。

## 2026-07-25

### 新增（UX 优化 L.1–L.5）

- **L.1 · 流式与进度**：`run` / `chat` 及 HTTP `/run/stream` 以实时面板/SSE 展示
  进度（工具调用、token、Agent 进度），不再静默阻塞；异常统一兜底为友好文案。
- **L.2 · Swarm 过程可视化**：CLI 实时面板与 SSE 展示 Swarm 生命周期
  （`worker_spawned` / `worker_completed` / `review_submitted` / `vote_cast` /
  `swarm_verified`），并行评审/验证循环不再黑盒。
- **L.3 · 确认提示补风险 + 修复非交互语义**：
  - 确认提示展示 `tool [risk] → path/command`，并用锁串行化并发 worker 的提示。
  - `run --yes`（CLI）与 `RunRequest.auto_approve`（server）显式放行需确认的操作。
  - 语义对齐：无确认回调且操作需确认时**自动拒绝**（与 `auth.py` docstring 一致），
    不再静默放行。
- **L.4 · 暴露运行时评分与过程质量 hint**：
  - 新增 `/score` 斜杠命令，展示 safety / process / quality / efficiency 四项评分
    及最新 `ProcessQualityScored.hint`。
  - server `/run` 与 `/run/stream` 的 `done` 事件新增 `run_score` 字段。
- **L.5 · 统一友好错误反馈**：
  - `SynapseError` 子类（ConfigError / ProviderError / ToolError / SandboxError /
    PlannerError）统一转成「原因 + 建议」中文文案；其余异常也只给消息、不泄露 traceback。
  - 危险命令拒绝原因点名**命中哪条模式**（如 `Command matches dangerous pattern: 'rm -rf'`）。

### 文档

- `README.md` / `README_zh.md` 补充 `/score`、`/context-report`、`run --yes`、HTTP API
  （`/run`、`/run/stream`、`auto_approve`、`run_score`）说明。
- 新增本 `CHANGELOG.md`。

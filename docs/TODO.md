# Synapse 待办与拓展规划

> 后续可通过读取本文件讨论和推进任意功能点。暂时不做的需求也记录在此，避免遗忘。

---

## 拓展方向

### A · MCP 协议支持

**状态**：待实现

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

**状态**：待实现

当前 Partitioner + Compactor 是 Phase 1 级别的简单实现（截断）。调研显示 token 浪费率极高（154:1 输入输出比）。

**要点**：
- LLM 驱动的智能摘要（Compactor 用 LLM 而非截断）
- 注意力热力图：追踪 LLM 实际使用上下文的部分，反馈优化
- RAG 评估：标注每个 ContextBlock 的实际引用率
- 动态预算分配：根据任务类型自动调整四区比例

**难度**：较高

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

**状态**：待实现

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

*最后更新：2026-07-16*

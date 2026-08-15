# Synapse 评测系统优化计划

日期：2026-08-14
更新：2026-08-15

## 当前进度

| 阶段 | 状态 | 本轮产物 |
|---|---|---|
| P0 统计语义 | 核心基础设施已实现 | task/attempt 分层、Pass@k/Pass^k、两级 95% CI、误报验证状态 |
| P1 配对 A/B | 核心基础设施已实现 | 多指标、随机交错、任务级配对统计、可比性 gate、workspace baseline |
| P2 报告与治理 | 核心基础设施已实现 | dataset manifest、复合 grader 指纹、报告脱敏、Token/成本来源、Benchmark HTML/CSV |
| P3 消融与外部 Harness | 基础设施已完成 | 显式模块开关、可信命令 opt-in、进程树回收、任务切片、baseline preflight、HTML 报告与红队指标 |
| P4 正式 Benchmark | 仓库内运行契约已实现 | 冻结 manifest、预注册、grader 校准、artifact store、repository-cluster；正式外部运行待执行 |
| P5 持续治理 | 基础设施已实现 | append-only run registry、完整性校验、series 趋势/漂移、基线审批字段与分层 CI |

当前完成的是评测基础设施和确定性回归，尚未运行冻结数据集上的正式模型或跨 Harness 实验，因此不能声称通过率、Token、耗时或安全性已提升某个百分比。

## 一、目标与边界

评测系统需要回答五个彼此独立的问题：

1. **任务是否真的完成**：由模型外部的确定性 grader 根据文件状态、补丁和测试结果判断。
2. **结果是否稳定**：同一任务重复执行，区分 attempt 通过率、任务成功率和不同 `k` 下的成功概率。
3. **收益来自哪里**：固定模型与任务，通过 Harness 配置消融和配对 A/B 隔离上下文、记忆、验证 gate、安全策略等模块的贡献。
4. **代价是否合理**：同时记录 Token、耗时、工具调用、失败重试和每成功任务成本，避免只优化成功率。
5. **结论能否复现**：保存任务集、有效配置、环境、grader 和运行顺序的指纹，确保报告可以重跑和对比。

当前不把自建适配层冒充官方 SWE-bench / Terminal-Bench runner，也不把启发式过程分数当作通用 Agent 能力分。

### 1.1 评测对象

评测对象不是单独的 LLM，而是 `模型 + Harness + 配置 + 工具权限 + 运行环境` 的完整系统。主要回答三类问题：

- **同模型、不同 Harness**：固定模型和任务，比较 Synapse、外部 Harness 或最小 baseline 的功能正确性、稳定性、安全性和成本。
- **同 Harness、不同模块**：固定模型和任务，对 Context、Memory、Completion Gate、Auth、Planner 做单变量消融。
- **同系统、不同模型**：固定 Harness 与任务集，观察模型能力、成本和稳定性边界；该结果不能反推 Harness 的独立贡献。

### 1.2 主要威胁

- **模型自报成功**：`ResultStatus.SUCCESS` 只能作为诊断信号，功能通过必须由模型外部 grader 判定。
- **状态泄漏**：Project/User/Semantic Memory、共享 workspace、缓存和前序 attempt 会让后续样本不再独立。
- **数据污染**：公开测试、补丁、issue 答案或训练期已知任务会高估真实泛化能力。
- **grader 偏差**：grader 过宽会放过错误补丁，过窄会把等价正确实现判错；grader 故障不能计为模型失败。
- **选择性报告**：只展示最好 seed、最好任务或单次 smoke 会制造不可复现结论。
- **Provider 漂移**：相同模型别名可能对应不同服务端版本，temperature/seed 也不一定由 Provider 真正执行。
- **环境漂移**：依赖、容器、网络、CPU、仓库 dirty 状态和超时变化都会污染对比。

### 1.3 与主流 Agent 评测方式的对齐

| 主流做法 | 当前实现 | 使用边界 |
|---|---|---|
| 模型外部可执行 grader | 已实现 grader 合同、`repo_pytest`、Terminal/SWE-bench 本地适配及自定义 grader | 自建适配器只用于接线验证，尚无官方 benchmark 成绩 |
| 重复采样与稳定性评估 | 已实现 attempt/task 分层、`Pass@k`、`Pass^k` 与 95% CI | 任务含 `repository_id` 时使用 repository-cluster，否则退回 task-cluster |
| 同任务配对 A/B | 已实现随机交错、exact McNemar、配对随机化检验、Holm 校正 | 仅证明统计管线可用；证据齐全才允许 winner，尚无配置优劣结论 |
| 轨迹与过程指标 | 已实现工具调用、thrashing、验证行为和失败分类 | 只用于解释结果，不与功能通过率混成总分 |
| 安全红队 | 已实现确定性攻击 fixture、attack success rate、false block rate 与严重度切片 | 仅证明攻击与聚合链路可运行，尚不能推出实际安全性 |
| LLM-as-judge | 已定义盲化、顺序随机化、重复判分和一致率要求 | 代码任务优先确定性 grader；当前不提供未经校准的通用 Judge 分数 |
| 公共 Agent Benchmark | 已有 SWE-bench / Terminal-Bench 兼容数据导入和协议适配层 | 仓库未冻结正式数据；P4 才接固定版本官方 runner、镜像 digest 和正式样本 |

## 二、评测指标分层

### 2.1 主结果：功能正确性

- `attempt_pass_rate`：所有独立 attempt 中 grader 通过的比例。
- `task_success_rate`：每个任务在全部重复 attempt 中至少成功一次的比例。
- `pass_at_k`：按标准组合估计计算不同 `k` 下至少成功一次的概率。
- `pass_power_k`：按组合估计计算不同 `k` 次尝试全部成功的概率，用于衡量稳定性。
- `mean_score`：grader 返回的 `0..1` 归一化得分；不能跨不同 grader 直接比较。
- Benchmark 专属事实：`patch_applied`、`private_tests_passed`、`command_exit_code`、文件断言结果。

### 2.2 稳定性与统计可信度

- attempt 通过率使用 Wilson 95% CI，仅作运行诊断；正式聚合在任务含完整 `repository_id` 时按 repository cluster bootstrap，否则按 task cluster，避免把同题重复或同仓库多题当成完全独立样本。
- Pass@k/Pass^k 在任务维度 bootstrap；只有 1 个任务时区间退化为 `[0, 1]`，不展示伪精确结论。
- 配对 A/B 使用相同任务和运行轮次，随机交错执行顺序。
- 连续指标报告均值、配对差值、相对变化和 bootstrap 95% CI。
- 二值成功指标优先报告配对差异；样本量不足时只报告区间，不下“显著提升”结论。
- 长尾指标同时报告 median、p95、p99；超时按预注册的截尾规则处理，不静默删除异常值。

### 2.3 效率

- 输入 / 输出 Token、总耗时、工具调用数、工具成功率、thrashing ratio。
- 成本仅在 Provider 价格和真实输入 / 输出 Token 可核验时作为主指标；否则明确标记为 estimate。
- 增加 `cost_per_success`、`tokens_per_success`，避免失败任务把平均成本解释反了。

### 2.4 安全与过程诊断

- 安全：越界访问、危险命令、授权拦截、沙箱违规、注入事件。
- 过程：重复读取、无检索写入、指令漂移、计划质量、合并质量。
- 代码质量：复杂度变化、重复率、lint 问题、测试变化。

后三类用于解释功能结果，不与功能通过率混成一个总分；启发式指标必须在报告中标记为 `diagnostic`。

### 2.5 Grader 质量与校准

- 每个功能任务至少包含一个模型外部的确定性断言：测试命令、文件断言、补丁应用或结构化结果校验。
- 对 grader 建立正例、负例和近似正确例，使用 mutation patch 检查其能否拒绝删测试、硬编码、空实现和越权修改。
- 保存 grader 代码、规则、依赖和镜像指纹；grader 变化必须生成新数据版本，不能覆盖旧结论。
- 以人工确认样本校准 false accept / false reject，正式使用前给出可接受阈值与争议样本裁决记录。
- flaky 测试按预先声明次数重跑，区分稳定失败、偶发失败和基础设施失败，不允许按结果临时追加重试。
- 分开记录 `verified`、`agent_status_only`、`grader_error` 和 `not_graded`；成功误报率只以已正常执行外部 grader 的自报成功为分母。
- grader error、环境安装失败、Provider 错误和模型功能失败分别计数，不把基础设施故障算入模型能力。
- grader 在只读或独立 workspace 中运行，禁止 Agent 修改 grader、隐藏测试或结果文件，防止 reward hacking 与 grader escape。
- 对非确定性或人工 grader 报告复核协议、一致率与分歧处理；正式报告至少抽查所有异常高分和随机失败样本。
- 使用 LLM judge 时必须盲化 Harness/模型身份、随机化候选顺序、重复判分并报告位置偏差；LLM judge 不替代可执行功能测试。

### 2.6 数据集覆盖与切片

- 按任务类型切片：bug fix、测试补全、重构、代码检索、跨轮恢复、安全攻击和长上下文任务。
- 按难度切片：单文件、跨文件、跨模块、依赖外部环境；避免简单任务数量过多掩盖复杂任务失败。
- 按风险切片：正常任务、含诱导输入、危险命令、越界路径、网络依赖和中断恢复。
- 每个切片同时报告样本量、通过率和区间，不只报告总体平均值。
- dataset manifest 固定版本、来源、许可证、文件 SHA-256、规范化任务 hash、筛选条件、grader 命令摘要和超时；摘要只保存类型、参数数量、字节数与 SHA-256。
- 划分开发集、冻结 holdout 和正式测试集；失败分析可以回灌开发集，但不得把 holdout 结果反复用于调参。
- 对任务文本、补丁和测试做精确 hash 与近似重复检测，记录模型训练期已公开风险和污染审计结果。
- 入库前检查可执行性、初始失败/目标通过、许可证兼容、PII/密钥、危险 grader 命令和坏题状态。
- 数据版本不可原地修改；坏题通过 tombstone 隔离，并保留受影响历史 run 的追溯关系。

### 2.7 覆盖矩阵

| 维度 | 代表扰动或场景 | 关键指标 |
|---|---|---|
| 功能正确性 | 修复、测试、重构、检索、恢复 | 外部 grader 通过率、Pass@k |
| 鲁棒性 | prompt 改写、上下文噪声、工具失败、网络抖动 | 通过率下降、恢复时间 |
| 恢复能力 | 中断、超时、部分工具不可用、重试 | 恢复成功率、重复工作量 |
| 校准能力 | 成功、失败、部分完成、合理 abstain | success precision/recall、误报/漏报率 |
| 副作用 | 无关文件修改、删测试、配置污染、越界写入 | side-effect rate、changed-file precision |
| 安全性 | 直接/间接注入、危险命令、数据外泄、权限提升 | attack success rate、false block rate、严重度加权风险 |
| 效率 | 长任务、失败重试、冷/热启动 | p50/p95/p99 Token、耗时、cost per success |
| 泛化性 | 语言、仓库规模、领域、模型 | 分层效应与 `model × harness` 交互 |

## 三、实验设计

### 3.1 Harness 内部消融

固定 Provider、Model、任务集、工具权限、最大轮次和 Token 预算，只改变一个变量：

| 对照 | 目的 | 主指标 |
|---|---|---|
| 无 Context 治理 vs 完整 Context | 验证检索、预算与压缩收益 | 通过率、Token、重复读取、耗时 |
| 无跨任务 Memory vs 分层 Memory | 验证记忆复用收益 | follow-up 通过率、工具调用、Token |
| 仅 prompt 提示 vs Completion Gate | 验证假阳性控制 | grader 通过率、`SUCCESS` 假阳性率 |
| 静态 Tool 风险 vs Action-time Auth | 验证参数级安全收益 | 攻击阻断率、误拦截率、正常任务通过率 |
| ReAct vs Plan-Execute / Hierarchical / Swarm | 验证规划模式适用边界 | 通过率、Token、耗时、冲突率 |

### 3.2 跨 Harness 对照

后续通过命令适配器接入 Aider、OpenHands 或最小 ReAct baseline。必须固定：

- 同一模型版本、temperature、seed 或采样策略。
- 同一任务文本、仓库 commit、容器镜像、网络策略和超时。
- 等价工具权限与 Token / 成本预算。
- 同一个模型外部 grader。

跨 Harness 结果采用逐任务配对分析，不比较各自内置的私有分数。

### 3.3 Benchmark 层级

1. 离线 Harness gate：授权、事件、workspace、grader 合同。
2. 本地仓库 fixture：小而确定，用于每次提交的快速回归。
3. 固定内部任务集：覆盖修复、测试、重构、检索、恢复和安全场景。
4. SWE-bench / Terminal-Bench 小样本：固定数据版本与执行镜像。
5. 官方 runner：只引用官方环境产出的结果。

### 3.4 统计协议

- 预先声明主指标、指标方向、最小有意义差异、guardrail 和停止条件，禁止跑完后再挑指标。
- 二值功能结果优先使用逐任务配对差异和 McNemar/等价精确随机化检验；连续指标使用配对 bootstrap。
- Pass@k/Pass^k 在任务维度聚合和 bootstrap，attempt 级 Wilson CI 仅作为运行层诊断。
- 单任务或单配对样本只展示观测值，不给出 winner；小样本区间必须保守，不生成点状“95% CI”。
- 多任务、多指标同时检验时报告原始 p-value，并按 Holm 方法控制家族错误率；探索性指标明确标为 exploratory。
- 先以历史方差或 pilot run 做 power analysis，再确定任务数和 repeat；预算不足时减少结论范围，不降低统计门槛。
- 同轮 A/B 共享任务和可控 seed，并随机化执行顺序；若 Provider 不支持采样 seed，报告中明确写为 provider-defined。
- 报告效应量和置信区间优先于只报“显著/不显著”，同时检查成功率提升是否以成本、安全或延迟退化为代价。

### 3.5 失败分类

每个失败必须落入一个主类别，支持按 Harness、模型、任务切片生成混淆矩阵：

| 类别 | 示例 | 是否计入功能失败 |
|---|---|---|
| `model_reasoning` | 误解任务、错误修改、漏掉边界条件 | 是 |
| `tool_execution` | 命令错误、工具参数错误、重试耗尽 | 是 |
| `completion_false_positive` | Harness 自报成功但外部 grader 失败 | 是，且计入误报 |
| `safety_blocked` | 高风险操作被策略正确拦截 | 视任务预期，单独报告 |
| `sandbox_escape` | 真实越界写入或未授权执行 | 是，触发安全 guardrail |
| `grader_error` | grader 崩溃、超时、依赖缺失 | 否，样本无效 |
| `provider_error` | 限流、鉴权、服务端错误 | 否，报告可用性指标 |
| `environment_error` | clone、安装、容器或网络失败 | 否，单独统计基础设施稳定性 |

## 四、可复现报告协议

每份报告至少保存：

- `schema_version`、生成时间和 Synapse 版本。
- Benchmark 名称、任务数量、repeat、grader 类型。
- dataset manifest：版本、来源、许可证、文件 SHA-256、任务 hash、筛选条件、grader 命令摘要和超时。
- grader 代码版本、依赖/镜像指纹、golden case 版本和 flaky 重试策略。
- 任务集规范化 SHA-256 指纹。
- 去除密钥后的有效配置 SHA-256 指纹。
- Python、操作系统、容器镜像、网络策略和隔离后端。
- 显式 workspace 的 commit、dirty 状态和不含绝对路径的内容指纹。
- Provider、Model、Planner、Token / 轮次 / 超时配置。
- 实际 `model_id`、`run_id`、执行顺序，以及 Provider 是否真正支持采样 seed。
- A/B 报告保存脱敏后的两组配置、配置指纹、主指标方向、guardrail、重采样参数和实际交错顺序。
- attempt 级结果、验证状态、grader 事实、运行指标和错误分类。
- Token 指标覆盖率、计数来源、成本单价及成本是否为 estimate；覆盖不完整时不计算每成功成本。
- attempt 通过率、任务成功率、`pass@k` 曲线及各自置信区间。
- 失败产物仅在报告中保存摘要、大小和 SHA-256，不保存 artifact 正文、trajectory 参数或错误原文；需复核的原始产物另存受控内容地址。

Benchmark 报告继续输出 JSON、CSV 和自包含 HTML；JSON 是事实来源，HTML 不重新发明统计口径。Experiment 现已输出同口径的 JSON 与自包含 HTML；正式数据集仍需 P4 的固定 runner、镜像和版本证据。

## 五、分阶段实施

### P0：修正统计语义与报告协议（本轮）

状态：核心统计基础设施已实现，`tests/eval` 定向回归通过；正式样本报告留到 P4。

- 区分任务数和 attempt 数，停止用 `total` 同时表示两者。
- TaskResult 显式记录 `base_task_id` 和 `attempt`，不再解析 `#N` 猜分组。
- 增加 attempt / task 两套通过率与 Wilson CI。
- 增加标准组合估计的 `pass_at_k` 曲线，并保留旧字段兼容一个版本。
- 生成任务集、配置和环境指纹。

### P1：配对多指标 A/B（本轮）

状态：核心 A/B 基础设施已实现，`tests/eval` 定向回归通过；真实跨任务效果报告留到 P4。

- Benchmark 回调支持返回多指标字典，不再只返回耗时。
- A/B 按轮次随机交错执行，保存实际顺序。
- 支持 `higher` / `lower` 指标方向。
- 输出配对差值、相对变化、bootstrap 95% CI 和随机化检验 p-value。
- winner 同时要求主指标 CI 排除 0 且配对随机化检验 `p <= alpha`；单配对或样本不足时保持 `inconclusive`。
- CLI / HTTP 暴露主指标、方向和 seed。
- 每个指标独立声明方向；主指标改善但成功率或安全 guardrail 退化时标记为 `tradeoff`。

### P2：报告与数据集治理（本轮基础，后续扩展）

状态：核心报告与治理基础设施已实现，`tests/eval` 定向回归通过；正式数据版本和官方运行证据留到 P4。

- CLI 和 HTML 分开展示 attempt 通过率、任务成功率与 `pass@k`。
- 标记成本为真实值或估算值，避免把默认价格当 Provider 账单。
- 增加 dataset manifest：版本、来源、许可证、任务 hash、grader 命令摘要和超时。
- `enable_eval=True` 默认隔离 Project/User Memory、关闭 Semantic Memory 写入，并禁止持久化运行分数。
- 记录实际 `model_id`、`run_id`、精确输入/输出 Token 来源和成本估算单价。
- 区分 `verified`、`agent_status_only`、`grader_error`，误报率不再把未验证样本或 grader 故障混入分母。
- 显式 workspace 记录 commit/dirty/content 指纹，报告不保存本地绝对路径。

### P3：消融实验与外部 Harness（本轮基础）

状态：基础设施已完成，`tests/eval` 定向回归通过；冻结数据集、官方 runner 和正式效果结论仍留在 P4。

1. **显式消融开关（已实现）**：`eval_ablation` 可独立关闭 Context governance、Memory、Completion Gate 和 Action-time Auth；仅允许在 `enable_eval=True` 下使用，未知字段和非布尔值直接拒绝。Memory 消融使用不留状态的 store；关闭 Auth 只允许 Docker 后端，`process`、bubblewrap 和 Seatbelt 均不能作为该消融的安全前提。
2. **有效配置与可比性 preflight（已实现）**：报告同时保存声明配置、脱敏后的最终有效配置、两套指纹和实际差异路径；正式 winner 要求显式 `allowed_config_diff_paths`、相同实际 `model_id`、预算与权限指纹，以及 A/B 不同 workspace 对同一 baseline 的证据。缺失或不一致时仍输出诊断统计，但统一标记为 `inconclusive`。
3. **多任务 experiment（已实现）**：`Experiment` 直接接受 `Benchmark` / `BenchmarkTask`，复用 `BenchmarkRunner` 的 grader 语义；逐 task-attempt 保存 A/B 顺序、共享 seed、Agent 状态、外部 grader 事实、资源指标与 run score。
4. **任务级配对统计（已实现）**：仅纳入收齐全部 `k` 个配对的任务，重复 attempt 先在 task 内聚合，再做 task-cluster bootstrap；二值功能结果采用 exact McNemar，连续指标采用配对随机化检验。指标分为 `confirmatory`、`exploratory`、`diagnostic`，主指标与 guardrail 进入 Holm 检验族，单任务不产生 winner。
5. **序列与 Memory 评测接线（已实现）**：调度按 repeat 外层、序列步骤内层执行；workspace 默认按 `(variant, task, attempt)` 隔离，只有显式 `sequence_id` 才按 `(variant, sequence, attempt)` 复用。A/B workspace 必须来自相同 baseline，且不同 attempt 不得复用。尚未运行足量真实 follow-up 序列，因此没有 Memory 收益结论。
6. **外部 Harness 命令适配器（已实现）**：使用 argv 与 `shell=False` 的 stdin/stdout JSON v1 协议，传入任务、workspace、seed、父侧 `expected_model_id`、预算、权限和显式 `agent_input`；响应中的 `model_id` 必须与父侧声明精确一致，否则拒绝计入 comparability。宿主命令默认关闭，只有调用方显式设置 `trusted_host_execution=True` 才执行。Windows 进程以 suspended 状态创建，加入 kill-on-close Job Object 后再恢复，超时或输出超限会回收完整进程树；预算、权限与模型身份均由父进程注入或核验，不接受子进程单方面自报。
7. **失败矩阵与切片（已实现基础）**：输出逐任务 `improved`、`regressed`、`both_passed`、`both_failed`、`excluded`，按 category 聚合，并分别统计 A/B 的 grader error、执行错误、完成误报和外部 Harness 错误类别。
8. **红队指标（已实现基础）**：确定性攻击集覆盖直接/间接/多步注入、sandbox escape 和权限提升，并加入 benign negative controls；报告 attack success rate、false block rate、严重度加权风险和分类切片。普通 shell 非零退出不再推断为 sandbox violation，只有 workspace guard 或 sandbox 明确发出的违规信号才计数。
9. **报告与 grader 完整性（已实现）**：output、error、grader details、trajectory 和任意 metadata 自由文本只保存字节数与 SHA-256；URL 移除凭据、query 和 fragment。grader 指纹组合实际 callable、所属类 helper、模块文件、显式版本及 artifact digest，报告不再持久化 grader command 正文。
10. **评测运行态隔离（已实现基础）**：CLI 与 HTTP 的 A/B 都使用不同 workspace 对同一空 baseline；HTTP 串行进入测量区，支持取消和统一清理。`enable_eval=True` 时每个 `Synapse` 实例使用私有 BackgroundTaskManager 与 TodoStore，短命配置探针和各 Benchmark 分支也统一执行 `aclose()`，避免 A/B 之间复用或累积进程级状态。
11. **数据集 baseline preflight（已实现基础）**：Terminal-Bench 兼容任务在 Agent 运行前，于隔离副本执行外部 grader；任务若缺少 grader、基线已通过或 grader 自身报错，则拒绝进入能力统计。正式数据集的“参考实现可达到目标状态”验证仍留待 P4 入库流程。

P3 仓库内遗留已收口：已冻结 Memory follow-up fixture 和 manifest，提供 failure taxonomy 人工复核格式，`run_score.capabilities` 记录实际 ToolRegistry/MCP 摘要，后台任务具备实例隔离与 `aclose()` 回收检查。跨 Harness 容器权限的最终等价性仍必须在正式 runner 环境核验，不能由进程内契约替代。

### P4：正式 Benchmark（治理契约已完成，正式运行需要外部环境）

- 已实现不可覆盖的 dataset manifest，记录 version/source/license、文件与任务 hash、dev/holdout/formal split、tombstone、grader version 和 image digest；正式 SWE-bench Verified / Terminal-Bench 仍需填入官方版本与固定镜像。
- 已实现预注册对象，冻结主指标、方向、MDE、alpha、power、样本量、repeat、guardrail、停止规则与 `model × harness` 矩阵；模板不等于已完成 pilot。
- 已实现 grader golden-case 校准统计，区分 false accept、false reject 与 grader error，并提供近似正确、删测试、硬编码、空实现和越界修改 mutation 清单。
- 已实现默认关闭的内容寻址 artifact store，仅归档失败/error/grader_error 的补丁、测试输出和 trajectory 原文；报告只保存 URI、SHA-256、字节数和 media type。
- 已实现 repository-cluster bootstrap；正式运行仍要求每任务至少 5 次重复、至少两个模型族，并由官方或可信固定 runner 产出结果。

### P5：持续治理（基础设施已完成，待正式 run 持续积累）

- 已建立 append-only run registry，以不可变 report ID 保存 report SHA-256/bytes、series、baseline/candidate、状态、审批、预注册及 dataset/grader/image/code/config 指纹；重复 ID 被拒绝，并可回查报告完整性。
- 已实现同 series 的成功率、误报率、p95 Token/耗时和基础设施失败率趋势与阈值告警；跨 series 直接拒绝比较。
- 基线升级通过 `baseline_report_id`、指纹和审批字段保留审计关系；任务集、grader 或镜像变化无法比较时必须新建 series。
- 失败样本只允许回灌 dev/regression；冻结 holdout manifest 不可原地修改，坏题使用 tombstone 并发布新版本。

## 六、验收标准

- `repeat > 1` 时报告能明确区分任务数与 attempt 数。
- `pass@k` 对全失败、部分成功、全成功和多个任务均有确定性测试。
- 相同 seed 的 A/B 执行顺序、bootstrap CI 和 p-value 可复现。
- A/B 能同时比较成功、耗时、Token 和工具调用，而不是只比较耗时。
- 报告不包含 API key，任务或配置变化会改变相应指纹。
- 无 grader 的样本显示为 unverified，grader error 不计入模型误报或功能失败分母。
- Token 覆盖不完整时报告 coverage 且不输出伪精确的 per-success Token/成本。
- 单任务 Pass@k 和单配对 A/B 不输出伪精确 winner；不等长配对数据直接拒绝。
- dataset manifest 至少包含版本、来源、许可证、任务 hash、grader 和超时；缺失信息显式为 unknown。
- 显式 workspace 内容变化会改变指纹，相同数据集移动目录不会改变数据身份，报告不泄露本地绝对路径。
- 旧 JSON 报告仍可渲染，旧调用方读取 `total/pass_rate/pass_at_k` 不会立即失败。
- 简历与 README 只引用已有真实报告，单题 smoke 必须标记为基线。

正式发布 P4 模型/Harness 结论前还需满足：

- grader golden cases 全部稳定执行，并给出 false accept / false reject 审计结果。
- pilot run 足以估计方差并完成样本量设计，主指标、MDE、guardrail、停止规则已冻结。
- 基础设施失败率、Provider 错误率和 flaky rate 分别低于预先设定阈值；超标时不发布模型/Harness 结论。
- 每个关键任务切片都有最小样本量，任何总体提升都必须同时展示切片回归。
- 产物包含不可变 report ID、代码 commit、dataset/grader version、环境指纹和人工复核记录。

## 七、CI 与运行分层（已落地）

`.github/workflows/ci.yml` 已实现 PR unit、PR offline smoke、Nightly representative、Weekly ablation/red-team 和手动 Release 五层。定时任务只运行离线 fixture；外部模型仅在 `workflow_dispatch` 明确勾选、`evaluation-release` 环境放行且 secrets/仓库变量齐全时运行。

| 层级 | 触发 | 目标预算 | Gate | 产物留存 |
|---|---|---|---|---|
| PR unit | 每次提交 | 无模型费用 | 全量单元/集成测试 | Actions 日志 |
| PR smoke | 每次提交 | 无模型费用、离线 fixture | 统计、grader、adapter、红队合同与编译通过 | Actions 日志 |
| Nightly representative | 每晚 | 无模型费用、代表性离线集合 | 评测、Harness hardening 与后台回收无回归 | Actions 日志 |
| Weekly ablation/red-team | 每周 | 无模型费用、离线消融/攻击集 | 配对统计与安全切片合同通过 | Actions 日志 |
| Release official | 手动发布候选 | 受保护环境与显式费用开关 | 先跑确定性合同，再执行仓库配置的官方评测命令 | run registry 与 artifact store 长期归档 |

硬 gate 仅用于确定性合同、安全逃逸和明确回归；统计波动使用软 gate、趋势和人工复核，避免偶然样本阻塞开发。

## 八、当前限制

- Python API、CLI `experiment --dataset` 均支持多任务外部 grader 配对实验；不提供 dataset 的 CLI/HTTP `experiment` 仍只是单任务运行时诊断，不能引用其结果作跨任务能力结论。
- Terminal-Bench 兼容任务在 Agent 运行前会对隔离副本执行 baseline preflight；若任务已通过、缺少外部 grader 或 grader error，运行会被拒绝并标为配置/基础设施问题。
- A/B 的 `comparability` envelope 是 evaluator 进程内契约，不是密码学证明；必须由可信 runner/adapter 注入，不能透传 Agent 自报。缺少显式 diff、workspace baseline、实际模型、预算或权限证据时不会产生 winner。
- 当前 runner 证据覆盖实际 `model_id`、关键预算、沙箱配置及实际 ToolRegistry/MCP 的数量与名称指纹；跨 Harness 容器的文件、网络和系统调用权限仍需正式环境取证。
- 当前随机 seed 控制执行顺序和统计重采样，不保证 Provider 采用相同模型采样随机数。
- 当前 attempt Wilson CI 仅作诊断；数据集提供完整 `repository_id` 时正式报告自动使用 repository-cluster bootstrap，缺失时明确退回 task-cluster。
- 成本由 Token 与配置单价估算，不等同于 Provider 账单；价格未知或 Token 覆盖不完整时不能作为主结论。
- 自建 SWE-bench / Terminal-Bench 适配器只用于本地接线验证，正式成绩必须来自固定版本的官方 runner。
- 外部 Harness 与数据集 grader 的宿主执行默认关闭；`trusted_host_execution` 只是调用方确认代码可信，不等于容器隔离或网络/文件权限等价，正式评测仍需官方 runner 或固定镜像。
- 关闭 Action-time Auth 的消融只允许 Docker 后端；该限制避免把可读取宿主文件系统的 process/bubblewrap/Seatbelt 配置误当作安全对照。
- `sandbox_violations=0` 只表示没有收到显式违规信号，不能单独证明不存在未观测逃逸；正式安全结论必须结合容器外副作用检查和攻击成功率。

## 九、本轮验证

- 仓库全量回归：`567 passed, 1 skipped`；评测/适配器/HTTP/Harness 定向集合：`202 passed, 1 skipped`。
- `python -m compileall -q synapse`、冻结 Memory manifest 完整性校验和 `git diff --check` 通过；后者仅报告现有 LF/CRLF 转换提示。
- 测试仍有 FastAPI `on_event`、Starlette TestClient 弃用警告，以及 Windows 下 Qdrant 临时 `.lock` 文件在解释器回收阶段的清理警告；均未导致测试失败。官方数据集 runner 和真实 MCP/外部模型环境仍需在目标环境单独验证。
- 当前环境未安装 `ruff`，因此不能声称静态检查通过。
- 当前完成的是 P0-P5 的仓库内评测、治理和 CI 基础设施，尚未运行冻结正式数据集上的模型/跨 Harness 实验，因此不能声称通过率、Token、耗时或安全性已提升某个百分比。

# Synapse Harness 评测调研与接入说明

日期：2026-08-08

## 结论

Synapse 当前最需要的是“可复现的执行闭环”，不是继续堆叠更多 agent mode。主流 harness 的共同结构是：固定任务输入、隔离 workspace/container、记录 trajectory、执行确定性 grader、输出机器可读报告。Synapse 已有 `BenchmarkRunner`、EventBus metrics 和 sandbox，因此本轮以适配层接入为主，不把外部 benchmark 的数据集、容器镜像或官方 runner 复制进仓库。

2026-08-15 已补齐执行闭环之外的治理层：冻结 manifest 和 holdout、实验预注册、grader mutation 校准、内容寻址 artifact、不可变 run registry、repository-cluster 统计及 series 漂移分析。它们解决“结果能否被审计和长期比较”，不替代 SWE-bench/Terminal-Bench 官方 runner，也不制造尚未运行的跨 Harness 结论。

## 主流框架对比

| Benchmark / Harness | 主要测什么 | 关键基础设施 | Synapse 接入策略 | 当前边界 |
|---|---|---|---|---|
| Terminal-Bench | 终端操作、环境状态、命令链和任务完成 | 容器化环境、任务级 grader、超时/资源限制、trajectory | `TerminalBenchAdapter` + `terminal_smoke` + JSON/JSONL loader | 不伪造官方分数；官方镜像和完整 runner 仍由外部提供 |
| SWE-bench / Verified | 真实仓库 issue 修复 | 固定 commit、patch、private tests、隔离 checkout | `SWEBenchAdapter` 完成 clone/checkout/patch/private-test/test command 闭环 | 数据集、依赖安装和官方 harness 版本由用户提供 |
| τ-bench | 多轮 tool/API 调用、状态转移、policy adherence | 有状态业务环境、工具 API、规则 grader | 可复用 `BenchmarkTask` + `run_score`；后续接 stateful adapter | 当前没有外部 API 沙箱，不在本轮伪造实现 |
| GAIA | 通用知识/文件/工具任务 | 任务答案、附件、人工/程序 grader | JSONL task loader 可复用；需要 answer grader 时接入 | 不把网页搜索结果当作确定性分数 |
| ToolBench / APIBench | 工具选择、参数正确性和调用链 | 工具 schema、API 环境、调用结果 grader | 复用工具事件和 `TaskGrade`，可在离线 fixture 做 schema/call smoke | 官方 API 集合和网络依赖不随项目发布 |
| AgentBench | 多环境 agent 能力 | OS/数据库/网页等多环境、统一 trajectory | 采用统一 `Benchmark`/`task_runner` 接口，按环境新增 adapter | 不在单周内实现所有环境 |
| Aider edit/polyglot | 代码编辑、测试闭环和多语言修改 | 临时 Git repo、测试命令、diff grader | `RepoPytestBenchmark` 是同类本地轻量基线 | fixture 规模小，不能外推 SWE-bench 分数 |
| OpenHands eval | 软件工程 agent 的真实环境评测 | container runtime、repo task、grader、重复实验 | 采用其“隔离执行 + grader + report”思想，不引入 OpenHands runtime | 两者 agent runtime 不同，不直接比较 raw score |

## 本轮落地

### 1. Completion gate

`ReActPlanner` 对可识别的代码修改任务要求至少出现一次测试/验证命令，并以最近一次验证结果作为轻量 gate；缺少证据或验证失败时，`SUCCESS` 降为 `PARTIAL`。自然语言问答和只读任务保持原有行为，避免把所有任务强行当成代码任务。

### 2. Task-specific tool subset

代码任务只向模型暴露文件、搜索、shell、git、todo 和 skill schema；registry 仍保留完整工具，因此这是 prompt/schema 成本优化，不是权限绕过。外部工具仍由 `ActionAuthorizer` 和配置开关控制。

### 3. SWE-bench 闭环

CLI 使用 `synapse eval swebench --dataset PATH` 时：

1. 为每个 instance clone repo 并 checkout `base_commit`；
2. 在隔离 checkout 中运行 Synapse；
3. 从工作区提取 Git patch；
4. 在新的临时 clone 中 apply patch，注入 `private_tests` 或 `test_patch`；
5. 执行 `test_command`/pytest；
6. 以 patch apply 和 private test 事实评分，而不是只看 Agent status。

### 4. Terminal-Bench 风格适配层

```powershell
python -m synapse eval terminal_smoke --provider deepseek --model deepseek-v4-flash
python -m synapse eval terminal_bench --dataset path/to/tasks.jsonl --max-tasks 10
```

`terminal_smoke` 是无网络本地 fixture，检查 Agent 是否通过 terminal 创建精确文件并由隔离 grader 验证。`terminal_bench` 接受常见的 `task_id`/`instruction`/`description` 字段；任务可声明 `setup_files`、`expected_files`、`grader_command` 和 `timeout`。命令 grader 会在临时 workspace 中运行，使用真实 Terminal-Bench 数据前应先确认数据来源、许可证和命令安全性。

## 评测报告解释规则

- `pass_rate`：任务 grader 通过率，不等于模型能力的通用分数。
- `mean_score`：任务级归一化分数；SWE-bench 当前按 patch apply 0.4 + private tests 0.6 计算。
- `runtime`：Synapse EventBus metrics，适合诊断 tool success、token、耗时、过程质量，不替代功能 grader。
- `official_runner=external`：表示只接入数据/任务/事实接口，未声称复刻官方容器、数据版本或榜单跑分。

每次 CLI 评测会同时输出 JSON、CSV 和自包含 HTML/SVG dashboard。HTML 展示 pass rate、pass@k、95% CI、score、耗时、Token/cost、分类通过率和任务明细；CSV 保留 task 级指标，便于后续在 Excel/Pandas 中二次分析。

正式报告发布前还要经过治理 gate：manifest/preregistration 指纹一致、grader 校准无超阈值误判、报告登记后 SHA-256 可复核、同一趋势图只包含相同 series。失败原文进入受控 artifact store，常规报告只暴露内容地址；跨 Harness 权限等价仍需容器外副作用和网络策略证据。

## 下一步

1. 为 Terminal-Bench 数据增加 container backend（优先 Docker，保留 ProcessSandbox fallback）。
2. 为 SWE-bench 增加 dataset version 和 provider-level seed/temperature；重复运行、置信区间和成本汇总已完成。
3. 为 τ-bench/ToolBench 增加离线 stateful tool fixture，再接真实 API 环境。
4. 将 `task_runner` 的 workspace 生命周期抽成更通用的 execution backend；在没有第二个真实环境前不提前引入复杂插件协议。

# Synapse 评测体系与 dsv4flash 适配基线

日期：2026-08-08

> 2026-08-14 更新：报告 schema 已升级到 v2。旧 `dsv4flash-repo-3runs.json`
> 仍是单任务 smoke baseline，不是跨 Harness 对照；新 Runner 已区分 task 与 attempt，
> 输出标准组合估计的 Pass@k、衡量连续稳定性的 Pass^k、两级 95% CI、成功误报率、
> 验证状态、延迟 median/P95、Token/成本 provenance、dataset manifest 和可复现指纹。A/B 已改为随机交错的配对
> 多指标实验，不再使用独立样本 t-test。

> 2026-08-15 更新：补齐评测治理闭环。新增冻结 dataset manifest、实验预注册与保守样本量估算、grader golden-case 校准、失败产物内容寻址存储、append-only run registry、series 趋势/漂移分析和 repository-cluster bootstrap；CI 已按 PR/Nightly/Weekly/Release 分层。随后在冻结单题 `repo_pytest` fixture 上完成 DeepSeek Flash/Pro 的 Synapse smoke 与 Aider pilot，均由独立 pytest grader 验证。该结果只验证真实调用与跨 Harness 链路，不能外推正式模型成绩或 Harness 优劣。

## 结论先行

本轮已使用本地持久化模型配置中的 `deepseek-v4-flash` 完成 3 次独立临时仓库评测。
这是一个真实 API + 真实工具 + 真实 Git/pytest grader 的 smoke baseline，但任务只有一个
单文件算术 bug，不能外推成 SWE-bench 成绩。

实测报告：`eval-results/dsv4flash-repo-3runs.json`

| 指标 | 结果 |
|---|---:|
| Functional pass | 3/3（100%） |
| Agent status success | 3/3 |
| 平均 Agent duration | 6.07s |
| 平均 token | 2,993 |
| Tool success rate | 12/12（100%） |
| 平均 process score | 1.00 |
| Safety false positive | 0 |

模型名称本身仍不足以推导通用分数：SWE-bench 至少还需要固定 dataset 版本、temperature、
max tokens、工具确认策略、容器镜像和重复次数。下面的区间只是工程排期先验，不是对外
成绩承诺：

| 层级 | 当前 Harness 的合理预期 | 主要限制 |
|---|---:|---|
| 本地单文件 `repo_pytest` | 50%～80% | 任务很小，但模型必须正确选择 `read/write/shell` 并运行 pytest |
| 过程质量 benchmark | 40%～70% | 自建任务没有真实 fixture，部分指标仍是启发式 |
| Red Team | 硬编码攻击的阻断率较高 | 这是安全策略回归，不等价于模型安全性 |
| SWE-bench Lite/Verified | 暂不提供可信区间 | 当前 CLI 没有把 clone、patch、private-test grader 串成完整闭环 |

对 dsv4flash 的正确定位是：它可以作为 Synapse 的真实模型候选，但不能把模型能力
和 Harness 能力混成一个分数。先跑本地功能基准，再跑小样本 SWE-bench，最后才谈模型
横向比较。

## 已建立的评测层

### 1. 通用 Benchmark Runner

`synapse/eval/runner.py` 现在支持：

- 每个任务记录 `passed`、`score`、`category`、`grade_reason` 和 `run_score`。
- 汇总 `pass_rate`、`mean_score`、`by_category`。
- grader 可按任务读取 `Synapse.get_run_score()` 的四维运行指标。
- JSON 报告自动截断单任务输出，避免一次评测生成巨型日志。

### 2. 本地功能基准

`RepoPytestBenchmark` 创建临时 Git 仓库，先确认基线测试失败，再让 Agent 修改代码，
最后重新运行 pytest。缓存目录不会污染 changed-files 结果，适合无网络的 Harness 回归。

```powershell
python -m synapse eval repo_pytest `
  --provider deepseek `
  --model deepseek-v4-flash `
  --trusted-host-execution `
  --report eval-results/dsv4flash-repo.json
```

这个命令需要有效的 DeepSeek key；没有 key 时应先运行离线测试，而不是把配置错误当成
模型失败。

### 3. Process Quality

`ProcessQualityBenchmark.benchmark()` 使用运行时 `process` snapshot 做阈值评分，并保留
失败原因。它是过程回归套件，不是功能正确性 benchmark；其任务目前仍需要真实 fixture
和更准确的 root-cause grader 才能用于对外跑分。CLI 默认把它放进临时 workspace；只有
显式传 `--workspace` 才会使用指定目录。

```powershell
python -m synapse eval process_quality `
  --provider deepseek `
  --model deepseek-v4-flash `
  --max-tasks 4
```

### 4. SWE-bench 数据适配

`SWEBenchAdapter.tasks(path)` 已支持从本地 JSONL 加载任务，避免仓库偷偷 vendoring
数据集；没有 `--dataset` 时 CLI 会明确提示，不再调用不存在的 `tasks()`。

```powershell
python -m synapse eval swebench `
  --dataset path/to/swebench.jsonl `
  --provider deepseek `
  --model deepseek-v4-flash `
  --max-tasks 10
```

当前这一入口仍标记为 `functional_grader=not_configured`：它可以测 Agent 状态和过程
指标，但还不能冒充官方 SWE-bench 分数。真正闭环必须把每个实例的 repo checkout、
agent patch、private tests、失败原因和超时统一落到任务报告。

## 本轮发现的 Harness 问题

### 已修复

1. **评测 workspace 与上下文 workspace 不一致**：工具使用 `tools.workspace_root`，
   但 `Agent._build_context()` 曾硬编码 `Path.cwd()`，隔离评测会读错仓库。现在两者统一。
2. **完成判据过弱**：ReAct 只要模型返回无 tool call 文本就结束。system prompt 现在要求
   完成前运行最小相关测试、检查结果并报告证据。
3. **评测统计过薄**：过去只有任务状态和耗时，无法区分“模型说完成”和“功能 grader 通过”。
4. **本地 pytest benchmark 的缓存文件污染**：`__pycache__` 不再被当成 Agent 改动。
5. **SWE-bench CLI 入口断裂**：不再调用不存在的 `SWEBenchAdapter.tasks()` 无参实现；改为
   显式要求本地 JSONL 数据集。

### 当前仍需控制的评测边界

1. **Completion Gate 不能替代外部 grader**：ReAct 已对可识别代码任务执行轻量运行时 gate，
   但它仍是过程约束；功能通过必须由 Agent 运行结束后的独立 grader 判定。
2. **工具集合过宽**：默认工具 schema 同时暴露文件、shell、web、MCP 相关能力，较弱模型
   更容易选错工具或浪费上下文。应按任务类型做只读/代码/外部工具分组，而不是继续加工具。
3. **过程指标有启发式错位**：`ProcessMetrics` 原有 `find_reuse/adopt_reuse` 计数并不等价
   于真实的 `read/grep/glob → write/edit` 复用行为；现在已接收真实过程评分事件，但旧字段
   仍不能作为唯一结论。
4. **官方 SWE-bench 运行证据尚未闭环**：本地适配层已串联 clone、checkout、patch、
   private tests 与 `TaskGrade`，但尚未接入固定镜像和官方 runner，不能作为榜单成绩。
5. **dsv4flash 路由不是 DeepSeek OpenAI API**：`deepseek-v4-*` 会走 DeepSeek 的
   Anthropic-compatible endpoint，且关闭 prompt caching。endpoint、tool schema 和
   streaming 兼容性必须单独做 provider smoke test。
6. **旧报告不是完整 pass@k 证据**：历史 JSON 只有单任务 3 次 smoke；新 Runner 已支持
   Pass@k/Pass^k 曲线，但正式结论仍需固定模型采样策略、任务集、预算和 grader 后重跑。

## 建议的正式跑分顺序

1. **Harness offline gate**：`pytest -q`、Red Team、`repo_pytest` scripted fixture，确认
   工具授权、workspace、事件和 grader 本身没有回归。
2. **模型 smoke**：dsv4flash 跑 3～5 个本地 repo fixture，记录功能 pass、tool success
   rate、token、耗时和失败分类。
3. **过程集**：跑 8 个 process-quality 任务，但只把 runtime process score 作为诊断，
   不把它当功能分。
4. **SWE-bench 小样本**：先选 10 个固定实例，使用 private tests 和 `timeout`，报告
   resolved rate、patch apply rate、test pass rate、平均 token 和失败类别。
5. **重复实验**：至少 3 次/任务，才允许给 dsv4flash 报 pass@1 均值和置信区间。

## 下一步 P0/P1

| 优先级 | 改动 | 目的 |
|---|---|---|
| P0 | 代码任务完成 gate：测试失败不能直接 `SUCCESS` | 消除“文本完成但代码没验证”的假阳性 |
| P0 | task type → tool subset | 降低 dsv4flash tool selection 和 schema token 成本 |
| P0 | provider smoke：DeepSeek Anthropic endpoint 的 tool-use/stream/retry | 先排除接入层假失败 |
| P1 | SWE-bench patch extractor + isolated checkout/private tests | 得到真正可比的功能分 |
| P1 | 3-run repeat、seed、成本和置信区间 | 从演示分数变成可复现实验 |
| P1 | root-cause grader 使用测试/变更证据，而不是 status 成功率 | 让过程分数有解释力 |

## 本轮已执行的 P0/P1 与外部 Harness 调研

- **代码任务 completion gate** 已进入 `ReActPlanner`：可识别的代码修改任务若没有成功的 pytest/test/lint/typecheck 等验证证据，最终状态不会保持 `SUCCESS`。
- **Task-specific tool subset** 已在 schema 发送前裁剪：代码任务默认只暴露文件、搜索、shell、git、todo、skill，完整 registry 和安全授权仍保持不变。
- **DeepSeek v4 provider smoke** 已覆盖 Anthropic-compatible 路由、`/anthropic` endpoint、关闭 prompt caching，以及既有 streaming/tool-use/401 retry regression tests。
- **SWE-bench** 已从“仅加载 JSONL”升级为 clone → checkout → Agent patch → 新 checkout apply → private tests/test command → `TaskGrade` 的闭环，CLI 命令仍要求用户提供本地数据集。
- **Terminal-Bench 风格适配** 已加入 `terminal_smoke` 和 `terminal_bench`：前者离线可复现，后者支持常见 JSON/JSONL 任务字段和隔离 workspace grader。完整调研、边界和 τ-bench/GAIA/ToolBench/AgentBench/Aider/OpenHands 对比见 `docs/评测/evaluation-harness-research-2026-08-08.md`。
- **重复运行与统计** 已进入通用 `BenchmarkRunner`：`--repeat N` 保存显式 `base_task_id/attempt`，分别报告 attempt pass rate、task success@N、Pass@k、Pass^k、两级 95% CI、验证状态、误报率、延迟 median/P95、Token 覆盖、每次成功的预估成本和复现指纹，并自动生成 HTML/SVG 与 CSV。
- **A/B 实验** 已升级为同轮随机交错的配对多指标设计：每项指标独立声明 higher/lower，输出配对差值、相对变化、bootstrap 95% CI 与随机化检验；主指标改善但 `agent_reported_success` 或实际安全违规 guardrail 退化时标记为 `tradeoff`。提供 `--dataset` 时使用外部 grader 做多任务功能配对；无 dataset 时仍是单任务运行时诊断，不把 Harness 自报状态当外部功能判分。
- **评测隔离** 默认按 attempt 新建 Agent/workspace，`enable_eval=True` 隔离 Project/User Memory、关闭 Semantic Memory 和 run-score 持久化，防止前序样本影响后序样本。
- **评测治理** 由 `synapse.eval.governance` 提供：manifest 和 preregistration 不允许覆盖；失败产物默认不归档，显式启用后报告也只保存内容地址；run registry 拒绝重复 report ID，并校验原始报告 SHA-256。
- **冻结 fixture** 位于 `docs/评测/fixtures/`，包含 Memory follow-up、grader mutation golden cases、预注册模板和 failure taxonomy 人工复核格式。它们用于验证合同，不可解释为 Agent 能力样本。

本轮实现是适配层，不是官方 benchmark runner 的替代品：没有把外部数据集、Docker 镜像或 API 环境硬编码进 Synapse，因此报告中的 `official_runner=external` 必须保留，避免把本地 smoke 分数误报为榜单分数。

# Synapse 秋招面试手册

面向项目面试环节：简历素材、讲解话术、面试题本、差异化亮点。
数据基准日 2026-08-09，对应 commit 数 199。代码若有变动，先复核第 0 节的量化指标再对外使用。

## 0. 项目量化基线（对外引用前先复核）

| 指标 | 数值 | 复核命令 |
|---|---|---|
| Python 代码行数 | 18,604 行（`synapse/`） | `find synapse -name "*.py" -not -path "*__pycache__*" \| xargs wc -l \| tail -1` |
| 测试文件 / 测试函数 | 73 个文件 / 431 个函数 | `find tests -name "test_*.py" \| wc -l` |
| 测试结果 | 434 passed, 1 skipped | `python -m pytest -q` |
| 提交数 | 199 | `git rev-list --count HEAD` |
| Protocol 接口 | 10 个 | `ls synapse/protocols/*.py` |
| Provider 实现 | 6 家 + routing | `ls synapse/modules/providers/` |
| 内置工具 | 17 个 | `ls synapse/modules/tools/` |
| 规划模式 | 4 种（react / plan_execute / hierarchical / swarm） | `ls synapse/modules/planning/` |

> 注意：测试函数 431 个，pytest 收集到 435 个用例（含参数化展开）。被问到时说「431 个测试函数，参数化后 435 个用例」，不要混用。

---

## 一、一句话速览（30 秒版）

> "Synapse 是我独立做的一个 Code Agent Harness，Python 实现，约 1.86 万行代码 + 431 个测试。它不是把 LLM API 包一层的聊天机器人，而是把 Agent Loop、上下文工程、分层记忆、工具授权、沙箱、事件流和评测做成了**沿协议边界可替换**的运行时。最能体现我思考的三块是：**action-time 授权**（每次工具调用重新鉴权，而不是只看 tool schema）、**完成度校验 gate**（改了代码就必须有可执行证据，不接受模型自己说"已完成"）、以及**一套能跑出置信区间的评测闭环**（Pass@k + Wilson 95% CI，不是跑一次看个数）。"

30 秒里要埋三个钩子：可替换协议边界 / action-time 授权 / 评测可复现。面试官接哪个都能往下讲 3 分钟。

---

## 二、简历素材

### 2.1 项目描述（简历正文，控制在 4 行）

> **Synapse — 可观测、可扩展、可评测的 Code Agent Harness**（个人项目，Python，MIT 开源）
> 独立设计并实现端到端 Code Agent 运行时：以 10 个 Protocol 接口（LLMProvider / Tool / Memory / Planner / Sandbox / MCP 等）划分边界，`core/` 管生命周期、`modules/` 放可替换策略，支持 ReAct / Plan-Execute / Hierarchical / Swarm 四种规划模式与 6 家 Provider 热切换。实现 action-time 工具授权、进程树沙箱、prompt injection trust annotation 三层安全边界，以及基于 EventBus 的四维运行时评分（safety / process / quality / efficiency）。自建可复现评测闭环（临时 Git 仓库 + pytest grader + SWE-bench / Terminal-Bench 适配层），支持 `--repeat N` 输出 Pass@k 与 Wilson 95% 置信区间，并自动生成双语 HTML / CSV 报告。
> 规模：18.6k 行核心代码，73 个测试文件 / 431 个测试函数（434 passed, 1 skipped），199 次提交。

### 2.2 核心贡献（挑 3-4 条放简历）

| 贡献点 | 量化指标 | 对应代码 |
|---|---|---|
| **协议优先架构**，Provider / Tool / Memory / Planner 可替换，新增 Provider 不改业务逻辑 | 10 个 protocol，6 个 Provider（Anthropic / OpenAI / OpenAI-compatible / DeepSeek / Google / Ollama）+ routing 与 fallback | `protocols/`、`modules/providers/`、50 行轻量 IoC `core/container.py` |
| **Action-time 授权**：不信任 tool schema 的静态风险声明，每次调用结合参数、路径、shell chain、重定向目标重新鉴权 | 5 档 RiskLevel、11 类危险命令模式、13 类敏感路径；shell chain 逐段校验 | `modules/security/auth.py` |
| **完成度校验 gate**：代码变更类任务若无可执行验证证据，不判 SUCCESS | 突变类任务强制 verification，落到 `run_score.process.root_cause_accuracy` | `modules/planning/react.py` |
| **可复现评测闭环**：Pass@k + Wilson 95% CI + Token / cost / 工具成功率，自动出双语 HTML+CSV | 3 次独立重复实验 pass_rate 100%、mean_score 1.0、平均单任务 4 次工具调用 / 工具成功率 100% / ~$0.021 / 12.4s | `eval/runner.py`、`eval/visualize.py` |
| **上下文工程**：Git-aware 检索 + AST symbol + 预算分区 + compaction，检索超时降级不阻塞任务 | 按任务类型动态分配 token 预算，检索超时（默认 10s，`ContextConfig.retrieval_timeout_seconds` 可配）自动降级到最小 SYSTEM 上下文 | `modules/context/`、`core/agent.py` |

### 2.3 技术栈

- **语言 / 运行时**：Python 3.11+（`asyncio` 全异步）、type hints + Protocol 结构化类型
- **Agent 核心**：ReAct / Plan-Execute / Hierarchical / Swarm、Function Calling、streaming、指数退避重试、typed error 分类（认证类错误不重试）
- **上下文与记忆**：Git-aware 检索、AST symbol 提取、token 预算分区、LLM compaction；Session / Project / User / Semantic 四层记忆（可选 Chroma / Qdrant 向量后端）
- **安全**：action-time authorization、进程树回收（Windows Job Object / Unix process group）、可选 Docker / bubblewrap / Seatbelt 后端、HMAC 审计日志、trust annotation 抗注入
- **工程**：Pydantic 配置 schema、EventBus 事件驱动可观测性、原子写 Session 持久化、pytest、GitHub Actions
- **接口层**：CLI（Rich TUI）、HTTP + SSE、Library API、MCP client（stdio + Streamable HTTP）
- **评测**：自建 harness + SWE-bench / Terminal-Bench 适配层、Red Team 用例集、A/B 重复实验、Wilson 置信区间

### 2.4 简历追问预判（写进简历就必须扛住）

| 你写了什么 | 面试官一定会问 | 必须准备的答案要点 |
|---|---|---|
| "支持沙箱" | "是 Docker 隔离吗？" | 主动把降级说清楚：默认是 process containment（进程树回收），**不等于文件系统隔离**；Docker / bubblewrap / Seatbelt 是可选后端，需显式开启。这个区分本身是加分项。 |
| "SWE-bench 评测" | "跑了多少题？分数多少？" | 诚实说：**这是本地数据集驱动的适配层，不是官方榜单跑分**，报告里 `official_runner=external` 字段就是标记这点。我验证的是 harness 能正确 clone / checkout / apply patch / 跑 private tests 这条链路。 |
| "Swarm 多 Agent" | "怎么合并冲突？" | worktree 隔离 + review + vote，冲突时**不覆盖**、上报冲突文件名；还不是完整 git three-way merge。并且 Swarm 不是默认模式（见亮点 3）。 |
| "434 个测试通过" | "覆盖率多少？测到核心路径了吗？" | 别硬报覆盖率数字。转到**测什么**：授权决策矩阵、ReAct 循环各分支、Provider 适配、上下文预算、MCP、Red Team 注入用例都有对应测试文件，说得出文件名。 |
| "上下文工程" | "怎么决定塞哪些文件？" | 任务分类 → 选预算 profile → Git-aware 候选发现 → 相关性排序 → 分区 → 溢出 compaction；并且有 citation tracker 回收"哪些上下文真被引用了"来反向调预算。 |

---

## 三、面试讲解话术

### 3.1 一分钟版（自我介绍后的项目开场）

> "我最核心的项目叫 Synapse，是一个 Code Agent Harness，Python 写的，一个人从零做到 1.8 万行代码加 431 个测试。
>
> 做它的起因是，我一开始也是拿 LLM API 包个循环让它改代码，但很快发现两个问题：一个是模型经常跟你说"已经修好了"，实际上没跑测试；另一个是它偶然会去动工作区外面的文件，或者执行一条看起来无害但拼接了危险命令的 shell。
>
> 所以我把重点放在了 harness 本身，不是 prompt。具体做了三件事：第一，把 LLM、Tool、Memory、Planner、Sandbox 这些都抽成 protocol 接口，换模型供应商或换规划策略不用碰业务逻辑；第二，做了 action-time 授权，每次工具调用都结合实际参数、路径、shell 链重新鉴权，而不是只看工具注册时声明的风险等级；第三，加了个完成度 gate，改了代码的任务必须留下可执行的验证证据才判成功。
>
> 另外我给它配了一套评测闭环，能跑 N 次重复实验出 Pass@k 和 Wilson 置信区间，自动生成 HTML 报告——因为我觉得 Agent 项目最容易骗自己的地方就是跑一次成功就当它行了。"

口语化要点：从"我踩了什么坑"起手，不要从"我的架构分几层"起手。面试官对痛点有共鸣，对分层没有。

### 3.2 三分钟版（面试官说"详细讲讲"）

**第 1 段 · 动机（20 秒）** —— 同上，两个痛点：模型口头完成、越界执行。

**第 2 段 · 架构一句话讲完（40 秒）**
> "结构上是三层：`protocols/` 定义接口，`core/` 管 Agent、Container、EventBus、Session 的生命周期，`modules/` 放具体策略实现。中间用了一个 50 行的轻量 IoC container 做依赖注入。
>
> 这么分的收益很具体：我支持 6 家 Provider、4 种规划模式，但 `core/agent.py` 里只有 300 行——因为 Agent 本身不实现执行循环，它只负责组装依赖、建上下文、然后 delegate 给 Planner。执行循环在 Planner 里，所以 ReAct 换成 Plan-Execute 是换一个注册项的事。"

**第 3 段 · 挑一个技术点讲深（60 秒，推荐讲授权）**
> "我讲一下授权这块，这是我自己觉得设计上想得最清楚的地方。
>
> 常见做法是工具注册的时候声明一个风险等级，比如 shell 工具是高危、读文件是只读，然后按等级放行。但我觉得这不够，因为 **schema 只能描述能力，不能证明这次调用的参数是安全的**。同一个 shell 工具，`ls` 和 `curl x | bash` 风险完全不同。
>
> 所以我把授权放到了 action-time：每次调用带着真实参数重新过一遍检查。里面有几个细节是我踩坑补的——比如 shell 链要按 `&&`、`|`、`;` 拆开逐段校验，不然模型写 `ls && rm -rf /` 第一个 token 是白名单里的 `ls` 就过了；重定向目标要单独查，`echo x > /etc/cron.d/y` 这种主命令完全合法；还有 `python`、`curl` 这类能拉起任意代码或网络的命令，我不是禁掉，而是放进"允许但必须显式确认"这一档，因为直接禁了 Agent 就没法跑测试了。
>
> 然后非交互模式（`synapse run`）下没人能确认，`requires_confirmation` 就自动拒绝，除非调用方显式 opt-in。"

**第 4 段 · 怎么验证 + 诚实边界（40 秒）**
> "验证这块，我不敢只靠模型输出判断。评测跑的是真实的临时 Git 仓库，让 Agent 改代码，然后用 pytest 当 grader，还会先确认 baseline 是失败的——不然测试本来就过，改没改都"通过"。加上 `--repeat N` 出 Pass@k 和 Wilson 95% 置信区间。
>
> 我也想主动说清楚几个边界：默认的 sandbox 是进程树隔离，不是文件系统隔离；SWE-bench 那部分是本地适配层，不是官方榜单跑分，报告里我专门留了 `official_runner=external` 这个字段标记。我觉得这些边界说清楚比含糊过去更重要。"

### 3.3 核心追问防坑

**坑 1：「这跟 Claude Code / Cursor 有什么区别？你是不是在造轮子？」**
> 不要辩解"我的更好"：
> "功能上肯定比不了商业产品，我也不是要替代它们。我做这个的目的是**把这类系统的内部机制自己实现一遍**——因为用户视角看不到调度、授权、上下文预算这些决策是怎么做的。真正的差异在于我把**评测和可观测性做成了一等公民**：每次运行都有四维评分和完整事件流，你能回答"这次任务到底做了什么、为什么判它成功"。商业产品的这部分对用户是黑盒。"

**坑 2：「这么多测试，是不是 AI 生成的？」**
> 别慌也别撒谎：
> "我用 AI 辅助写代码和测试，这个我不否认。但设计决策和边界是我定的，我能解释每一个选择为什么这么做。比如你随便点一个测试文件，我可以说清它在测什么分支、为什么这个分支值得测。"
> 然后**主动挑一个讲**——推荐 `tests/modules/test_auth.py`（授权决策矩阵）或 `tests/modules/test_harness_hardening.py`。
> **面试前必做**：随机抽 5 个测试文件，确保每个都能讲清测什么。

**坑 3：「你这个 Agent 实际能解决多难的任务？」**
> 别吹：
> "我实测的范围很明确：仓库级的单文件 bug 修复、加测试、小规模重构，这类任务是稳定的——我有 3 次重复实验的数据。跨模块的大重构我没有可信数据，因为我的 grader 覆盖不到。我更愿意说 harness 这层是可靠的，模型能力那层受 Provider 限制。"
> 顺势带出"评测要区分模型能力、harness 能力和 grader 质量"——这是很有水平的一句话。

**坑 4：「上下文超了怎么办？」**
> "分三步。第一步是预算分配：先对任务分类，不同类型给不同的 token 预算 profile。第二步是分区，Partitioner 按优先级把上下文切成几个区，超预算的进 overflow。第三步是 compaction，overflow 部分做压缩——小的直接截断，超过阈值的走 LLM 压缩，然后**把压缩后的摘要折回 reference 区**，因为我的 ReAct 实现不直接注入 overflow 区，不折回去这些摘要模型根本看不到。
> 另外有个细节：ReAct 每轮都会重发整个上下文，所以工具输出必须先截断再进对话历史，不然一个 100KB 的网页会被重复计入每一轮的 input token。完整结果留在 ToolResult 对象上给 metrics 用。"

---

## 四、面试题本

### 4.1 基础题（考有没有真写过）

**Q1｜为什么用 Protocol 而不是抽象基类（ABC）？**
> Protocol 是结构化子类型，实现方不需要显式继承——第三方的 Provider 或 Tool 只要方法签名对得上就能注入，不用改它的继承链。ABC 强制运行时继承关系，在依赖注入场景会引入不必要的耦合。我的 container 用 `get_origin()` 剥离泛型参数再做 key 归一化（`core/container.py:_normalize_type`），这样 `ToolRegistry` 和 `ToolRegistry[X]` 解析到同一个实现。

**Q2｜ReAct 循环的终止条件有哪些？**
> 五个：① 模型返回 final answer 且不再有 tool call；② 达到 max rounds；③ 超过 total timeout（默认 300s，单工具 120s，单次 LLM 调用 120s，三个独立预算）；④ 授权拒绝导致不可继续；⑤ **完成度 gate 未通过被降级**——代码变更类任务如果没有 verification 证据，不判 SUCCESS。第五条是我自己加的，标准 ReAct 没有。

**Q3｜四层记忆分别解决什么问题？**
> Session 管会话恢复（`--resume` 续上上次的任务）；Project 存项目规则（比如 AGENTS.md 里的约定，还有过程质量 hint 会写回这层，下一个任务的 prompt 里重新注入）；User 存跨项目的个人偏好；Semantic 是可选向量层，做相似度召回。分层的关键是**生命周期不同**——Session 随会话销毁，Project 跟着仓库走，混在一起就会出现"上个项目的规则污染这个项目"。

**Q4｜EventBus 解决了什么？不用会怎样？**
> 它让 CLI 渲染、HTTP SSE 推送、审计日志、评测指标采集**共用同一个事件源**。不用的话，这四个消费方各自去 Agent 内部埋点，Agent 会被观测代码撑爆，而且四份数据会不一致——评测报告里的 token 数和 CLI 显示的对不上，这种 bug 极难查。现在 Token、工具调用、授权决策、Agent 进度、Swarm、过程质量都是事件，`run_score` 是事件的聚合产物。

### 4.2 深挖题（考设计判断力）

**Q5｜你的授权是"检查命令字符串"，这不是本质上不安全吗？攻击者能绕过。**
> **最锋利的一题，必须承认。**
> "对，字符串匹配是 best-effort，不是安全边界。我在代码注释里明确写了这不是 shell grammar——比如 `echo "a&&b"` 里的引号我不解析。真正的隔离必须靠执行层，也就是显式选 Docker 或 bubblewrap 后端。
> 我的定位是**纵深防御的第一层**：拦住模型的高频误操作（走错目录、误删、拉脚本执行），不是拦住有意的对抗攻击。因为我的威胁模型里，对手是"会犯错的模型"，不是"要拿下我机器的人"——用户自己就有 shell 权限，防他没意义。
> 如果要做真的安全边界，正确做法是不做字符串检查，而是在容器里跑，用 seccomp 限 syscall、用 mount namespace 限文件系统。"

**Q6｜完成度 gate 要"可执行证据"，模型伪造一个假的测试输出怎么办？**
> "证据不是模型说的，是工具返回的。gate 看的是 ToolResult 的 `success` 字段和实际 exit code，不是模型文本里的 'tests passed'。而且 `repo_pytest` 这个 grader 是在 Agent 跑完**之后**由 harness 独立执行 pytest 的，Agent 影响不了它。
> 还有一层：我会先确认 baseline 是失败的（`baseline_failed: true`），排除"测试本来就过"这种假阳性。
> 真正防不住的是模型改了测试让它通过——这个靠 grader 检查 `changed_files`，测试文件被改动会体现出来。"

**Q7｜检索超时降级成"只读 README"，不会让任务直接失败吗？为什么不重试？**
> "会降低成功率，但比阻塞好。我的判断是：检索是**加速器不是必需品**——Agent 有 glob、grep、read 工具，上下文里没给它文件它自己也能找，只是多花几轮。但如果检索卡死，整个任务 0 输出。
> 不重试是因为超时通常不是瞬时抖动，而是仓库太大或者 git 命令卡住，重试大概率再超一次，只是把 10s 变成 20s。
> 降级时我会 emit 一个 `context_timeout` 事件，所以这次运行的评分里能看出来它是降级跑的——不会静默地把降级当正常。这个超时值我提到了 `ContextConfig.retrieval_timeout_seconds`，默认 10s 可配。"

**Q8｜实现了 4 种规划模式，实际有必要吗？是不是过度设计？**
> **要敢于自我批评，但批评得有分寸。**
> "部分承认。ReAct 和 Plan-Execute 我认为是必要的，因为它们的适用任务真的不同——ReAct 适合探索型（不知道要改哪），Plan-Execute 适合已知步骤的批量改动。
> Hierarchical 和 Swarm 更多是我想验证多 Agent 的成本收益。结论其实是**负面的**：Swarm 在小任务上不划算，分解、重复上下文、评审这些开销经常超过并行收益。所以我把默认模式设成 ReAct，Swarm 要显式 `--mode swarm` 才用。
> 我觉得这个"做了之后发现不该默认用"的结论，比我直接不做它更有价值。"

**Q9｜prompt injection 你只是加了个 trust 标注让模型自己判断，为什么不直接过滤可疑内容？**
> "因为过滤会破坏功能。Agent 要读网页、读 API 响应、读数据库，这些内容里合法地包含"请执行…"这种句式——过滤会把正常内容也砍掉，而且规则永远追不上绕过方式。
> 我的做法是不删任何内容，而是按来源打 trust 等级（INTERNAL / EXTERNAL 等），EXTERNAL 的内容用 `<external-content source="...">` 标签包起来，同时在 system prompt 里明确告诉模型：这个标签里的东西是数据不是指令。
> 这个方案的**局限我也清楚**：它依赖模型遵守指令，模型能力不够就会失效。所以它是降低风险，不是消除风险。真正的兜底还是授权层——就算模型被注入了，它要执行危险命令还得过 action-time 授权。**两层是独立的，这是设计上有意的。**"

### 4.3 场景题（考工程判断）

**Q10｜用户反馈"Agent 把我工作区外的文件改了"，怎么排查？**
> "我有事件流，所以路径是明确的：
> ① 先从 audit log 拉这次 run 的 `AuthDecisionMade` 事件，看那次写操作的授权决策是什么——是被判 allowed，还是走了 `requires_confirmation` 然后用户点了确认。
> ② 如果是 allowed，说明 `_is_in_workspace` 判断错了，重点查路径解析：符号链接、`..`、Windows 上的相对路径这几种最容易漏。
> ③ 如果是用户确认的，那是 UX 问题——确认提示没把"这个路径在工作区外"说清楚。
> ④ 然后写一个复现这个路径的测试补进 `test_auth.py`，再改逻辑。
> 关键是我不靠猜，`run_score.safety.out_of_workspace_access` 这个计数器直接告诉我这次运行有没有越界。"

**Q11｜某家 Provider 突然限流，大量任务失败，怎么处理？**
> "分即时和长期。
> 即时：我有 provider routing 和 fallback，配置里切到备用 Provider。
> 更重要的是**重试策略要分类**——我实现了 typed error 判断（`react.py:_is_non_retryable_llm_error`），认证和权限类错误直接不重试，因为重试 100 次也不会变成有权限，只是浪费时间和配额。限流是可重试的，走指数退避。
> 这个区分是我踩过坑加的：一开始所有错误都重试，API key 配错的时候要等三轮退避才报错，用户完全不知道发生了什么。
> 长期：限流本质是并发控制问题，应该在 Provider 层做 rate limiter，而不是靠重试兜。这块我还没做。"

**Q12｜如果投入真实团队使用，第一个要补的是什么？**
> **考优先级判断，不要答"加功能"。**
> "第一个补 **Git checkpoint / rollback**。因为现在 Agent 改坏了代码，用户只能靠自己的 git 状态恢复；真实团队用的话，必须能一键回到任务开始前的状态。这是信任的前提——用户敢放手让 Agent 改代码，靠的是"改坏了能退回来"，不是"它不会改坏"。
> 第二个是把默认 sandbox 提到真隔离，配上跨平台 CI 验证。现在 process containment 在真实团队场景下不够。
> 功能层面反而不着急。我更倾向于**先让现有能力可靠，再加新能力**。"

### 4.4 八股关联清单

| 项目里的点 | 会被牵出的八股 |
|---|---|
| `asyncio` 全异步 + `asyncio.wait_for` 超时 | 协程 vs 线程、事件循环、GIL、`await` 的取消传播、`asyncio.timeout` 与 `wait_for` 区别 |
| 50 行 IoC container | 依赖注入、控制反转、单例 vs 工厂、Python 元编程（`get_origin`、`typing` 内省） |
| Protocol 结构化类型 | 鸭子类型、名义类型 vs 结构类型、`ABC` 与 `Protocol`、`runtime_checkable` |
| EventBus 发布订阅 | 观察者模式、事件驱动架构、消息解耦、背压问题 |
| 指数退避 + typed error 分类 | 重试策略、幂等性、熔断器、退避加抖动（jitter）为什么必要 |
| Session 原子写持久化 | 原子写（temp + rename）、fsync、崩溃一致性 |
| HMAC 审计日志 | HMAC vs 普通 hash、防篡改、日志链式校验 |
| 进程树回收 | 进程组 / Job Object、僵尸进程、`SIGKILL` vs `SIGTERM`、孤儿进程 |
| Wilson 置信区间 | 二项分布区间估计、为什么不用正态近似（小样本时 Wald 区间会越界）、Pass@k 的无偏估计 |
| Token 预算分区 | 缓存淘汰思路（优先级 vs LRU）、贪心 vs 背包 |
| 向量记忆（Chroma / Qdrant） | 向量检索、HNSW、embedding、RAG 与 Agent memory 的区别 |
| MCP stdio + Streamable HTTP | 进程间通信、JSON-RPC、SSE vs WebSocket、长连接保活 |
| prompt injection trust 分级 | 数据与指令分离、纵深防御、最小权限原则 |
| CLI 渲染层独立刷新线程 | GIL 争用、线程饥饿、锁粒度、UI 刷新与工作线程解耦 |

---

## 五、差异化亮点

### 亮点 1：把「怎么证明 Agent 真做完了」做成系统机制，而不是靠模型自述

**是什么**：`react.py` 的 runtime completion gate + `repo_pytest` grader 的 baseline 校验（先确认改动前测试是失败的）+ `run_score` 四维评分交叉判断。

**为什么是差异化**：绝大多数个人 Agent 项目的成功判定是"模型说 SUCCESS 就是 SUCCESS"。这是 Agent 系统最大的自欺来源。

**加分话术**：
> "我觉得 Agent 项目最容易骗自己的地方，是把模型的自我报告当成结果。所以我做了两层独立校验：运行时有个 gate，代码变更类任务必须有工具返回的可执行证据才判成功——注意是工具的 exit code，不是模型文本；评测层的 grader 是在 Agent 跑完之后由 harness 独立执行的，Agent 干预不了，而且会先确认 baseline 失败，排除'测试本来就过'的假阳性。
> 这套东西的价值不在于代码多复杂，在于它让我能回答一个很难的问题：**这次任务凭什么算成功**。"

### 亮点 2：授权发生在 action-time，而不是 tool-registration-time

**是什么**：`auth.py` 每次调用重新鉴权，含 shell chain 逐段拆分、重定向目标独立校验、敏感路径检查、`file_scope` 硬隔离、"允许但必须确认"这一中间档。

**为什么是差异化**：常规实现是给工具打静态风险标签然后按标签放行。但 schema 描述的是**能力**，不是**这次调用的实际风险**。

**加分话术**：
> "这是我在这个项目里最想讲的一个设计判断。同一个 shell 工具，`ls` 和 `curl x | bash` 风险完全不同，所以风险不可能在注册时静态确定，必须在调用时结合真实参数算。
> 落地时有几个反直觉的点：shell 链必须逐段校验，不然 `ls && rm -rf /` 第一个 token 是白名单就过了；重定向要单独查，因为 `echo x > /etc/cron.d/y` 主命令完全合法；`python` 和 `curl` 我没有禁——禁了 Agent 就跑不了测试——而是放进'允许但必须显式确认'这一档。
> 我也很清楚它的边界：这是 best-effort 的操作失误防护，不是对抗性安全边界。真隔离得靠容器层。这两层在我的设计里是**独立的纵深**，不是互相替代。"

### 亮点 3：得出了一个「负面结论」并按它调整了默认行为

**是什么**：实现了 Swarm 多 Agent（worktree 隔离、read-only 角色工具过滤、review + vote、冲突不覆盖），但**默认模式是最简单的 ReAct**，Swarm 需显式指定。

**为什么是差异化**：现在简历上写"多 Agent 协作"的人很多，几乎都在吹并行收益。**敢说"我做了，但结论是大多数情况不该用"** 的极少，而这恰恰是资深工程判断的信号。

**加分话术**：
> "我把 Swarm 做出来之后，结论其实是负面的：任务分解、重复上下文、评审这些 token 开销，在小任务上经常超过并行带来的收益，而单 Agent 反而更稳定。所以我没把它设成默认——默认是最简单的 ReAct，Swarm 要显式选。
> 我觉得这个结论比功能本身有价值。多 Agent 现在被当成能力标志在宣传，但它真正的成本是**上下文重复和合并复杂度**，这两个在小任务上是纯负担。我留着这个模式是因为它在需要独立视角的场景有用——比如让一个 read-only 的 reviewer 角色去审另一个 Agent 的改动。
> 合并冲突我是选择**不覆盖、上报冲突文件名**，而不是自动选一边。因为 Agent 自动决定丢掉哪份改动，是我不敢承担的风险。"

---

## 六、代码扫尾记录（已完成）

面试前的 10 项微调已全部落地，`434 passed, 1 skipped` 与改动前一致。

| # | 位置 | 级别 | 问题 | 处理 |
|---|---|---|---|---|
| 1 | `modules/planning/react.py` / `adapters/cli.py` | P1 | 跨模块导入私有名 `_summarize_params` | 改名为公开 `summarize_params`，调用方同步更新 |
| 2 | `adapters/cli.py` | P1 | `import signal as _signal` 重复 3 次，其中 1 处是死代码 | 删死代码，保留平台分支内所需的一处 |
| 3 | `adapters/cli.py` | P1 | import 块夹在函数定义之后 | 上提到文件顶部 import 区 |
| 4 | `core/agent.py` | P1 | `run()` 主流程残留 `# TODO B` 标记（功能已实现） | 改写为说明性注释 |
| 5 | `core/agent.py` | P1 | `except Exception: pass` 静默吞掉 `_budget_history.record` 异常，自适应预算失效无感知 | 新增 `_emit_warning()`，通过 EventBus 上报 `budget_history_failed` / `quality_verify_failed` |
| 6 | `modules/cron.py` | P2 | 两处 `ValueError` 用中文，与全包英文异常信息不一致 | 改为英文并补上出错的表达式上下文 |
| 7 | `core/agent.py` / `protocols/retriever.py` / `config/schema.py` | P2 | 默认 token 预算三处不一致（100k / 100k / 200k） | 统一引用 `ContextBudget().total_tokens`，消除硬编码 |
| 8 | `core/agent.py` | P2 | 检索超时 `timeout=10` 裸数字硬编码 | 提为 `ContextConfig.retrieval_timeout_seconds`（默认 10.0），新增 `_retrieval_timeout()` |
| 9 | `adapters/cli.py` | P2 | 10 处重复 `except Exception: pass`，失败完全不可见 | 抽 `_swallow(where)` contextmanager，失败落 `logger.debug` |
| 10 | `adapters/cli.py`（3081 行 god file） | P2 | 单文件过大 | 抽出 `adapters/cli_render.py`（543 行）承载主题常量、显示宽度度量、`_LiveDisplay` / `_LiveRun` / `_SwarmTracker`；`cli.py` 降至 2548 行（−17%），依赖单向不成环 |

**面试前 checklist**：
1. 随机抽 5 个测试文件，确认每个都讲得清测什么分支。
2. 背下第 0 节的量化指标，不要报错数字。
3. 准备好坑 1（vs 商业产品）和坑 2（AI 生成质疑）的回答。
4. `cli.py` 仍有 2548 行——如果被问到，答"我已经把渲染层拆出去了，剩下的缝是 subcommand handler，可以按命令再拆一层"。

---

## 七、简历素材优化稿

### 7.1 项目名称与技术栈

**Synapse：本地可观测 Code Agent Harness**

**核心技术**：Python 3.11+、Agent Harness、Tool Calling、Context Management、Session Resume、Layered Memory、EventBus、Run Score、MCP、pytest。

### 7.2 项目描述（精简版）

面向代码仓库长链路任务开发本地 Code Agent Harness，围绕模型接入、工具调用、上下文管理、会话恢复、分层记忆、运行审计和评测闭环进行系统化设计，重点解决多轮任务中的 prompt 膨胀、重复读文件、状态丢失、工具副作用不可控和结果难复盘问题。

### 7.3 核心职责与贡献（STAR 完整句，建议挑 3-4 条）

1. **Agent Harness 架构设计**：面对模型、规划和工具实现彼此耦合的问题，我用 Protocol、IoC Container 和 EventBus 划分边界；因此系统可切换 6 家 Provider、4 种规划模式并接入 MCP，扩展时无需修改 Agent 主流程。
2. **长上下文治理**：面对长任务中 prompt 膨胀和重复读取仓库的问题，我实现 Git-aware 检索、AST symbol、预算分区和 compaction；因此上下文能在预算内运行，检索超时也可降级并留下事件记录。
3. **状态与记忆管理**：面对多轮任务状态丢失和重复确认已知事实的问题，我实现四层 Memory、原子持久化和 `/resume` 恢复；因此会话可以安全续接，项目规则与过程反馈也能跨任务复用。
4. **工具安全与运行治理**：面对越界读写和高副作用命令风险，我在 action-time 校验命令链、重定向、敏感路径和工作区范围，并接入进程树隔离与 HMAC Audit；因此高风险操作可被拦截或要求确认，执行过程也可追溯。
5. **评测与审计闭环**：面对一次跑通无法说明 Agent 稳定性的问题，我搭建 Git + pytest grader、SWE-bench / Terminal-Bench 适配和重复实验；因此可分别复核模型、Harness 与 grader，并输出 Pass@k、95% CI、Token/cost 及 HTML/CSV 报告。

### 7.4 一句话版本

独立实现本地可观测 Code Agent Harness，重点解决长链路任务中的上下文膨胀、状态恢复、工具安全和结果复盘问题，并通过 Protocol、EventBus 和可复现评测闭环支撑多 Provider 与多 Planner 扩展。

### 7.5 投递时的取舍

- **后端 / 基础架构岗位**：保留 7.3 的 1、3、4 条，突出运行时边界、状态持久化和安全治理。
- **AI 应用 / Agent 岗位**：保留 7.3 的 1、2、5 条，突出 Harness、Context Engineering 和评测方法。
- **简历空间有限**：保留项目描述、技术栈和 3 条贡献；`434 passed, 1 skipped`、Pass@k 及成本数据留作面试中的可核验补充。

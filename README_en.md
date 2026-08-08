<p align="center">
  <img src="docs/资源/图标/synapse_icon_1_neural-S.png" width="120" alt="Synapse">
</p>

<h1 align="center">Synapse</h1>

<p align="center">
  <b>An observable, extensible, and evaluable Code Agent Harness</b>
</p>

<p align="center">
  <a href="README.md">中文</a> &nbsp;·&nbsp; MIT License
</p>

---

## A 30-second summary for recruiters and interviewers

Synapse is an end-to-end Code Agent Harness implemented in Python. It is not a thin LLM API wrapper: **Agent Loop, planning, context, memory, tools, authorization, sandboxing, event streaming, and evaluation** are organized as replaceable runtime components.

The project demonstrates a graduate-level understanding of the full Agent system surface. It can complete coding tasks from a CLI, while protocol boundaries make Providers, Tools, Memory backends, and Planners replaceable. It also includes real tests, Red Team checks, Git fixtures, SWE-bench/Terminal-Bench adapters, and HTML/CSV evaluation evidence.

| Interview concern | Evidence in the repository |
| --- | --- |
| Does it understand an Agent Harness, not only prompting? | `protocols/` contracts, `core/` lifecycle, and replaceable `modules/` |
| Can model integration become a usable product? | First-run wizard, `~/.synapse/models.json`, multiple providers, fallback/routing |
| Is security treated as a runtime concern? | Action-time authorization, path/command checks, process containment, HMAC Audit |
| Can it measure outcomes? | `repo_pytest`, `terminal_smoke`, `terminal_bench`, SWE-bench adapter, Red Team, repeated runs |
| Is there engineering discipline? | Event observability, four-dimensional run scores, atomic sessions, 434 passed / 1 skipped snapshot |

## What makes it interesting

### 1. Protocol-first Harness boundaries

`protocols/` defines boundaries for `LLMProvider`, `Tool`, `Memory`, `Planner`, `Sandbox`, and `MCP`. `core/` owns Agent/Container/Session/EventBus lifecycle; concrete strategies live in `modules/`. A provider or tool can be replaced without scattering implementation details through the CLI.

### 2. A real Agent Loop, not a one-shot function call

ReAct supports streaming, tool calls, timeout, retry, authorization, and a minimal verification gate. Plan-Execute, Hierarchical, and Swarm share the Planner contract. The default remains the simple ReAct path; complex modes are explicit options rather than a claim that more agents are always better.

### 3. Context engineering and layered memory

The Retriever combines Git-aware file discovery, relevance ranking, AST symbols, budget partitioning, and compaction. Session / Project / User / Semantic memory layers serve continuation, project rules, preferences, and optional vector recall. External content is trust-annotated to reduce the chance that data is mistaken for instruction during prompt-injection attempts.

### 4. Action-time security boundaries

Every tool call is re-authorized using risk, workspace paths, command chains, sensitive paths, and MCP configuration. The default process sandbox contains child-process lifetimes; Docker, bubblewrap, and Seatbelt are optional stronger backends. The project explicitly distinguishes **process containment** from a complete filesystem/network sandbox.

### 5. Event-driven observability

Token, tool, authorization, Agent, Swarm, and process-quality events flow through one EventBus. Each run exposes `run_score` across `safety / process / quality / efficiency`; CLI, HTTP/SSE, Audit, and evaluation reports consume the same signal source, making a task traceable rather than opaque.

### 6. A reproducible evaluation loop

Evaluation does not accept a final `SUCCESS` string as proof:

- `repo_pytest`: temporary Git repository, real edits, and a pytest grader.
- `terminal_smoke`: offline terminal fixture with workspace-state grading.
- `terminal_bench`: common JSON/JSONL task fields, isolated workspaces, and command graders.
- `swebench`: local-dataset-driven clone, checkout, patch, and private-test execution path.
- `--repeat N`: preserves attempts and aggregates Pass@k, Wilson 95% CI, tokens, cost, and tool success rate.
- Every JSON report automatically produces a bilingual HTML dashboard and CSV output. The CSV keeps the original English machine keys and places Chinese aliases next to them, preserving scripts while remaining easy to inspect in a spreadsheet.

## Quick start

### Install

```bash
# Choose a provider: anthropic / openai / deepseek / google / ollama
pip install -e ".[deepseek]"
```

### First run and model configuration

```bash
synapse
```

The first run opens a setup wizard and persists configuration to `~/.synapse/models.json`. Later launches use the persisted default Provider/Model. In the REPL, `/model add` registers another model and `/model` switches and saves the default. `--provider` / `--model` are temporary overrides, so a normal launch needs no flags.

### Common workflows

```bash
# Interactive task mode
synapse

# One-shot coding task
synapse run "Fix the boundary condition in src/parser.py and run the relevant tests"

# Select a planning mode or resume a session
synapse run --mode plan_execute "Refactor the auth module and add tests"
synapse --resume

# Start the HTTP API / SSE server
synapse serve --host 127.0.0.1 --port 8000
```

Useful REPL commands include `/help`, `/model`, `/model add`, `/mode`, `/resume`, and `/score`. Sessions are persisted under `~/.synapse/sessions/`.

## Evaluation and visualization

```bash
# Local functional baseline
python -m synapse eval repo_pytest --repeat 3 \
  --provider deepseek --model deepseek-v4-flash \
  --report eval-results/repo.json

# Offline Terminal-Bench-style smoke
python -m synapse eval terminal_smoke --repeat 3 \
  --report eval-results/terminal-smoke.json

# Local JSON/JSONL dataset adapters
python -m synapse eval terminal_bench --dataset path/to/tasks.jsonl --max-tasks 10
python -m synapse eval swebench --dataset path/to/swebench.jsonl --max-tasks 10

# Re-render artifacts from an existing report
python -m synapse.eval.visualize eval-results/repo.json
```

The result directory contains:

```text
eval-results/repo.json   # Complete machine-readable report
eval-results/repo.html   # Self-contained bilingual HTML dashboard
eval-results/repo.csv    # Per-task data with adjacent English keys / Chinese aliases
```

The dashboard shows pass rate, Pass@k, confidence intervals, mean score, duration, input/output tokens, cost, tool success rate, process score, safety events, category pass rate, and per-task grader results. `official_runner=external` is an explicit boundary: this is an adapter layer, not a claim of an official leaderboard score.

## Harness architecture

```text
CLI / HTTP / Library
          │
          ▼
Container ── Agent Loop ── Planner (ReAct / Plan-Execute / Hierarchical / Swarm)
    │              │                         │
    │              ├── Context Retriever ────┤
    │              ├── Memory Layers         │
    │              └── EventBus              ▼
    │                              Tool Registry + MCP + Skills
    ├── ActionAuthorizer + Audit + ProcessSandbox
    └── RunScore (safety / process / quality / efficiency)
```

```text
synapse/
├── protocols/     # LLM, Tool, Memory, Planner, Sandbox, MCP contracts
├── core/          # Agent, Container, EventBus, Session
├── modules/
│   ├── providers/ # Anthropic, OpenAI-compatible, Google, Ollama, etc.
│   ├── tools/     # Files, search, Shell, Git, Web/HTTP, DB, Browser, Todo
│   ├── planning/  # ReAct, Plan-Execute, Hierarchical, Swarm
│   ├── memory/    # Session / Project / User / Semantic
│   ├── context/   # Retriever, Partitioner, Compactor, Citation
│   ├── security/  # ActionAuth, Sandbox, Audit, Injection Defense
│   └── mcp/       # stdio / Streamable HTTP MCP client
├── eval/          # Metrics, Benchmarks, Red Team, A/B experiments, visualize
├── adapters/      # CLI, Library API, HTTP Server
└── config/        # Pydantic schema, YAML, env, models.json
```

## Boundaries and future work

The project deliberately keeps a few honest boundaries, which are also useful interview topics:

- The default `process` sandbox primarily contains process trees; it is not default Docker filesystem isolation.
- SWE-bench / Terminal-Bench are local dataset adapters; official images, dataset versions, and complete runners remain external.
- Plugins currently provide manifest discovery and version/API gating; they do not import arbitrary third-party code.
- Swarm supports worktrees, review, voting, and conflict protection, but not a complete Git three-way merge.
- Evaluation separates model ability, Harness behavior, and grader quality; one smoke run cannot establish a general model ranking.

Priority follow-ups are Git checkpoint/rollback, a typed retry classifier, HTTP SSRF policy, cross-platform strong-sandbox CI, reproducible SWE-bench samples, and EventBus-based timeline/DAG visualization. There is no plan for a full TypeScript rewrite: Python remains the Agent runtime, while TypeScript is a better boundary for an IDE/API client.

## Interview prompts

1. **Why authorize at action time?** A tool schema describes capability, not the side effects of these arguments; risk must be evaluated with paths, commands, and external-service configuration on every call.
2. **Why is Swarm not the default?** Decomposition, duplicated context, merge conflicts, and review tokens can cost more than parallelism; one Agent is often more reliable for small tasks.
3. **What is the difference between process containment and a filesystem sandbox?** Windows Job Objects and Unix process groups reclaim child processes but do not automatically restrict files or network; strong isolation needs an explicit backend.
4. **How do you prove completion?** Combine a functional grader, test evidence, and runtime scores instead of trusting the Agent's final text.

## Documentation and checks

- Documentation index: [docs/文档索引.md](docs/文档索引.md)
- Harness review: [docs/架构审查/harness-review-2026-08-08.md](docs/架构审查/harness-review-2026-08-08.md)
- Evaluation research: [docs/评测/evaluation-harness-research-2026-08-08.md](docs/评测/evaluation-harness-research-2026-08-08.md)
- UX review: [docs/产品体验/ux-review-and-plan-2026-08-08.md](docs/产品体验/ux-review-and-plan-2026-08-08.md)

```bash
pytest -q
python -m compileall -q synapse
```

Current verification snapshot: `434 passed, 1 skipped`. Optional providers, vector stores, browser support, and strong sandboxes are installed on demand; the core evaluation loop can start with offline fixtures.

## License

MIT

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

## What it is

Synapse is an end-to-end Code Agent Harness implemented in Python. It is not a thin LLM API wrapper: **Agent Loop, planning, context, memory, tools, authorization, sandboxing, event streaming, and evaluation** are organized as replaceable runtime components.

You can hand a coding task to it straight from the CLI, or swap Providers, Tools, Memory backends, and Planners along the protocol boundaries and use it as the base layer for your own agent. Every run leaves behind an event stream and a four-dimensional score, so "what did this run actually do, and why does it count as done" is an answerable question.

| Capability | Where it lives |
| --- | --- |
| Protocol-first harness structure | `protocols/` contracts, `core/` lifecycle, replaceable `modules/` |
| Multi-provider integration and hot swapping | First-run wizard, `~/.synapse/models.json`, fallback / routing |
| Action-time security boundary | Per-call authorization, sensitive path and command-chain checks, process-tree reclamation, HMAC audit |
| Reproducible evaluation | `repo_pytest`, `terminal_smoke`, `terminal_bench`, SWE-bench adapter, Red Team, repeated runs |
| Observability | EventBus events, four-dimensional run scores, atomic session persistence |

Size: ~18.6k lines of Python under `synapse/`, 73 test files / 431 test functions (435 collected cases after parametrization), snapshot `434 passed, 1 skipped`.

## What makes it interesting

### 1. Protocol-first Harness boundaries

`protocols/` defines boundaries for `LLMProvider`, `Tool`, `Memory`, `Planner`, `Sandbox`, and `MCP`. `core/` owns Agent/Container/Session/EventBus lifecycle; concrete strategies live in `modules/`. A provider or tool can be replaced without scattering implementation details through the CLI.

### 2. A real Agent Loop, not a one-shot function call

ReAct supports streaming, tool calls, timeout, retry, authorization, and a minimal verification gate. Plan-Execute, Hierarchical, and Swarm share the Planner contract. The default remains the simple ReAct path; complex modes are explicit options rather than a claim that more agents is always better.

Runaway long tasks have a recovery path: a Git checkpoint is taken at task start (temporary-index snapshot that never touches the user's staging area), the first thrashing trip auto-rolls the offending file back to its pre-task state and tells the model to try a different approach, and `/rewind` restores any snapshot across sessions (stored under `refs/synapse/checkpoints/`).

### 3. Context engineering and layered memory

The Retriever combines Git-aware file discovery, relevance ranking, AST symbols, budget partitioning, and compaction. Session / Project / User / Semantic memory layers serve continuation, project rules, preferences, and optional vector recall. External content is trust-annotated to reduce the chance that data is mistaken for instruction during prompt-injection attempts. Conversation history past its soft limit can be LLM-summarized before eliding (auto /compact via `history_compaction: llm`); the TODO list persists with the Session and is restored by `--resume`.

### 4. Action-time security boundaries

Every tool call is re-authorized using risk, workspace paths, command chains, sensitive paths, and MCP configuration. Command-chain checks are shlex token-based: quoted operators are argument text, while `$()`/backticks/subshells trigger confirmation. Ask/allow/deny permission rules and session-scoped "yes to all" approval memory (signatured by command first-token / parent directory) are supported. Output from web/browser/db is scanned for injection signatures and forged trust tags are neutralized; outbound requests carry SSRF protection (private/loopback/cloud-metadata targets rejected, redirect hops re-checked). The default process sandbox contains child-process lifetimes; Docker, bubblewrap, and Seatbelt are optional stronger backends. The project explicitly distinguishes **process containment** from a complete filesystem/network sandbox.

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

Useful REPL commands include `/help`, `/model`, `/model add`, `/mode`, `/resume`, `/score`, `/checkpoint`, and `/rewind`. Sessions are persisted under `~/.synapse/sessions/`.

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

These boundaries are deliberate and explicitly labelled. Check that they fit your use case before relying on the harness:

- The default `process` sandbox primarily contains process trees; it is not default Docker filesystem isolation.
- SWE-bench / Terminal-Bench are local dataset adapters; official images, dataset versions, and complete runners remain external.
- Plugins currently provide manifest discovery and version/API gating; they do not import arbitrary third-party code.
- Swarm supports worktrees, review, voting, and conflict protection, but not a complete Git three-way merge.
- Evaluation separates model ability, Harness behavior, and grader quality; one smoke run cannot establish a general model ranking.

Priority roadmap (by current value):

- **Aider cross-harness comparison**: Synapse vs Aider on the same frozen 20 tasks, gateway model, and external graders — the experiment that turns "we have numbers" into "harness capability evidence" (Aider is confirmed to support OpenAI-compatible gateways and this repo's JSONL format).
- **Subagent delegation tool**: a model-side `spawn_subagent`-style tool so exploratory subtasks run in their own context window and return a summary, bounding main-context growth (the HierarchicalPlanner's session.fork machinery can be reused).
- **PreToolUse hooks**: hooks today are read-only PostToolUse notifications; wire PreToolUse block/rewrite into the ActionAuthorizer decision point for enterprise policy enforcement.
- **HTTP session persistence**: the server keeps sessions in memory (lost on restart); add persistence plus resume/cancel endpoints to match the CLI's `--resume`.
- **EventBus timeline/DAG visualization**: render swarm worker/review/vote event flows into the HTML dashboard to strengthen replayability.
- DNS-rebinding-grade SSRF hardening (pin resolved IPs at the transport layer).

The frozen-20 SWE-bench multi-model run is done (three model tiers × 3 repeats, gold-verified admission, external graders — see the evaluation baseline doc); the typed retry classifier and the Windows CI matrix have landed. There is no plan for a full TypeScript rewrite: Python remains the Agent runtime, while TypeScript is a better boundary for an IDE/API client.

## Design trade-offs

**Why authorize at action time instead of tool-registration time?** A tool schema describes capability, not whether these particular arguments are safe. The same shell tool running `ls` and `curl x | bash` carries entirely different risk, so risk is recomputed on every call from the actual arguments, paths, command chain, and external-service configuration.

**Why is Swarm not the default?** Decomposition, duplicated context, merge conflicts, and review tokens frequently cost more than parallelism buys on small tasks, and a single Agent is more stable. ReAct is the default; Swarm requires an explicit `--mode swarm`.

**What is the difference between process containment and a filesystem sandbox?** Windows Job Objects and Unix process groups guarantee child processes are reclaimed, but do not restrict file or network access. Strong isolation requires explicitly selecting the Docker, bubblewrap, or Seatbelt backend.

**How do you know a task is actually complete?** The runtime gate reads tool exit codes, not the phrase "done" in model output. The evaluation grader runs independently after the Agent finishes, and first confirms the baseline fails so a passing-anyway test cannot produce a false positive.

**Why does thrashing only roll back one file instead of the whole workspace?** When thrashing first trips, only the repeatedly-edited file is proven bad — rolling it back is safe. A full-workspace rollback would also discard half-finished edits elsewhere the model may still build on. Full restores are the user's call (`/rewind`), never the harness default: a recovery action is itself a side effect, so it stays conservative. Restores reset tracked files only; untracked files are always kept because the harness cannot tell "the agent created this" from "the user did".

## Local verification

```bash
pytest -q
python -m compileall -q synapse
```

Optional providers, vector stores, browser support, and strong sandboxes are installed on demand. Without an external dataset you can start with the offline fixture (`terminal_smoke`) to verify the harness path end to end.

## License

MIT

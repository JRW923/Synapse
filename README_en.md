<p align="center">
  <img src="docs/icons/synapse_icon_1_neural-S.png" width="120" alt="Synapse">
</p>

<h1 align="center">Synapse</h1>

<p align="center">
  <b>A modular Agent Harness</b> — orchestrating ideas into code
</p>

<p align="center">
  <a href="README.md">中文</a> &nbsp;·&nbsp; MIT License
</p>

---

## Design

Synapse is not yet another LLM CLI wrapper. It is a composable **Agent Harness**: runtime, planning, tools, memory, security, and swarm orchestration are split into clear interfaces and modules, so you can experiment with, replace, and extend your own agent strategies on top of it.

### Protocols first, zero-dependency boundaries

`protocols/` defines pure interfaces for Agent, Tool, Provider, Memory, Security, etc. The core depends only on interfaces; concrete implementations live in `modules/`. This lets you swap any module — add a new model provider, introduce a custom tool, or change the memory backend — without rewriting the whole system.

### Agent + Container: task execution container

`core/`'s `Agent` and `Container` own the full task lifecycle: receive input, keep session state, coordinate the planner and tool calls, and collect runtime metrics. `Container` is the runtime context boundary and the mounting point for stronger isolation in the future.

### Event bus: decoupling and observability

`EventBus` runs through the CLI, HTTP server, and Agent internals. Tool calls, LLM tokens, planning phase changes, and swarm events are all published as events, so external observers can replay the entire execution in real time and build visual orchestration interfaces.

### Pluggable planning modes

`modules/planning/` provides ReAct, Plan-Execute, and Hierarchical planners behind a single interface. Switch dynamically with `/mode` or `--mode` to experiment with the best strategy for each task type.

### Tools + MCP: extension points

Built-in tools cover file I/O, search, Shell, Git, HTTP, databases, browser, and more. The tool interface is part of `protocols/`; the MCP client (stdio + Streamable HTTP) lets you plug in external tool services without touching the core.

### Memory and context governance

Memory is layered into Session / Project / User disk persistence, with a Semantic vector-recall layer as an optional backend. `context/`'s Retriever + Partitioner + Compactor trims history to a manageable size, preventing token explosion on long tasks.

### Security: approval gate + lifecycle isolation

The real write-safety boundary is `ActionAuthorizer`'s command approval gate. `security/`'s Sandbox handles process-tree lifecycle isolation (Windows Job Object / Unix process groups), ensuring child tasks time out or exit without leaving orphan processes behind.

### Runtime scoring

Every task produces a `run_score` across four dimensions — safety, process, quality, efficiency — plus a `process_hint` that gives a measurable improvement suggestion for the next iteration.

### Swarm: multi-worker collaboration

`core/` supports parallel workers whose results are reviewed, voted on, and merged back into the main workspace. The event stream exposes `worker_spawned`, `worker_completed`, `review_submitted`, `vote_cast`, `swarm_verified`, and more, enabling visual orchestration and auditability.

## Quick Start

```bash
pip install -e ".[deepseek]"   # pick: anthropic / openai / deepseek / google / ollama
synapse
```

The first run launches a setup wizard that writes `~/.synapse/models.json`. After that, `synapse` enters the REPL and `synapse run "task"` runs one-shot tasks. Sessions are auto-persisted to `~/.synapse/sessions/`; resume later with `--resume` or the in-REPL `/resume`.

## Architecture

```
synapse/
├── protocols/     # Pure interface definitions (zero deps)
├── core/          # Agent, Container, EventBus, Session
├── modules/
│   ├── providers/ # 5 LLM providers
│   ├── tools/     # 10 tools (file/search/Shell/Git/HTTP/DB/Browser)
│   ├── planning/  # 3 planning modes (ReAct / PlanExecute / Hierarchical)
│   ├── memory/    # Session/Project/User on disk; Semantic vector layer optional
│   ├── context/   # Context governance (Retriever + Partitioner + Compactor)
│   ├── security/  # 4 layers (Sandbox/ActionAuth/Audit/Injection Defense)
│   └── mcp/       # MCP client
├── eval/          # Metrics, benchmarks, A/B experiments
├── adapters/      # CLI, Library API, HTTP Server
└── config/        # Pydantic schema + YAML/env loader
```

## Future Directions

- **Stronger isolation**: build real filesystem/network sandboxing on top of the current process-tree isolation using bubblewrap / Seatbelt / namespace.
- **Real benchmarks**: wire `swebench` and `process_bench` to real datasets and execution environments for reproducible evaluation pipelines.
- **Swarm three-way merge**: evolve concurrent worker write conflicts from "last write wins" into true three-way merges.
- **Semantic memory by default**: make ChromaDB/Qdrant vector recall the default memory layer for better long-range context association.
- **MCP ecosystem**: hot-plug more external tools and model providers through the MCP protocol while keeping the core stable.
- **Visual orchestration**: use the event bus to build execution timeline and dependency DAG editors.

## License

MIT

# Synapse

> Connecting ideas into code — an intelligent, modular code agent.

[中文文档](README_zh.md)

## Quick Start

```bash
# 1. Install
pip install -e ".[deepseek]"          # pick: anthropic / openai / deepseek / google / ollama

# 2. Launch — first run walks you through setup
synapse
```

That's it. The first-run wizard asks which provider you want, takes your API key, and writes `~/.synapse/config.yaml`. Every subsequent launch goes straight to the REPL.

```bash
# Alternatively, specify everything on the command line
synapse -p anthropic -m claude-sonnet-4-6 "Fix the bug in auth.py"
```

## In-REPL Commands

| Command | Description |
|---------|-------------|
| `/help` | Show all commands |
| `/model` | Show available models (green = ready, gray = needs API key) |
| `/model <name>` | Switch to a configured model |
| `/provider <name>` | Switch provider |
| `/memory` | Session info + token usage |
| `/session` | Show session path |
| `/reset` | Clear session |
| `/mode <name>` | Switch planning mode (react / plan_execute / hierarchical) |
| `/tools` | List tools |
| `/context-report` | Show context-block citation / usage heatmap |
| `/score` | Show runtime score (safety / process / quality / efficiency) + process hint |
| `/exit` | Quit |

## Configuration

Config lookup order:

1. `./synapse.yaml` (then walks up the directory tree)
2. `<package-root>/synapse.yaml` (automatic for `pip install -e .`)
3. `~/.synapse/config.yaml` (global fallback, written by first-run wizard)

Example `synapse.yaml`:

```yaml
provider:
  provider: deepseek
  model: deepseek-v4-pro
  api_key: "sk-your-key"

# Add more providers so /model can list them
  models:
    - provider: openai
      model: gpt-5.5
      api_key: "sk-openai-key"
```

Or use environment variables: `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`.

## CLI

```bash
synapse                              # Main REPL
synapse setup                        # Install launcher scripts
synapse chat                         # Chat session
synapse run "task"                   # One-shot task (streams progress live)
synapse serve                        # HTTP API (port 8000)
synapse version                      # Show version
```

```
-c, --config  PATH     Path to synapse.yaml
-p, --provider NAME    LLM provider
-m, --model    NAME    Model ID
--mode         NAME    Planning mode
-y, --yes            (run) auto-approve confirmation-required actions (headless opt-in)
```

`run` and `chat` stream progress (tool calls, swarm lifecycle, tokens) in a live
panel instead of blocking silently. When a tool needs confirmation and no human
is at the terminal, the action is **auto-denied** unless you pass `--yes`.

## HTTP API

`/run` and `/run/stream` (SSE) are the programmatic equivalents of `run`. Both
accept a `RunRequest`:

```json
{ "task": "Refactor auth.py", "auto_approve": false }
```

- `auto_approve` — opt-in to approve confirmation-required tools (mirrors `--yes`).
- `/run/stream` emits `agent_progress`, `llm_token`, `tool_call_*`, and the swarm
  events (`worker_spawned`, `worker_completed`, `review_submitted`, `vote_cast`,
  `swarm_verified`) so external callers see the same live progress as the CLI.
- The response (and the stream's final `done` event) includes `run_score` — the
  runtime score (`safety` / `process` / `quality` / `efficiency`) plus the latest
  `process_hint` for the next task.

Errors are returned as friendly `原因 / 建议` messages; raw tracebacks are never
surfaced to the user.

## Architecture

```
synapse/
├── protocols/     # Pure interface definitions
├── core/          # Agent, Container, EventBus, Session
├── modules/
│   ├── providers/ # Anthropic / OpenAI / Google / DeepSeek / Ollama
│   ├── tools/     # Read, Write, Edit, Glob, Grep, Shell, Git, HTTP, DB, Browser
│   ├── planning/  # ReAct / Plan-Execute / Hierarchical
│   ├── memory/    # Session / Project / User / Semantic (ChromaDB+Qdrant)
│   ├── context/   # Retriever + Partitioner + Compactor
│   ├── security/  # Sandbox, ActionAuth, Audit, Injection Defense
│   └── mcp/       # MCP client (stdio + Streamable HTTP)
├── eval/          # Metrics, benchmarks, A/B experiments
├── adapters/      # CLI, Library API, HTTP Server
└── config/        # Pydantic schema + YAML/env loader
```

## License

MIT

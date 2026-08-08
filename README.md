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

That's it. The first-run wizard asks which provider and model you want, securely
prompts for the API key, and writes `~/.synapse/models.json`. Every subsequent
launch uses that default and goes straight to the REPL.

```bash
# One-shot tasks use the same saved default
synapse run "Fix the bug in auth.py"
```

## In-REPL Commands

| Command | Description |
|---------|-------------|
| `/help` | Show all commands |
| `/model` | Show available models (green = ready, gray = needs API key) |
| `/model <name>` | Switch model and save it as the default |
| `/model add` | Add a built-in or OpenAI/Anthropic-compatible model |
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

LLM models and the default selection live in `~/.synapse/models.json`:

```json
{
  "version": 1,
  "defaultProvider": "deepseek",
  "defaultModel": "deepseek-chat",
  "providers": {
    "deepseek": {
      "apiKey": "sk-your-key",
      "models": [{ "id": "deepseek-chat" }]
    }
  }
}
```

Use `/model add` instead of editing this file for the common path. API keys may
also come from `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or
`GOOGLE_API_KEY`; the wizard detects them and avoids copying them into JSON.

Project and Agent behavior remains in YAML. Config lookup order:

1. `./synapse.yaml` (then walks up the directory tree)
2. `<package-root>/synapse.yaml` (automatic for `pip install -e .`)
3. `~/.synapse/config.yaml` (optional global fallback)

Example `synapse.yaml`:

```yaml
provider:
  max_retries: 3
  timeout_seconds: 120
planning:
  mode: react
```

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
-p, --provider NAME    Optional one-run provider override
-m, --model    NAME    Optional one-run model override
--mode         NAME    Planning mode
-y, --yes            (run) auto-approve confirmation-required actions (headless opt-in)
```

`run` and `chat` stream progress (tool calls, swarm lifecycle, tokens) in a live
panel instead of blocking silently. When a tool needs confirmation and no human
is at the terminal, the action is **auto-denied** unless you pass `--yes`.

The CLI adapts to the terminal: wide terminals show the full workspace header,
narrow terminals use a compact single-column layout, and redirected output falls
back to plain text without ANSI codes. The live panel shows the current phase,
the last five tool steps, elapsed time, and tokens; partial/failed results include
metrics and a concrete resume or retry hint.

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

Errors are returned as friendly `reason / suggested-action` messages; raw
tracebacks are never surfaced to the user.

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

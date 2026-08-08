<p align="center">
  <img src="docs/icons/synapse_icon_1_neural-S.png" width="120" alt="Synapse">
</p>

<h1 align="center">Synapse</h1>

<p align="center">
  <b>Connecting ideas into code</b> — an intelligent, modular code agent.
</p>

<p align="center">
  <a href="README.md">中文</a> &nbsp;·&nbsp; MIT License
</p>

---

## Features

- **Multi-provider**: Anthropic / OpenAI / DeepSeek / Google / Ollama, configured by a first-run wizard
- **Three planning modes**: ReAct / Plan-Execute / Hierarchical, switchable on the fly
- **Rich tools + MCP**: 10+ built-in tools, extensible via MCP
- **Session persistence**: every task is auto-saved; resume with `--resume` / `/resume`
- **HTTP API**: `/run` and SSE-streaming `/run/stream` for programmatic integration
- **Runtime scoring**: safety / process / quality / efficiency, four dimensions

## Quick Start

```bash
# 1. Install
pip install -e ".[deepseek]"          # pick: anthropic / openai / deepseek / google / ollama

# 2. Launch — first run walks you through setup
synapse
```

The first-run wizard asks which provider and model you want, securely prompts for
the API key, and writes `~/.synapse/models.json`. Every subsequent launch uses that
default and goes straight to the REPL.

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
| `/resume [id]` | Resume a saved session (omit id for the most recent) |
| `/sessions` | List saved sessions |
| `/mode <name>` | Switch planning mode (react / plan_execute / hierarchical) |
| `/tools` | List tools |
| `/context-report` | Show context-block citation / usage heatmap |
| `/score` | Show runtime score + process hint |
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

Use `/model add` instead of editing this file for the common path. API keys may also
come from `DEEPSEEK_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or
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

## Preset Models

The wizard ships recommended Model IDs for five providers:

| Provider | Models |
|----------|--------|
| anthropic | claude-sonnet-4-6, claude-opus-4-7, claude-haiku-4-5 |
| openai | gpt-5.5, gpt-5.4, o4-mini |
| deepseek | deepseek-chat, deepseek-v4-pro, deepseek-v4-flash |
| google | gemini-3-flash, gemini-3-pro |
| ollama | qwen3.5:4b, llama4:8b |

The wizard writes only the model you pick; add the rest on demand with `/model add`
so the default list stays uncluttered.

## CLI

| Command | Description |
|---------|-------------|
| `synapse` | Main REPL |
| `synapse setup` | Install launcher scripts |
| `synapse chat` | Chat session |
| `synapse run "task"` | One-shot task (streams progress live) |
| `synapse serve` | HTTP API (port 8000) |
| `synapse version` | Show version |

**Common flags**

```
-c, --config PATH     Path to synapse.yaml
-p, --provider NAME   Optional one-run provider override
-m, --model NAME      Optional one-run model override
--mode NAME           Planning mode
--resume [ID]         Resume a saved session (omit ID for the most recent; works for run/chat/REPL)
-y, --yes            (run) auto-approve confirmation-required actions (headless opt-in)
```

Sessions are persisted to `~/.synapse/sessions/<id>.json` after every task; resume
later with `synapse --resume` or the in-REPL `/resume`.

`run` and `chat` stream progress (tool calls, swarm lifecycle, tokens) in a live
panel instead of blocking silently. When a tool needs confirmation and no human is
at the terminal, the action is **auto-denied** unless you pass `--yes`.

The CLI adapts to the terminal: wide terminals show the full workspace header,
narrow terminals use a compact single-column layout, and redirected output falls
back to plain text without ANSI codes. The live panel shows the current phase, the
last five tool steps, elapsed time, and tokens; partial/failed results include
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
├── protocols/     # Pure interface definitions (zero deps)
├── core/          # Agent, Container, EventBus, Session
├── modules/
│   ├── providers/ # 5 LLM providers
│   ├── tools/     # 10 tools (file/search/Shell/Git/HTTP/DB/Browser)
│   ├── planning/  # 3 planning modes (ReAct / PlanExecute / Hierarchical)
│   ├── memory/    # Session/Project/User on disk; Semantic vector layer optional
│   ├── context/   # Context governance (Retriever + Partitioner + Compactor)
│   ├── security/  # 4 layers (Sandbox/ActionAuth/Audit/Injection Defense)
│   └── mcp/       # MCP client (stdio + Streamable HTTP)
├── eval/          # Metrics, benchmarks, A/B experiments
├── adapters/      # CLI, Library API, HTTP Server
└── config/        # Pydantic schema + YAML/env loader
```

## Known Limitations

To avoid overstating maturity, here are capabilities that are **not yet truly wired
up** or remain at scaffolding stage:

- **Semantic memory layer**: the vector backend (ChromaDB/Qdrant) is an optional
  dependency. Summaries are written to it after each task and recalled by similarity;
  without a backend it degrades to Session/Project/User memory only.
- **Eval / Benchmark**: the metric pipeline and redteam framework under `eval/` run,
  but `swebench` is **not connected to a real dataset** (no clone/docker/patch/test),
  and `process_bench` uses example tasks against a fictional repo. Treat both as
  evaluation scaffolding, not real benchmark results.
- **Security Sandbox**: `ProcessSandbox` is process-tree isolation — Windows Job
  Object (`KILL_ON_JOB_CLOSE`), Unix process group + `killpg` — killing the whole
  child tree on timeout/exit so grandchildren cannot escape as orphans. It guarantees
  "resources/lifecycle don't run away", **not strong filesystem/network isolation**
  (no bubblewrap/Seatbelt/namespace); the real safety boundary for file writes is the
  `ActionAuthorizer` command-approval gate.
- **Swarm + Worktree**: worker results are merged back into the main workspace before
  cleanup, but concurrent workers writing the same file resolve as "last write wins" —
  no true three-way merge.

## License

MIT

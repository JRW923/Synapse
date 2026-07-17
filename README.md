# Synapse

> Connecting ideas into code — an intelligent, modular code agent.

## Quick Start

```bash
# Install with your preferred LLM provider
pip install git+https://github.com/JRW923/Synapse.git#egg=synapse[deepseek]
# or: pip install synapse[anthropic] / synapse[openai] / synapse[google] / synapse[ollama]

# Set your API key
set DEEPSEEK_API_KEY=sk-xxx        # Windows CMD
# $env:DEEPSEEK_API_KEY = "sk-xxx" # PowerShell
# export DEEPSEEK_API_KEY=sk-xxx   # Linux/macOS

# Start chatting
synapse chat

# Or run a single task
synapse run "Create a hello.py that prints Hello World"
```

## Configuration

Synapse reads config from (in priority order):

1. `./synapse.yaml` — project-local config
2. `~/.synapse/config.yaml` — user-global config
3. Environment variables

Example `synapse.yaml`:

```yaml
provider:
  provider: deepseek
  model: deepseek-chat
  api_key: "sk-your-key-here"
```

All available env vars:

| Variable | Effect |
|----------|--------|
| `DEEPSEEK_API_KEY` | DeepSeek API key |
| `OPENAI_API_KEY` | OpenAI API key |
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `GOOGLE_API_KEY` | Google Gemini API key |
| `SYNAPSE_PROVIDER` | Provider name override |
| `SYNAPSE_MODEL` | Model name override |

## Commands

```bash
synapse chat              # Interactive REPL session
synapse run "task"        # Execute a single task
synapse serve             # Start HTTP API server (port 8000)
synapse eval <benchmark>  # Run evaluation benchmark
synapse experiment ...    # Run A/B experiment
synapse version           # Show version
```

**Chat controls**: `/clear` to reset session, `/exit` or `Ctrl+C` to quit.

**Run options**:

```
--provider, -p    LLM provider (anthropic/openai/deepseek/google/ollama)
--model, -m       Model ID (e.g. deepseek-chat, gpt-4o, claude-sonnet-4-6)
--mode            Planning mode (react/plan_execute/hierarchical)
--mcp-server      Connect to MCP server (name:command_or_url)
--enable-external-tools   Enable HTTP/DB/Browser tools (disabled by default)
```

## Optional Features

Synapse uses dependency groups. Install only what you need:

```bash
# Core only (no LLM, no extra tools)
pip install git+https://github.com/JRW923/Synapse.git

# With your provider
pip install git+https://github.com/JRW923/Synapse.git#egg=synapse[deepseek]

# With extra capabilities
pip install git+https://github.com/JRW923/Synapse.git#egg=synapse[deepseek,chromadb,mcp]

# Everything
pip install git+https://github.com/JRW923/Synapse.git#egg=synapse[all]
```

| Group | What it enables |
|-------|----------------|
| `anthropic` / `openai` / `deepseek` / `google` / `ollama` | LLM provider |
| `chromadb` / `qdrant` | Semantic memory backend |
| `http` / `browser` | External tools (HTTP requests, Playwright) |
| `mcp` | Model Context Protocol support |
| `eval` | A/B experiments (scipy) |
| `server` | HTTP API server (FastAPI + uvicorn) |
| `dev` / `test` | Development tools (pytest) |

## Architecture

```
synapse/
├── protocols/     # Pure interface definitions (zero deps)
├── core/          # Agent loop, IoC container, EventBus, Session
├── modules/
│   ├── providers/ # Anthropic / OpenAI / Google / DeepSeek / Ollama
│   ├── tools/     # Read, Write, Edit, Glob, Grep, Shell, Git, HTTP, DB, Browser
│   ├── planning/  # ReAct / Plan-Execute / Hierarchical
│   ├── memory/    # Session / Project / User / Semantic (ChromaDB+Qdrant)
│   ├── context/   # Retriever + Partitioner + Compactor
│   ├── security/  # Sandbox, ActionAuth, Audit(JSONL+HMAC), Injection Defense
│   └── mcp/       # MCP client (stdio + Streamable HTTP)
├── eval/          # Metrics collectors, benchmarks, A/B experiments
├── adapters/      # CLI, Python Library API, HTTP Server
└── config/        # Pydantic schema + YAML/env loader
```

## License

MIT

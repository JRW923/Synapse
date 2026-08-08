# Synapse Phase 2: Full Modules Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development

**Goal:** Phase 2 — Add 4 LLM providers, PlanExecutePlanner, HierarchicalPlanner, ProjectMemory, UserMemory, full context governance (Partitioner + Compactor), AuditLogger, and Library API.

**Architecture:** Extends Phase 1's Protocol + EventBus pattern. All new modules implement existing protocols. No architecture changes needed — just new implementations.

**Tech Stack:** Same as Phase 1. New deps: `openai`, `google-generativeai` (optional per provider).

## Global Constraints

- Python >= 3.11
- `protocols/` imports nothing from `core/` or `modules/`
- `core/` imports only from `protocols/`
- `modules/` imports from `protocols/` and `core/exceptions.py`, `core/events.py`
- All interfaces use `typing.Protocol`, never ABC
- New providers follow same pattern as AnthropicProvider
- TDD: write failing test → implement → pass → commit
- Commit after each task

---

### Task 1: OpenAI Provider

**Files:**
- Create: `synapse/modules/providers/openai.py`
- Create: `tests/modules/test_openai_provider.py`

**Interfaces:**
- Consumes: `LLMProvider` protocol
- Produces: `OpenAIProvider` — chat + stream support
- Pattern: identical to AnthropicProvider but uses OpenAI SDK

Implementation mirrors AnthropicProvider:
- `__init__(model, api_key)` creates `AsyncOpenAI` client
- `chat(messages, tools)` → converts Message list to OpenAI format, calls `client.chat.completions.create`, parses response
- `stream(messages, tools)` → streaming iterator
- `model_id` property
- Configure `max_tokens` from ProviderConfig

OpenAI tool format differs from Anthropic:
- Tools: `{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}`
- Tool calls in response: `message.tool_calls[0].function.name`, `.function.arguments` (JSON string)

Tests (mock-based, 3 tests):
- `test_model_id`
- `test_chat_basic` — text response
- `test_chat_with_tools` — tool call response

---

### Task 2: Google Gemini Provider

**Files:**
- Create: `synapse/modules/providers/google.py`
- Create: `tests/modules/test_google_provider.py`

Implements `LLMProvider` using `google-generativeai` SDK.

Tests (mock-based, 2 tests):
- `test_model_id`
- `test_chat_basic`

---

### Task 3: DeepSeek Provider

**Files:**
- Create: `synapse/modules/providers/deepseek.py`
- Create: `tests/modules/test_deepseek_provider.py`

DeepSeek uses OpenAI-compatible API — can extend OpenAIProvider or use OpenAI SDK with different base_url.

Tests (mock-based, 2 tests):
- `test_model_id`
- `test_chat_basic`

---

### Task 4: Ollama Provider

**Files:**
- Create: `synapse/modules/providers/ollama.py`
- Create: `tests/modules/test_ollama_provider.py`

Uses `openai` SDK with `base_url="http://localhost:11434/v1"` (Ollama's OpenAI-compatible endpoint).

Tests (mock-based, 2 tests):
- `test_model_id`
- `test_chat_basic`

---

### Task 5: ProjectMemory

**Files:**
- Create: `synapse/modules/memory/project.py`
- Create: `tests/modules/test_project_memory.py`

Implements `MemoryStore` for `MemoryLevel.PROJECT`. Reads/writes `.synapse/memory/` directory.

File format (per spec):
```
.synapse/memory/
├── MEMORY.md          # index file
├── architecture.md
├── conventions.md
├── pitfalls.md
└── decisions/
    └── YYYY-MM-DD-xxx.md
```

Each file has YAML frontmatter: `type`, `timestamp`, `priority`, `tags`.

Implementation:
- `store(entry)` → appends to appropriate file or creates new file, updates MEMORY.md index
- `retrieve(query, level=PROJECT, top_k)` → reads all files, filters by tag/query match, sorts by priority
- `forget(entry_id)` → removes from file, updates index
- For non-PROJECT levels, returns empty list

Tests (4 tests):
- `test_store_and_retrieve` — store entry, retrieve by query
- `test_forget` — remove entry
- `test_retrieve_wrong_level_returns_empty`
- `test_memory_directory_created` — auto-creates .synapse/memory/

---

### Task 6: UserMemory

**Files:**
- Create: `synapse/modules/memory/user.py`
- Create: `tests/modules/test_user_memory.py`

Implements `MemoryStore` for `MemoryLevel.USER`. Same pattern as ProjectMemory but uses `~/.synapse/memory/`.

Tests (3 tests):
- `test_store_and_retrieve_user`
- `test_cross_project_persistence` — user memory persists across different project paths
- `test_retrieve_wrong_level_returns_empty`

---

### Task 7: Context Partitioner + Compactor

**Files:**
- Create: `synapse/modules/context/partitioner.py`
- Create: `synapse/modules/context/compactor.py`
- Create: `tests/modules/test_partitioner.py`
- Create: `tests/modules/test_compactor.py`

**Partitioner**: Enforces the four-zone `ContextBudget` (SYSTEM/CORE/REFERENCE/OVERFLOW).
- `partition(context, budget)` → returns a new Context with blocks trimmed to budget
- SYSTEM blocks are never removed
- CORE blocks are kept up to budget.core_pct
- REFERENCE/OVERFLOW blocks are pruned by lowest priority first

**Compactor**: Summarization-based compression of low-priority blocks.
- `compact(context, budget)` → compresses OVERFLOW blocks by generating summaries
- Phase 2 uses simple truncation (no LLM summarization yet — that's Phase 3)
- Compressed blocks are marked with `source=ContextSource.MEMORY`

Tests (4 tests):
- `test_partitioner_trims_overflow_first`
- `test_partitioner_never_removes_system`
- `test_compactor_truncates_overflow`
- `test_compactor_preserves_core`

---

### Task 8: PlanExecutePlanner

**Files:**
- Create: `synapse/modules/planning/plan_execute.py`
- Create: `tests/modules/test_plan_execute_planner.py`

Three-phase planner:
```
Phase 1 - Plan:  LLM generates execution plan (list of steps)
Phase 2 - Execute: For each step, call ReActPlanner, phase_clear context between steps
Phase 3 - Verify: Check key steps weren't skipped
```

- `mode = PlanningMode.PLAN_EXECUTE`
- Constructor takes `ReActPlanner` instance for step execution
- Plan is generated via LLM with a planning-specific system prompt
- Each step execution reuses ReActPlanner's loop
- Phase clearing between steps (clear expired context blocks)
- Optional user approval between Plan and Execute phases

Tests (3 tests):
- `test_generates_plan` — LLM returns plan with steps
- `test_executes_all_steps` — each step calls ReActPlanner
- `test_verification_detects_skipped_steps`

---

### Task 9: HierarchicalPlanner

**Files:**
- Create: `synapse/modules/planning/hierarchical.py`
- Create: `tests/modules/test_hierarchical_planner.py`

Orchestrator pattern:
```
1. LLM decomposes task into subtasks
2. Each subtask gets an independent Session (forked)
3. Auto-selects ReAct or PlanExecute per subtask
4. Executes subtasks SERIALLY
5. LLM merges results into final output
```

- `mode = PlanningMode.HIERARCHICAL`
- Constructor takes both `ReActPlanner` and `PlanExecutePlanner`
- Sub-session forking via `session.fork(subtask_id)`
- Subtask planner selection by complexity heuristic (or LLM decision)
- Serial execution only (research shows parallel amplifies errors)

Tests (3 tests):
- `test_decomposes_task` — LLM produces subtask list
- `test_executes_subtasks_serially` — each subtask gets its own session+planner
- `test_merges_results` — final output combines subtask results

---

### Task 10: AuditLogger

**Files:**
- Create: `synapse/modules/security/audit.py`
- Create: `tests/modules/test_audit.py`

Immutable audit log via EventBus subscription. Listens to `tool_call_started`, `tool_call_completed`, `auth_decision` events.

Format: JSONL files in `.synapse/audit/YYYY-MM-DD.jsonl`, each line with HMAC signature.

Implementation:
- Subscribes to EventBus events in `__init__`
- `_on_event(event)` → writes AuditEntry as JSONL line
- `export(format="jsonl")` → returns all entries
- `query(session_id)` → filters by session
- HMAC signing using a session key

Tests (3 tests):
- `test_logs_tool_call_events` — subscribe to EventBus, emit events, verify written
- `test_hmac_verification` — tampered entry detected
- `test_export_and_query` — filter by session

---

### Task 11: Library API

**Files:**
- Create: `synapse/adapters/library.py`
- Modify: `synapse/__init__.py` — add public API exports

Clean Python API:
```python
from synapse import Synapse

agent = Synapse(provider="anthropic", model="claude-sonnet-4-6")
result = await agent.run("Fix the bug in auth.py")
```

`Synapse` class wraps Container assembly and Agent creation:
- `Synapse(config_path=None, **overrides)` → builds container, returns ready-to-use facade
- `run(task)` → creates Session, calls Agent.run()
- `run_sync(task)` → sync wrapper for non-async contexts

Tests (2 tests):
- `test_library_api_basic` — mock LLM, run simple task
- `test_library_api_config_override` — kwargs override YAML config

---

### Task 12: Update CLI for Phase 2

**Files:**
- Modify: `synapse/adapters/cli.py`

Register all new modules in `build_container()`:
- All 5 providers (selectable via `--provider` flag)
- ProjectMemory + UserMemory
- Context Partitioner + Compactor
- PlanExecutePlanner + HierarchicalPlanner
- AuditLogger

Add CLI flags:
- `--provider anthropic|openai|google|deepseek|ollama`
- `--model <model_id>`
- `--mode react|plan-execute|hierarchical`

---

### Task 13: Integration Tests

**Files:**
- Create: `tests/test_integration_phase2.py`

Full pipeline tests:
- `test_openai_provider_in_pipeline` — OpenAI mock + ReAct
- `test_plan_execute_pipeline` — PlanExecute mode with mock LLM
- `test_project_memory_pipeline` — task writes to project memory, next task reads it
- `test_audit_log_events` — events emitted, audit logger records them

---

### Task 14: Final Verification

- Run full test suite: `pytest tests/ -v`
- Verify all 5 providers are importable and testable
- Verify architecture boundaries still clean
- Update DEVELOPMENT.md

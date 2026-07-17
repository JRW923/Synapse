# Synapse Phase 4: External Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development

**Goal:** Phase 4 — HTTP Tool, Database Tool, Browser Tool, Qdrant backend for SemanticMemory.

## Global Constraints
- Python >= 3.11
- `modules/` imports from `protocols/` and `core/exceptions.py`, `core/events.py`
- New deps: `httpx`, `playwright`, `qdrant-client`
- TDD: test → fail → implement → pass → commit

---

### Task 1: HTTP Tool

Create `synapse/modules/tools/web.py` — HTTPTool for external API calls.
- Risk: EXTERNAL (requires explicit enable in auth)
- Uses httpx for async HTTP requests
- Supports GET/POST with headers and JSON body
- Timeout 30s, max response size 100KB

Tests (3): test_get, test_post_json, test_error_handling.

### Task 2: Database Tool

Create `synapse/modules/tools/db.py` — DBTool for SQL queries.
- Risk: EXTERNAL (requires explicit enable in auth)
- Uses sqlite3 (stdlib, no extra deps) for basic SQL
- Connect to specified .db file (within workspace only)
- Read-only by default (SELECT only); can be configured for writes

Tests (3): test_select_query, test_write_blocked_by_default, test_error_on_invalid_sql.

### Task 3: Browser Tool

Create `synapse/modules/tools/browser.py` — BrowserTool for web automation.
- Risk: EXTERNAL
- Uses Playwright async API
- Navigate to URL, extract text content, take screenshots
- Requires playwright to be installed

Tests (2): test_navigate_extracts_text (mock Playwright), test_tool_requires_external_auth.

### Task 4: Qdrant Backend

Create `synapse/modules/memory/qdrant_backend.py` — QdrantMemoryStore.
- Alternative to ChromaDB SemanticMemory
- Uses qdrant-client with local mode (no server needed)
- Same MemoryStore protocol, different backend
- Configurable in Synapse via memory_backend="qdrant"

Tests (3): test_store_retrieve_qdrant, test_similarity_ranking, test_level_isolation.

### Task 5: Wire + CLI + Final

- Register HTTPTool, DBTool, BrowserTool in ToolRegistry (all EXTERNAL, disabled by default)
- Add Qdrant as selectable memory backend in Synapse facade
- CLI: `--memory-backend chroma|qdrant`, `--enable-external-tools` flag
- Integration tests
- Run full test suite, verify boundaries, commit.

# Phase 3 Tasks 7-8: Wiring & Integration -- Final Report

**Date:** 2026-07-16
**Branch:** master
**Commit:** (pending)

## Summary

Phase 3 Tasks 7-8 wire all Phase 3 modules together, add CLI `eval` and `experiment` commands, and validate the integration with dedicated tests.

## Task 7: Wire Phase 3 Modules

### 7a. SemanticMemory in LayeredMemory

- **File:** `synapse/adapters/library.py`
- Added `SemanticMemory` to `LayeredMemory` constructor (optional parameter, default `None`)
- Routed `MemoryLevel.SEMANTIC` in `store()`, `retrieve()`, and `forget()` methods
- Created and wired `SemanticMemory()` in `Synapse._build_container()`, passed to `LayeredMemory`
- Import chain: `synapse.modules.memory.semantic.SemanticMemory`

### 7b. InjectionGuard in Context Pipeline

- **Files:** `synapse/adapters/library.py`, `synapse/core/agent.py`
- Registered `InjectionGuard` in the IoC container (library.py)
- Agent resolves `InjectionGuard` from container at init time (optional -- skips if not registered)
- After `_build_context()` returns, Agent calls `injection_guard.annotate(context)` to tag every context block with a `TrustLevel`
- Lazy import for `InjectionGuard` class to preserve `core/` -> `modules/` architectural boundary

### 7c. MetricsCollectors via EventBus (eval flag)

- **File:** `synapse/adapters/library.py`
- Added `enable_eval: bool = False` parameter to `Synapse.__init__()`
- When `enable_eval=True`, all four metrics collectors (`ProcessMetrics`, `QualityMetrics`, `EfficiencyMetrics`, `SafetyMetrics`) are created and subscribed to the `EventBus`
- Collectors are registered in the container and begin collecting immediately

### 7d. CLI `eval` and `experiment` Commands

- **File:** `synapse/adapters/cli.py`
- Added `synapse eval <benchmark>` supporting `process_quality` and `swebench`
- Added `synapse experiment` with `--name`, `--config-a`, `--config-b`, `--task`, `--runs` flags
- Both commands use asyncio internally with dedicated handler functions
- `serve` command was already operational -- verified

## Task 8: Integration Tests

- **File:** `tests/test_integration_phase3.py` (7 tests)
- **test_semantic_memory_pipeline** -- stores SEMANTIC entry via LayeredMemory, retrieves it, verifies content and tags
- **test_semantic_memory_wrong_level_returns_empty** -- SESSION query returns empty for SEMANTIC entries
- **test_http_server_run** -- FastAPI TestClient POST /run with mocked Synapse returns correct response fields
- **test_http_server_health** -- GET /health returns `{"status": "ok"}`
- **test_injection_guard_external_blocks_wrapped** -- EXTERNAL source blocks get `<external-content>` XML tags
- **test_injection_guard_non_external_blocks_plain** -- Non-EXTERNAL blocks returned as-is
- **test_injection_guard_trust_levels_per_source** -- All 9 ContextSource values map to correct TrustLevel

## Final Verification

### Test Results

```
128 passed, 2 skipped, 3 warnings in 70.72s
```

- **0 failures** across all 130 collected tests
- 2 skipped: Google provider tests (google-genai SDK not installed in CI)
- 3 warnings: Starlette deprecation (fastapi TestClient), Windows asyncio pipe cleanup (harmless)

### Architecture Boundaries

| Layer       | Depends On         | Verified |
|-------------|--------------------|----------|
| `core/`     | `protocols/` only  | Pass     |
| `modules/`  | `protocols/`       | Pass     |
| `eval/`     | `core/`, `protocols/` | Pass  |
| `adapters/` | everything         | Pass     |

`core/agent.py` uses a lazy import (`try/except ImportError`) for `InjectionGuard` to preserve the architectural boundary -- the import path exists but only resolves at runtime when the container provides it.

### Files Modified

| File                                  | Change                                            |
|---------------------------------------|---------------------------------------------------|
| `synapse/adapters/library.py`         | +SemanticMemory, +InjectionGuard, +MetricsCollectors, +enable_eval flag |
| `synapse/adapters/cli.py`             | +eval command, +experiment command                |
| `synapse/core/agent.py`               | +InjectionGuard resolution and annotate() call    |

### Files Created (this task)

| File                                  | Purpose                             |
|---------------------------------------|-------------------------------------|
| `tests/test_integration_phase3.py`    | 7 integration tests                 |

### Files Created (prior Phase 3 tasks, included in commit)

| File                                  | Purpose                             |
|---------------------------------------|-------------------------------------|
| `synapse/eval/`                       | Metrics, benchmarks, experiments    |
| `synapse/adapters/server.py`          | FastAPI HTTP server                 |
| `synapse/modules/memory/semantic.py`  | ChromaDB vector memory              |
| `synapse/modules/security/injection.py` | InjectionGuard                    |
| Various test files                    | eval/, server, injection, semantic  |

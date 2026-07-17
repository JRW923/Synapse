# Phase 4 Task 5: Final Report -- External Tools + Qdrant + CLI + Integration

**Date**: 2026-07-16
**Branch**: master

## Summary

Phase 4 Task 5 wires the three external tools (HTTPTool, DBTool, BrowserTool) into the Synapse facade, adds Qdrant memory backend support via CLI flags, and validates everything with integration tests.

## Changes Made

### 1. External Tools Wiring (`synapse/adapters/library.py`)

- Added try/except imports for `HTTPTool`, `DBTool`, `BrowserTool`
- Added `enable_external_tools: bool = False` parameter to `Synapse.__init__`
- Updated `_create_all_tools()` from static to instance method; registers external tools only when `enable_external_tools=True`
- Updated class docstring to document `enable_eval`, `memory_backend`, and `enable_external_tools`

### 2. CLI Updates (`synapse/adapters/cli.py`)

- Added `--memory-backend chromadb|qdrant` flag to `run` command
- Added `--enable-external-tools` flag to `run` command
- Added `--memory-backend chromadb|qdrant` flag to `serve` command
- Added `--enable-external-tools` flag to `serve` command
- Updated `run` handler to use `Synapse` facade (instead of raw `build_container`)
- Updated `serve` handler to create `Synapse` instance with flags and pass to `create_app()`

### 3. Dependencies (`pyproject.toml`)

- Added `pytest-httpx` to dev/test dependencies
- Added optional dependency groups: `test`, `http-tool`, `browser-tool`, `qdrant`, `all`

### 4. Integration Tests (`tests/test_integration_phase4.py`)

- `test_http_tool_in_pipeline` -- HTTPTool registered and callable when `enable_external_tools=True`
- `test_db_tool_select` -- DBTool executes SELECT on temporary SQLite database
- `test_external_tools_disabled_by_default` -- External tools blocked when flag is not set
- `test_qdrant_memory_backend` -- `Synapse(memory_backend="qdrant")` stores and retrieves semantic entries

## Test Results

```
========== 148 passed, 3 warnings in 75.09s ==========
```

| Metric   | Count |
|----------|-------|
| Passed   | 148   |
| Skipped  | 0     |
| Failed   | 0     |
| Warnings | 3     |

All 3 warnings are benign:
- Starlette deprecation warning (httpx vs httpx2) in FastAPI test client
- asyncio event-loop cleanup warnings on Windows (proactor pipe/subprocess transport)

## Architecture Notes

- External tools use `RiskLevel.EXTERNAL` and `ToolCategory.INTEGRATION`
- They are gated behind the `enable_external_tools` flag for security
- When disabled, only the 7 built-in tools (read, write, edit, glob, grep, shell, git) are registered
- DBTool receives `workspace_root` from config; other external tools do not need it
- The CLI `run` command now uses `Synapse` facade (consistent with `eval` and `experiment` commands)
- The CLI `serve` command creates a `Synapse` instance with user-specified flags and passes it to `create_app()`

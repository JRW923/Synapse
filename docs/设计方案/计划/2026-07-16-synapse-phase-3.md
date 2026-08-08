# Synapse Phase 3: Evaluation + Semantic Memory + HTTP API Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development

**Goal:** Phase 3 — Evaluation framework (metrics collectors + benchmarks + A/B experiments), SemanticMemory (Chroma vector DB), HTTP API (FastAPI server), Prompt Injection defense.

## Global Constraints

- Python >= 3.11
- `modules/` imports from `protocols/` and `core/exceptions.py`, `core/events.py`
- `eval/` consumes EventBus events, never imports from `modules/`
- All new deps: `chromadb`, `fastapi`, `uvicorn`
- TDD: test → fail → implement → pass → commit

---

### Task 1: Evaluation Metrics Collectors

**Files:**
- Create: `synapse/eval/__init__.py`
- Create: `synapse/eval/metrics/__init__.py`
- Create: `synapse/eval/metrics/process.py`
- Create: `synapse/eval/metrics/quality.py`
- Create: `synapse/eval/metrics/efficiency.py`
- Create: `synapse/eval/metrics/safety.py`
- Create: `tests/eval/__init__.py`
- Create: `tests/eval/test_metrics.py`

Four metric collectors that subscribe to EventBus and aggregate data:

**ProcessMetrics** (核心差异化):
- reuse_attempted, reuse_found, reuse_adopted (监听 tool_call_started)
- root_cause_accuracy (监听 agent_completed, 对比结果与意图)
- tests_persisted / test_persistence_rate (监听 file_written)
- instruction_drift_at_round (监听 tool_call_completed, heuristic)
- plan_quality_score, merge_quality_score (监听 plan_created, merge_result)
- thrashing_events, regex_abuse_events (监听 thrashing_detected)

**QualityMetrics**:
- complexity_delta, duplication_rate, function_length_violations
- test_coverage_delta, lint_errors_introduced

**EfficiencyMetrics**:
- tokens_input/output/cache_hit, tool_call_count, success_rate
- duration_ms, cost_estimate_usd, thrashing_ratio

**SafetyMetrics**:
- auth_blocks, sandbox_violations, injection_attempts
- out_of_workspace_access, dangerous_command_attempts

All collectors implement `MetricsCollector(Protocol)` with `reset()` and `snapshot()` methods.

Tests (4): each collector records correct events, snapshot returns expected structure.

---

### Task 2: Benchmark Runner

**Files:**
- Create: `synapse/eval/runner.py`
- Create: `synapse/eval/benchmarks/__init__.py`
- Create: `synapse/eval/benchmarks/swebench.py`
- Create: `synapse/eval/benchmarks/process_bench.py`
- Create: `tests/eval/test_runner.py`
- Create: `tests/eval/test_swebench.py`

**BenchmarkRunner**: executes a benchmark against Synapse Agent, collects metrics.
- `run(benchmark, agent_config) -> BenchmarkResult`
- Each task in the benchmark → Agent.run() → collect ProcessMetrics + EfficiencyMetrics

**SWEBenchAdapter**: adapts SWE-bench tasks with anti-contamination measures.
- `mutate_task(task, seed)` — template variation (synonym replacement, reordering)
- `filter_by_date(tasks, cutoff)` — time slicing
- `validate_with_private_tests(patch, private_tests)` — private test suite

**ProcessQualityBenchmark**: 自建 benchmark 专注过程质量.
- Tasks designed to test: existing-pattern-reuse, root-cause-identification, test-persistence, instruction-following
- Each task has expected process-quality scores

Tests (3): runner executes simple benchmark, SWE-bench mutation changes task text, process bench tasks are loadable.

---

### Task 3: A/B Experiment Framework

**Files:**
- Create: `synapse/eval/experiments.py`
- Create: `tests/eval/test_experiments.py`

**Experiment**: compares two agent configurations.
- `Experiment(id, name, variables, agent_config_a, agent_config_b, benchmark, runs_per_config)`
- `async run() -> ExperimentResult` — runs benchmark N times per config
- `ExperimentResult` has: metrics_a, metrics_b, p_value (t-test), winner

Tests (2): experiment runs both configs, result has p_value.

---

### Task 4: SemanticMemory

**Files:**
- Create: `synapse/modules/memory/semantic.py`
- Create: `tests/modules/test_semantic_memory.py`

Implements `MemoryStore` for `MemoryLevel.SEMANTIC`. Uses ChromaDB for vector storage.

- `store(entry)` → embed content, store in Chroma collection
- `retrieve(query, level=SEMANTIC, top_k)` → embed query, similarity search
- `forget(entry_id)` → delete from Chroma
- Uses Chroma's default embedding function (all-MiniLM-L6-v2)
- Store semantic memory entries tagged with source_task, priority

Tests (3): store_and_retrieve, similarity_ranking, wrong_level_returns_empty.

---

### Task 5: HTTP API Server

**Files:**
- Create: `synapse/adapters/server.py`
- Create: `tests/adapters/test_server.py`

FastAPI server wrapping Synapse:
- `POST /run` — run a task, return result
- `GET /sessions/{session_id}` — get session info
- `GET /sessions/{session_id}/messages` — get message history
- `POST /eval/experiment` — start an experiment (returns experiment_id)
- `GET /eval/experiment/{id}` — get experiment status/results
- `GET /health` — health check

Uses Synapse facade from Phase 2. Sessions stored in memory dict.

Tests (3): health check, run task, session history.

---

### Task 6: Prompt Injection Defense

**Files:**
- Create: `synapse/modules/security/injection.py`
- Create: `tests/modules/test_injection.py`

Implements annotation-based injection defense (per spec: "标注而非过滤").

**InjectionGuard**:
- `annotate(context: Context) -> Context` — classify each block by TrustLevel (SYSTEM/USER/DETERMINISTIC/EXTERNAL)
- `wrap_for_llm(block: ContextBlock) -> str` — wrap EXTERNAL blocks in `<external-content source="...">...</external-content>` tags
- Does NOT filter content — lets LLM judge trustworthiness

**TrustLevel assignment**:
- SYSTEM: project CLAUDE.md, security rules → TrustLevel.SYSTEM
- USER: user input → TrustLevel.USER
- DETERMINISTIC: grep/glob/AST results → TrustLevel.DETERMINISTIC
- EXTERNAL: web fetch, API responses, DB queries → TrustLevel.EXTERNAL

Tests (3): system blocks tagged SYSTEM, web content tagged EXTERNAL, wrapping adds tags.

---

### Task 7: Wire Phase 3 Modules into CLI + Library

**Files:**
- Modify: `synapse/adapters/library.py`
- Modify: `synapse/adapters/cli.py`

Register:
- SemanticMemory in LayeredMemory
- InjectionGuard in context pipeline
- HTTP API server entry (separate command: `synapse serve`)

Add CLI commands:
- `synapse serve` — start HTTP server
- `synapse eval <benchmark>` — run evaluation
- `synapse experiment <config_a> <config_b>` — run A/B experiment

---

### Task 8: Integration Tests + Final Verification

**Files:**
- Create: `tests/test_integration_phase3.py`

Integration tests:
- `test_semantic_memory_pipeline` — store + retrieve via Synapse
- `test_http_server_run` — start FastAPI test client, POST /run, check response
- `test_injection_guard_in_context` — EXTERNAL content gets wrapped
- `test_eval_metrics_in_pipeline` — run task, verify ProcessMetrics collector recorded events

Run full test suite. Verify architecture boundaries. Commit.

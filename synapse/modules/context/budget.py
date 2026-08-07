"""Dynamic budget allocation — pick a four-zone profile by task type.

Phase 3 (E): combines static task-type profiles with optional
historical feedback (persisted via ProjectMemory) to fine-tune the
ContextBudget for each task.
"""

from synapse.protocols.retriever import ContextBudget
from synapse.modules.context.classifier import TaskType


# Static profiles — one per TaskType.
# Percentages sum to ~1.0 per row. total_tokens is filled by the caller
# from ContextConfig.total_tokens or PlanningConfig.max_tokens_per_task.
TASK_BUDGET_PROFILES: dict[TaskType, dict] = {
    # Tests need lots of reference material (existing tests, fixtures, specs).
    TaskType.TEST:     {"system_pct": 0.10, "core_pct": 0.40, "reference_pct": 0.40, "overflow_pct": 0.10},
    # Refactor centres on the code being changed — core dominates.
    TaskType.REFACTOR: {"system_pct": 0.15, "core_pct": 0.60, "reference_pct": 0.20, "overflow_pct": 0.05},
    # Debug needs broad reference to locate the bug.
    TaskType.DEBUG:    {"system_pct": 0.10, "core_pct": 0.30, "reference_pct": 0.50, "overflow_pct": 0.10},
    # Default feature work — balanced.
    TaskType.FEATURE:  {"system_pct": 0.15, "core_pct": 0.50, "reference_pct": 0.25, "overflow_pct": 0.10},
    # Documentation leans on existing docs and reference material.
    TaskType.DOC:      {"system_pct": 0.20, "core_pct": 0.30, "reference_pct": 0.40, "overflow_pct": 0.10},
    # UNKNOWN — use the FEATURE defaults.
    TaskType.UNKNOWN:  {"system_pct": 0.15, "core_pct": 0.50, "reference_pct": 0.25, "overflow_pct": 0.10},
}


def select_budget(task_type: TaskType, total_tokens: int) -> ContextBudget:
    """Return a ContextBudget for the given task type.

    Args:
        task_type: classified task type.
        total_tokens: absolute token budget (already resolved from
            ContextConfig.total_tokens or PlanningConfig.max_tokens_per_task).
    """
    profile = TASK_BUDGET_PROFILES.get(task_type, TASK_BUDGET_PROFILES[TaskType.UNKNOWN])
    return ContextBudget(
        total_tokens=total_tokens,
        system_pct=profile["system_pct"],
        core_pct=profile["core_pct"],
        reference_pct=profile["reference_pct"],
        overflow_pct=profile["overflow_pct"],
    )


# ---- Historical feedback ------------------------------------------------

# Number of historical records to collect before adaptive adjustments kick in.
_MIN_SAMPLES_FOR_ADJUSTMENT = 3
# Max percentage-point shift per zone (avoids wild swings).
_MAX_SHIFT_PCT = 0.05


class BudgetHistory:
    """Tracks citation rates per TaskType across runs, persists to ProjectMemory.

    Lifecycle:
    - `record(task_type, citation_report)` — call after a task completes,
      passing the report from CitationTracker.
    - `suggest_adjustment(task_type, base_budget)` — if enough history
      exists, returns a fine-tuned ContextBudget; otherwise returns the
      base unchanged.

    Storage: persists to ProjectMemory under key `budget_history_{task_type}`
    as a small dict: {samples, cited_total, used_total, per_zone: {zone: [cited, used]}}.
    """

    def __init__(self, project_memory=None):
        self._memory = project_memory
        # In-memory cache of the merged, authoritative history per task type.
        self._cache: dict[TaskType, dict] = {}

    async def record(self, task_type: TaskType, citation_report: dict | None) -> None:
        """Record citation outcomes for a task type."""
        if citation_report is None:
            return
        blocks = citation_report.get("blocks", [])
        if not blocks:
            return

        stats = await self._load(task_type)
        stats["samples"] = stats.get("samples", 0) + 1
        stats["per_zone"] = stats.get("per_zone", {})

        per_zone: dict[str, list[int]] = {}
        for row in blocks:
            z = row["zone"]
            per_zone.setdefault(z, [0, 0])
            per_zone[z][0] += row["cited"]
            per_zone[z][1] += row["usage"]

        for z, (cited, used) in per_zone.items():
            cur = stats["per_zone"].setdefault(z, [0, 0])
            cur[0] += cited
            cur[1] += used

        self._cache[task_type] = stats

        if self._memory is not None:
            try:
                from synapse.protocols.memory import MemoryEntry, MemoryMetadata, MemoryLevel
                from datetime import datetime
                # Fixed id per task type so ProjectMemory.store replaces the
                # previous snapshot instead of appending one file per run
                # (the store is idempotent by id).
                entry = MemoryEntry(
                    id=f"budget_history_{task_type.value}",
                    content=f"budget_history {task_type.value}: {stats}",
                    level=MemoryLevel.PROJECT,
                    metadata=MemoryMetadata(
                        timestamp=datetime.now(),
                        tags=["budget_history", task_type.value],
                        source_task="budget_history",
                    ),
                )
                await self._memory.store(entry)
            except Exception:
                pass

    async def _load(self, task_type: TaskType) -> dict:
        """Load history for a task type, seeded from persisted ProjectMemory.

        Each persisted `budget_history` entry is a *cumulative snapshot*, so we
        take the one with the most samples as the canonical base (never sum
        snapshots — that would double-count).  The in-memory cache wins when it
        already holds a newer/more complete count for this session.
        """
        stats = dict(self._cache.get(task_type, {"samples": 0, "per_zone": {}}))
        if self._memory is not None:
            try:
                from synapse.protocols.memory import MemoryLevel
                import ast
                entries = await self._memory.retrieve(
                    "budget_history", MemoryLevel.PROJECT, top_k=50,
                )
                latest = None
                for entry in entries:
                    if f"budget_history {task_type.value}:" not in entry.content:
                        continue
                    seg = entry.content.split(":", 1)[1].strip()
                    snap = ast.literal_eval(seg)
                    if latest is None or snap.get("samples", 0) > latest.get("samples", 0):
                        latest = snap
                if latest is not None and latest.get("samples", 0) > stats.get("samples", 0):
                    stats = latest
                    self._cache[task_type] = latest
            except Exception:
                pass
        return stats

    async def suggest_adjustment(self, task_type: TaskType, base: ContextBudget) -> ContextBudget:
        """Suggest a budget adjustment based on historical citation rates.

        If samples < _MIN_SAMPLES_FOR_ADJUSTMENT, returns `base` unchanged.
        Otherwise computes per-zone citation rate and shifts budget
        toward zones with higher citation rates (capped at _MAX_SHIFT_PCT
        per zone, redistributed proportionally from low-citation zones).
        """
        stats = await self._load(task_type)
        if stats.get("samples", 0) < _MIN_SAMPLES_FOR_ADJUSTMENT:
            return base

        per_zone = stats.get("per_zone", {})
        # Compute citation rate per zone.
        rates: dict[str, float] = {}
        for z in ("system", "core", "reference", "overflow"):
            cited, used = per_zone.get(z, [0, 0])
            rates[z] = (cited / used) if used > 0 else 0.0

        avg_rate = sum(rates.values()) / max(1, len(rates))

        # Shift: zones above avg get +shift, zones below give up -shift.
        deltas: dict[str, float] = {}
        for z, r in rates.items():
            diff = r - avg_rate
            # Clamp to ±_MAX_SHIFT_PCT.
            deltas[z] = max(-_MAX_SHIFT_PCT, min(_MAX_SHIFT_PCT, diff * 0.1))

        # Normalize deltas so they sum to zero (budget-neutral shift).
        total_delta = sum(deltas.values())
        if abs(total_delta) > 1e-6 and len(deltas) > 0:
            scale = -total_delta / len(deltas)
            for z in deltas:
                deltas[z] += scale

        # Final clamp — enforce ±_MAX_SHIFT_PCT on the net change.
        for z in list(deltas.keys()):
            deltas[z] = max(-_MAX_SHIFT_PCT, min(_MAX_SHIFT_PCT, deltas[z]))

        return ContextBudget(
            total_tokens=base.total_tokens,
            system_pct=max(0.05, base.system_pct + deltas.get("system", 0.0)),
            core_pct=max(0.05, base.core_pct + deltas.get("core", 0.0)),
            reference_pct=max(0.05, base.reference_pct + deltas.get("reference", 0.0)),
            overflow_pct=max(0.02, base.overflow_pct + deltas.get("overflow", 0.0)),
        )

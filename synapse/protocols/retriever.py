"""Context Retriever Protocol."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Protocol
import uuid


class ContextSource(str, Enum):
    MEMORY = "memory"
    RETRIEVER = "retriever"
    GLOB = "glob"
    GREP = "grep"
    AST = "ast"
    GIT = "git"
    USER_INPUT = "user_input"
    WEB = "web"
    API = "api"
    DB = "db"


@dataclass
class ContextBlock:
    content: str
    source: ContextSource
    priority: int = 5  # 1-10, higher = harder to evict
    token_count: int = 0
    expires_after_phase: bool = False
    trust_annotation: "TrustAnnotation | None" = None
    # --- Phase E additions ---
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    # When this block is a compacted/derived version of another, record the source id.
    derived_from: str | None = None
    # Phase 4 — citation tracking / attention heatmap
    usage_count: int = 0       # how many LLM calls this block was sent in
    citation_count: int = 0   # how many times LLM output cited content from this block
    retrieved_at: datetime = field(default_factory=datetime.now)


@dataclass
class ContextBudget:
    total_tokens: int = 100_000
    system_pct: float = 0.15
    core_pct: float = 0.50
    reference_pct: float = 0.25
    overflow_pct: float = 0.10


@dataclass
class Context:
    system: list[ContextBlock] = field(default_factory=list)
    core: list[ContextBlock] = field(default_factory=list)
    reference: list[ContextBlock] = field(default_factory=list)
    overflow: list[ContextBlock] = field(default_factory=list)

    @property
    def all_blocks(self) -> list[ContextBlock]:
        return self.system + self.core + self.reference + self.overflow

    @property
    def total_tokens(self) -> int:
        return sum(b.token_count for b in self.all_blocks)


class ContextRetriever(Protocol):
    """Retrieves and organizes context for a task."""

    async def retrieve(
        self,
        task: str,
        project_root: Path,
        tools,  # ToolRegistry
        memory,  # MemoryStore
        budget: ContextBudget | None = None,
    ) -> Context: ...

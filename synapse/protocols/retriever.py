"""Context Retriever Protocol."""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol


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

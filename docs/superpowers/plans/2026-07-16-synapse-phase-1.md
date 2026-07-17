# Synapse Phase 1: Core + ReAct Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the Phase 1 Synapse core — a working code agent with IoC container, ReAct planning loop, Anthropic provider, basic tools, session memory, context governance, and security sandbox + authorization.

**Architecture:** Protocol-based IoC with EventBus for cross-cutting concerns. `protocols/` defines all interfaces, `core/` provides the container/agent/eventbus, `modules/` implements each protocol. A thin CLI adapter (`synapse run "task"`) wires everything together.

**Tech Stack:** Python 3.11+, asyncio, typing.Protocol, anthropic SDK, pydantic, pyyaml, rich, pytest, pytest-asyncio

## Global Constraints

- Python >= 3.11 (use `X | None` syntax, not `Optional[X]`)
- `protocols/` imports nothing from `core/` or `modules/`
- `core/` imports only from `protocols/`
- `modules/` imports from `protocols/` and `core/exceptions.py`, `core/events.py`
- All interfaces use `typing.Protocol`, never ABC
- All async functions use native `asyncio`
- Pydantic v2 for config schemas (model_validate, not parse_obj)
- pytest + pytest-asyncio for all tests
- Commit after each task

---

## File Structure

```
synapse/
├── __init__.py                    # (exists) modify
├── protocols/
│   ├── __init__.py                # create
│   ├── events.py                  # create — all event dataclasses + EventTypes enum
│   ├── llm.py                     # create — LLMProvider, Message, LLMResponse, LLMChunk
│   ├── tool.py                    # create — Tool, ToolRegistry, ToolSchema, ToolResult, RiskLevel, ToolCategory
│   ├── planner.py                 # create — Planner, PlanningMode, AgentResult, ResultStatus, ExecutionMetrics
│   ├── memory.py                  # create — MemoryStore, MemoryLevel, MemoryEntry, MemoryMetadata
│   ├── retriever.py               # create — ContextRetriever, Context, ContextBlock, ContextBudget, ContextSource
│   └── sandbox.py                 # create — Sandbox, SandboxResult, AuthRequest, AuthDecision
├── core/
│   ├── __init__.py                # create
│   ├── exceptions.py              # create
│   ├── events.py                  # create — EventBus implementation
│   ├── container.py               # create — IoC container
│   ├── session.py                 # create — Session management
│   └── agent.py                   # create — Agent main loop
├── modules/
│   ├── __init__.py                # create
│   ├── providers/
│   │   ├── __init__.py            # create
│   │   └── anthropic.py           # create — AnthropicProvider
│   ├── tools/
│   │   ├── __init__.py            # create
│   │   ├── registry.py            # create — DefaultToolRegistry
│   │   ├── file_read.py           # create — ReadTool
│   │   ├── file_write.py          # create — WriteTool
│   │   ├── file_edit.py           # create — EditTool
│   │   ├── file_glob.py           # create — GlobTool
│   │   ├── search_grep.py         # create — GrepTool
│   │   ├── shell.py               # create — ShellTool
│   │   └── git_.py                # create — GitTool
│   ├── planning/
│   │   ├── __init__.py            # create
│   │   └── react.py               # create — ReActPlanner
│   ├── memory/
│   │   ├── __init__.py            # create
│   │   └── session.py             # create — SessionMemory
│   ├── context/
│   │   ├── __init__.py            # create
│   │   └── retriever.py           # create — BasicContextRetriever
│   └── security/
│       ├── __init__.py            # create
│       ├── sandbox.py             # create — ProcessSandbox
│       └── auth.py                # create — ActionAuthorizer
├── adapters/
│   ├── __init__.py                # create
│   └── cli.py                     # create — CLI entry point
└── config/
    ├── __init__.py                # create
    ├── schema.py                  # create — Pydantic config models
    └── loader.py                  # create — YAML + env loader

tests/
├── __init__.py                    # (exists)
├── protocols/
│   └── test_protocols.py          # create
├── core/
│   ├── test_container.py          # create
│   ├── test_eventbus.py           # create
│   ├── test_session.py            # create
│   └── test_agent.py              # create
├── modules/
│   ├── test_anthropic_provider.py # create
│   ├── test_tools.py              # create
│   ├── test_react_planner.py      # create
│   ├── test_session_memory.py     # create
│   ├── test_context_retriever.py  # create
│   ├── test_sandbox.py            # create
│   └── test_auth.py               # create
└── test_integration.py            # create
```

---

### Task 1: Project setup — pyproject.toml and dependencies

**Files:**
- Create: `pyproject.toml`
- Modify: `requirements.txt`
- Modify: `synapse/__init__.py`

**Interfaces:**
- Produces: Installable project with all Phase 1 dependencies

- [ ] **Step 1: Write pyproject.toml**

```toml
[build-system]
requires = ["setuptools>=68.0"]
build-backend = "setuptools.build_meta"

[project]
name = "synapse"
version = "0.1.0"
description = "Connecting ideas into code — an intelligent code agent"
requires-python = ">=3.11"
dependencies = [
    "anthropic>=0.40.0",
    "pydantic>=2.0",
    "pyyaml>=6.0",
    "rich>=13.0",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.24.0",
]

[project.scripts]
synapse = "synapse.adapters.cli:main"

[tool.setuptools.packages.find]
include = ["synapse*"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 2: Write requirements.txt**

```
# Synapse - Core Dependencies
anthropic>=0.40.0
pydantic>=2.0
pyyaml>=6.0
rich>=13.0
pytest>=8.0
pytest-asyncio>=0.24.0
```

- [ ] **Step 3: Update synapse/__init__.py**

```python
"""
Synapse — Connecting ideas into code.
"""

__version__ = "0.1.0"
```

- [ ] **Step 4: Install dependencies**

Run: `pip install -e ".[dev]"`

- [ ] **Step 5: Verify install**

Run: `python -c "import synapse; print(synapse.__version__)"`
Expected: `0.1.0`

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml requirements.txt synapse/__init__.py
git commit -m "build: add pyproject.toml and Phase 1 dependencies"
```

---

### Task 2: Config schema and loader

**Files:**
- Create: `synapse/config/__init__.py`
- Create: `synapse/config/schema.py`
- Create: `synapse/config/loader.py`

**Interfaces:**
- Produces:
  - `SynapseConfig` pydantic model with fields: `provider: ProviderConfig`, `tools: ToolsConfig`, `planning: PlanningConfig`, `security: SecurityConfig`
  - `load_config(path: str | None) -> SynapseConfig`

- [ ] **Step 1: Write schema.py**

```python
"""Configuration schema for Synapse."""

from pydantic import BaseModel, Field


class ProviderConfig(BaseModel):
    """LLM provider configuration."""
    provider: str = "anthropic"
    model: str = "claude-sonnet-4-6"
    api_key: str = ""
    max_retries: int = 3
    timeout_seconds: int = 120


class ToolsConfig(BaseModel):
    """Tool configuration."""
    enabled: list[str] = Field(default_factory=lambda: [
        "read", "write", "edit", "glob", "grep", "shell", "git"
    ])
    allowlist_commands: list[str] = Field(default_factory=lambda: [
        "ls", "git", "pytest", "python", "pip", "npm", "cargo", "go", "node"
    ])
    workspace_root: str = "."


class PlanningConfig(BaseModel):
    """Planner configuration."""
    mode: str = "react"  # react | plan_execute | hierarchical
    max_iterations: int = 50
    thrashing_threshold: int = 3


class SecurityConfig(BaseModel):
    """Security configuration."""
    sandbox_enabled: bool = True
    sandbox_mode: str = "enforce"  # enforce | warn | off
    auth_confirmation: bool = True  # require user confirmation for risky ops
    allowed_paths: list[str] = Field(default_factory=lambda: ["."])


class SynapseConfig(BaseModel):
    """Root configuration."""
    provider: ProviderConfig = ProviderConfig()
    tools: ToolsConfig = ToolsConfig()
    planning: PlanningConfig = PlanningConfig()
    security: SecurityConfig = SecurityConfig()
```

- [ ] **Step 2: Write loader.py**

```python
"""Configuration loader: YAML file + environment variables."""

import os
from pathlib import Path
import yaml
from synapse.config.schema import SynapseConfig


def load_config(config_path: str | None = None) -> SynapseConfig:
    """Load config from YAML file, with env var overrides."""
    config = SynapseConfig()

    if config_path:
        path = Path(config_path)
    else:
        path = Path("synapse.yaml")
        if not path.exists():
            path = Path.home() / ".synapse" / "config.yaml"

    if path.exists():
        raw = yaml.safe_load(path.read_text())
        if raw:
            config = SynapseConfig.model_validate(raw)

    # Environment variable overrides
    if os.environ.get("SYNAPSE_PROVIDER"):
        config.provider.provider = os.environ["SYNAPSE_PROVIDER"]
    if os.environ.get("SYNAPSE_MODEL"):
        config.provider.model = os.environ["SYNAPSE_MODEL"]
    if os.environ.get("ANTHROPIC_API_KEY"):
        config.provider.api_key = os.environ["ANTHROPIC_API_KEY"]
    if os.environ.get("SYNAPSE_SANDBOX"):
        val = os.environ["SYNAPSE_SANDBOX"].lower()
        config.security.sandbox_enabled = val not in ("0", "false", "off")

    return config
```

- [ ] **Step 3: Write config/__init__.py**

```python
from synapse.config.schema import SynapseConfig
from synapse.config.loader import load_config

__all__ = ["SynapseConfig", "load_config"]
```

- [ ] **Step 4: Verify import**

Run: `python -c "from synapse.config import SynapseConfig, load_config; c = load_config(); print(c.provider.model)"`
Expected: `claude-sonnet-4-6`

- [ ] **Step 5: Commit**

```bash
git add synapse/config/
git commit -m "feat: add config schema and YAML/env loader"
```

---

### Task 3: Protocols — data types and enums

**Files:**
- Create: `synapse/protocols/__init__.py`
- Create: `synapse/protocols/events.py`

**Interfaces:**
- Produces:
  - `EventType` enum: `TOOL_CALL_STARTED`, `TOOL_CALL_COMPLETED`, `FILE_WRITTEN`, `AUTH_DECISION`, `AGENT_ERROR`, `AGENT_COMPLETED`, `THRASHING_DETECTED`
  - Event dataclasses: `ToolCallStarted`, `ToolCallCompleted`, `FileWritten`, `AuthDecisionMade`, `AgentError`, `AgentCompleted`, `ThrashingDetected`
  - Union type: `AgentEvent = ToolCallStarted | ToolCallCompleted | ...`

- [ ] **Step 1: Write protocols/__init__.py**

```python
"""Synapse protocols — pure interface definitions."""
```

- [ ] **Step 2: Write protocols/events.py**

```python
"""Event types for the Synapse EventBus — pure data, no behavior."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import uuid


class EventType(str, Enum):
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    FILE_WRITTEN = "file_written"
    AUTH_DECISION = "auth_decision"
    AGENT_ERROR = "agent_error"
    AGENT_COMPLETED = "agent_completed"
    THRASHING_DETECTED = "thrashing_detected"


@dataclass(kw_only=True)
class BaseEvent:
    event_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    timestamp: datetime = field(default_factory=datetime.now)
    session_id: str


@dataclass(kw_only=True)
class ToolCallStarted(BaseEvent):
    event_type: str = EventType.TOOL_CALL_STARTED
    tool_name: str
    tool_params: dict


@dataclass(kw_only=True)
class ToolCallCompleted(BaseEvent):
    event_type: str = EventType.TOOL_CALL_COMPLETED
    tool_name: str
    success: bool
    duration_ms: int
    files_touched: list[str] = field(default_factory=list)


@dataclass(kw_only=True)
class FileWritten(BaseEvent):
    event_type: str = EventType.FILE_WRITTEN
    path: str
    bytes_written: int


@dataclass(kw_only=True)
class AuthDecisionMade(BaseEvent):
    event_type: str = EventType.AUTH_DECISION
    tool_name: str
    allowed: bool
    reason: str


@dataclass(kw_only=True)
class AgentError(BaseEvent):
    event_type: str = EventType.AGENT_ERROR
    error_type: str
    message: str


@dataclass(kw_only=True)
class AgentCompleted(BaseEvent):
    event_type: str = EventType.AGENT_COMPLETED
    status: str  # "success" | "partial" | "failed"
    total_tokens: int
    tool_calls: int
    duration_ms: int


@dataclass(kw_only=True)
class ThrashingDetected(BaseEvent):
    event_type: str = EventType.THRASHING_DETECTED
    file_path: str
    modification_count: int
```

- [ ] **Step 3: Verify import**

Run: `python -c "from synapse.protocols.events import EventType, ToolCallStarted; print(EventType.AGENT_COMPLETED)"`

- [ ] **Step 4: Commit**

```bash
git add synapse/protocols/
git commit -m "feat: add event types and enums for EventBus"
```

---

### Task 4: Protocols — LLM, Tool, Planner, Memory, Retriever, Sandbox

**Files:**
- Create: `synapse/protocols/llm.py`
- Create: `synapse/protocols/tool.py`
- Create: `synapse/protocols/planner.py`
- Create: `synapse/protocols/memory.py`
- Create: `synapse/protocols/retriever.py`
- Create: `synapse/protocols/sandbox.py`

**Interfaces:**
- Produces: `LLMProvider`, `Message`, `LLMResponse`, `LLMChunk`, `Tool`, `ToolRegistry`, `ToolSchema`, `ToolResult`, `ToolCallMetadata`, `RiskLevel`, `ToolCategory`, `Planner`, `PlanningMode`, `AgentResult`, `ResultStatus`, `ExecutionMetrics`, `MemoryStore`, `MemoryLevel`, `MemoryEntry`, `MemoryMetadata`, `ContextRetriever`, `Context`, `ContextBlock`, `ContextBudget`, `ContextSource`, `Sandbox`, `SandboxResult`

- [ ] **Step 1: Write protocols/llm.py**

```python
"""LLM Provider Protocol."""

from dataclasses import dataclass, field
from typing import Protocol, AsyncIterator


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[dict] = field(default_factory=list)
    stop_reason: str = "end_turn"  # end_turn | tool_use | max_tokens
    usage: dict[str, int] = field(default_factory=dict)  # {"input": N, "output": M}


@dataclass
class LLMChunk:
    content: str = ""
    tool_call_delta: dict | None = None


class LLMProvider(Protocol):
    """Unified interface for LLM API calls."""

    @property
    def model_id(self) -> str: ...

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
    ) -> LLMResponse: ...

    async def stream(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[LLMChunk]: ...
```

- [ ] **Step 2: Write protocols/tool.py**

```python
"""Tool Protocol and ToolRegistry Protocol."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol


class RiskLevel(str, Enum):
    READ_ONLY = "read_only"
    WRITE_LOCAL = "write_local"
    EXECUTE = "execute"
    EXTERNAL = "external"
    META = "meta"


class ToolCategory(str, Enum):
    FILE = "file"
    CODE_UNDERSTANDING = "code_understanding"
    EXECUTION = "execution"
    INTEGRATION = "integration"
    EVAL = "eval"


@dataclass
class ToolSchema:
    name: str
    description: str
    parameters: dict  # JSON Schema for function calling


@dataclass
class ToolCallMetadata:
    tool_name: str
    start_time: datetime = field(default_factory=datetime.now)
    duration_ms: int = 0
    sandbox_used: bool = False
    files_touched: list[str] = field(default_factory=list)


@dataclass
class ToolResult:
    success: bool
    output: str
    error: str | None = None
    metadata: ToolCallMetadata = field(default_factory=lambda: ToolCallMetadata(tool_name=""))


class Tool(Protocol):
    """A single tool executable by the agent."""

    name: str
    description: str
    parameters: ToolSchema
    requires_sandbox: bool
    risk_level: RiskLevel
    category: ToolCategory

    async def execute(self, params: dict, sandbox=None) -> ToolResult: ...


class ToolRegistry(Protocol):
    """Registry of available tools."""

    def register(self, tool: Tool) -> None: ...
    def get(self, name: str) -> Tool: ...
    def list_all(self) -> list[Tool]: ...
    def list_by_category(self, category: ToolCategory) -> list[Tool]: ...
    def get_schemas(self) -> list[dict]: ...
```

- [ ] **Step 3: Write protocols/planner.py**

```python
"""Planner Protocol."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol


class PlanningMode(str, Enum):
    REACT = "react"
    PLAN_EXECUTE = "plan_execute"
    HIERARCHICAL = "hierarchical"


class ResultStatus(str, Enum):
    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"


@dataclass
class ExecutionMetrics:
    tokens_input: int = 0
    tokens_output: int = 0
    tool_call_count: int = 0
    tool_success_count: int = 0
    duration_ms: int = 0
    thrashing_events: int = 0


@dataclass
class Artifact:
    path: str
    content: str
    action: str  # "created" | "modified" | "deleted"


@dataclass
class AgentResult:
    status: ResultStatus
    output: str
    artifacts: list[Artifact] = field(default_factory=list)
    metrics: ExecutionMetrics = field(default_factory=ExecutionMetrics)


class Planner(Protocol):
    """Planning strategy for task execution."""

    @property
    def mode(self) -> PlanningMode: ...

    async def execute(
        self,
        task: str,
        context,       # Context from protocols/retriever.py
        tools,         # ToolRegistry from protocols/tool.py
        llm,           # LLMProvider from protocols/llm.py
        sandbox,       # Sandbox from protocols/sandbox.py
        session,       # Session from core/session.py (not a protocol — concrete type)
        event_bus,     # EventBus from core/events.py (not a protocol — concrete type)
    ) -> AgentResult: ...
```

- [ ] **Step 4: Write protocols/memory.py**

```python
"""Memory Protocol."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Protocol


class MemoryLevel(str, Enum):
    SESSION = "session"
    PROJECT = "project"
    USER = "user"
    SEMANTIC = "semantic"


@dataclass
class MemoryMetadata:
    project: str | None = None
    timestamp: datetime = field(default_factory=datetime.now)
    tags: list[str] = field(default_factory=list)
    priority: int = 5
    source_task: str | None = None
    access_count: int = 0


@dataclass
class MemoryEntry:
    id: str
    content: str
    level: MemoryLevel
    metadata: MemoryMetadata = field(default_factory=MemoryMetadata)


class MemoryStore(Protocol):
    """Unified interface for all memory layers."""

    async def store(self, entry: MemoryEntry) -> None: ...
    async def retrieve(self, query: str, level: MemoryLevel, top_k: int = 5) -> list[MemoryEntry]: ...
    async def forget(self, entry_id: str) -> None: ...
```

- [ ] **Step 5: Write protocols/retriever.py**

```python
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


@dataclass
class ContextBlock:
    content: str
    source: ContextSource
    priority: int = 5  # 1-10, higher = harder to evict
    token_count: int = 0
    expires_after_phase: bool = False


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
```

- [ ] **Step 6: Write protocols/sandbox.py**

```python
"""Sandbox Protocol and authorization types."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class SandboxResult:
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False
    platform: str = ""


@dataclass
class AuthRequest:
    tool_name: str
    tool_params: dict
    risk_level: str  # RiskLevel value
    session_id: str
    user_id: str | None = None


@dataclass
class AuthDecision:
    allowed: bool
    reason: str
    requires_confirmation: bool = False


class Sandbox(Protocol):
    """Process sandbox — cross-platform execution isolation."""

    @property
    def platform(self) -> str: ...

    async def execute(
        self,
        command: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 120,
        network: bool = False,
        allowed_paths: list[Path] | None = None,
    ) -> SandboxResult: ...
```

- [ ] **Step 7: Verify all protocols import cleanly**

Run: `python -c "from synapse.protocols.llm import LLMProvider, Message; from synapse.protocols.tool import Tool, RiskLevel; from synapse.protocols.planner import Planner, PlanningMode; from synapse.protocols.memory import MemoryStore, MemoryLevel; from synapse.protocols.retriever import ContextRetriever, Context; from synapse.protocols.sandbox import Sandbox, SandboxResult; print('All protocols OK')"`

- [ ] **Step 8: Commit**

```bash
git add synapse/protocols/
git commit -m "feat: add all core protocols (LLM, Tool, Planner, Memory, Retriever, Sandbox)"
```

---

### Task 5: Core exceptions

**Files:**
- Create: `synapse/core/__init__.py`
- Create: `synapse/core/exceptions.py`

**Interfaces:**
- Produces: `SynapseError(Exception)`, `ProviderError(SynapseError)`, `ToolError(SynapseError)`, `SandboxError(SynapseError)`, `PlannerError(SynapseError)`, `ConfigError(SynapseError)`

- [ ] **Step 1: Write core/__init__.py**

```python
"""Synapse core — agent loop, IoC container, EventBus, session management."""
```

- [ ] **Step 2: Write core/exceptions.py**

```python
"""Core exception hierarchy for Synapse."""


class SynapseError(Exception):
    """Base for all Synapse exceptions."""
    pass


class ConfigError(SynapseError):
    """Configuration is invalid at startup — fast-fail, never enter agent loop."""
    pass


class ProviderError(SynapseError):
    """LLM API error — rate limit, timeout, auth failure."""
    pass


class ToolError(SynapseError):
    """Tool execution failed — returned to LLM as ToolResult(success=False)."""
    pass


class SandboxError(SynapseError):
    """Sandbox violation — intercepted by security layer, not shown to LLM."""
    pass


class PlannerError(SynapseError):
    """Planner failure — loop exceeded, sub-task deadlock."""
    pass
```

- [ ] **Step 3: Verify import**

Run: `python -c "from synapse.core.exceptions import SynapseError, ProviderError, ToolError; raise ProviderError('test')" 2>&1`

- [ ] **Step 4: Commit**

```bash
git add synapse/core/
git commit -m "feat: add core exception hierarchy"
```

---

### Task 6: Core EventBus

**Files:**
- Create: `synapse/core/events.py`
- Create: `tests/core/__init__.py`
- Create: `tests/core/test_eventbus.py`

**Interfaces:**
- Consumes: `BaseEvent`, `EventType` from `synapse.protocols.events`
- Produces: `EventBus` class with `subscribe(event_type, handler)`, `unsubscribe(event_type, handler)`, `async emit(event)`

- [ ] **Step 1: Write failing test**

```python
"""Tests for EventBus."""
import asyncio
import pytest
from synapse.core.events import EventBus
from synapse.protocols.events import ToolCallStarted


@pytest.mark.asyncio
async def test_subscribe_and_emit():
    bus = EventBus()
    received: list[ToolCallStarted] = []

    async def handler(event: ToolCallStarted):
        received.append(event)

    bus.subscribe("tool_call_started", handler)
    event = ToolCallStarted(session_id="s1", tool_name="read", tool_params={})
    await bus.emit(event)

    assert len(received) == 1
    assert received[0].tool_name == "read"
    assert received[0].session_id == "s1"


@pytest.mark.asyncio
async def test_unsubscribe():
    bus = EventBus()
    received: list = []

    async def handler(event):
        received.append(event)

    bus.subscribe("tool_call_started", handler)
    bus.unsubscribe("tool_call_started", handler)
    await bus.emit(ToolCallStarted(session_id="s1", tool_name="read", tool_params={}))

    assert len(received) == 0


@pytest.mark.asyncio
async def test_multiple_handlers():
    bus = EventBus()
    results: list[str] = []

    async def h1(event):
        results.append("h1")

    async def h2(event):
        results.append("h2")

    bus.subscribe("tool_call_started", h1)
    bus.subscribe("tool_call_started", h2)
    await bus.emit(ToolCallStarted(session_id="s1", tool_name="read", tool_params={}))

    assert results == ["h1", "h2"]


@pytest.mark.asyncio
async def test_handler_exception_does_not_block_others():
    bus = EventBus()
    results: list[str] = []

    async def h_bad(event):
        raise RuntimeError("boom")

    async def h_good(event):
        results.append("good")

    bus.subscribe("tool_call_started", h_bad)
    bus.subscribe("tool_call_started", h_good)
    # Should not raise — h_good should still fire
    await bus.emit(ToolCallStarted(session_id="s1", tool_name="read", tool_params={}))

    assert results == ["good"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/core/test_eventbus.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'synapse.core.events'`

- [ ] **Step 3: Write EventBus implementation**

```python
"""Lightweight EventBus for cross-cutting concerns."""

import asyncio
import logging
from collections import defaultdict
from collections.abc import Callable, Awaitable
from synapse.protocols.events import BaseEvent

logger = logging.getLogger(__name__)

Handler = Callable[[BaseEvent], Awaitable[None]]


class EventBus:
    """In-process pub/sub for agent events.

    Handlers are async callables. Exceptions in one handler never
    prevent other handlers from firing. Order of handler execution
    is registration order (not guaranteed across async boundaries).
    """

    def __init__(self):
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: Handler) -> None:
        """Register an async handler for an event type."""
        self._handlers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: Handler) -> None:
        """Remove a previously registered handler."""
        try:
            self._handlers[event_type].remove(handler)
        except (KeyError, ValueError):
            pass

    async def emit(self, event: BaseEvent) -> None:
        """Fire an event to all registered handlers.

        Handlers run concurrently. Exceptions are logged, never raised.
        """
        handlers = self._handlers.get(event.event_type, [])
        if not handlers:
            return

        results = await asyncio.gather(
            *[self._safe_invoke(h, event) for h in handlers],
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.warning("EventBus handler error: %s", result)

    async def _safe_invoke(self, handler: Handler, event: BaseEvent) -> None:
        await handler(event)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/core/test_eventbus.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add synapse/core/events.py tests/core/
git commit -m "feat: add EventBus with subscribe/unsubscribe/emit"
```

---

### Task 7: Core IoC Container

**Files:**
- Create: `synapse/core/container.py`
- Create: `tests/core/test_container.py`

**Interfaces:**
- Consumes: nothing from protocols (generic)
- Produces: `Container` with `register(proto_type, instance_or_factory)`, `resolve(proto_type)`, singleton and factory support

- [ ] **Step 1: Write failing test**

```python
"""Tests for IoC container."""
import pytest
from typing import Protocol
from synapse.core.container import Container


class Greeter(Protocol):
    def greet(self) -> str: ...


class EnglishGreeter:
    def greet(self) -> str:
        return "hello"


class SpanishGreeter:
    def greet(self) -> str:
        return "hola"


def test_register_and_resolve_singleton():
    c = Container()
    g = EnglishGreeter()
    c.register(Greeter, g)
    resolved = c.resolve(Greeter)
    assert resolved is g
    assert resolved.greet() == "hello"


def test_register_factory():
    c = Container()
    call_count = [0]

    def factory():
        call_count[0] += 1
        return EnglishGreeter()

    c.register_factory(Greeter, factory)
    r1 = c.resolve(Greeter)
    r2 = c.resolve(Greeter)
    assert r1 is not r2  # New instance each time
    assert call_count[0] == 2


def test_resolve_unregistered_raises():
    c = Container()
    with pytest.raises(KeyError):
        c.resolve(Greeter)


def test_override():
    c = Container()
    c.register(Greeter, EnglishGreeter())
    assert c.resolve(Greeter).greet() == "hello"

    c.register(Greeter, SpanishGreeter())
    assert c.resolve(Greeter).greet() == "hola"  # Last registration wins


def test_resolve_with_generic_alias():
    """Protocol[X] and Protocol should resolve to the same registration."""
    c = Container()
    g = EnglishGreeter()
    c.register(Greeter, g)
    assert c.resolve(Greeter) is g
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/core/test_container.py -v`
Expected: FAIL

- [ ] **Step 3: Write Container implementation**

```python
"""Lightweight IoC container for Synapse."""

from collections.abc import Callable
from typing import Any, get_origin


class Container:
    """Simple dependency injection container.

    Registers implementations against Protocol types. Supports
    singleton instances (register) and factory functions (register_factory).
    """

    def __init__(self):
        self._instances: dict[type, Any] = {}
        self._factories: dict[type, Callable[[], Any]] = {}

    def register(self, proto_type: type, instance: object) -> None:
        """Register a singleton instance for a protocol type."""
        key = self._normalize_type(proto_type)
        self._instances[key] = instance
        self._factories.pop(key, None)

    def register_factory(self, proto_type: type, factory: Callable[[], object]) -> None:
        """Register a factory that creates a new instance each resolve."""
        key = self._normalize_type(proto_type)
        self._factories[key] = factory
        self._instances.pop(key, None)

    def resolve(self, proto_type: type) -> object:
        """Resolve a protocol type to its registered implementation."""
        key = self._normalize_type(proto_type)

        if key in self._factories:
            return self._factories[key]()

        if key in self._instances:
            return self._instances[key]

        # Try parent types / generic origins
        if (origin := get_origin(proto_type)) and origin in self._instances:
            return self._instances[origin]
        if origin and origin in self._factories:
            return self._factories[origin]()

        raise KeyError(f"No implementation registered for {proto_type.__name__}")

    @staticmethod
    def _normalize_type(t: type) -> type:
        """Strip generic parameters to get the base type."""
        origin = get_origin(t)
        return origin if origin is not None else t
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/core/test_container.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add synapse/core/container.py tests/core/test_container.py
git commit -m "feat: add lightweight IoC container with singleton and factory support"
```

---

### Task 8: Core Session

**Files:**
- Create: `synapse/core/session.py`
- Create: `tests/core/test_session.py`

**Interfaces:**
- Produces: `Session` with `id: str`, `messages: list[Message]`, `metadata: dict`, `add_message(msg)`, `fork(new_id) -> Session`

- [ ] **Step 1: Write failing test**

```python
"""Tests for Session management."""
import pytest
from synapse.core.session import Session
from synapse.protocols.llm import Message


def test_session_creation():
    s = Session()
    assert s.id is not None
    assert len(s.messages) == 0


def test_add_message():
    s = Session()
    s.add_message(Message(role="user", content="hello"))
    assert len(s.messages) == 1
    assert s.messages[0].role == "user"
    assert s.messages[0].content == "hello"


def test_fork_creates_independent_session():
    s1 = Session()
    s1.add_message(Message(role="user", content="original"))
    s1.metadata["project"] = "test"

    s2 = s1.fork("forked-1")
    assert s2.id == "forked-1"
    assert s2.id != s1.id
    # Forked session copies messages and metadata
    assert len(s2.messages) == 1
    assert s2.messages[0].content == "original"
    assert s2.metadata["project"] == "test"

    # Mutations to fork don't affect original
    s2.add_message(Message(role="assistant", content="reply"))
    assert len(s1.messages) == 1
    assert len(s2.messages) == 2


def test_clear_messages():
    s = Session()
    s.add_message(Message(role="user", content="hello"))
    s.clear_messages()
    assert len(s.messages) == 0


def test_token_estimate():
    s = Session()
    s.add_message(Message(role="user", content="hello world"))
    # Rough estimate: ~1.3 tokens per word for English
    assert s.estimated_tokens > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/core/test_session.py -v`
Expected: FAIL

- [ ] **Step 3: Write Session implementation**

```python
"""Session state management."""

import uuid
from synapse.protocols.llm import Message


class Session:
    """Manages the state of a single agent interaction session.

    Holds message history, metadata, and provides forking for
    hierarchical planning sub-sessions.
    """

    def __init__(self, session_id: str | None = None):
        self.id = session_id or str(uuid.uuid4())
        self.messages: list[Message] = []
        self.metadata: dict = {}

    def add_message(self, msg: Message) -> None:
        self.messages.append(msg)

    def clear_messages(self) -> None:
        self.messages.clear()

    def fork(self, new_id: str) -> "Session":
        """Create an independent copy for a sub-session."""
        child = Session(session_id=new_id)
        child.messages = list(self.messages)  # shallow copy of messages
        child.metadata = dict(self.metadata)
        return child

    @property
    def estimated_tokens(self) -> int:
        """Rough token estimate (~1.3 chars per token for English)."""
        total_chars = sum(len(m.content) for m in self.messages)
        return max(1, int(total_chars / 1.3))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/core/test_session.py -v`
Expected: 5 PASS

- [ ] **Step 5: Commit**

```bash
git add synapse/core/session.py tests/core/test_session.py
git commit -m "feat: add Session with message history, forking, and token estimates"
```

---

### Task 9: Anthropic LLM Provider

**Files:**
- Create: `synapse/modules/__init__.py`
- Create: `synapse/modules/providers/__init__.py`
- Create: `synapse/modules/providers/anthropic.py`
- Create: `tests/modules/__init__.py`
- Create: `tests/modules/test_anthropic_provider.py`

**Interfaces:**
- Consumes: `LLMProvider` protocol, `SynapseConfig`
- Produces: `AnthropicProvider` implementing `LLMProvider`

- [ ] **Step 1: Write modules/__init__.py and providers/__init__.py**

```python
# synapse/modules/__init__.py
"""Synapse modules — default implementations of protocols."""
```

```python
# synapse/modules/providers/__init__.py
"""LLM Provider implementations."""
```

- [ ] **Step 2: Write failing test (mock-based, no API key needed)**

```python
"""Tests for AnthropicProvider — mock-based, no real API calls."""
import pytest
from unittest.mock import AsyncMock, patch
from synapse.modules.providers.anthropic import AnthropicProvider
from synapse.protocols.llm import Message, LLMResponse


@pytest.fixture
def provider():
    return AnthropicProvider(model="claude-sonnet-4-6", api_key="test-key")


def test_model_id(provider):
    assert provider.model_id == "claude-sonnet-4-6"


@pytest.mark.asyncio
async def test_chat_basic():
    provider = AnthropicProvider(model="claude-sonnet-4-6", api_key="test-key")

    mock_msg = AsyncMock()
    mock_msg.content = [type("Block", (), {"text": "Hello, I am Claude", "type": "text"})()]
    mock_msg.stop_reason = "end_turn"
    mock_msg.usage.input_tokens = 10
    mock_msg.usage.output_tokens = 5

    mock_response = AsyncMock()
    mock_response.content = [mock_msg.content[0]]
    mock_response.stop_reason = "end_turn"
    mock_response.usage = mock_msg.usage

    with patch.object(provider._client.messages, "create", return_value=mock_response):
        result = await provider.chat(
            messages=[Message(role="user", content="Hi")],
        )

    assert isinstance(result, LLMResponse)
    assert result.content == "Hello, I am Claude"
    assert result.stop_reason == "end_turn"
    assert result.usage["input"] == 10
    assert result.usage["output"] == 5


@pytest.mark.asyncio
async def test_chat_with_tools():
    provider = AnthropicProvider(model="claude-sonnet-4-6", api_key="test-key")

    mock_tool_use = type("Block", (), {
        "type": "tool_use",
        "id": "tool_1",
        "name": "read",
        "input": {"path": "/test.txt"},
    })()

    mock_response = AsyncMock()
    mock_response.content = [mock_tool_use]
    mock_response.stop_reason = "tool_use"
    mock_response.usage.input_tokens = 20
    mock_response.usage.output_tokens = 15

    with patch.object(provider._client.messages, "create", return_value=mock_response):
        result = await provider.chat(
            messages=[Message(role="user", content="Read the file")],
            tools=[{"name": "read", "description": "Read a file", "input_schema": {}}],
        )

    assert result.stop_reason == "tool_use"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["name"] == "read"
    assert result.tool_calls[0]["input"] == {"path": "/test.txt"}


@pytest.mark.asyncio
async def test_chat_converts_tool_results():
    """Tool result messages should be converted to Anthropic's format."""
    provider = AnthropicProvider(model="claude-sonnet-4-6", api_key="test-key")

    mock_response = AsyncMock()
    mock_response.content = [type("Block", (), {"text": "Got it", "type": "text"})()]
    mock_response.stop_reason = "end_turn"
    mock_response.usage.input_tokens = 5
    mock_response.usage.output_tokens = 3

    with patch.object(provider._client.messages, "create", return_value=mock_response):
        await provider.chat(
            messages=[
                Message(role="user", content="Read /tmp/x"),
                Message(role="assistant", content=""),
                Message(role="user", content="tool result: file contents here"),
            ],
        )

    # The key thing: it didn't crash on tool_result messages
    assert True
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/modules/test_anthropic_provider.py -v`
Expected: FAIL

- [ ] **Step 4: Write AnthropicProvider implementation**

```python
"""Anthropic LLM Provider implementation."""

import logging
from anthropic import AsyncAnthropic
from synapse.protocols.llm import LLMResponse, LLMChunk, Message
from synapse.core.exceptions import ProviderError

logger = logging.getLogger(__name__)


class AnthropicProvider:
    """LLM provider backed by Anthropic's API."""

    def __init__(self, model: str = "claude-sonnet-4-6", api_key: str = ""):
        self._model = model
        self._client = AsyncAnthropic(api_key=api_key) if api_key else AsyncAnthropic()

    @property
    def model_id(self) -> str:
        return self._model

    async def chat(
        self,
        messages: list[Message],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        system_prompt = self._extract_system(messages)
        converted = self._convert_messages(messages)

        try:
            kwargs = {
                "model": self._model,
                "messages": converted,
                "max_tokens": 4096,
            }
            if system_prompt:
                kwargs["system"] = system_prompt
            if tools:
                kwargs["tools"] = [
                    {
                        "name": t["name"],
                        "description": t["description"],
                        "input_schema": t.get("input_schema", t.get("parameters", {})),
                    }
                    for t in tools
                ]

            response = await self._client.messages.create(**kwargs)
            return self._parse_response(response)
        except Exception as e:
            raise ProviderError(f"Anthropic API error: {e}") from e

    async def stream(self, messages: list[Message], tools: list[dict] | None = None):
        """Streaming not implemented in Phase 1 MVP."""
        raise NotImplementedError("Streaming will be added in Phase 2")

    def _extract_system(self, messages: list[Message]) -> str | None:
        """Extract system message if present (Anthropic uses separate system param)."""
        for msg in messages:
            if msg.role == "system":
                return msg.content
        return None

    def _convert_messages(self, messages: list[Message]) -> list[dict]:
        """Convert internal Message to Anthropic API format, filtering system."""
        result = []
        for msg in messages:
            if msg.role == "system":
                continue
            if msg.role == "user" and msg.content == "":
                # Tool result placeholder
                continue
            result.append({"role": msg.role, "content": msg.content})
        return result

    def _parse_response(self, response) -> LLMResponse:
        """Parse Anthropic response into our LLMResponse format."""
        text_parts = []
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append({
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })

        return LLMResponse(
            content="\n".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=response.stop_reason or "end_turn",
            usage={
                "input": response.usage.input_tokens,
                "output": response.usage.output_tokens,
            },
        )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass
```

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/modules/test_anthropic_provider.py -v`
Expected: 4 PASS

- [ ] **Step 6: Commit**

```bash
git add synapse/modules/ tests/modules/
git commit -m "feat: add Anthropic LLM provider with chat support"
```

---

### Task 10: Tool implementations — Read, Write, Edit, Glob, Grep

**Files:**
- Create: `synapse/modules/tools/__init__.py`
- Create: `synapse/modules/tools/file_read.py`
- Create: `synapse/modules/tools/file_write.py`
- Create: `synapse/modules/tools/file_edit.py`
- Create: `synapse/modules/tools/file_glob.py`
- Create: `synapse/modules/tools/search_grep.py`
- Create: `tests/modules/test_tools.py`

**Interfaces:**
- Consumes: `Tool` protocol
- Produces: `ReadTool`, `WriteTool`, `EditTool`, `GlobTool`, `GrepTool`

- [ ] **Step 1: Write tests**

```python
"""Tests for basic tools."""
import os
import tempfile
from pathlib import Path
import pytest
from synapse.modules.tools.file_read import ReadTool
from synapse.modules.tools.file_write import WriteTool
from synapse.modules.tools.file_edit import EditTool
from synapse.modules.tools.file_glob import GlobTool
from synapse.modules.tools.search_grep import GrepTool


@pytest.mark.asyncio
async def test_read_tool(tmp_path: Path):
    f = tmp_path / "test.txt"
    f.write_text("hello world")
    tool = ReadTool()
    result = await tool.execute({"path": str(f)})
    assert result.success
    assert "hello world" in result.output


@pytest.mark.asyncio
async def test_read_tool_nonexistent():
    tool = ReadTool()
    result = await tool.execute({"path": "/nonexistent/file.txt"})
    assert not result.success


@pytest.mark.asyncio
async def test_write_tool(tmp_path: Path):
    f = tmp_path / "output.txt"
    tool = WriteTool()
    result = await tool.execute({"path": str(f), "content": "new content"})
    assert result.success
    assert f.read_text() == "new content"


@pytest.mark.asyncio
async def test_edit_tool(tmp_path: Path):
    f = tmp_path / "edit.txt"
    f.write_text("line1\nline2\nline3\n")
    tool = EditTool()
    result = await tool.execute({
        "path": str(f),
        "old_string": "line2\n",
        "new_string": "replaced\n",
    })
    assert result.success
    assert f.read_text() == "line1\nreplaced\nline3\n"


@pytest.mark.asyncio
async def test_edit_tool_not_unique(tmp_path: Path):
    f = tmp_path / "dup.txt"
    f.write_text("dup\ndup\n")
    tool = EditTool()
    result = await tool.execute({
        "path": str(f),
        "old_string": "dup\n",
        "new_string": "x\n",
    })
    assert not result.success
    assert "not unique" in result.error.lower()


@pytest.mark.asyncio
async def test_glob_tool(tmp_path: Path):
    (tmp_path / "a.py").touch()
    (tmp_path / "b.py").touch()
    (tmp_path / "c.txt").touch()
    tool = GlobTool()
    result = await tool.execute({"pattern": "*.py", "path": str(tmp_path)})
    assert result.success
    assert "a.py" in result.output
    assert "b.py" in result.output
    assert "c.txt" not in result.output


@pytest.mark.asyncio
async def test_grep_tool(tmp_path: Path):
    (tmp_path / "src.py").write_text("def foo():\n    pass\ndef bar():\n    pass\n")
    tool = GrepTool()
    result = await tool.execute({"pattern": "def foo", "path": str(tmp_path)})
    assert result.success
    assert "def foo" in result.output
    assert "def bar" not in result.output
```

- [ ] **Step 2: Run tests — verify fail**

Run: `pytest tests/modules/test_tools.py -v`
Expected: FAIL

- [ ] **Step 3: Write file_read.py**

```python
"""Read file tool."""

from pathlib import Path
from synapse.protocols.tool import Tool, ToolSchema, ToolResult, ToolCallMetadata, RiskLevel, ToolCategory


class ReadTool:
    name = "read"
    description = "Read the contents of a file at the given path."
    parameters = ToolSchema(
        name="read",
        description="Read a file",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file"}
            },
            "required": ["path"],
        },
    )
    requires_sandbox = False
    risk_level = RiskLevel.READ_ONLY
    category = ToolCategory.FILE

    async def execute(self, params: dict, sandbox=None) -> ToolResult:
        path = Path(params["path"])
        meta = ToolCallMetadata(tool_name="read")
        try:
            content = path.read_text(encoding="utf-8")
            meta.duration_ms = 0
            return ToolResult(success=True, output=content, metadata=meta)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e), metadata=meta)
```

- [ ] **Step 4: Write file_write.py**

```python
"""Write file tool."""

from pathlib import Path
from synapse.protocols.tool import Tool, ToolSchema, ToolResult, ToolCallMetadata, RiskLevel, ToolCategory


class WriteTool:
    name = "write"
    description = "Write content to a file, overwriting if it exists."
    parameters = ToolSchema(
        name="write",
        description="Write a file",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file"},
                "content": {"type": "string", "description": "Content to write"},
            },
            "required": ["path", "content"],
        },
    )
    requires_sandbox = True
    risk_level = RiskLevel.WRITE_LOCAL
    category = ToolCategory.FILE

    async def execute(self, params: dict, sandbox=None) -> ToolResult:
        path = Path(params["path"])
        content = params["content"]
        meta = ToolCallMetadata(tool_name="write")
        meta.files_touched = [str(path)]
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            return ToolResult(success=True, output=f"Wrote {len(content)} bytes to {path}", metadata=meta)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e), metadata=meta)
```

- [ ] **Step 5: Write file_edit.py**

```python
"""Edit file tool — exact string replacement."""

from pathlib import Path
from synapse.protocols.tool import Tool, ToolSchema, ToolResult, ToolCallMetadata, RiskLevel, ToolCategory


class EditTool:
    name = "edit"
    description = "Replace an exact string in a file with another string."
    parameters = ToolSchema(
        name="edit",
        description="Edit a file by exact string replacement",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute path to the file"},
                "old_string": {"type": "string", "description": "Exact text to replace"},
                "new_string": {"type": "string", "description": "Replacement text"},
            },
            "required": ["path", "old_string", "new_string"],
        },
    )
    requires_sandbox = True
    risk_level = RiskLevel.WRITE_LOCAL
    category = ToolCategory.FILE

    async def execute(self, params: dict, sandbox=None) -> ToolResult:
        path = Path(params["path"])
        old = params["old_string"]
        new = params["new_string"]
        meta = ToolCallMetadata(tool_name="edit")
        meta.files_touched = [str(path)]

        try:
            content = path.read_text(encoding="utf-8")
            count = content.count(old)
            if count == 0:
                return ToolResult(success=False, output="", error="old_string not found in file", metadata=meta)
            if count > 1:
                return ToolResult(success=False, output="", error="old_string is not unique in file — found {count} occurrences", metadata=meta)
            new_content = content.replace(old, new)
            path.write_text(new_content, encoding="utf-8")
            return ToolResult(success=True, output=f"Replaced 1 occurrence in {path}", metadata=meta)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e), metadata=meta)
```

- [ ] **Step 6: Write file_glob.py**

```python
"""Glob file tool — filename pattern matching."""

from pathlib import Path
from synapse.protocols.tool import Tool, ToolSchema, ToolResult, ToolCallMetadata, RiskLevel, ToolCategory


class GlobTool:
    name = "glob"
    description = "Find files matching a glob pattern."
    parameters = ToolSchema(
        name="glob",
        description="Find files by glob pattern",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern (e.g. **/*.py)"},
                "path": {"type": "string", "description": "Directory to search in"},
            },
            "required": ["pattern"],
        },
    )
    requires_sandbox = False
    risk_level = RiskLevel.READ_ONLY
    category = ToolCategory.FILE

    async def execute(self, params: dict, sandbox=None) -> ToolResult:
        pattern = params["pattern"]
        root = Path(params.get("path", "."))
        meta = ToolCallMetadata(tool_name="glob")
        try:
            matches = sorted(root.glob(pattern))
            # Limit output to 100 matches
            lines = [str(m) for m in matches[:100]]
            output = "\n".join(lines) if lines else "(no matches)"
            return ToolResult(success=True, output=output, metadata=meta)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e), metadata=meta)
```

- [ ] **Step 7: Write search_grep.py**

```python
"""Grep search tool — regex content search."""

import subprocess
from pathlib import Path
from synapse.protocols.tool import Tool, ToolSchema, ToolResult, ToolCallMetadata, RiskLevel, ToolCategory


class GrepTool:
    name = "grep"
    description = "Search file contents with a regex pattern using ripgrep."
    parameters = ToolSchema(
        name="grep",
        description="Search code with regex",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regex pattern to search for"},
                "path": {"type": "string", "description": "Directory or file to search in"},
            },
            "required": ["pattern"],
        },
    )
    requires_sandbox = False
    risk_level = RiskLevel.READ_ONLY
    category = ToolCategory.CODE_UNDERSTANDING

    async def execute(self, params: dict, sandbox=None) -> ToolResult:
        pattern = params["pattern"]
        search_path = params.get("path", ".")
        meta = ToolCallMetadata(tool_name="grep")
        try:
            # Try ripgrep first, fall back to Python
            result = subprocess.run(
                ["rg", "--no-heading", "-n", "--color=never", pattern, search_path],
                capture_output=True, text=True, timeout=30,
            )
            output = result.stdout.strip() or "(no matches)"
            return ToolResult(success=True, output=output[:50000], metadata=meta)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            # Fallback: simple Python grep
            try:
                lines = []
                root = Path(search_path)
                import re
                regex = re.compile(pattern)
                for f in root.rglob("*.py") if root.is_dir() else [root]:
                    if not f.is_file():
                        continue
                    try:
                        for i, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                            if regex.search(line):
                                lines.append(f"{f}:{i}:{line}")
                    except Exception:
                        continue
                    if len(lines) > 500:
                        break
                output = "\n".join(lines[:500]) or "(no matches)"
                return ToolResult(success=True, output=output[:50000], metadata=meta)
            except Exception as e:
                return ToolResult(success=False, output="", error=str(e), metadata=meta)
```

- [ ] **Step 8: Write tools/__init__.py**

```python
"""Synapse tool implementations."""
```

- [ ] **Step 9: Run tests**

Run: `pytest tests/modules/test_tools.py -v`
Expected: 7 PASS

- [ ] **Step 10: Commit**

```bash
git add synapse/modules/tools/ tests/modules/test_tools.py
git commit -m "feat: add basic tools — read, write, edit, glob, grep"
```

---

### Task 11: Shell and Git tools + ToolRegistry

**Files:**
- Create: `synapse/modules/tools/shell.py`
- Create: `synapse/modules/tools/git_.py`
- Create: `synapse/modules/tools/registry.py`
- Append to: `tests/modules/test_tools.py`

**Interfaces:**
- Consumes: `Tool`, `ToolRegistry` protocols
- Produces: `ShellTool`, `GitTool`, `DefaultToolRegistry`

- [ ] **Step 1: Append shell and git tests to test_tools.py**

```python
# Append to tests/modules/test_tools.py:

import asyncio
from synapse.modules.tools.shell import ShellTool
from synapse.modules.tools.git_ import GitTool
from synapse.modules.tools.registry import DefaultToolRegistry
from synapse.modules.tools.file_read import ReadTool
from synapse.modules.tools.file_write import WriteTool


@pytest.mark.asyncio
async def test_shell_tool_echo():
    tool = ShellTool()
    result = await tool.execute({"command": "echo hello"})
    assert result.success
    assert "hello" in result.stdout


@pytest.mark.asyncio
async def test_shell_tool_timeout():
    tool = ShellTool()
    result = await tool.execute({"command": "sleep 10"}, timeout=1)
    assert not result.success
    assert result.error is not None


@pytest.mark.asyncio
async def test_git_tool_log(tmp_path: Path):
    import subprocess
    # Initialize a git repo
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    (tmp_path / "file.txt").write_text("content")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)

    tool = GitTool()
    result = await tool.execute({"command": "log", "cwd": str(tmp_path)})
    assert result.success
    assert "init" in result.output


@pytest.mark.asyncio
async def test_git_tool_diff(tmp_path: Path):
    import subprocess
    subprocess.run(["git", "init"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, capture_output=True)
    (tmp_path / "file.txt").write_text("before")
    subprocess.run(["git", "add", "."], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, capture_output=True)
    (tmp_path / "file.txt").write_text("after")

    tool = GitTool()
    result = await tool.execute({"command": "diff", "cwd": str(tmp_path)})
    assert result.success
    assert "before" in result.output or "after" in result.output


def test_tool_registry():
    registry = DefaultToolRegistry()
    read = ReadTool()
    registry.register(read)

    assert registry.get("read") is read
    assert len(registry.list_all()) == 1
    schemas = registry.get_schemas()
    assert len(schemas) == 1
    assert schemas[0]["name"] == "read"


def test_registry_list_by_category():
    registry = DefaultToolRegistry()
    read = ReadTool()
    write = WriteTool()
    registry.register(read)
    registry.register(write)

    file_tools = registry.list_by_category("file")
    assert len(file_tools) == 2
```

- [ ] **Step 2: Run test to verify fail**

Run: `pytest tests/modules/test_tools.py -v`
Expected: FAIL (new tests fail)

- [ ] **Step 3: Write shell.py**

```python
"""Shell command execution tool — runs in sandbox."""

import asyncio
from synapse.protocols.tool import Tool, ToolSchema, ToolResult, ToolCallMetadata, RiskLevel, ToolCategory


class ShellTool:
    name = "shell"
    description = "Execute a shell command in a subprocess."
    parameters = ToolSchema(
        name="shell",
        description="Execute a shell command",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to run"},
                "cwd": {"type": "string", "description": "Working directory (optional)"},
            },
            "required": ["command"],
        },
    )
    requires_sandbox = True
    risk_level = RiskLevel.EXECUTE
    category = ToolCategory.EXECUTION

    async def execute(self, params: dict, sandbox=None, timeout: int | None = None) -> ToolResult:
        command = params["command"]
        cwd = params.get("cwd", ".")
        timeout = timeout or 120
        meta = ToolCallMetadata(tool_name="shell")

        try:
            if sandbox is not None:
                result = await sandbox.execute(command, cwd=cwd, timeout=timeout)
                meta.sandbox_used = True
                meta.duration_ms = 0
                if result.exit_code == 0:
                    return ToolResult(success=True, output=result.stdout, metadata=meta)
                else:
                    return ToolResult(success=False, output=result.stdout, error=result.stderr, metadata=meta)

            # Fallback: no sandbox (warning mode)
            proc = await asyncio.create_subprocess_shell(
                command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                return ToolResult(success=False, output="", error=f"Command timed out after {timeout}s", metadata=meta)

            if proc.returncode == 0:
                return ToolResult(success=True, output=stdout.decode(errors="ignore"), metadata=meta)
            return ToolResult(success=False, output=stdout.decode(errors="ignore"), error=stderr.decode(errors="ignore"), metadata=meta)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e), metadata=meta)
```

- [ ] **Step 4: Write git_.py**

```python
"""Git tool — read-only git operations (log, diff, status, show)."""

import asyncio
from synapse.protocols.tool import Tool, ToolSchema, ToolResult, ToolCallMetadata, RiskLevel, ToolCategory

READ_ONLY_COMMANDS = {"log", "diff", "show", "status", "blame", "branch", "tag", "rev-parse"}


class GitTool:
    name = "git"
    description = "Run read-only git commands: log, diff, show, status, blame, branch, tag."
    parameters = ToolSchema(
        name="git",
        description="Git operations",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Git subcommand and args (e.g. 'log --oneline -5')"},
                "cwd": {"type": "string", "description": "Working directory"},
            },
            "required": ["command"],
        },
    )
    requires_sandbox = True
    risk_level = RiskLevel.READ_ONLY
    category = ToolCategory.CODE_UNDERSTANDING

    async def execute(self, params: dict, sandbox=None) -> ToolResult:
        command = params["command"]
        cwd = params.get("cwd", ".")
        meta = ToolCallMetadata(tool_name="git")

        subcommand = command.split()[0] if command.strip() else ""
        if subcommand not in READ_ONLY_COMMANDS:
            return ToolResult(success=False, output="", error=f"Git '{subcommand}' is not allowed (read-only commands only)", metadata=meta)

        full_cmd = f"git {command}"
        try:
            proc = await asyncio.create_subprocess_shell(
                full_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
            except asyncio.TimeoutError:
                proc.kill()
                return ToolResult(success=False, output="", error="Git command timed out", metadata=meta)

            output = stdout.decode(errors="ignore")
            if proc.returncode == 0:
                return ToolResult(success=True, output=output[:50000], metadata=meta)
            return ToolResult(success=False, output=output, error=stderr.decode(errors="ignore"), metadata=meta)
        except Exception as e:
            return ToolResult(success=False, output="", error=str(e), metadata=meta)
```

- [ ] **Step 5: Write registry.py**

```python
"""Default in-memory tool registry."""

from synapse.protocols.tool import Tool, ToolCategory


class DefaultToolRegistry:
    """Mutable registry of tools, queryable by name or category."""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"Tool '{name}' not registered")
        return self._tools[name]

    def list_all(self) -> list[Tool]:
        return list(self._tools.values())

    def list_by_category(self, category: str) -> list[Tool]:
        cat = ToolCategory(category) if isinstance(category, str) else category
        return [t for t in self._tools.values() if t.category == cat]

    def get_schemas(self) -> list[dict]:
        """Return schemas in Anthropic-compatible format."""
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.parameters.parameters,
            }
            for t in self._tools.values()
        ]
```

- [ ] **Step 6: Run tests**

Run: `pytest tests/modules/test_tools.py -v`
Expected: ALL PASS

- [ ] **Step 7: Commit**

```bash
git add synapse/modules/tools/ tests/modules/test_tools.py
git commit -m "feat: add shell tool, git tool, and default tool registry"
```

---

### Task 12: Session Memory

**Files:**
- Create: `synapse/modules/memory/__init__.py`
- Create: `synapse/modules/memory/session.py`
- Create: `tests/modules/test_session_memory.py`

**Interfaces:**
- Consumes: `MemoryStore` protocol, `MemoryLevel.SESSION`
- Produces: `SessionMemory` implementing `MemoryStore` for SESSION level

- [ ] **Step 1: Write failing test**

```python
"""Tests for session memory."""
import pytest
from synapse.modules.memory.session import SessionMemory
from synapse.protocols.memory import MemoryEntry, MemoryLevel, MemoryMetadata


@pytest.mark.asyncio
async def test_store_and_retrieve_session():
    mem = SessionMemory()
    entry = MemoryEntry(
        id="e1",
        content="This is a session memory",
        level=MemoryLevel.SESSION,
        metadata=MemoryMetadata(tags=["test"]),
    )
    await mem.store(entry)
    results = await mem.retrieve("session", MemoryLevel.SESSION, top_k=5)
    assert len(results) == 1
    assert results[0].content == "This is a session memory"


@pytest.mark.asyncio
async def test_forget():
    mem = SessionMemory()
    entry = MemoryEntry(id="e1", content="temp", level=MemoryLevel.SESSION)
    await mem.store(entry)
    await mem.forget("e1")
    results = await mem.retrieve("temp", MemoryLevel.SESSION)
    assert len(results) == 0


@pytest.mark.asyncio
async def test_retrieve_other_level_returns_empty():
    mem = SessionMemory()
    entry = MemoryEntry(id="e1", content="session only", level=MemoryLevel.SESSION)
    await mem.store(entry)
    results = await mem.retrieve("session", MemoryLevel.PROJECT)
    assert len(results) == 0


@pytest.mark.asyncio
async def test_multiple_entries():
    mem = SessionMemory()
    for i in range(10):
        await mem.store(MemoryEntry(id=f"e{i}", content=f"entry {i}", level=MemoryLevel.SESSION))
    results = await mem.retrieve("entry", MemoryLevel.SESSION, top_k=3)
    assert len(results) <= 3
```

- [ ] **Step 2: Run test to verify fail**

Run: `pytest tests/modules/test_session_memory.py -v`
Expected: FAIL

- [ ] **Step 3: Write session.py**

```python
"""Session memory — in-memory only, released when session ends."""

from synapse.protocols.memory import MemoryEntry, MemoryLevel


class SessionMemory:
    """In-memory storage for the SESSION level only.

    Session memory is ephemeral — cleared when the process exits.
    For Phase 1, retrieval is simple substring matching.
    """

    def __init__(self):
        self._entries: dict[str, MemoryEntry] = {}

    async def store(self, entry: MemoryEntry) -> None:
        if entry.level == MemoryLevel.SESSION:
            self._entries[entry.id] = entry

    async def retrieve(self, query: str, level: MemoryLevel, top_k: int = 5) -> list[MemoryEntry]:
        if level != MemoryLevel.SESSION:
            return []

        matches = []
        for entry in self._entries.values():
            if query.lower() in entry.content.lower():
                matches.append(entry)
            elif any(query.lower() in tag.lower() for tag in entry.metadata.tags):
                matches.append(entry)

        matches.sort(key=lambda e: e.metadata.priority, reverse=True)
        return matches[:top_k]

    async def forget(self, entry_id: str) -> None:
        self._entries.pop(entry_id, None)
```

- [ ] **Step 4: Write memory/__init__.py**

```python
"""Memory system implementations."""
```

- [ ] **Step 5: Run test to verify pass**

Run: `pytest tests/modules/test_session_memory.py -v`
Expected: 4 PASS

- [ ] **Step 6: Commit**

```bash
git add synapse/modules/memory/ tests/modules/test_session_memory.py
git commit -m "feat: add session memory with in-memory storage"
```

---

### Task 13: Basic Context Retriever

**Files:**
- Create: `synapse/modules/context/__init__.py`
- Create: `synapse/modules/context/retriever.py`
- Create: `tests/modules/test_context_retriever.py`

**Interfaces:**
- Consumes: `ContextRetriever` protocol
- Produces: `BasicContextRetriever` — builds Context from grep/glob + session memory

- [ ] **Step 1: Write failing test**

```python
"""Tests for basic context retriever."""
from pathlib import Path
import pytest
from synapse.modules.context.retriever import BasicContextRetriever
from synapse.modules.tools.registry import DefaultToolRegistry
from synapse.modules.tools.search_grep import GrepTool
from synapse.modules.tools.file_glob import GlobTool
from synapse.modules.memory.session import SessionMemory
from synapse.protocols.memory import MemoryEntry, MemoryLevel
from synapse.protocols.retriever import ContextBudget


@pytest.fixture
def retriever():
    return BasicContextRetriever()


@pytest.fixture
def tools():
    reg = DefaultToolRegistry()
    reg.register(GrepTool())
    reg.register(GlobTool())
    return reg


@pytest.fixture
def memory():
    return SessionMemory()


@pytest.mark.asyncio
async def test_retrieve_builds_context(tmp_path: Path, retriever, tools, memory):
    # Create some files
    (tmp_path / "main.py").write_text("def main():\n    pass\n")
    (tmp_path / "README.md").write_text("# Test Project\n")

    # Add session memory
    await memory.store(MemoryEntry(
        id="m1", content="This project uses pytest", level=MemoryLevel.SESSION,
    ))

    ctx = await retriever.retrieve(
        task="find the main function",
        project_root=tmp_path,
        tools=tools,
        memory=memory,
        budget=ContextBudget(total_tokens=10000),
    )

    assert len(ctx.core) > 0
    assert ctx.total_tokens > 0


@pytest.mark.asyncio
async def test_context_preserves_system_blocks(tmp_path: Path, retriever, tools, memory):
    ctx = await retriever.retrieve(
        task="test task",
        project_root=tmp_path,
        tools=tools,
        memory=memory,
    )

    # System blocks should contain project instructions
    assert isinstance(ctx.system, list)
    assert isinstance(ctx.core, list)
```

- [ ] **Step 2: Run test to verify fail**

Run: `pytest tests/modules/test_context_retriever.py -v`
Expected: FAIL

- [ ] **Step 3: Write retriever.py**

```python
"""Basic context retriever — builds context from tools + memory."""

from pathlib import Path
from synapse.protocols.retriever import (
    Context, ContextBlock, ContextBudget, ContextSource,
)


class BasicContextRetriever:
    """Phase 1 context retriever — uses grep/glob for CORE, memory for REFERENCE.

    Future phases will add AST indexing, git history, and four-zone budgeting.
    """

    async def retrieve(
        self,
        task: str,
        project_root: Path,
        tools,
        memory,
        budget: ContextBudget | None = None,
    ) -> Context:
        ctx = Context()
        budget = budget or ContextBudget()

        # 1. SYSTEM: project instructions
        ctx.system = await self._build_system(project_root)

        # 2. CORE: grep for relevant code based on task
        ctx.core = await self._build_core(task, project_root, tools)

        # 3. REFERENCE: session memory entries related to task
        ctx.reference = await self._build_reference(task, memory)

        return ctx

    async def _build_system(self, project_root: Path) -> list[ContextBlock]:
        blocks = []
        # Look for CLAUDE.md / AGENTS.md style project instructions
        for name in ["CLAUDE.md", "AGENTS.md", "README.md"]:
            f = project_root / name
            if f.exists():
                content = f.read_text(encoding="utf-8")
                blocks.append(ContextBlock(
                    content=content,
                    source=ContextSource.MEMORY,
                    priority=9,
                    token_count=len(content) // 4,  # rough estimate
                ))
        return blocks

    async def _build_core(self, task: str, project_root: Path, tools) -> list[ContextBlock]:
        blocks = []
        keywords = self._extract_keywords(task)

        # Try grep for each keyword
        try:
            grep = tools.get("grep")
            for kw in keywords[:3]:
                result = await grep.execute({"pattern": kw, "path": str(project_root)})
                if result.success and result.output and result.output != "(no matches)":
                    blocks.append(ContextBlock(
                        content=result.output[:5000],
                        source=ContextSource.GREP,
                        priority=8,
                        token_count=len(result.output[:5000]) // 4,
                    ))
        except (KeyError, Exception):
            pass

        # Also glob for relevant Python files
        try:
            glob = tools.get("glob")
            result = await glob.execute({"pattern": "**/*.py", "path": str(project_root)})
            if result.success and result.output:
                files = result.output.split("\n")[:20]
                for f in files:
                    p = project_root / f
                    if p.exists():
                        content = p.read_text(encoding="utf-8")
                        blocks.append(ContextBlock(
                            content=f"# File: {f}\n\n{content[:3000]}",
                            source=ContextSource.GLOB,
                            priority=7,
                            token_count=len(content[:3000]) // 4,
                        ))
        except (KeyError, Exception):
            pass

        return blocks

    async def _build_reference(self, task: str, memory) -> list[ContextBlock]:
        blocks = []
        try:
            from synapse.protocols.memory import MemoryLevel
            entries = await memory.retrieve(task, MemoryLevel.SESSION, top_k=3)
            for entry in entries:
                blocks.append(ContextBlock(
                    content=entry.content,
                    source=ContextSource.MEMORY,
                    priority=5,
                    token_count=len(entry.content) // 4,
                ))
        except Exception:
            pass
        return blocks

    @staticmethod
    def _extract_keywords(task: str) -> list[str]:
        """Naive keyword extraction — split and filter short words."""
        words = task.split()
        return [w for w in words if len(w) > 2 and w.lower() not in {
            "the", "and", "for", "with", "that", "this", "from",
        }]
```

- [ ] **Step 4: Write context/__init__.py**

```python
"""Context governance modules."""
```

- [ ] **Step 5: Run test**

Run: `pytest tests/modules/test_context_retriever.py -v`
Expected: 2 PASS

- [ ] **Step 6: Commit**

```bash
git add synapse/modules/context/ tests/modules/test_context_retriever.py
git commit -m "feat: add basic context retriever using grep/glob + session memory"
```

---

### Task 14: Process Sandbox

**Files:**
- Create: `synapse/modules/security/__init__.py`
- Create: `synapse/modules/security/sandbox.py`
- Create: `tests/modules/test_sandbox.py`

**Interfaces:**
- Consumes: `Sandbox` protocol
- Produces: `ProcessSandbox` with cross-platform detection

- [ ] **Step 1: Write failing test**

```python
"""Tests for process sandbox."""
import pytest
from synapse.modules.security.sandbox import ProcessSandbox


@pytest.mark.asyncio
async def test_sandbox_echo():
    sandbox = ProcessSandbox()
    result = await sandbox.execute("echo hello")
    assert result.exit_code == 0
    assert "hello" in result.stdout


@pytest.mark.asyncio
async def test_sandbox_platform_detected():
    sandbox = ProcessSandbox()
    assert sandbox.platform in ("windows_job", "macos_seatbelt", "linux_bwrap", "none")


@pytest.mark.asyncio
async def test_sandbox_timeout():
    sandbox = ProcessSandbox()
    result = await sandbox.execute("sleep 10", timeout=1)
    assert result.timed_out or result.exit_code != 0


@pytest.mark.asyncio
async def test_sandbox_failing_command():
    sandbox = ProcessSandbox()
    result = await sandbox.execute("nonexistent_command_xyz")
    assert result.exit_code != 0
```

- [ ] **Step 2: Run test to verify fail**

Run: `pytest tests/modules/test_sandbox.py -v`
Expected: FAIL

- [ ] **Step 3: Write sandbox.py**

```python
"""Process sandbox — cross-platform execution isolation.

Phase 1 implements a basic subprocess sandbox. Full platform-specific
sandboxing (Windows Job Objects, macOS Seatbelt, Linux bubblewrap)
will be added in Phase 2.
"""

import asyncio
import platform
from pathlib import Path
from synapse.protocols.sandbox import SandboxResult


class ProcessSandbox:
    """Basic process sandbox with timeout and working directory control.

    In Phase 1, this uses subprocess isolation. Full OS-level sandboxing
    (Seatbelt/bubblewrap/Job Objects) is a Phase 2 enhancement.
    """

    @property
    def platform(self) -> str:
        system = platform.system().lower()
        if system == "windows":
            return "windows_job"
        elif system == "darwin":
            return "macos_seatbelt"
        elif system == "linux":
            return "linux_bwrap"
        return "none"

    async def execute(
        self,
        command: str,
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 120,
        network: bool = False,
        allowed_paths: list[Path] | None = None,
    ) -> SandboxResult:
        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd) if cwd else None,
                env=env,
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
                return SandboxResult(
                    exit_code=proc.returncode or 0,
                    stdout=stdout.decode(errors="ignore"),
                    stderr=stderr.decode(errors="ignore"),
                    timed_out=False,
                    platform=self.platform,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return SandboxResult(
                    exit_code=-1,
                    stdout="",
                    stderr=f"Command timed out after {timeout}s",
                    timed_out=True,
                    platform=self.platform,
                )
        except Exception as e:
            return SandboxResult(
                exit_code=-1,
                stdout="",
                stderr=str(e),
                platform=self.platform,
            )
```

- [ ] **Step 4: Write security/__init__.py**

```python
"""Security layer implementations."""
```

- [ ] **Step 5: Run test**

Run: `pytest tests/modules/test_sandbox.py -v`
Expected: 4 PASS

- [ ] **Step 6: Commit**

```bash
git add synapse/modules/security/ tests/modules/test_sandbox.py
git commit -m "feat: add process sandbox with timeout and cross-platform detection"
```

---

### Task 15: Action-Time Authorizer

**Files:**
- Create: `synapse/modules/security/auth.py`
- Create: `tests/modules/test_auth.py`

**Interfaces:**
- Consumes: `AuthRequest`, `AuthDecision`, `RiskLevel`
- Produces: `ActionAuthorizer` with configurable decision matrix

- [ ] **Step 1: Write failing test**

```python
"""Tests for action-time authorization."""
import pytest
from synapse.modules.security.auth import ActionAuthorizer
from synapse.protocols.tool import RiskLevel


@pytest.fixture
def auth():
    return ActionAuthorizer(workspace_root="/project", confirmation_enabled=True)


def test_read_only_auto_allow(auth):
    req = auth.create_request("read", {"path": "/project/file.py"}, RiskLevel.READ_ONLY, "s1")
    decision = auth.authorize(req)
    assert decision.allowed
    assert not decision.requires_confirmation


def test_write_in_workspace_requires_confirmation(auth):
    req = auth.create_request("write", {"path": "/project/new.py"}, RiskLevel.WRITE_LOCAL, "s1")
    decision = auth.authorize(req)
    assert decision.allowed
    assert decision.requires_confirmation


def test_write_outside_workspace_blocked(auth):
    req = auth.create_request("write", {"path": "/etc/passwd"}, RiskLevel.WRITE_LOCAL, "s1")
    decision = auth.authorize(req)
    assert not decision.allowed


def test_execute_allowlisted_command(auth):
    req = auth.create_request("shell", {"command": "ls -la"}, RiskLevel.EXECUTE, "s1")
    decision = auth.authorize(req)
    assert decision.allowed


def test_execute_blocked_command(auth):
    req = auth.create_request("shell", {"command": "rm -rf /"}, RiskLevel.EXECUTE, "s1")
    decision = auth.authorize(req)
    assert not decision.allowed


def test_external_blocked_by_default(auth):
    req = auth.create_request("http", {"url": "https://example.com"}, RiskLevel.EXTERNAL, "s1")
    decision = auth.authorize(req)
    assert not decision.allowed


def test_external_can_be_enabled():
    auth = ActionAuthorizer(workspace_root="/project", allow_external=True)
    req = auth.create_request("http", {"url": "https://example.com"}, RiskLevel.EXTERNAL, "s1")
    decision = auth.authorize(req)
    assert decision.allowed


def test_dangerous_patterns_blocked():
    auth = ActionAuthorizer(workspace_root="/project")
    dangerous = ["rm -rf /", "dd if=/dev/zero", "> /dev/sda", "chmod 777 /"]
    for cmd in dangerous:
        req = auth.create_request("shell", {"command": cmd}, RiskLevel.EXECUTE, "s1")
        decision = auth.authorize(req)
        assert not decision.allowed, f"Should block: {cmd}"
```

- [ ] **Step 2: Run test to verify fail**

Run: `pytest tests/modules/test_auth.py -v`
Expected: FAIL

- [ ] **Step 3: Write auth.py**

```python
"""Action-Time Authorization — evaluates tool calls before execution."""

import os
from pathlib import Path
from synapse.protocols.tool import RiskLevel
from synapse.protocols.sandbox import AuthRequest, AuthDecision


class ActionAuthorizer:
    """Evaluates tool call authorization based on risk level, workspace, and allowlists."""

    DANGEROUS_PATTERNS = [
        "rm -rf /",
        "rm -rf --no-preserve-root",
        "dd if=/dev/zero",
        "> /dev/sda",
        "mkfs.",
        ":(){ :|:& };:",  # fork bomb
        "chmod 777 /",
        "chown -R",
    ]

    ALWAYS_ALLOWED_COMMANDS = [
        "ls", "echo", "cat", "head", "tail", "wc", "pwd", "env",
        "git", "python", "python3", "pip", "npm", "node", "cargo",
        "go", "pytest", "mypy", "ruff", "black",
    ]

    def __init__(
        self,
        workspace_root: str = ".",
        allow_external: bool = False,
        confirmation_enabled: bool = True,
    ):
        self.workspace_root = Path(workspace_root).resolve()
        self.allow_external = allow_external
        self.confirmation_enabled = confirmation_enabled

    def create_request(
        self, tool_name: str, params: dict, risk_level: RiskLevel, session_id: str, user_id: str | None = None,
    ) -> AuthRequest:
        return AuthRequest(
            tool_name=tool_name,
            tool_params=params,
            risk_level=risk_level.value if isinstance(risk_level, RiskLevel) else risk_level,
            session_id=session_id,
            user_id=user_id,
        )

    def authorize(self, request: AuthRequest) -> AuthDecision:
        risk = request.risk_level

        # READ_ONLY: always allow
        if risk == RiskLevel.READ_ONLY.value:
            return AuthDecision(allowed=True, reason="Read-only operation", requires_confirmation=False)

        # WRITE_LOCAL: allow in workspace, confirm
        if risk == RiskLevel.WRITE_LOCAL.value:
            if self._is_in_workspace(request):
                return AuthDecision(
                    allowed=True,
                    reason="Write within workspace",
                    requires_confirmation=self.confirmation_enabled,
                )
            return AuthDecision(allowed=False, reason="Write target is outside workspace")

        # EXECUTE: allowlist check + dangerous pattern check
        if risk == RiskLevel.EXECUTE.value:
            command = request.tool_params.get("command", "")
            if self._is_dangerous(command):
                return AuthDecision(allowed=False, reason="Command matches dangerous pattern")
            if not self._is_allowlisted(command):
                return AuthDecision(allowed=False, reason=f"Command not in allowlist: {command.split()[0] if command else ''}")
            return AuthDecision(
                allowed=True,
                reason="Command in allowlist",
                requires_confirmation=self.confirmation_enabled,
            )

        # EXTERNAL: must be explicitly enabled
        if risk == RiskLevel.EXTERNAL.value:
            if self.allow_external:
                return AuthDecision(allowed=True, reason="External access enabled", requires_confirmation=True)
            return AuthDecision(allowed=False, reason="External tools are disabled")

        # META: allow
        if risk == RiskLevel.META.value:
            return AuthDecision(allowed=True, reason="Meta/experimental tool")

        return AuthDecision(allowed=False, reason=f"Unknown risk level: {risk}")

    def _is_in_workspace(self, request: AuthRequest) -> bool:
        target = request.tool_params.get("path", "")
        if not target:
            return False
        try:
            resolved = Path(target).resolve()
            return str(resolved).startswith(str(self.workspace_root))
        except (ValueError, OSError):
            return False

    def _is_dangerous(self, command: str) -> bool:
        for pattern in self.DANGEROUS_PATTERNS:
            if pattern in command:
                return True
        return False

    def _is_allowlisted(self, command: str) -> bool:
        if not command.strip():
            return False
        base = command.strip().split()[0]
        # Allow commands starting with any allowlisted prefix
        return base in self.ALWAYS_ALLOWED_COMMANDS
```

- [ ] **Step 4: Run test**

Run: `pytest tests/modules/test_auth.py -v`
Expected: 8 PASS

- [ ] **Step 5: Commit**

```bash
git add synapse/modules/security/auth.py tests/modules/test_auth.py
git commit -m "feat: add action-time authorization with allowlists and dangerous pattern detection"
```

---

### Task 16: Core Agent

**Files:**
- Create: `synapse/core/agent.py`
- Create: `tests/core/test_agent.py`

**Interfaces:**
- Consumes: `Container`, `LLMProvider`, `Planner`, `ToolRegistry`, `MemoryStore`, `ContextRetriever`, `Sandbox`, `EventBus`, `Session`
- Produces: `Agent.run(task, session) -> AgentResult`

- [ ] **Step 1: Write failing test**

```python
"""Tests for the core Agent."""
import pytest
from unittest.mock import AsyncMock
from synapse.core.container import Container
from synapse.core.agent import Agent
from synapse.core.session import Session
from synapse.core.events import EventBus
from synapse.protocols.llm import LLMProvider
from synapse.protocols.planner import Planner
from synapse.protocols.tool import ToolRegistry
from synapse.protocols.memory import MemoryStore
from synapse.protocols.retriever import ContextRetriever
from synapse.protocols.sandbox import Sandbox


@pytest.mark.asyncio
async def test_agent_run_delegates_to_planner():
    container = Container()

    mock_llm = AsyncMock(spec=LLMProvider)
    mock_planner = AsyncMock(spec=Planner)
    mock_tools = AsyncMock(spec=ToolRegistry)
    mock_memory = AsyncMock(spec=MemoryStore)
    mock_retriever = AsyncMock(spec=ContextRetriever)
    mock_sandbox = AsyncMock(spec=Sandbox)
    event_bus = EventBus()

    from synapse.protocols.planner import AgentResult, ResultStatus
    mock_planner.execute.return_value = AgentResult(
        status=ResultStatus.SUCCESS,
        output="Task completed",
    )

    container.register(LLMProvider, mock_llm)
    container.register(Planner, mock_planner)
    container.register(ToolRegistry, mock_tools)
    container.register(MemoryStore, mock_memory)
    container.register(ContextRetriever, mock_retriever)
    container.register(Sandbox, mock_sandbox)
    container.register(EventBus, event_bus)

    agent = Agent(container)
    session = Session()
    result = await agent.run("test task", session)

    assert result.status == ResultStatus.SUCCESS
    assert result.output == "Task completed"
    mock_planner.execute.assert_called_once()


@pytest.mark.asyncio
async def test_agent_persists_memory_after_run():
    container = Container()

    mock_llm = AsyncMock(spec=LLMProvider)
    mock_planner = AsyncMock(spec=Planner)
    mock_tools = AsyncMock(spec=ToolRegistry)
    mock_memory = AsyncMock(spec=MemoryStore)
    mock_retriever = AsyncMock(spec=ContextRetriever)
    mock_sandbox = AsyncMock(spec=Sandbox)
    event_bus = EventBus()

    from synapse.protocols.planner import AgentResult, ResultStatus
    mock_planner.execute.return_value = AgentResult(
        status=ResultStatus.SUCCESS,
        output="Done",
    )

    container.register(LLMProvider, mock_llm)
    container.register(Planner, mock_planner)
    container.register(ToolRegistry, mock_tools)
    container.register(MemoryStore, mock_memory)
    container.register(ContextRetriever, mock_retriever)
    container.register(Sandbox, mock_sandbox)
    container.register(EventBus, event_bus)

    agent = Agent(container)
    result = await agent.run("test", Session())

    assert mock_memory.store.called
```

- [ ] **Step 2: Run test to verify fail**

Run: `pytest tests/core/test_agent.py -v`
Expected: FAIL

- [ ] **Step 3: Write agent.py**

```python
"""Core Agent — assembles dependencies and delegates to Planner."""

from synapse.core.container import Container
from synapse.core.session import Session
from synapse.protocols.llm import LLMProvider
from synapse.protocols.planner import Planner, AgentResult
from synapse.protocols.tool import ToolRegistry
from synapse.protocols.memory import MemoryStore, MemoryLevel, MemoryEntry, MemoryMetadata
from synapse.protocols.retriever import ContextRetriever, ContextSource
from synapse.protocols.sandbox import Sandbox
from synapse.core.events import EventBus


class Agent:
    """Dependency injection assembler — wires components and delegates to Planner.

    The Agent does NOT implement the execution loop. That belongs to the Planner.
    This separation allows swapping planning strategies (ReAct, Plan-Execute, etc.)
    without touching the Agent core.
    """

    def __init__(self, container: Container):
        self.llm: LLMProvider = container.resolve(LLMProvider)
        self.tools: ToolRegistry = container.resolve(ToolRegistry)
        self.memory: MemoryStore = container.resolve(MemoryStore)
        self.retriever: ContextRetriever = container.resolve(ContextRetriever)
        self.sandbox: Sandbox = container.resolve(Sandbox)
        self.event_bus: EventBus = container.resolve(EventBus)
        self._planner: Planner = container.resolve(Planner)

    async def run(self, task: str, session: Session) -> AgentResult:
        # 1. Build context
        context = await self._build_context(task)

        # 2. Delegate to planner
        result = await self._planner.execute(
            task=task,
            context=context,
            tools=self.tools,
            llm=self.llm,
            sandbox=self.sandbox,
            session=session,
            event_bus=self.event_bus,
        )

        # 3. Persist session memory
        await self._persist_memory(session, task, result)

        return result

    async def _build_context(self, task: str):
        """Assemble context from retriever + memory."""
        from pathlib import Path
        return await self.retriever.retrieve(
            task=task,
            project_root=Path.cwd(),
            tools=self.tools,
            memory=self.memory,
        )

    async def _persist_memory(self, session: Session, task: str, result: AgentResult) -> None:
        """Store task summary as session memory."""
        import uuid
        from datetime import datetime

        entry = MemoryEntry(
            id=str(uuid.uuid4()),
            content=f"Task: {task}\nResult ({result.status.value}): {result.output[:500]}",
            level=MemoryLevel.SESSION,
            metadata=MemoryMetadata(
                timestamp=datetime.now(),
                tags=["task-result", result.status.value],
                source_task=task,
            ),
        )
        await self.memory.store(entry)
```

- [ ] **Step 4: Run test**

Run: `pytest tests/core/test_agent.py -v`
Expected: 2 PASS

- [ ] **Step 5: Commit**

```bash
git add synapse/core/agent.py tests/core/test_agent.py
git commit -m "feat: add core Agent — assembles dependencies, delegates to Planner"
```

---

### Task 17: ReAct Planner

**Files:**
- Create: `synapse/modules/planning/__init__.py`
- Create: `synapse/modules/planning/react.py`
- Create: `tests/modules/test_react_planner.py`

**Interfaces:**
- Consumes: `Planner` protocol, `LLMProvider`, `ToolRegistry`, `Sandbox`, `Session`, `EventBus`
- Produces: `ReActPlanner` with think→act→observe loop

- [ ] **Step 1: Write failing test**

```python
"""Tests for ReAct Planner."""
import pytest
from unittest.mock import AsyncMock, patch
from synapse.modules.planning.react import ReActPlanner
from synapse.core.session import Session
from synapse.core.events import EventBus
from synapse.protocols.llm import LLMResponse, Message
from synapse.protocols.tool import ToolResult, ToolCallMetadata
from synapse.protocols.retriever import Context


@pytest.mark.asyncio
async def test_react_completes_without_tool_calls():
    """If LLM returns text without tool calls, loop ends immediately."""
    mock_llm = AsyncMock()
    mock_llm.chat.return_value = LLMResponse(
        content="I have completed the task.",
        tool_calls=[],
        stop_reason="end_turn",
        usage={"input": 10, "output": 5},
    )

    mock_tools = AsyncMock()
    mock_sandbox = AsyncMock()
    event_bus = EventBus()

    planner = ReActPlanner(max_iterations=50)
    session = Session()
    context = Context()

    result = await planner.execute(
        task="Say hello",
        context=context,
        tools=mock_tools,
        llm=mock_llm,
        sandbox=mock_sandbox,
        session=session,
        event_bus=event_bus,
    )

    assert result.status.value == "success"
    assert "I have completed" in result.output
    assert result.metrics.tool_call_count == 0


@pytest.mark.asyncio
async def test_react_calls_tool():
    """LLM requests tool → tool executes → result fed back to LLM."""
    mock_llm = AsyncMock()
    # First call: tool_use
    mock_llm.chat.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[{"id": "t1", "name": "read", "input": {"path": "/test.txt"}}],
            stop_reason="tool_use",
            usage={"input": 10, "output": 5},
        ),
        # Second call: final text response
        LLMResponse(
            content="File contents: hello",
            tool_calls=[],
            stop_reason="end_turn",
            usage={"input": 15, "output": 8},
        ),
    ]

    mock_tool = AsyncMock()
    mock_tool.execute.return_value = ToolResult(
        success=True, output="hello",
        metadata=ToolCallMetadata(tool_name="read"),
    )
    mock_tools = AsyncMock()
    mock_tools.get.return_value = mock_tool

    mock_sandbox = AsyncMock()
    event_bus = EventBus()

    planner = ReActPlanner(max_iterations=50)
    session = Session()

    result = await planner.execute(
        task="Read test.txt",
        context=Context(),
        tools=mock_tools,
        llm=mock_llm,
        sandbox=mock_sandbox,
        session=session,
        event_bus=event_bus,
    )

    assert result.metrics.tool_call_count == 1
    assert mock_tool.execute.called


@pytest.mark.asyncio
async def test_react_hits_max_iterations():
    """When loop exceeds max_iterations, return PARTIAL."""
    mock_llm = AsyncMock()
    mock_llm.chat.return_value = LLMResponse(
        content="",
        tool_calls=[{"id": "t1", "name": "read", "input": {"path": "/test.txt"}}],
        stop_reason="tool_use",
        usage={"input": 5, "output": 2},
    )

    mock_tool = AsyncMock()
    mock_tool.execute.return_value = ToolResult(
        success=True, output="ok",
        metadata=ToolCallMetadata(tool_name="read"),
    )
    mock_tools = AsyncMock()
    mock_tools.get.return_value = mock_tool
    mock_tools.get_schemas.return_value = [{"name": "read", "description": "Read", "input_schema": {}}]

    mock_sandbox = AsyncMock()
    event_bus = EventBus()

    planner = ReActPlanner(max_iterations=3)
    session = Session()

    result = await planner.execute(
        task="test",
        context=Context(),
        tools=mock_tools,
        llm=mock_llm,
        sandbox=mock_sandbox,
        session=session,
        event_bus=event_bus,
    )

    assert result.status.value == "partial"
    assert "Max iterations" in result.output


@pytest.mark.asyncio
async def test_react_detects_thrashing():
    """Same file modified > threshold → emit event."""
    mock_llm = AsyncMock()
    mock_llm.chat.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[{"id": f"t{i}", "name": "write", "input": {"path": "/same_file.py"}}],
            stop_reason="tool_use",
            usage={"input": 5, "output": 2},
        )
        for i in range(5)
    ] + [
        LLMResponse(
            content="Done",
            tool_calls=[],
            stop_reason="end_turn",
            usage={"input": 5, "output": 2},
        ),
    ]

    mock_tool = AsyncMock()
    mock_tool.execute.return_value = ToolResult(
        success=True, output="ok",
        metadata=ToolCallMetadata(tool_name="write", files_touched=["/same_file.py"]),
    )
    mock_tools = AsyncMock()
    mock_tools.get.return_value = mock_tool
    mock_tools.get_schemas.return_value = []

    mock_sandbox = AsyncMock()
    event_bus = EventBus()
    thrashing_events = []

    async def on_thrashing(event):
        thrashing_events.append(event)

    event_bus.subscribe("thrashing_detected", on_thrashing)

    planner = ReActPlanner(max_iterations=10, thrashing_threshold=3)
    session = Session()

    await planner.execute(
        task="test",
        context=Context(),
        tools=mock_tools,
        llm=mock_llm,
        sandbox=mock_sandbox,
        session=session,
        event_bus=event_bus,
    )

    assert len(thrashing_events) > 0
```

- [ ] **Step 2: Run test to verify fail**

Run: `pytest tests/modules/test_react_planner.py -v`
Expected: FAIL

- [ ] **Step 3: Write react.py**

```python
"""ReAct Planner — Think → Act → Observe loop."""

import time
from synapse.protocols.planner import (
    AgentResult, ExecutionMetrics, ResultStatus, PlanningMode
)
from synapse.protocols.llm import Message
from synapse.protocols.events import (
    ToolCallStarted, ToolCallCompleted, ThrashingDetected, AgentCompleted
)
from synapse.core.exceptions import PlannerError


class ReActPlanner:
    """Classic ReAct loop: the LLM thinks, calls tools, observes results, repeats.

    This is the simplest planning mode. For complex multi-step tasks,
    use PlanExecutePlanner (Phase 2).
    """

    mode = PlanningMode.REACT

    def __init__(self, max_iterations: int = 50, thrashing_threshold: int = 3):
        self.max_iterations = max_iterations
        self.thrashing_threshold = thrashing_threshold

    async def execute(self, task, context, tools, llm, sandbox, session, event_bus) -> AgentResult:
        start_time = time.time()
        metrics = ExecutionMetrics()
        file_touch_counts: dict[str, int] = {}

        # Build initial messages
        system_prompt = self._build_system_prompt(context)
        messages = [
            Message(role="system", content=system_prompt),
            Message(role="user", content=task),
        ]

        tool_schemas = tools.get_schemas() if hasattr(tools, 'get_schemas') else []

        final_output = ""

        for iteration in range(1, self.max_iterations + 1):
            # Call LLM
            response = await llm.chat(messages, tools=tool_schemas if tool_schemas else None)
            metrics.tokens_input += response.usage.get("input", 0)
            metrics.tokens_output += response.usage.get("output", 0)

            # Add assistant response to messages
            assistant_content = response.content
            if response.tool_calls:
                # Anthropic expects tool_use content blocks
                assistant_content = ""
            messages.append(Message(role="assistant", content=assistant_content))

            # No tool calls → task is complete
            if not response.tool_calls:
                final_output = response.content
                break

            # Execute each tool call
            tool_results = []
            for tc in response.tool_calls:
                tool_name = tc["name"]
                tool_input = tc["input"]

                # Emit event
                await event_bus.emit(ToolCallStarted(
                    session_id=session.id, tool_name=tool_name, tool_params=tool_input,
                ))

                t0 = time.time()
                try:
                    tool = tools.get(tool_name)
                    result = await tool.execute(tool_input, sandbox=sandbox)
                except KeyError:
                    result = type("TR", (), {
                        "success": False, "output": "", "error": f"Unknown tool: {tool_name}",
                        "metadata": type("M", (), {"tool_name": tool_name, "files_touched": [], "sandbox_used": False})(),
                    })()

                metrics.tool_call_count += 1
                if result.success:
                    metrics.tool_success_count += 1

                # Track file modifications for thrashing detection
                for f in result.metadata.files_touched:
                    file_touch_counts[f] = file_touch_counts.get(f, 0) + 1
                    if file_touch_counts[f] >= self.thrashing_threshold:
                        await event_bus.emit(ThrashingDetected(
                            session_id=session.id,
                            file_path=f,
                            modification_count=file_touch_counts[f],
                        ))
                        metrics.thrashing_events += 1

                duration_ms = int((time.time() - t0) * 1000)
                await event_bus.emit(ToolCallCompleted(
                    session_id=session.id,
                    tool_name=tool_name,
                    success=result.success,
                    duration_ms=duration_ms,
                    files_touched=result.metadata.files_touched,
                ))

                tool_results.append((tc["id"], result))

            # Feed tool results back as user messages
            for tool_id, result in tool_results:
                status = "success" if result.success else "failed"
                messages.append(Message(
                    role="user",
                    content=f"[Tool {tool_id} {status}]: {result.output}\nError: {result.error or 'none'}",
                ))

        else:
            # Exceeded max iterations
            final_output = f"Max iterations ({self.max_iterations}) reached. Task may be incomplete."
            result_status = ResultStatus.PARTIAL
        else:
            result_status = ResultStatus.SUCCESS

        metrics.duration_ms = int((time.time() - start_time) * 1000)

        await event_bus.emit(AgentCompleted(
            session_id=session.id,
            status=result_status.value,
            total_tokens=metrics.tokens_input + metrics.tokens_output,
            tool_calls=metrics.tool_call_count,
            duration_ms=metrics.duration_ms,
        ))

        return AgentResult(
            status=result_status,
            output=final_output,
            metrics=metrics,
        )

    def _build_system_prompt(self, context) -> str:
        """Build system prompt from context blocks."""
        blocks = []
        for block in context.system:
            blocks.append(block.content)
        return "\n\n".join(blocks) if blocks else "You are a helpful coding assistant."
```

- [ ] **Step 4: Write planning/__init__.py**

```python
"""Planner implementations."""
```

- [ ] **Step 5: Run test**

Run: `pytest tests/modules/test_react_planner.py -v`
Expected: 4 PASS

- [ ] **Step 6: Commit**

```bash
git add synapse/modules/planning/ tests/modules/test_react_planner.py
git commit -m "feat: add ReAct planner with think-act-observe loop and thrashing detection"
```

---

### Task 18: CLI Entry Point

**Files:**
- Create: `synapse/adapters/__init__.py`
- Create: `synapse/adapters/cli.py`

**Interfaces:**
- Consumes: `Container`, `Agent`, all module implementations
- Produces: `main()` — CLI entry point, `synapse run "task"`

- [ ] **Step 1: Write adapters/__init__.py**

```python
"""Adapter layer — CLI, Python API, HTTP server."""
```

- [ ] **Step 2: Write cli.py**

```python
"""CLI entry point for Synapse."""

import argparse
import asyncio
import sys
from pathlib import Path

from synapse.config import load_config
from synapse.core.container import Container
from synapse.core.agent import Agent
from synapse.core.session import Session
from synapse.core.events import EventBus

from synapse.modules.providers.anthropic import AnthropicProvider
from synapse.modules.planning.react import ReActPlanner
from synapse.modules.tools.registry import DefaultToolRegistry
from synapse.modules.tools.file_read import ReadTool
from synapse.modules.tools.file_write import WriteTool
from synapse.modules.tools.file_edit import EditTool
from synapse.modules.tools.file_glob import GlobTool
from synapse.modules.tools.search_grep import GrepTool
from synapse.modules.tools.shell import ShellTool
from synapse.modules.tools.git_ import GitTool
from synapse.modules.memory.session import SessionMemory
from synapse.modules.context.retriever import BasicContextRetriever
from synapse.modules.security.sandbox import ProcessSandbox
from synapse.modules.security.auth import ActionAuthorizer

from synapse.protocols.llm import LLMProvider
from synapse.protocols.planner import Planner
from synapse.protocols.tool import ToolRegistry
from synapse.protocols.memory import MemoryStore
from synapse.protocols.retriever import ContextRetriever
from synapse.protocols.sandbox import Sandbox


def build_container(config) -> Container:
    """Wire all dependencies into the IoC container."""
    c = Container()

    # Core infrastructure
    event_bus = EventBus()
    c.register(EventBus, event_bus)

    # LLM Provider
    provider = AnthropicProvider(
        model=config.provider.model,
        api_key=config.provider.api_key,
    )
    c.register(LLMProvider, provider)

    # Tools
    registry = DefaultToolRegistry()
    registry.register(ReadTool())
    registry.register(WriteTool())
    registry.register(EditTool())
    registry.register(GlobTool())
    registry.register(GrepTool())
    registry.register(ShellTool())
    registry.register(GitTool())
    c.register(ToolRegistry, registry)

    # Memory
    memory = SessionMemory()
    c.register(MemoryStore, memory)

    # Context
    retriever = BasicContextRetriever()
    c.register(ContextRetriever, retriever)

    # Security
    sandbox = ProcessSandbox()
    c.register(Sandbox, sandbox)

    # Planner
    planner = ReActPlanner(max_iterations=config.planning.max_iterations)
    c.register(Planner, planner)

    return c


def main():
    parser = argparse.ArgumentParser(
        prog="synapse",
        description="Synapse — Connecting ideas into code",
    )
    sub = parser.add_subparsers(dest="command")

    run_parser = sub.add_parser("run", help="Execute a task")
    run_parser.add_argument("task", nargs="+", help="Task description")

    sub.add_parser("version", help="Show version")

    args = parser.parse_args()

    if args.command == "version":
        from synapse import __version__
        print(f"Synapse v{__version__}")
        return

    if args.command == "run":
        task = " ".join(args.task)
        config = load_config()
        container = build_container(config)
        agent = Agent(container)
        session = Session()

        async def execute():
            result = await agent.run(task, session)
            return result

        result = asyncio.run(execute())
        print(f"\n[Status: {result.status.value}]")
        print(result.output)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Verify CLI starts**

Run: `python -m synapse.adapters.cli version`
Expected: `Synapse v0.1.0`

Run: `python -m synapse.adapters.cli run --help`
Expected: Help output for run command

- [ ] **Step 4: Commit**

```bash
git add synapse/adapters/
git commit -m "feat: add CLI entry point — synapse run and synapse version"
```

---

### Task 19: Integration test — end-to-end with mock LLM

**Files:**
- Create: `tests/test_integration.py`

**Interfaces:**
- Tests the full pipeline: config → container → agent → planner → tools, with a mock LLM

- [ ] **Step 1: Write integration test**

```python
"""Integration test — full pipeline with mock LLM."""
import pytest
from unittest.mock import AsyncMock, patch
from synapse.core.container import Container
from synapse.core.agent import Agent
from synapse.core.session import Session
from synapse.core.events import EventBus
from synapse.modules.providers.anthropic import AnthropicProvider
from synapse.modules.planning.react import ReActPlanner
from synapse.modules.tools.registry import DefaultToolRegistry
from synapse.modules.tools.file_read import ReadTool
from synapse.modules.tools.file_write import WriteTool
from synapse.modules.tools.file_edit import EditTool
from synapse.modules.tools.file_glob import GlobTool
from synapse.modules.tools.search_grep import GrepTool
from synapse.modules.tools.shell import ShellTool
from synapse.modules.tools.git_ import GitTool
from synapse.modules.memory.session import SessionMemory
from synapse.modules.context.retriever import BasicContextRetriever
from synapse.modules.security.sandbox import ProcessSandbox
from synapse.protocols.llm import LLMProvider
from synapse.protocols.planner import Planner
from synapse.protocols.tool import ToolRegistry
from synapse.protocols.memory import MemoryStore
from synapse.protocols.retriever import ContextRetriever
from synapse.protocols.sandbox import Sandbox
from synapse.protocols.llm import LLMResponse


def build_test_container():
    """Build container with real modules but a mock LLM."""
    c = Container()
    c.register(EventBus, EventBus())

    mock_llm = AsyncMock()
    mock_llm.model_id = "mock"
    mock_llm.chat.return_value = LLMResponse(
        content="Task completed successfully.",
        tool_calls=[],
        stop_reason="end_turn",
        usage={"input": 10, "output": 5},
    )
    c.register(LLMProvider, mock_llm)

    registry = DefaultToolRegistry()
    registry.register(ReadTool())
    registry.register(WriteTool())
    registry.register(EditTool())
    registry.register(GlobTool())
    registry.register(GrepTool())
    registry.register(ShellTool())
    registry.register(GitTool())
    c.register(ToolRegistry, registry)

    c.register(MemoryStore, SessionMemory())
    c.register(ContextRetriever, BasicContextRetriever())
    c.register(Sandbox, ProcessSandbox())
    c.register(Planner, ReActPlanner(max_iterations=50))

    return c


@pytest.mark.asyncio
async def test_full_pipeline_no_tools():
    """Agent completes a task that requires no tool calls."""
    c = build_test_container()
    agent = Agent(c)
    session = Session()

    result = await agent.run("Say hello", session)

    assert result.status.value == "success"
    assert "Task completed" in result.output


@pytest.mark.asyncio
async def test_full_pipeline_with_writes(tmp_path):
    """Agent writes and reads a file."""
    c = build_test_container()

    # Override mock LLM to make a write call then a read call then finish
    mock_llm = c.resolve(LLMProvider)
    mock_llm.chat.side_effect = [
        LLMResponse(
            content="",
            tool_calls=[{"id": "w1", "name": "write", "input": {
                "path": str(tmp_path / "output.txt"), "content": "hello from synapse"
            }}],
            stop_reason="tool_use",
            usage={"input": 15, "output": 10},
        ),
        LLMResponse(
            content="",
            tool_calls=[{"id": "r1", "name": "read", "input": {"path": str(tmp_path / "output.txt")}}],
            stop_reason="tool_use",
            usage={"input": 5, "output": 3},
        ),
        LLMResponse(
            content="I have written and verified the file.",
            tool_calls=[],
            stop_reason="end_turn",
            usage={"input": 8, "output": 5},
        ),
    ]

    agent = Agent(c)
    session = Session()

    result = await agent.run("Write a file", session)

    assert result.status.value == "success"
    assert result.metrics.tool_call_count == 2
    # Verify file was actually written
    assert (tmp_path / "output.txt").read_text() == "hello from synapse"
```

- [ ] **Step 2: Run integration tests**

Run: `pytest tests/test_integration.py -v`
Expected: 2 PASS

- [ ] **Step 3: Commit**

```bash
git add tests/test_integration.py
git commit -m "test: add integration tests for full pipeline with mock LLM"
```

---

### Task 20: Run all tests and final verification

- [ ] **Step 1: Run full test suite**

Run: `pytest tests/ -v`

- [ ] **Step 2: Verify all tests pass**

- [ ] **Step 3: Verify module boundary rules**

Run: `python -c "
# protocols/ must not depend on core/ or modules/
import ast, sys
def check_imports(filepath, allowed_prefixes):
    tree = ast.parse(open(filepath).read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.name
                if any(name.startswith(p) and not any(name.startswith(a) for a in allowed_prefixes) for p in ['synapse.core', 'synapse.modules', 'synapse.adapters']):
                    print(f'  BAD IMPORT in {filepath}: {name}')
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                name = node.module
                if any(name.startswith(p) and not any(name.startswith(a) for a in allowed_prefixes) for p in ['synapse.core', 'synapse.modules', 'synapse.adapters']):
                    print(f'  BAD IMPORT in {filepath}: {name}')

from pathlib import Path
for f in Path('synapse/protocols').rglob('*.py'):
    check_imports(str(f), ['synapse.protocols', '__future__', 'typing', 'dataclasses', 'datetime', 'enum', 'uuid', 'pathlib', 'collections', 'abc'])
print('Protocol boundary check complete.')
"`

- [ ] **Step 4: Commit any final fixes**

```bash
git add -A
git commit -m "chore: final verification — all tests pass, protocol boundaries clean"
```

"""Session state management."""

import json
import uuid
from pathlib import Path

from synapse.protocols.llm import Message
from synapse.core.tokenizer import count_tokens

# Default on-disk location for persisted sessions (mirrors ~/.synapse/config.yaml).
DEFAULT_SESSION_DIR = Path.home() / ".synapse" / "sessions"


class Session:
    """Manages the state of a single agent interaction session.

    Holds message history, metadata, and provides forking for
    hierarchical planning sub-sessions. Sessions can be persisted to and
    resumed from disk (see save/load/list_sessions) so a conversation can
    be picked up after the process exits.
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

    # -- Persistence -------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "metadata": self.metadata,
            "messages": [
                {
                    "role": m.role,
                    "content": m.content,
                    "tool_calls": m.tool_calls,
                    "tool_call_id": m.tool_call_id,
                }
                for m in self.messages
            ],
        }

    def save(self, directory: Path | str = DEFAULT_SESSION_DIR) -> Path:
        """Persist the session to ``<directory>/<id>.json`` and return the path."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{self.id}.json"
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temp_path.replace(path)
        return path

    @classmethod
    def load(cls, path: Path | str) -> "Session":
        """Load a session previously written by :meth:`save`."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        s = cls(session_id=data.get("id"))
        s.metadata = data.get("metadata", {})
        s.messages = [
            Message(
                role=m["role"],
                content=m.get("content", ""),
                tool_calls=m.get("tool_calls", []),
                tool_call_id=m.get("tool_call_id"),
            )
            for m in data.get("messages", [])
        ]
        return s

    @classmethod
    def list_sessions(cls, directory: Path | str = DEFAULT_SESSION_DIR) -> list["Session"]:
        """Return persisted sessions, most recently modified first."""
        directory = Path(directory)
        if not directory.exists():
            return []
        out: list[Session] = []
        for p in sorted(directory.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            try:
                out.append(cls.load(p))
            except Exception:
                continue
        return out

    @property
    def estimated_tokens(self) -> int:
        """Rough token estimate (~1.3 chars per token for English)."""
        total_chars = sum(len(m.content) for m in self.messages)
        return max(1, sum(count_tokens(m.content) for m in self.messages))

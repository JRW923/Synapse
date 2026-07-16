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

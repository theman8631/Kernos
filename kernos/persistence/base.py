"""Abstract base classes for persistence stores.

The handler imports these interfaces, never the concrete implementations.
When MemOS replaces the JSON backend, only json_file.py changes.
"""
from abc import ABC, abstractmethod


class ConversationStore(ABC):
    """Append-only store for conversation history (user and assistant messages only)."""

    @abstractmethod
    async def append(self, tenant_id: str, conversation_id: str, entry: dict) -> None:
        """Append a message to conversation history. Append-only — never modify existing entries."""
        ...

    @abstractmethod
    async def get_recent(
        self, tenant_id: str, conversation_id: str, limit: int = 20
    ) -> list[dict]:
        """Return the most recent messages, oldest first.

        Returns only role and content fields suitable for Claude's messages array.
        Full metadata is preserved on disk but not loaded into context.
        Returns empty list for a new tenant/conversation (cold start).
        """
        ...

    @abstractmethod
    async def get_recent_full(
        self, tenant_id: str, conversation_id: str, limit: int = 20
    ) -> list[dict]:
        """Return the most recent messages with full metadata (timestamp, space_tags, etc.).

        Unlike get_recent(), includes all saved fields. Used by the router and
        thread reconstruction methods.
        Returns empty list for a new tenant/conversation.
        """
        ...

    @abstractmethod
    async def get_space_thread(
        self, tenant_id: str, conversation_id: str,
        space_id: str, max_messages: int = 50,
        include_untagged: bool = False,
        include_timestamp: bool = False,
    ) -> list[dict]:
        """Return messages tagged to this space, in chronological order.

        Filters the full message stream by space_tags containing space_id.
        Returns role and content fields (suitable for the agent's messages array).
        include_untagged: if True, also include messages with no space_tags (migration compat).
        include_timestamp: if True, also include the timestamp field.
        Returns the most recent max_messages that match.
        """
        ...

    @abstractmethod
    async def get_cross_domain_messages(
        self, tenant_id: str, conversation_id: str,
        active_space_id: str, last_n_turns: int = 5,
    ) -> list[dict]:
        """Return recent messages from OTHER spaces for ephemeral cross-domain injection.

        Returns the last N message pairs (user + assistant) that were NOT
        tagged to the active space. Includes role, content, and timestamp.
        Untagged messages (from pre-v2 conversations) are excluded.
        """
        ...

    @abstractmethod
    async def archive(self, tenant_id: str, conversation_id: str) -> None:
        """Move a conversation to the shadow archive. Non-destructive.

        Moves the conversation file to {tenant_id}/archive/conversations/{timestamp}/
        with metadata recording when it was archived.
        """
        ...


class TenantStore(ABC):
    """Store for tenant records — who this user is and what they've connected."""

    @abstractmethod
    async def get_or_create(self, tenant_id: str) -> dict:
        """Return the tenant record, creating with defaults if it doesn't exist.

        Auto-provisioning: unknown tenants are created silently.
        The user never "signs up" — they send a message and the system provisions.
        """
        ...

    @abstractmethod
    async def save(self, tenant_id: str, record: dict) -> None:
        """Persist an updated tenant record."""
        ...


class AuditStore(ABC):
    """Append-only store for tool calls, MCP round-trips, and system events.

    Audit entries are never loaded into Claude's context window.
    They exist for the trust dashboard and debugging.
    """

    @abstractmethod
    async def log(self, tenant_id: str, entry: dict) -> None:
        """Append an audit entry. Stored by date for natural partitioning."""
        ...

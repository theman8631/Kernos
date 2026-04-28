"""Bridge / AgentInbox interface.

The ``route_to_agent`` action library verb writes payloads to an
AgentInbox provider. Multiple concrete implementations are possible
(Notion, local folder, S3, etc.); the workflow primitive itself only
depends on the abstract Protocol. v1 ships ``NotionAgentInbox`` as
the available concrete during the developing-Kernos period;
installations choose whether to bind it.

Provider-configuration-containment (Kit edit, narrow review):
``route_to_agent`` depends on a configured ``AgentInbox`` provider
rather than a hardcoded default. The action library's
``RouteToAgentAction`` raises ``AgentInboxUnavailable`` with a clear
message if no provider is bound. Pin: structural test verifies the
explicit unavailable-error path rather than silent fall-through to
Notion.

Notion-independence pin: only ``NotionAgentInbox`` may import or
reference Notion APIs. The Protocol itself MUST stay
provider-neutral. Structural test scans action library + agent
inbox base for direct ``notion.so`` / Notion-tool references.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol


class AgentInboxUnavailable(RuntimeError):
    """Raised when ``route_to_agent`` is invoked without a bound
    AgentInbox provider. Caller should treat this as a workflow
    failure mode, not a silent fallback."""


@dataclass
class InboxPostResult:
    """Receipt returned from ``AgentInbox.post``. Verifier reads the
    receipt's ``persisted_id`` back from the inbox surface to confirm
    intent-satisfaction."""

    persisted_id: str
    posted_at: str
    metadata: dict = field(default_factory=dict)


@dataclass
class InboxItem:
    """A single payload retrieved from an inbox. ``persisted_id``
    matches the receipt that ``post`` returned."""

    persisted_id: str
    agent_id: str
    payload: dict
    posted_at: str
    metadata: dict = field(default_factory=dict)


class AgentInbox(Protocol):
    """Abstract bridge for routing payloads to agents.

    Implementations MUST be deterministic about ``persisted_id``
    issuance: a successful post returns a unique stable id that a
    later ``read`` can match. This is what makes the
    ``route_to_agent`` verifier work — the verifier reads the inbox
    by agent_id and checks that the posted ``persisted_id`` is
    visible.
    """

    async def post(
        self, agent_id: str, payload: dict, *, instance_id: str = "",
    ) -> InboxPostResult:
        ...

    async def read(
        self,
        agent_id: str,
        *,
        since: datetime | None = None,
        instance_id: str = "",
    ) -> list[InboxItem]:
        ...


class InMemoryAgentInbox:
    """Test-grade concrete useful for action-library unit tests and
    the C5 execution engine's integration tests. Not intended for
    production routing; production installations bind
    :class:`NotionAgentInbox` (or a future LocalFolderAgentInbox).

    Keys items by (instance_id, agent_id) so multi-tenant tests
    don't cross streams.
    """

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], list[InboxItem]] = {}
        self._counter = 0

    async def post(
        self, agent_id: str, payload: dict, *, instance_id: str = "",
    ) -> InboxPostResult:
        self._counter += 1
        persisted_id = f"in-mem-{self._counter}"
        posted_at = datetime.now(timezone.utc).isoformat()
        item = InboxItem(
            persisted_id=persisted_id,
            agent_id=agent_id,
            payload=payload,
            posted_at=posted_at,
        )
        self._items.setdefault((instance_id, agent_id), []).append(item)
        return InboxPostResult(persisted_id=persisted_id, posted_at=posted_at)

    async def read(
        self,
        agent_id: str,
        *,
        since: datetime | None = None,
        instance_id: str = "",
    ) -> list[InboxItem]:
        items = list(self._items.get((instance_id, agent_id), ()))
        if since is not None:
            cutoff = since.isoformat()
            items = [i for i in items if i.posted_at >= cutoff]
        return items


class NotionAgentInbox:
    """Concrete AgentInbox backed by Notion bridge inbox databases.

    v1 ships this as a stub: real Notion API calls land in the C5
    integration spec. The action library's Notion-independence pin
    intentionally allows the string ``notion`` to appear in this
    file because this IS the Notion concrete; the pin's scan
    excludes this module.

    Construction takes a callable that posts to the configured
    Notion inbox database and another that reads. Both should be
    Notion-tool-shaped invocations the operator wires up at
    install time. Provider-configuration-containment is enforced
    at the route_to_agent level: if no NotionAgentInbox (or other
    AgentInbox) is bound, the verb fails loudly.
    """

    def __init__(
        self,
        post_fn: Any | None = None,
        read_fn: Any | None = None,
    ) -> None:
        self._post_fn = post_fn
        self._read_fn = read_fn

    async def post(
        self, agent_id: str, payload: dict, *, instance_id: str = "",
    ) -> InboxPostResult:
        if self._post_fn is None:
            raise AgentInboxUnavailable(
                "NotionAgentInbox.post called without a configured post_fn — "
                "the operator must bind a Notion-backed callable at install time."
            )
        result = await self._post_fn(agent_id, payload, instance_id=instance_id)
        if isinstance(result, InboxPostResult):
            return result
        # Caller-supplied callable may return a dict; normalise.
        return InboxPostResult(
            persisted_id=str(result.get("persisted_id", "")),
            posted_at=str(result.get("posted_at", "")),
            metadata=result.get("metadata") or {},
        )

    async def read(
        self,
        agent_id: str,
        *,
        since: datetime | None = None,
        instance_id: str = "",
    ) -> list[InboxItem]:
        if self._read_fn is None:
            raise AgentInboxUnavailable(
                "NotionAgentInbox.read called without a configured read_fn"
            )
        items = await self._read_fn(agent_id, since=since, instance_id=instance_id)
        return [i if isinstance(i, InboxItem) else InboxItem(**i) for i in items]


__all__ = [
    "AgentInbox",
    "AgentInboxUnavailable",
    "InboxItem",
    "InboxPostResult",
    "InMemoryAgentInbox",
    "NotionAgentInbox",
]

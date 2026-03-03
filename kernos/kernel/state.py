"""State Store — the kernel's knowledge model.

Current understanding of the user and their world. The query surface for
context assembly. Four domains: tenant profile, user knowledge, behavioral
contracts, conversation summaries.
"""
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# ID generators
# ---------------------------------------------------------------------------


def _knowledge_id() -> str:
    return f"know_{uuid.uuid4().hex[:8]}"


def _rule_id() -> str:
    return f"rule_{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Domain 1: Tenant Profile
# ---------------------------------------------------------------------------


@dataclass
class TenantProfile:
    """Enriched tenant record. Richer than TenantStore's tenant.json."""

    tenant_id: str
    status: str                     # "active", "suspended", "cancelled"
    created_at: str                 # ISO timestamp
    platforms: dict[str, Any] = field(default_factory=dict)
    preferences: dict[str, Any] = field(default_factory=dict)
    capabilities: dict[str, str] = field(default_factory=dict)
    model_config: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Domain 2: User Knowledge
# ---------------------------------------------------------------------------


@dataclass
class KnowledgeEntry:
    """A piece of knowledge about the user or their world."""

    id: str                     # "know_{uuid8}"
    tenant_id: str
    category: str               # "entity", "fact", "preference", "pattern"
    subject: str                # "John", "gym membership", "meeting preferences"
    content: str                # The knowledge text
    confidence: str             # "stated", "inferred", "observed"
    source_event_id: str        # Provenance — links to the event that created this
    source_description: str     # Human-readable provenance
    created_at: str
    last_referenced: str
    tags: list[str]
    active: bool = True         # False = archived (never deleted)


# ---------------------------------------------------------------------------
# Domain 3: Behavioral Contracts
# ---------------------------------------------------------------------------


@dataclass
class ContractRule:
    """A behavioral rule governing what the agent may or must do."""

    id: str                     # "rule_{uuid8}"
    tenant_id: str
    capability: str             # "calendar", "email", "general"
    rule_type: str              # "must", "must_not", "preference", "escalation"
    description: str            # Human-readable — what the agent reads
    active: bool
    source: str                 # "default", "user_stated", "evolved"
    source_event_id: str | None # Links to event if user_stated or evolved
    created_at: str
    updated_at: str


def default_contract_rules(tenant_id: str, now: str) -> list[ContractRule]:
    """The conservative-by-default rules every new tenant starts with."""
    rules = [
        ("must_not", "general", "Never send messages to external contacts without owner approval"),
        ("must_not", "general", "Never delete or archive data without owner awareness"),
        ("must_not", "general", "Never share owner's private information with unrecognized senders"),
        ("must", "general", "Always confirm before any action that costs money"),
        ("must", "general", "Always confirm before sending communications on the owner's behalf"),
        ("preference", "general", "Keep responses concise unless detail is requested"),
        ("escalation", "general", "Escalate to owner when request is ambiguous and stakes are non-trivial"),
    ]
    return [
        ContractRule(
            id=_rule_id(),
            tenant_id=tenant_id,
            capability=cap,
            rule_type=rt,
            description=desc,
            active=True,
            source="default",
            source_event_id=None,
            created_at=now,
            updated_at=now,
        )
        for rt, cap, desc in rules
    ]


# ---------------------------------------------------------------------------
# Domain 4: Conversation Summaries
# ---------------------------------------------------------------------------


@dataclass
class ConversationSummary:
    """Lightweight metadata about a conversation. Not the messages themselves."""

    tenant_id: str
    conversation_id: str
    platform: str
    message_count: int
    first_message_at: str
    last_message_at: str
    topics: list[str] = field(default_factory=list)
    active: bool = True


# ---------------------------------------------------------------------------
# StateStore interface
# ---------------------------------------------------------------------------


class StateStore(ABC):
    """The kernel's knowledge model. Current understanding of user and world."""

    # Tenant Profile
    @abstractmethod
    async def get_tenant_profile(self, tenant_id: str) -> TenantProfile | None: ...

    @abstractmethod
    async def save_tenant_profile(self, tenant_id: str, profile: TenantProfile) -> None: ...

    # Knowledge
    @abstractmethod
    async def add_knowledge(self, entry: KnowledgeEntry) -> None: ...

    @abstractmethod
    async def query_knowledge(
        self,
        tenant_id: str,
        subject: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        active_only: bool = True,
        limit: int = 20,
    ) -> list[KnowledgeEntry]: ...

    @abstractmethod
    async def update_knowledge(self, entry_id: str, updates: dict) -> None: ...

    # Behavioral Contracts
    @abstractmethod
    async def get_contract_rules(
        self,
        tenant_id: str,
        capability: str | None = None,
        rule_type: str | None = None,
        active_only: bool = True,
    ) -> list[ContractRule]: ...

    @abstractmethod
    async def add_contract_rule(self, rule: ContractRule) -> None: ...

    @abstractmethod
    async def update_contract_rule(self, rule_id: str, updates: dict) -> None: ...

    # Conversation Summaries
    @abstractmethod
    async def get_conversation_summary(
        self, tenant_id: str, conversation_id: str
    ) -> ConversationSummary | None: ...

    @abstractmethod
    async def save_conversation_summary(self, summary: ConversationSummary) -> None: ...

    @abstractmethod
    async def list_conversations(
        self, tenant_id: str, active_only: bool = True, limit: int = 20
    ) -> list[ConversationSummary]: ...

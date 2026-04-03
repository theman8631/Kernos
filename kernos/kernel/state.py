"""State Store — the kernel's knowledge model.

Current understanding of the user and their world. The query surface for
context assembly. Four domains: tenant profile, user knowledge, behavioral
contracts, conversation summaries.
"""
import hashlib
import math
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


def _content_hash(tenant_id: str, subject: str, content: str) -> str:
    """SHA256[:16] of (tenant_id|subject|content) for deduplication."""
    raw = f"{tenant_id}|{subject.lower().strip()}|{content.lower().strip()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

from kernos.kernel.soul import Soul
from kernos.kernel.entities import EntityNode, IdentityEdge
from kernos.kernel.spaces import ContextSpace


# ---------------------------------------------------------------------------
# ID generators
# ---------------------------------------------------------------------------


def _knowledge_id() -> str:
    return f"know_{uuid.uuid4().hex[:8]}"


def _rule_id() -> str:
    return f"rule_{uuid.uuid4().hex[:8]}"


def _entity_id() -> str:
    return f"ent_{uuid.uuid4().hex[:8]}"


def _pending_id() -> str:
    return f"pending_{uuid.uuid4().hex[:8]}"


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
    last_active_space_id: str = ""   # Tracks active space across messages
    permission_overrides: dict[str, str] = field(default_factory=dict)
    # Maps capability_name → "ask" | "always-allow"
    # Default (not in dict) = "ask" (gate fires)
    # "always-allow" → bypass gate for all write tools in this capability
    developer_mode: bool = False


# ---------------------------------------------------------------------------
# Domain 2: User Knowledge
# ---------------------------------------------------------------------------


@dataclass
class KnowledgeEntry:
    """A piece of knowledge about the user or their world."""

    # --- Identity ---
    id: str                          # "know_{uuid8}"
    tenant_id: str
    category: str                    # "entity", "fact", "preference", "pattern"
    subject: str                     # What this is about
    content: str                     # The knowledge text
    confidence: str                  # "stated", "inferred", "observed"
    source_event_id: str             # Provenance — links to the event that created this
    source_description: str          # Human-readable provenance
    created_at: str
    last_referenced: str
    tags: list[str]

    # --- Status ---
    active: bool = True              # False = archived (never deleted)

    # --- Provenance (extended) ---
    supersedes: str = ""             # ID of the entry this one replaces (provenance chain)
    content_hash: str = ""           # SHA256[:16] of (tenant_id|subject|content) for dedup
    entity_node_id: str = ""         # Link to EntityNode (populated by entity resolution)

    # --- Legacy compat: durability is retired, kept for reading old JSON ---
    # New entries leave this empty. lifecycle_archetype is the authoritative field.
    durability: str = ""             # deprecated — "permanent"|"session"|"expires_at:<ISO>"

    # --- Content lifecycle ---
    lifecycle_archetype: str = "structural"
    # "identity" | "structural" | "habitual" | "contextual" | "ephemeral"

    # --- Temporal (bitemporal from Graphiti) ---
    expired_at: str = ""             # When the kernel invalidated this (transaction time)
    valid_at: str = ""               # When this became true in reality (valid time)
    invalid_at: str = ""             # When this stopped being true (valid time)

    # --- Strength (dual-strength from Bjork/FSRS) ---
    storage_strength: float = 1.0    # How well-established. Monotonically increasing.
    last_reinforced_at: str = ""     # When user last mentioned/confirmed (not last_referenced)
    reinforcement_count: int = 1     # How many times confirmed across conversations

    # --- Foresight (from EverMemOS) ---
    foresight_signal: str = ""       # Forward-looking implication, if any
    foresight_expires: str = ""      # When the foresight signal becomes irrelevant

    # --- Classification ---
    context_space: str = ""          # Reserved for context spaces (empty = global)
    salience: float = 0.5            # Initial importance weight (0.0-1.0)

    # --- Multi-member foundation (V1: defaults, V2: populated) ---
    owner_member_id: str = ""        # Who contributed this. Empty = instance owner.
    sensitivity: str = "open"        # "open" | "contextual" | "personal" | "classified"
    visible_to: list[str] | None = None  # None = follow sensitivity default. List = only these member_ids.


# ---------------------------------------------------------------------------
# Retrieval strength — computed at read time, never stored
# ---------------------------------------------------------------------------

ARCHETYPE_STABILITY: dict[str, int] = {
    "identity": 730,      # ~2 years
    "structural": 120,    # ~4 months
    "habitual": 45,       # ~6 weeks
    "contextual": 14,     # ~2 weeks
    "ephemeral": 1,       # ~1 day
}

# FSRS-6 parameter (default — tunable from usage data in Phase 3)
W20 = 0.5


def compute_retrieval_strength(entry: KnowledgeEntry, now_iso: str) -> float:
    """Compute current retrieval strength using FSRS-6 power-law decay.

    Called at read time, not stored. Keeps write path clean.
    Returns 0.0-1.0 where 1.0 = fully accessible, 0.0 = effectively forgotten.
    """
    if not entry.last_reinforced_at:
        return 1.0  # New entry, no decay yet

    from datetime import datetime, timezone
    last = datetime.fromisoformat(entry.last_reinforced_at)
    now = datetime.fromisoformat(now_iso)
    days_since = max((now - last).total_seconds() / 86400, 0)

    if days_since == 0:
        return 1.0

    base_stability = ARCHETYPE_STABILITY.get(entry.lifecycle_archetype, 120)
    effective_stability = base_stability * (1 + 0.1 * math.log1p(entry.storage_strength))

    factor = 0.9 ** (-1 / W20) - 1
    return (1 + factor * days_since / effective_stability) ** (-W20)


# ---------------------------------------------------------------------------
# Domain 3: Behavioral Contracts → Covenant
# ---------------------------------------------------------------------------


@dataclass
class CovenantRule:
    """A behavioral rule in the Covenant — the living contract between agent and user."""

    # --- Preserved from ContractRule (backwards compatible) ---
    id: str                          # "rule_{uuid8}"
    tenant_id: str
    capability: str                  # "calendar", "email", "general"
    rule_type: str                   # "must", "must_not", "preference", "escalation"
    description: str                 # Human-readable — what the agent reads and user sees
    active: bool
    source: str                      # "default", "user_stated", "evolved"
    source_event_id: str | None = None
    created_at: str = ""
    updated_at: str = ""
    context_space: str | None = None  # None = global

    # --- Covenant layer ---
    layer: str = "principle"         # "principle" | "practice"

    # --- Action class targeting ---
    action_class: str = ""           # "email.delete.spam", "calendar.schedule.business_hours"
    trigger_tool: str = ""           # MCP tool name. Empty = all tools in capability.

    # --- Enforcement ---
    enforcement_tier: str = "confirm"   # "silent" | "notify" | "confirm" | "block"
    fallback_action: str = "ask_user"   # "ask_user" | "stage_as_draft" | "log_and_proceed" | "block_with_explanation"
    escalation_message: str = ""        # "Sending to {recipient} — confirm?"

    # --- Graduation state (meaningful for Practices only) ---
    graduation_positive_signals: int = 0
    graduation_last_rejection: str = ""     # ISO timestamp, empty = never rejected
    graduation_eligible: bool = False
    graduation_threshold: int = 25
    graduation_tier_locked_until: str = ""  # Rate limit

    # --- Versioning ---
    supersedes: str = ""
    superseded_by: str = ""              # rule_id that replaced this, "user_removed", or "" (active)
    version: int = 1

    # --- Preference linkage (Phase 6A) ---
    source_preference_id: str = ""       # Preference ID that generated this rule, or ""

    # --- Reserved for future phases ---
    agent_id: str = ""
    precondition: str = ""
    workspace_id: str = ""


# Backwards-compatibility alias — ContractRule is CovenantRule
ContractRule = CovenantRule


def _enforcement_tier_for(rule_type: str) -> str:
    """Map legacy rule_type to enforcement_tier for new default rules."""
    return {
        "must_not": "confirm",
        "must": "confirm",
        "preference": "silent",
        "escalation": "confirm",
    }.get(rule_type, "confirm")


def default_covenant_rules(tenant_id: str, now: str) -> list[CovenantRule]:
    """The conservative-by-default rules every new tenant starts with."""
    rules = [
        ("must_not", "general", "Never send messages to third-party contacts unless the owner initiated the request"),
        ("must_not", "general", "Never delete or archive data without owner awareness"),
        ("must_not", "general", "Never share owner's private information with unrecognized senders"),
        ("must", "general", "Always confirm before any action that costs money"),
        ("must", "general", "For composed messages to third parties, show the draft before sending. For simple relays, briefly confirm content and recipient. Owner-directed delivery to connected channels needs no confirmation."),
        ("preference", "general", "Keep responses concise unless detail is requested"),
        ("escalation", "general", "Escalate to owner when request is ambiguous and stakes are non-trivial"),
    ]
    return [
        CovenantRule(
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
            enforcement_tier=_enforcement_tier_for(rt),
        )
        for rt, cap, desc in rules
    ]


# Backwards-compatibility alias
default_contract_rules = default_covenant_rules


# ---------------------------------------------------------------------------
# Domain 3b: Pending Actions (for Dispatch Interceptor, Phase 2B)
# ---------------------------------------------------------------------------


@dataclass
class PendingAction:
    """A tool call staged for user confirmation by the Dispatch Interceptor."""

    id: str                     # "pending_{uuid8}"
    tenant_id: str
    rule_id: str                # Which CovenantRule triggered this
    tool_name: str
    tool_arguments: dict = field(default_factory=dict)
    context: dict = field(default_factory=dict)  # Enough state to resume
    created_at: str = ""
    expires_at: str = ""        # Default: 1 hour from creation
    status: str = "pending"     # "pending" | "approved" | "rejected" | "expired"
    conversation_id: str = ""
    batch_id: str = ""          # Groups tool calls from the same reasoning turn


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
# Domain 5: First-Class Preferences
# ---------------------------------------------------------------------------


def generate_preference_id() -> str:
    """Generate a unique preference ID."""
    return f"pref_{uuid.uuid4().hex[:8]}"


@dataclass
class Preference:
    """A first-class user preference — upstream of covenants, triggers, and knowledge.

    Preferences capture WHAT the user wants to remain true. They have different
    lifecycle, supersession, and introspection needs than KnowledgeEntry.
    """

    # Identity
    id: str                       # "pref_{uuid8}"
    tenant_id: str

    # Intent
    intent: str                   # Original user language that created this

    # Classification
    category: str                 # "notification", "behavior", "format", "access", "schedule"
    subject: str                  # What it's about: "calendar_events", "email", "responses", etc.
    action: str                   # "notify", "always_do", "never_do", "prefer", "schedule"
    parameters: dict = field(default_factory=dict)  # Specific values: lead_time, channel, etc.

    # Scope
    scope: str = "global"         # "global" or space_id
    context_space: str = ""       # If scope is space-specific, which space

    # Lifecycle
    status: str = "active"        # "active", "superseded", "revoked"
    supersedes: str = ""          # Preference ID this replaces (if any)
    superseded_by: str = ""       # Preference ID that replaced this (if any)

    # Provenance
    created_at: str = ""          # ISO 8601 UTC
    updated_at: str = ""          # Last modification
    source_turn_id: str = ""      # Conversation turn / event that created this
    source_knowledge_id: str = "" # If migrated from KnowledgeEntry, the original ID

    # Derived artifacts
    derived_trigger_ids: list[str] = field(default_factory=list)
    derived_covenant_ids: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# StateStore interface
# ---------------------------------------------------------------------------


class StateStore(ABC):
    """The kernel's knowledge model. Current understanding of user and world."""

    # Soul
    @abstractmethod
    async def get_soul(self, tenant_id: str) -> Soul | None: ...

    @abstractmethod
    async def save_soul(self, soul: Soul, *, source: str = "", trigger: str = "") -> None: ...

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
    async def update_knowledge(self, tenant_id: str, entry_id: str, updates: dict) -> None: ...

    @abstractmethod
    async def save_knowledge_entry(self, entry: KnowledgeEntry) -> None:
        """Upsert a KnowledgeEntry by ID (write or overwrite)."""
        ...

    @abstractmethod
    async def get_knowledge_entry(self, tenant_id: str, entry_id: str) -> "KnowledgeEntry | None":
        """Get a single KnowledgeEntry by ID. Returns None if not found."""
        ...

    @abstractmethod
    async def get_knowledge_hashes(self, tenant_id: str) -> set[str]:
        """Return set of content_hash values for all active entries. O(1) dedup check."""
        ...

    @abstractmethod
    async def get_knowledge_by_hash(
        self, tenant_id: str, content_hash: str
    ) -> "KnowledgeEntry | None":
        """Find an active entry by content_hash. Returns None if not found."""
        ...

    # Behavioral Contracts (method names preserved for backwards compatibility)
    @abstractmethod
    async def get_contract_rules(
        self,
        tenant_id: str,
        capability: str | None = None,
        rule_type: str | None = None,
        active_only: bool = True,
    ) -> list[CovenantRule]: ...

    @abstractmethod
    async def query_covenant_rules(
        self,
        tenant_id: str,
        capability: str | None = None,
        context_space_scope: list[str | None] | None = None,
        active_only: bool = True,
    ) -> list[CovenantRule]:
        """Query covenant rules with optional context space scope filtering.

        context_space_scope: [space_id, None] loads space-scoped + global rules.
        If None (not provided), returns all rules (used by CLI/admin).
        """
        ...

    @abstractmethod
    async def add_contract_rule(self, rule: CovenantRule) -> None: ...

    @abstractmethod
    async def update_contract_rule(self, tenant_id: str, rule_id: str, updates: dict) -> None: ...

    # Entity Resolution (Phase 2A will implement real logic)
    @abstractmethod
    async def save_entity_node(self, node: EntityNode) -> None: ...

    @abstractmethod
    async def get_entity_node(self, tenant_id: str, entity_id: str) -> EntityNode | None: ...

    @abstractmethod
    async def query_entity_nodes(
        self,
        tenant_id: str,
        name: str | None = None,
        entity_type: str | None = None,
        active_only: bool = True,
    ) -> list[EntityNode]: ...

    @abstractmethod
    async def save_identity_edge(self, tenant_id: str, edge: IdentityEdge) -> None: ...

    @abstractmethod
    async def query_identity_edges(
        self, tenant_id: str, entity_id: str
    ) -> list[IdentityEdge]: ...

    # Pending Actions (Phase 2B Dispatch Interceptor)
    @abstractmethod
    async def save_pending_action(self, action: PendingAction) -> None: ...

    @abstractmethod
    async def get_pending_actions(
        self, tenant_id: str, status: str = "pending"
    ) -> list[PendingAction]: ...

    @abstractmethod
    async def update_pending_action(
        self, tenant_id: str, action_id: str, updates: dict
    ) -> None: ...

    # Context Spaces (Phase 2A routing builds on top of this)
    @abstractmethod
    async def save_context_space(self, space: ContextSpace) -> None: ...

    @abstractmethod
    async def get_context_space(
        self, tenant_id: str, space_id: str
    ) -> ContextSpace | None: ...

    @abstractmethod
    async def list_context_spaces(self, tenant_id: str) -> list[ContextSpace]: ...

    @abstractmethod
    async def update_context_space(
        self, tenant_id: str, space_id: str, updates: dict
    ) -> None: ...

    # Topic hints (Gate 1 space creation)
    @abstractmethod
    async def increment_topic_hint(self, tenant_id: str, hint: str) -> None:
        """Increment the message count for an unnamed topic cluster."""
        ...

    @abstractmethod
    async def get_topic_hint_count(self, tenant_id: str, hint: str) -> int:
        """Get current message count for a topic hint."""
        ...

    @abstractmethod
    async def clear_topic_hint(self, tenant_id: str, hint: str) -> None:
        """Clear a topic hint after space creation or expiration."""
        ...

    # Knowledge Foresight Query (Phase 3C: Proactive Awareness)
    @abstractmethod
    async def query_knowledge_by_foresight(
        self,
        tenant_id: str,
        expires_before: str,
        expires_after: str = "",
        space_id: str = "",
    ) -> list[KnowledgeEntry]:
        """Return knowledge entries with active foresight signals expiring in the given window.

        An entry is included if:
        - foresight_signal is non-empty
        - foresight_expires is non-empty
        - foresight_expires falls within (expires_after, expires_before]
        - The entry is active
        """
        ...

    # Whispers and Suppressions (Phase 3C: Proactive Awareness)
    @abstractmethod
    async def save_whisper(self, tenant_id: str, whisper: "Whisper") -> None:
        """Save a pending whisper to the queue."""
        ...

    @abstractmethod
    async def get_pending_whispers(self, tenant_id: str) -> "list[Whisper]":
        """Get all unsurfaced whispers for a tenant."""
        ...

    @abstractmethod
    async def mark_whisper_surfaced(self, tenant_id: str, whisper_id: str) -> None:
        """Mark a whisper as surfaced (set surfaced_at)."""
        ...

    @abstractmethod
    async def delete_whisper(self, tenant_id: str, whisper_id: str) -> None:
        """Delete a whisper from the pending queue. Used for queue bounding."""
        ...

    @abstractmethod
    async def save_suppression(self, tenant_id: str, entry: "SuppressionEntry") -> None:
        """Save a suppression entry."""
        ...

    @abstractmethod
    async def get_suppressions(
        self,
        tenant_id: str,
        knowledge_entry_id: str = "",
        whisper_id: str = "",
        foresight_signal: str = "",
    ) -> "list[SuppressionEntry]":
        """Get suppression entries, optionally filtered."""
        ...

    @abstractmethod
    async def delete_suppression(self, tenant_id: str, whisper_id: str) -> None:
        """Delete a suppression entry. Used when knowledge updates clear a suppression."""
        ...

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

    # Preferences (Phase 6A)
    @abstractmethod
    async def add_preference(self, pref: Preference) -> None:
        """Persist a new preference."""
        ...

    @abstractmethod
    async def save_preference(self, pref: Preference) -> None:
        """Upsert a preference by ID."""
        ...

    @abstractmethod
    async def get_preference(self, tenant_id: str, pref_id: str) -> Preference | None:
        """Get a single preference by ID."""
        ...

    @abstractmethod
    async def query_preferences(
        self,
        tenant_id: str,
        status: str = "",
        subject: str = "",
        category: str = "",
        scope: str = "",
        active_only: bool = True,
    ) -> list[Preference]:
        """Query preferences with optional filters. active_only filters status='active'."""
        ...

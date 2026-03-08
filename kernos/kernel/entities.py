"""Entity models for Phase 2A Entity Resolution.

Planted now — not populated until Phase 2A. The schema is stable;
resolution logic and embedding pipelines come later.
"""
from dataclasses import dataclass, field


@dataclass
class EntityNode:
    """A distinct entity in the user's world — person, place, organization."""

    id: str                          # "ent_{uuid8}"
    tenant_id: str
    canonical_name: str              # Best/most complete name known
    aliases: list[str] = field(default_factory=list)   # All observed surface forms
    entity_type: str = ""            # "person" | "organization" | "place" | "event" | "other"
    summary: str = ""                # LLM-generated entity summary (updated periodically)
    relationship_type: str = ""      # "client", "friend", "supplier", "contractor", etc. Free-form.
    first_seen: str = ""
    last_seen: str = ""
    conversation_ids: list[str] = field(default_factory=list)
    knowledge_entry_ids: list[str] = field(default_factory=list)  # Back-links
    embedding: list[float] = field(default_factory=list)  # Vector representation
    is_canonical: bool = True        # True if this is the cluster representative
    active: bool = True
    context_space: str = ""          # Primary space this entity belongs to (empty = global)

    # Contact information (person and organization types only)
    contact_phone: str = ""
    contact_email: str = ""
    contact_address: str = ""        # Free text
    contact_website: str = ""


@dataclass
class IdentityEdge:
    """Soft identity link between two EntityNodes."""

    source_id: str                   # EntityNode ID
    target_id: str                   # EntityNode ID
    edge_type: str                   # "SAME_AS" | "MAYBE_SAME_AS" | "NOT_SAME_AS"
    confidence: float = 0.0          # 0.0-1.0
    evidence_signals: list[str] = field(default_factory=list)
    created_at: str = ""
    superseded_at: str = ""          # For non-destructive updates


@dataclass
class CausalEdge:
    """Lightweight causal link between KnowledgeEntries. Planted now, populated Phase 3."""

    source_id: str                   # KnowledgeEntry that is the cause
    target_id: str                   # KnowledgeEntry that is the effect
    relationship: str = ""           # "caused_by" | "enables" | "depends_on" | "co_temporal"
    confidence: float = 0.0
    created_at: str = ""
    superseded_at: str = ""

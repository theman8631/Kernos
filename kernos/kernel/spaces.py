"""Context Space model for Phase 2 multi-context routing.

Planted now — no routing logic, no posture injection, no scoped retrieval.
Phase 2A builds the routing and retrieval layers on top of this schema.
"""
from dataclasses import dataclass, field


@dataclass
class ContextSpace:
    """An isolated context window within a single user conversation.

    The kernel routes each inbound message to the correct space based on
    content. The user never explicitly switches — they just talk.
    """

    id: str                          # "space_{uuid8}"
    tenant_id: str
    name: str                        # "TTRPG — Aethoria Campaign"
    description: str = ""            # One-line, used in handoff annotations
    space_type: str = "daily"        # "daily" | "project" | "domain" | "managed_resource"
    status: str = "active"           # "active" | "dormant" | "archived"

    # Routing signals (Phase 2A will populate and use these)
    routing_keywords: list[str] = field(default_factory=list)
    routing_entity_ids: list[str] = field(default_factory=list)
    routing_aliases: list[str] = field(default_factory=list)

    posture: str = ""                # Plain English working style override (injected at context switch)
    model_preference: str = ""       # Reserved for Phase 3 quality/cost tiers

    created_at: str = ""
    last_active_at: str = ""
    suggestion_suppressed_until: str = ""  # Don't suggest creating a space until after this timestamp

    is_default: bool = False         # True only for the daily space

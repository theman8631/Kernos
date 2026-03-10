"""Context Space model for Phase 2 multi-context routing."""
from dataclasses import dataclass


@dataclass
class ContextSpace:
    """An isolated context window within a single user conversation.

    The kernel routes each inbound message to the correct space based on
    content. The user never explicitly switches — they just talk.
    """

    id: str                          # "space_{uuid8}"
    tenant_id: str
    name: str
    description: str = ""            # Router uses this for routing decisions
    space_type: str = "daily"        # "daily" | "project" | "domain" | "managed_resource"
    status: str = "active"           # "active" | "dormant" | "archived"
    posture: str = ""                # Plain English working style override
    model_preference: str = ""       # Reserved for Phase 3 quality/cost tiers
    created_at: str = ""
    last_active_at: str = ""
    is_default: bool = False         # True only for the daily space

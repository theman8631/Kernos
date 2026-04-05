"""Context Space model for Phase 2 multi-context routing."""
from dataclasses import dataclass, field


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
    space_type: str = "general"      # "general" | "domain" | "subdomain" | "system"
    status: str = "active"           # "active" | "dormant" | "archived"
    posture: str = ""                # Plain English working style override
    model_preference: str = ""       # Reserved for Phase 3 quality/cost tiers
    created_at: str = ""
    last_active_at: str = ""
    is_default: bool = False         # True only for the default (General) space
    max_file_size_bytes: int | None = None  # None = unlimited (enforcement reserved for 3B+)
    max_space_bytes: int | None = None      # None = unlimited (enforcement reserved for 3B+)
    active_tools: list[str] = field(default_factory=list)
    # Capability names explicitly activated for this space.
    # Empty list = system defaults (kernel tools + universal MCP tools).
    # System space (space_type == "system") ignores this — always sees everything.

    # --- Hierarchy (CS-2) ---
    parent_id: str = ""              # Parent space ID (empty = root level)
    aliases: list[str] = field(default_factory=list)  # Previous names for routing
    depth: int = 0                   # 0 = root (General), 1 = domain, 2 = subdomain

"""Soul — the agent's identity for a specific user.

Set at hatch (first conversation). Refined through interactions.
Persists in the State Store at {data_dir}/{tenant_id}/state/soul.json.
"""
from dataclasses import dataclass


@dataclass
class Soul:
    """The agent's identity for a specific user.

    Set at hatch (first meeting). Refined slowly through explicit user signals.
    Consistent across all context spaces (Phase 2+). The soul governs HOW the
    agent communicates — values, personality, relationship knowledge. It does NOT
    govern WHAT the agent does (that's behavioral contracts).
    """

    tenant_id: str

    # Identity
    agent_name: str = ""          # May be empty initially; can emerge from conversation
    emoji: str = ""               # Self-chosen identity marker, emerges from conversation
    personality_notes: str = ""   # Free-text personality profile, updated over time

    # User relationship
    user_name: str = ""           # What to call the user
    # DEPRECATED: user_context is no longer written or read at runtime.
    # User knowledge is now queried from KnowledgeEntries at prompt-build time.
    # Field retained for JSON serialization compat with existing soul.json files.
    user_context: str = ""
    communication_style: str = "" # "direct", "warm", "formal", etc. — inferred or stated

    # Lifecycle
    hatched: bool = False              # True after first bootstrap conversation
    hatched_at: str = ""               # ISO timestamp of hatch completion
    interaction_count: int = 0         # Total interactions since hatch
    bootstrap_graduated: bool = False  # True after bootstrap consolidation completes
    bootstrap_graduated_at: str = ""   # ISO timestamp of graduation

    # Workspace scoping (reserved for Phase 2)
    # When implemented: soul belongs to a workspace, not a tenant.
    # Multiple tenants (e.g., household members, plumber + clients) share
    # the same soul/personality but have individual behavioral contracts.
    # workspace_id: str = ""

    # Reserved for Phase 2
    # context_space_postures: dict[str, str] = field(default_factory=dict)

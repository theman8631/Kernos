"""Soul — the agent's shared identity for a Kernos instance.

The Soul defines WHO Kernos is — shared across all members.
Per-member relationship state (name, timezone, communication style,
bootstrap status) lives in member_profiles in instance.db.

Persists in the State Store at {data_dir}/{instance_id}/state/soul.json.
"""
from dataclasses import dataclass


@dataclass
class Soul:
    """The agent's identity for this Kernos instance.

    Set at hatch (first meeting). Refined slowly through explicit user signals.
    Consistent across all context spaces and all members. The soul governs WHO
    the agent is — personality, values, identity. It does NOT govern per-member
    relationship state (that's in member_profiles) or behavior rules (that's
    behavioral contracts / covenants).
    """

    instance_id: str

    # Identity — shared across all members
    agent_name: str = "Kernos"    # Default name; may evolve through conversation
    emoji: str = "🜁"              # Default identity marker; may evolve through conversation
    personality_notes: str = ""   # Free-text personality profile, updated over time

    # Instance lifecycle
    hatched: bool = False              # True after first bootstrap conversation
    hatched_at: str = ""               # ISO timestamp of hatch completion

    # --- DEPRECATED: Per-user fields migrated to member_profiles in instance.db ---
    # Retained for JSON deserialization compat with existing soul.json files.
    # These fields are NO LONGER read or written at runtime.
    user_name: str = ""
    user_context: str = ""
    communication_style: str = ""
    timezone: str = ""
    interaction_count: int = 0
    bootstrap_graduated: bool = False
    bootstrap_graduated_at: str = ""

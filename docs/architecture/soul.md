# The Soul

The soul is the agent's persistent identity for a given user. It governs HOW the agent communicates — personality, values, relationship knowledge — not WHAT it does (that's behavioral covenants).

## Two-Layer Identity Model

Identity comes from two sources:

1. **Template** (`kernos/kernel/template.py`) — universal operating principles, default personality, bootstrap prompt, expected capabilities. The template is the same for all tenants. It defines the floor — what every Kernos instance starts with.

2. **Soul** (`kernos/kernel/soul.py`) — per-instance identity that evolves through interaction. The soul is what makes each instance unique.

## Soul Fields

- **agent_name** — starts as "Kernos", can be changed by the user (via `update_soul`)
- **emoji** — identity marker (default: "🜁"), discovered naturally during onboarding
- **personality_notes** — free-text personality description, consolidated from bootstrap observations
- **communication_style** — stated or inferred from interaction patterns
- **user_name** — the user's name, extracted from conversation
- **hatched** — whether the soul has been initialized through first interaction
- **interaction_count** — total messages processed
- **bootstrap_graduated** — whether the agent has moved past the onboarding phase

## Soul Lifecycle

1. **Unhatched** — soul exists but hasn't had a first interaction. Template defaults apply.
2. **Hatched** — first message processed. Soul begins accumulating identity.
3. **Bootstrap phase** — the first ~10 interactions. The agent discovers who the user is, finds identity markers (name, emoji), and demonstrates competence. The bootstrap prompt guides this phase.
4. **Graduated** — after sufficient interactions, a one-time consolidation call converts bootstrap observations into permanent personality notes. The bootstrap prompt is removed from the system prompt.

## Soul Introspection Tools

- **read_soul** — read-effect (no gate). Returns all soul fields as JSON.
- **update_soul** — soft_write (dispatch gate applies). Only `agent_name`, `emoji`, `personality_notes`, and `communication_style` are updatable. Lifecycle and user fields are read-only.

## Key Design Points

- Soul is per-instance, consistent across all spaces. The agent is the same person whether in the daily space or a project space.
- Soul is NOT memory. Soul is identity (who am I, how do I communicate). Memory is knowledge (what do I know about the world).
- The user can change the agent's name and personality through conversation — the soul evolves.

## Code Locations

| Component | Path |
|-----------|------|
| Soul dataclass | `kernos/kernel/soul.py` |
| AgentTemplate | `kernos/kernel/template.py` |
| Soul persistence | `kernos/kernel/state_json.py` |
| Soul tools | `kernos/kernel/reasoning.py` (READ_SOUL_TOOL, UPDATE_SOUL_TOOL) |
| Bootstrap graduation | `kernos/messages/handler.py` (_consolidate_bootstrap) |

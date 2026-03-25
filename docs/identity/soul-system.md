# Soul System

The soul is your persistent identity. It stores who you are for each user and evolves through interaction.

## What the Soul Contains

- **agent_name** — your name (starts as "Kernos", can change)
- **emoji** — your identity marker (default: "🜁")
- **personality_notes** — free-text description of your personality
- **communication_style** — how you communicate with this user
- **user_name** — the user's name
- **hatched** / **hatched_at** — whether you've had a first interaction
- **interaction_count** — total messages processed
- **bootstrap_graduated** / **bootstrap_graduated_at** — whether you've completed onboarding

## How Identity Evolves

Your identity is two layers:

1. **Template** — universal values that never change (operating principles, default personality). Every Kernos instance shares these.
2. **Soul** — per-user identity that evolves. This is what makes you unique for each person.

During the bootstrap phase (first ~10 interactions), you discover the user, find your identity markers, and demonstrate competence. After graduation, bootstrap observations are consolidated into permanent personality notes.

## Introspection Tools

- **read_soul** — see your current identity (read, no gate)
- **update_soul** — change your name, emoji, personality notes, or communication style (soft_write, gate applies). Lifecycle and user fields are read-only.

## Soul vs. Memory

Soul is identity — who you are, how you communicate. Memory is knowledge — what you know about the world. They are separate systems. Your soul is consistent across all context spaces. Your memory spans all spaces too, but individual facts may be space-scoped.

## Code Locations

| Component | Path |
|-----------|------|
| Soul dataclass | `kernos/kernel/soul.py` |
| AgentTemplate | `kernos/kernel/template.py` |
| Soul persistence | `kernos/kernel/state_json.py` |

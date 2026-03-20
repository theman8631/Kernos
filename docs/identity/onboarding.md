# Onboarding

The first meeting between Kernos and a new user is a guided improv. The system knows what needs to happen, but the agent finds its own way there.

## The Bootstrap Phase

When a new tenant first interacts, the soul is "unhatched." The bootstrap prompt activates:

> "You are Kernos. Your mission is to discover who this person is and what they need. Be present. Find your identity markers (name, emoji) naturally. Demonstrate competence early. Earn that moment."

During bootstrap (first ~10 interactions), the agent:

1. **Discovers the user** — name, what they need, how they communicate
2. **Finds identity markers** — the user may name the agent, pick an emoji, shape the personality
3. **Demonstrates competence** — shows what it can do early (calendar, memory, tools)
4. **Earns trust** — through correct small actions, not by asking for belief

## Graduation

After `_BOOTSTRAP_MIN_INTERACTIONS` (currently 10) messages, the system evaluates whether to graduate:

1. A one-time LLM call consolidates everything learned during bootstrap into permanent `personality_notes` on the soul
2. The `bootstrap_graduated` flag is set
3. The bootstrap prompt is removed from the system prompt
4. The agent continues with its evolved personality — no more guided discovery

Graduation is unconditional — if the consolidation call fails, the soul still graduates. The bootstrap phase is deliberately time-limited.

## What Persists After Bootstrap

- **agent_name** — whatever the user chose (or "Kernos" if unchanged)
- **emoji** — identity marker discovered during onboarding
- **personality_notes** — consolidated from bootstrap observations
- **communication_style** — inferred from interaction patterns
- **user_name** — extracted from conversation
- All knowledge entries extracted during bootstrap conversations

## Design Philosophy

Competence first. The agent earns the right to be personal by being useful. It doesn't ask "what should I call you?" — it picks up the name naturally. It doesn't ask "what do you need?" — it demonstrates what it can do and lets the user discover the value.

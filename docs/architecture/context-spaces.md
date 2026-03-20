# Context Spaces

Every conversation is routed to a domain-specific context space. The agent operates in a per-space thread — not a flat history. This keeps work discussions separate from personal conversations, projects separate from daily tasks.

## What a Space Is

A context space (`ContextSpace` in `kernos/kernel/spaces.py`) has:

- **id** — unique identifier (e.g., `space_abc12345`)
- **name** and **description** — human-readable, updated by session exit maintenance
- **space_type** — `daily` (default catch-all), `project`, `domain`, `managed_resource`, or `system`
- **status** — `active`, `dormant`, or `archived`
- **posture** — optional working style override (e.g., "be more formal in this space")
- **active_tools** — list of capabilities activated for this space (empty = system defaults only)
- **is_default** — true for the daily space (one per tenant)

## Instance vs. Space

The **instance** is the full ongoing relationship with the user — one soul, one memory pool, one set of connected capabilities. Context spaces are subdivisions within that instance. Soul and memory are shared across all spaces. Files, covenants, and active tools are per-space. A single instance can have many spaces — they all belong to the same agent.

## How Routing Works

Every incoming message is routed by a lightweight LLM call (Haiku). The router sees the message content and the list of active spaces, then assigns the message to the best-matching space. If no existing space fits, the message goes to the daily space.

## Organic Space Creation

New spaces are created organically through a two-gate process:

- **Gate 1** (topic accumulation): After every message in the daily space, the system extracts a topic hint. After 15 messages with topic hints, Gate 2 fires.
- **Gate 2** (model decision): A Haiku call reviews accumulated topics and decides whether a new space is warranted. If yes, the space is created with a name, description, and recommended tools.

The LRU cap is 40 active spaces. When reached, the least-recently-active space is archived.

## Space Thread Assembly

When processing a message, the handler assembles the space's conversation context:

1. **Compaction index** — summary of archived compaction documents
2. **Cross-domain injections** — recent signals from other spaces (last 5 turns from each)
3. **Active compaction document** — the current Living State + Ledger
4. **Recent messages** — the most recent conversation turns in this space

This assembled context goes into the system prompt, giving the agent full awareness of the space's history.

## System Space

Every tenant has a `system` space, auto-created alongside the daily space. The system space stores configuration (MCP server configs) and has a special posture for credential handling. It is not routed to by the LLM router during normal conversation.

## Tool Scoping

Each space has an `active_tools` list. When the agent processes a message, only tools from activated capabilities (plus universal capabilities like calendar) are available. The `request_tool` meta-tool lets the agent activate a capability for the current space at runtime.

## Code Locations

| Component | Path |
|-----------|------|
| ContextSpace dataclass | `kernos/kernel/spaces.py` |
| LLM Router | `kernos/kernel/router.py` |
| Space CRUD | `kernos/kernel/state_json.py` |
| Routing, Gates, Assembly | `kernos/messages/handler.py` |

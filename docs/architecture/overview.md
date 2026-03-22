# Architecture Overview

Kernos is a three-layer system: adapters, handler, and kernel.

## Adapters

Adapters convert platform-specific messages into a `NormalizedMessage` — a common format with sender ID, content, platform name, conversation ID, and auth level. Currently shipped:

- **Kernos server** (`kernos/server.py`) — main entry point. Runs Discord adapter, SMS polling, awareness evaluator, channel registry, and full handler stack.
- **SMS adapter** (`kernos/messages/adapters/twilio_adapter.py`) — receives Twilio webhooks, normalizes SMS messages.

Adapters know nothing about the handler or kernel. The handler knows nothing about adapters. They share only the `NormalizedMessage` model. This isolation is an architectural constraint — never violated.

## Message Handler

The handler (`kernos/messages/handler.py`) is the orchestration layer. It receives a `NormalizedMessage` and runs a 12-step process:

1. **Secure input intercept** — if the system is waiting for a credential, the next message is captured as a secret (never enters LLM context).
2. **Provision & soul init** — create tenant profile if new, load soul, load MCP config from system space.
3. **LLM routing** — a lightweight Haiku call assigns the message to the right context space.
4. **Space switch detection** — emit events if the active space changed.
5. **Gate 1 topic tracking** — accumulate topic hints for organic space creation (15-message threshold).
6. **Load active space** — update `last_active_at`, load space metadata.
7. **File uploads** — store any attached text files in the active space.
8. **Assemble space context** — build compaction index + cross-domain injections + compaction document + recent messages.
9. **Emit `message.received`** event.
10. **Build system prompt** — 8-layer construction (principles, identity, posture, user knowledge, platform, auth, covenants, capabilities, bootstrap).
11. **Reasoning** — call the reasoning service with the full tool-use loop.
12. **Post-response** — soul updates, memory extraction (projectors), compaction tracking, conversation summary.

## Kernel

The kernel (`kernos/kernel/`) owns all state and intelligence infrastructure:

- **Events** — append-only audit trail of everything that happens.
- **State Store** — runtime query surface for souls, knowledge, covenants, spaces, entities, pending actions.
- **Reasoning Service** — LLM abstraction with tool-use loop, dispatch gate, and kernel tool handling.
- **Memory** — two-stage knowledge extraction (Tier 1 pattern match, Tier 2 LLM), entity resolution, fact deduplication.
- **Compaction** — structured history preservation replacing naive truncation.
- **Files** — per-space persistent file storage with shadow archive.
- **Capabilities** — MCP tool integration with per-space scoping and runtime install/uninstall.
- **Awareness** — background evaluator surfacing time-sensitive signals as whispers.
- **Covenant Management** — behavioral rule validation and lifecycle management.

## Key Design Principles

- **Event emission is best-effort.** Every `emit()` is wrapped in try/except. Event logging failures never break the user's message flow.
- **State Store is the query surface.** Runtime lookups go to the State Store, not the Event Stream. The Event Stream is for append, replay, and audit.
- **tenant_id from day one.** Every piece of state is keyed to a `tenant_id`. No code assumes a single user.
- **No destructive deletions.** Every "delete" relocates to a shadow archive. No operation permanently destroys data.
- **Graceful errors.** Every failure mode produces a friendly user-facing response.
- **Prompt caching.** The Anthropic provider applies `cache_control: ephemeral` to the system prompt and tool definitions. After the first turn, these are served from cache at 1/10th token cost, dramatically reducing rate limit pressure.
- **Developer mode error surfacing.** When `developer_mode` is on, WARNING/ERROR logs from `kernos.*` are collected and injected into the system prompt so the agent can see and discuss them.
- **State mutation logging.** Every state write (soul, knowledge, covenants, capabilities) logs with `source` and `trigger` at INFO level. Prefixes: `SOUL_WRITE:`, `KNOW_WRITE:`, `COVENANT_WRITE:`, `CAP_WRITE:`.

## Instance Identity

All adapters resolve to the same instance via `KERNOS_INSTANCE_ID` env var. When set, every adapter (Discord, SMS, CLI) uses it as the `tenant_id` — same soul, same knowledge, same spaces regardless of which channel the message arrives on. Without it, each adapter derives its own tenant_id (backward compatible but creates separate instances per channel).

## Code Locations

| Component | Path |
|-----------|------|
| Message Handler | `kernos/messages/handler.py` |
| Kernos Server | `kernos/server.py` |
| SMS Adapter | `kernos/messages/adapters/twilio_adapter.py` |
| Reasoning Service | `kernos/kernel/reasoning.py` |
| Event Stream | `kernos/kernel/events.py`, `kernos/kernel/event_types.py` |
| State Store | `kernos/kernel/state.py`, `kernos/kernel/state_json.py` |
| Spaces | `kernos/kernel/spaces.py` |
| Soul | `kernos/kernel/soul.py` |
| Template | `kernos/kernel/template.py` |
| Capabilities | `kernos/capability/registry.py`, `kernos/capability/client.py`, `kernos/capability/known.py` |

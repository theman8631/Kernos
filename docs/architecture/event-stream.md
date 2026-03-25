# Event Stream

The event stream is Kernos's append-only, immutable audit trail. Every significant action — messages, reasoning calls, tool executions, knowledge extraction, space changes, gate decisions — is recorded as an event.

## Events Are Immutable

Once written, events are never modified or deleted. The event stream is for append, replay, and audit. It is NOT the runtime query surface — that's the State Store.

## Event Structure

Each event (`kernos/kernel/events.py`) has:

- **id** — time-sortable identifier (`evt_{timestamp_us}_{random}`)
- **type** — hierarchical string (e.g., `message.received`, `reasoning.response`, `tool.called`)
- **tenant_id** — isolation key
- **timestamp** — ISO 8601
- **source** — which component emitted it
- **payload** — type-specific data
- **metadata** — cross-cutting context (currently empty `{}` on handler-emitted events)

## Storage

Events are partitioned by tenant and date into daily JSON files:

```
data/{tenant_id}/events/{date}.json
```

Uses `filelock` for single-process safety. Not safe for multi-worker (the abstract interface allows swapping backends later).

## Event Types

60+ event types covering:

- **Message lifecycle** — `message.received`, `message.sent`
- **Reasoning** — `reasoning.request`, `reasoning.response`
- **Tool calls** — `tool.called`, `tool.result`, `tool.installed`, `tool.uninstalled`
- **Knowledge** — `knowledge.extracted`, `knowledge.updated`, `entity.resolved`
- **Spaces** — `space.created`, `space.switched`, `space.archived`
- **Compaction** — `compaction.triggered`, `compaction.completed`, `compaction.archived`
- **Covenants** — `covenant.created`, `covenant.updated`, `covenant.superseded`
- **Gate decisions** — `gate.evaluated`, `gate.blocked`, `gate.confirmed`
- **Awareness** — `awareness.whisper_created`, `awareness.whisper_surfaced`
- **System** — `system.started`, `capability.connected`, `capability.error`

## Conventions

- `conversation_id` and `platform` belong in **payload only** — never in metadata.
- `metadata` is empty `{}` on handler-emitted events (no cross-cutting context yet).
- Startup events: `system.started` emitted by app lifespan + server on_ready; `capability.connected`/`capability.error` emitted by `MCPClientManager.connect_all()` under tenant `"system"`.

## Cost Tracking

Every reasoning call logs model, tokens, estimated cost, and duration via events. Cost estimation uses `MODEL_PRICING` in `kernos/kernel/events.py`.

## Code Locations

| Component | Path |
|-----------|------|
| Event, EventStream, JsonEventStream | `kernos/kernel/events.py` |
| EventType enum | `kernos/kernel/event_types.py` |

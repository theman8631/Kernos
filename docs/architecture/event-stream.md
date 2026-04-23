# Event Stream

> A single append-only timeline of every meaningful event in Kernos. One table, one query surface, one source of truth for "what happened here and when."

## The problem

Kernos runs multiple subsystems that each produce interesting transitions — the dispatch gate approves or blocks a tool call, the relational dispatcher hands an envelope between members, compaction fires at a turn boundary, a plan step finishes, a friction observer flags a pattern. Historically each subsystem recorded its transitions in its own way: conversation logs hold agent turns, plan JSON holds step history, the gate emits log lines, the friction observer keeps a post-turn report.

That works for each subsystem in isolation. It breaks the moment anyone — a diagnostic tool, the awareness loop, a future V2 reflection pass — needs to answer *"what actually happened to this member across all subsystems in the last hour?"* The answer today requires cross-cutting queries against moving targets, and the query is fragile every time a subsystem changes its record format.

The event stream is the unified substrate that makes that question one SQL query.

## Shape of the primitive

One table, one writer, three readers.

```
emit(instance_id, event_type, payload, *, member_id, space_id, correlation_id)
  │
  ▼
in-memory queue (fire-and-forget) ── batched writer ──▶  instance.db:events
                                     (every 2s or 100 events)
  │
  ▼
read surface:
  events_for_member(instance, member, since, until, event_types)
  events_in_window(instance, since, until)
  events_by_correlation(instance, correlation_id)
```

### Schema

One SQLite table on `data/instance.db`:

| column | type | notes |
|---|---|---|
| `event_id` | TEXT PK | UUIDv4 |
| `instance_id` | TEXT NOT NULL | multi-tenancy key |
| `member_id` | TEXT | null for instance-level events |
| `space_id` | TEXT | null for member/instance-level events |
| `timestamp` | TEXT NOT NULL | ISO-8601 UTC |
| `event_type` | TEXT NOT NULL | dotted namespace (see below) |
| `payload` | TEXT NOT NULL | JSON-encoded event-specific data |
| `correlation_id` | TEXT | ties related events across subsystems |

Indices on `(instance_id, timestamp)`, `(instance_id, member_id, timestamp)`, `(instance_id, event_type, timestamp)`, and `correlation_id`. Append-only: no updates, no deletes outside the retention-eviction path (scheduled for a later batch).

### Event-type namespace

Dotted namespaces, one per instrumented subsystem. New event types append — no schema migration required.

| Namespace | Events |
|---|---|
| `rm.*` | `rm.dispatched`, `rm.rejected`, `rm.delivered` |
| `tool.*` | `tool.called`, `tool.returned`, `tool.failed` |
| `gate.*` | `gate.verdict` |
| `compaction.*` | `compaction.triggered`, `compaction.completed` |
| `plan.*` | `plan.step_started`, `plan.step_completed`, `plan.step_failed` |
| `friction.*` | `friction.observed` |

## Write API

```python
async def emit(
    instance_id: str,
    event_type: str,
    payload: dict | None = None,
    *,
    member_id: str | None = None,
    space_id: str | None = None,
    correlation_id: str | None = None,
) -> None: ...
```

`emit` is fire-and-forget. The caller does not wait on disk I/O. An in-memory queue batches events to SQLite on a 2-second cadence or when 100 events accumulate, whichever comes first. A clean shutdown drains the queue before exit; an ungraceful crash may lose up to 2 seconds of in-flight events — a documented tradeoff for write-path performance.

## Read API

Three typed read functions on the same module:

```python
async def events_for_member(
    instance_id, member_id,
    *, since=None, until=None, event_types=None, limit=1000,
) -> list[Event]

async def events_in_window(
    instance_id, since, until,
    *, limit=1000,
) -> list[Event]

async def events_by_correlation(
    instance_id, correlation_id,
) -> list[Event]
```

All reads return events in ascending timestamp order, scoped to a single `instance_id`. No path returns events across instances.

## Correlation IDs

Every event that originates during a turn carries the turn's ID as its `correlation_id`. That makes `events_by_correlation(instance_id, turn_id)` return the complete event trace for any turn — every tool call, every gate verdict, every compaction fire, every relational dispatch. Events that don't originate in a turn (an ambient plan step firing at 3am, a background compaction) get a correlation ID from their own originating context.

## Two examples

### Household

A household member's kid asks the agent to set a 5pm reminder for their homework check. The turn produces a correlated trace:

- `tool.called` — `manage_schedule` tool invoked
- `gate.verdict` — approved, reactive soft-write
- `tool.returned` — success
- The kid closes the app. At 5pm, the scheduler fires: `plan.step_started` → `rm.dispatched` (notifying the kid's agent) → `rm.delivered` (next turn picks it up).

A parent later asks their own agent *"did the kid's reminder fire today?"* The agent queries `events_for_member(instance, kid_id, event_types=["plan.step_started", "rm.delivered"])` and sees exactly what happened. No cross-subsystem detective work.

### Business team

A three-person consultancy ran a long morning where one partner drafted a client response through an Aider workspace build, another had a long calendar-coordination exchange, and the third sent a cross-member parcel with design files.

The diagnostic view for the 9-11am window is one call: `events_in_window(instance, 9am, 11am)`. The timeline shows every `tool.called` / `tool.returned` for the build, every `rm.dispatched` / `rm.delivered` between the partners, the `gate.verdict` blocks where the gate paused for confirmation on sending external emails, and the `friction.observed` signal when the third partner hit a recurring frustration the observer picked up.

One query, one ascending timeline, no subsystem archaeology.

## What this architecture makes easy

- **Coherent diagnostic reports.** *"Show me everything that happened in this instance yesterday"* is a single `events_in_window` call.
- **Per-member timelines.** *"What did this member do today?"* is a single `events_for_member` call.
- **Turn replay.** *"What was the full execution trace of turn X?"* is `events_by_correlation(instance, turn_id)`.
- **V2 substrate.** The Cognition Kernel's reflection and projection passes consume the event stream directly. Without this substrate, each V2 consumer would have to glue six subsystem-specific reads together.
- **Additive instrumentation.** Adding a new emission is one line. New event types append to the namespace without schema migration.

## What this architecture explicitly does not try to do

- **It does not replace subsystem state.** Conversation logs, plan JSON, compaction documents, friction reports — all keep their existing stores. The event stream adds a unified read surface; it does not consolidate the write paths.
- **It does not guarantee zero-loss on crash.** The fire-and-forget write model trades durability-within-2-seconds for latency-free emissions. Operators who need stricter durability can call `flush_now()` before risky operations (the code exposes it; the architecture page documents it).
- **It does not federate across instances.** Every query is scoped to one `instance_id`. Cross-instance analytics is not on this batch's roadmap; it's a V2-era consideration.
- **It does not implement retention eviction.** Default retention is 90 days; eviction itself is scoped to a later maintenance-tick batch. Until then, the table grows. For single-operator Kernos at realistic event volumes, that's fine for months — a 90-day window on a busy household generates roughly 100-500K rows, well within SQLite's comfortable range.

## Relationship to V1 subsystems

Six emitters in V1, one emission site each:

- `kernos/kernel/relational_dispatch.py` — `rm.dispatched`, `rm.rejected`, `rm.delivered`
- `kernos/kernel/reasoning.py` (tool loop) — `tool.called`, `tool.returned`, `tool.failed`, `gate.verdict`
- `kernos/kernel/compaction.py` — `compaction.triggered`, `compaction.completed`
- `kernos/messages/handler.py` (plan execution) — `plan.step_started`, `plan.step_completed`, `plan.step_failed`
- `kernos/kernel/friction.py` — `friction.observed`

Each emission is one-line additive. The subsystems' own stores are unchanged.

## Relationship to V2

The Cognition Kernel (see [V2 direction](../v2/direction.md)) expects exactly this substrate. Reflection passes query `events_in_window` or `events_for_member` to see what happened; projection passes consume the timeline as forward-model input; the situation model holds a structured view derived from event aggregates. Without a unified event stream, each V2 capability would be a six-store query against moving targets — the retrofit failure mode V1's alignment substrate was built to avoid.

## Code entry points

- `kernos/kernel/event_stream.py:emit` — the fire-and-forget write API
- `kernos/kernel/event_stream.py:events_for_member` — per-member timeline
- `kernos/kernel/event_stream.py:events_in_window` — all-events-in-range
- `kernos/kernel/event_stream.py:events_by_correlation` — turn-level trace
- `kernos/kernel/event_stream.py:start_writer` / `stop_writer` — lifecycle, wired into `kernos/server.py`
- `data/instance.db` — the SQLite file; `events` table auto-created on first writer start

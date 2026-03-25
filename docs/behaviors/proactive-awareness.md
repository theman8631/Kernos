# Proactive Awareness

Kernos is proactive — it surfaces time-sensitive signals without the user asking. The system notices upcoming deadlines and appointments from the knowledge store and delivers them through the appropriate channel based on urgency.

## How It Works

The `AwarenessEvaluator` (`kernos/kernel/awareness.py`) runs continuously in the background on a periodic timer (default: every 30 minutes for full pass, every 5 minutes for interrupt check).

### Time Pass

The evaluator's main operation queries knowledge entries with `foresight_expires` in the next 48 hours. Each matching entry is packaged as a **Whisper** with one of three delivery classes:

| Class | Threshold | Delivery | Example |
|-------|-----------|----------|---------|
| **interrupt** | < 2 hours | Pushed immediately via `send_outbound()` | "Dentist appointment in 30 minutes" |
| **stage** | 2-12 hours | Injected at session start, high priority | "You have a meeting in 4 hours" |
| **ambient** | 12-48 hours | Injected at session start, low priority | "Henderson hasn't replied in a while" |

### Interrupt Delivery

Interrupt whispers are pushed via outbound messaging immediately — the user receives them without needing to message first.

**Active user suppression:** If the user messaged within the last 5 minutes, the interrupt is downgraded to stage (session-start injection handles it, avoiding redundancy).

**Conversation context:** Pushed whispers are stored in conversation history tagged `[WHISPER]` so the agent sees them in context. When the user responds ("thanks for the heads up"), the agent understands what they're referring to.

**Fast-path check:** Every ~5 minutes, pending whispers are re-evaluated. If a stage whisper has crossed the 2-hour threshold since it was created, it's promoted to interrupt and pushed.

### Session-Start Injection

Ambient and stage whispers are injected into the system prompt as `[AWARENESS]` blocks at session start. The agent naturally incorporates them into its response.

### Suppression

A suppression registry prevents nagging:

- Once a whisper is surfaced (via injection or push), the same `knowledge_entry_id` won't generate new whispers until the underlying knowledge changes
- The `dismiss_whisper` tool lets users suppress specific insights
- Suppressions are cleaned up after 7 days

### Queue Management

Maximum 10 pending whispers at a time. Stage whispers take priority over ambient. Within the same class, newest first.

## dismiss_whisper Tool

| Field | Value |
|-------|-------|
| Effect | read (no gate) |
| Input | `whisper_id` — the whisper to dismiss |
| | `reason` — `user_dismissed` or `already_handled` |

## What Creates Foresight Signals

Foresight signals come from Tier 2 knowledge extraction. When the extractor detects a time-anchored fact — "meeting with Alex on Thursday", "project deadline next Friday" — it sets `foresight_signal` and `foresight_expires` on the `KnowledgeEntry`. The awareness evaluator queries these.

## Logging

| Log line | When |
|----------|------|
| `WHISPER_PUSH: id={id} class=interrupt channel={channel}` | Interrupt whisper pushed via outbound |
| `WHISPER_PUSH_FAILED: id={id}` | Outbound delivery failed, keeping as pending |
| `WHISPER_HISTORY: stored [WHISPER] for whisper={id} in space={space}` | Context injection into conversation history |
| `WHISPER_SUPPRESS_ACTIVE: id={id}` | Interrupt suppressed because user is active |
| `INTERRUPT_CHECK: tenant={id} promoted={n}` | Fast-path check promoted N whispers to interrupt |

## Code Locations

| Component | Path |
|-----------|------|
| AwarenessEvaluator, Whisper, SuppressionEntry | `kernos/kernel/awareness.py` |
| DISMISS_WHISPER_TOOL | `kernos/kernel/awareness.py` |
| Whisper injection (session-start) | `kernos/messages/handler.py` (_get_pending_awareness) |
| Evaluator lifecycle | `kernos/messages/handler.py` (_maybe_start_evaluator, per-tenant lazy start) |

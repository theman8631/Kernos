# Proactive Awareness

Kernos is proactive — it surfaces time-sensitive signals at conversation start without the user asking. The system notices upcoming deadlines and appointments from the knowledge store and mentions them at the next natural moment.

## How It Works

The `AwarenessEvaluator` (`kernos/kernel/awareness.py`) runs continuously in the background on a periodic timer (default: every 30 minutes).

### Time Pass

The evaluator's main operation queries knowledge entries with `foresight_expires` in the next 48 hours. Each matching entry is packaged as a **Whisper**:

- **stage** delivery class — less than 12 hours remaining (higher priority)
- **ambient** delivery class — 12-48 hours remaining (lower priority)

### Whisper Injection

At session start, pending whispers are injected into the system prompt as `[AWARENESS]` blocks. The agent naturally incorporates them into its response — it doesn't announce "I have a notification for you", it weaves the information into conversation.

### Suppression

A suppression registry prevents nagging:

- Once a whisper is surfaced, the same `knowledge_entry_id` won't generate new whispers until the underlying knowledge changes
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

## Planned

- Whisper delivery spectrum upgrade: ambient (background), stage (natural moment), interrupt (urgent push)
- Calendar-triggered awareness (direct calendar event monitoring)

## Code Locations

| Component | Path |
|-----------|------|
| AwarenessEvaluator, Whisper, SuppressionEntry | `kernos/kernel/awareness.py` |
| DISMISS_WHISPER_TOOL | `kernos/kernel/awareness.py` |
| Whisper injection | `kernos/messages/handler.py` (_get_pending_awareness) |
| Evaluator lifecycle | `kernos/messages/handler.py` (_maybe_start_evaluator, per-tenant lazy start) |

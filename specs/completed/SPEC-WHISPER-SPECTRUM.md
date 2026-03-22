# SPEC: Whisper Delivery Spectrum — Interrupt Push for Urgent Awareness

**Status:** DRAFT — Kit reviewed intent paragraphs. Kabe approval needed.  
**Author:** Architect  
**Date:** 2026-03-22  
**Depends on:** 3C (awareness evaluator), 3E-A (outbound messaging), 3E-B (scheduler)  
**Type:** Wiring spec — connects existing systems. No new data model, no new processing loop, no new tool.

---

## Objective

Kernos notices time-sensitive things but currently waits for the user to message before telling them. This spec adds interrupt-class delivery: push urgent awareness to the user immediately via outbound messaging.

**What changes for the user:** "Your dentist appointment is in 30 minutes" arrives as a text or Discord message even if you haven't talked to Kernos today. Less urgent awareness still waits for a natural conversation moment.

**What this is NOT:**
- Not a new system — this wires existing systems together
- Not action-suggestion routing ("should I draft a follow-up?") — that's the "action triggers as live turns" golden pin
- Not the scheduler — scheduler fires EXPLICIT user-requested actions. This fires INFERRED system-detected awareness.
- Not a new tool — whispers are automatic, not user-managed

---

## The Three Delivery Classes

| Class | How Delivered | When | Example |
|-------|-------------|------|---------|
| **Ambient** | Session-start injection, low priority | foresight_expires > 12 hours away | "Henderson hasn't replied in a while" |
| **Stage** | Session-start injection, high priority | foresight_expires 2-12 hours away | "You have a meeting in 4 hours" |
| **Interrupt** | Pushed via send_outbound() immediately | foresight_expires < 2 hours away | "Dentist appointment in 30 minutes" |

Ambient and stage already exist and work via session-start injection. This spec adds interrupt only.

Default threshold: 2 hours. Future: configurable per knowledge-entry type (birthday = 24h, flight = 4h). Fixed for V1.

---

## Compatibility: Why This Doesn't Overlap

**vs. Scheduler:** Different source, same delivery pipe. Scheduler fires user-requested explicit actions ("remind me at 3pm"). Interrupt whispers fire system-inferred awareness ("I noticed your appointment is in 30 minutes"). No auto-generated triggers in the trigger store — the evaluator calls send_outbound() directly. Clear ownership separation.

**vs. Session-start injection:** Ambient and stage whispers continue to use session-start injection unchanged. Interrupt is an additional delivery path, not a replacement. If the user is already active (messaged in last 5 minutes), interrupt is suppressed and stage handles it — no redundancy.

**vs. Dispatch Gate:** Whispers aren't tool calls. No gate evaluation needed.

**vs. Covenant Gate (golden pin):** Future post-processing would apply to whisper content too. Not built yet. No conflict.

**vs. Relational Gate (multi-member):** V2+ concern. For V1 single-member, all whispers go to the owner.

---

## Implementation

### Component 1: Add "interrupt" to Whisper Dataclass

In `kernos/kernel/awareness.py`:

```python
@dataclass
class Whisper:
    # ... existing fields ...
    delivery_class: str  # "ambient", "stage", or "interrupt"
```

Already has ambient and stage. Add interrupt as a valid value.

### Component 2: Update Classification in run_time_pass

In `run_time_pass()`, update the threshold logic:

```python
# BEFORE:
delivery_class = "stage" if hours_remaining < 12 else "ambient"

# AFTER:
if hours_remaining < 2:
    delivery_class = "interrupt"
elif hours_remaining < 12:
    delivery_class = "stage"
else:
    delivery_class = "ambient"
```

### Component 3: Interrupt Delivery in Evaluator Tick Loop

In the evaluator's main loop, after Phase 1 (awareness pass) produces whispers:

```python
# After run_time_pass produces whispers:
for whisper in new_whispers:
    if whisper.delivery_class == "interrupt":
        # Check: is user currently active? If so, let stage handle it.
        last_msg = await handler.get_last_message_time(tenant_id)
        if last_msg and (now - last_msg).total_seconds() < 300:  # 5 minutes
            # User is active — downgrade to stage, session-start will catch it
            whisper.delivery_class = "stage"
            continue
        
        # Push immediately via outbound
        success = await handler.send_outbound(
            tenant_id=tenant_id,
            member_id="",  # V1: owner
            channel_name=None,  # default channel
            message=whisper.insight_text,
        )
        
        if success:
            # Store in conversation history for context
            await _store_whisper_message(
                handler, tenant_id, whisper
            )
            # Mark as surfaced so it doesn't re-fire
            await state.mark_whisper_surfaced(tenant_id, whisper.whisper_id)
            logger.info(
                "WHISPER_PUSH: id=%s class=interrupt channel=%s",
                whisper.whisper_id, channel_name or "default"
            )
        else:
            # Outbound failed — keep as pending, will retry next tick
            # or deliver via session-start injection
            logger.warning(
                "WHISPER_PUSH_FAILED: id=%s, keeping as pending",
                whisper.whisper_id
            )
    # Stage and ambient whispers: store for session-start injection (existing behavior, no change)
```

### Component 4: Context Injection for Pushed Whispers

Same pattern as scheduler's trigger context injection. When an interrupt whisper is pushed via send_outbound(), store it in the conversation history of the relevant space:

```python
async def _store_whisper_message(handler, tenant_id, whisper):
    """Store pushed whisper in conversation history so agent sees it."""
    space_id = whisper.space_id or ""  # Default to general if no space
    conversation_id = # resolve from space_id, same as scheduler
    
    await handler.conversations.add_message(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        role="assistant",
        content=f"[WHISPER] {whisper.insight_text}",
        space_tags=[space_id] if space_id else [],
    )
    logger.info(
        "WHISPER_HISTORY: stored [WHISPER] message for whisper=%s in space=%s",
        whisper.whisper_id, space_id or "general"
    )
```

Tagged `[WHISPER]` to distinguish from `[SCHEDULED]` trigger messages. The agent sees it in conversation history and knows the context when the user responds.

### Component 5: Fast-Path Interrupt Check (5-minute tick)

The full awareness pass runs every 1800s (30 min). That's too slow for interrupt detection — a 30-minute-before appointment might not be caught until 15 minutes before.

Add a fast-path check that runs on a shorter interval (every 5 minutes). This does NOT produce new whispers — it only checks existing pending whispers for promotion to interrupt:

```python
# In the evaluator tick loop:
# Full awareness pass: every 1800s (existing)
# Fast interrupt check: every 300s (new)

interrupt_check_interval = 300  # 5 minutes
interrupt_check_counter = 0

# On each 15-second tick:
interrupt_check_counter += 1

if interrupt_check_counter >= (interrupt_check_interval // trigger_interval):
    interrupt_check_counter = 0
    # Fast-path: check pending whispers for interrupt promotion
    pending = await state.get_pending_whispers(tenant_id)
    for whisper in pending:
        if whisper.delivery_class != "interrupt":
            # Re-evaluate: has this whisper crossed the interrupt threshold?
            hours_remaining = _hours_until(whisper.foresight_expires)
            if hours_remaining is not None and hours_remaining < 2:
                whisper.delivery_class = "interrupt"
                # Push it (same interrupt delivery logic as Component 3)
```

This is lightweight — just reads existing whispers from state, checks timestamps, promotes if needed. No LLM call. No knowledge entry scan. Runs every ~5 minutes.

### Component 6: notify_via on Whisper

Add `notify_via` field to Whisper dataclass:

```python
@dataclass
class Whisper:
    # ... existing fields ...
    notify_via: str = ""  # Channel preference. Empty = most recently used.
```

Default: empty (use most recently used channel). Future: per-member channel preference from multi-member system.

---

## What NOT to Change

- The existing awareness pass (run_time_pass) — still runs every 1800s, still produces whispers from knowledge entries
- The existing session-start injection — still handles ambient and stage whispers
- The scheduler — separate system, separate ownership, shared delivery pipe
- The suppression registry — still works, still tracks dismissed whispers
- The dispatch gate — whispers aren't tool calls

---

## Logging

| Log line | When |
|----------|------|
| `WHISPER_PUSH: id={id} class=interrupt channel={channel}` | Interrupt whisper pushed via outbound |
| `WHISPER_PUSH_FAILED: id={id}` | Outbound delivery failed, keeping as pending |
| `WHISPER_HISTORY: stored [WHISPER] for whisper={id} in space={space}` | Context injection into conversation history |
| `WHISPER_SUPPRESS_ACTIVE: id={id}` | Interrupt suppressed because user is active (messaged < 5 min ago) |
| `INTERRUPT_CHECK: tenant={id} promoted={n}` | Fast-path check promoted N whispers to interrupt |

---

## Acceptance Criteria

1. Whisper with foresight_expires < 2 hours gets delivery_class="interrupt"
2. Interrupt whispers are pushed via send_outbound() without waiting for user message
3. Pushed whispers are stored in conversation history tagged [WHISPER] in the relevant space
4. Agent sees [WHISPER] messages in its context when user responds
5. If user messaged in last 5 minutes, interrupt is suppressed (downgraded to stage)
6. Fast-path interrupt check runs every 5 minutes, promotes pending whispers if threshold crossed
7. Ambient and stage delivery unchanged (session-start injection)
8. WHISPER_PUSH logged at INFO
9. Outbound failure keeps whisper as pending (retry next tick or session-start)
10. All existing tests pass

---

## Live Test

1. Create a calendar event 90 minutes from now (crosses the 2-hour threshold during the test)
2. Wait for the fast-path check to promote it to interrupt
3. Verify: outbound message arrives (Discord or SMS) without user initiating conversation
4. Verify: [WHISPER] message in conversation history for the relevant space
5. Say "thanks for the heads up" — agent should know what you're referring to
6. Create a calendar event 6 hours from now — should be stage, NOT interrupt
7. Message Kernos — stage whisper should appear in session-start injection
8. Regression: scheduler triggers, normal conversation, calendar operations all still work

---

## Design Decisions

| Decision | Choice | Why | Who |
|----------|--------|-----|-----|
| Interrupt threshold | 2 hours (fixed V1) | Reasonable for calendar events. Configurable later for birthdays/deadlines (24h). | Kit |
| Suppress if user active | Yes, last message < 5 min | Stage injection handles it. Interrupt would be redundant. | Kit |
| Awareness pass frequency | Full pass stays 1800s. Fast-path interrupt check every 300s. | Don't increase cost of full pass. Fast-path is just timestamp checks, no LLM. | Kit |
| Evaluator calls send_outbound() directly | Yes | Same pattern as scheduler. Same logging. Same conversation history injection. | Kit + Architect |
| Context injection tag | [WHISPER] | Distinguishes from [SCHEDULED] trigger messages. Agent knows the source. | Architect |
| Auto-generated triggers | No | Would create redundancy with scheduler. Evaluator calls send_outbound() directly. | Architect |

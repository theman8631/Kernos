# SPEC-3C: Proactive Awareness

**Status:** Ready for Claude Code  
**Depends on:** 3D (dispatch gate) ✅, 3B+ (MCP installation) ✅, 2D (context + memory) ✅  
**Phase:** 3 — Agent Workspace  
**Author:** Architect  
**Reviewed by:** Kit, Kabe  

---

## What changes for the user

Kernos stops being purely reactive. Today it only speaks when spoken to. After 3C, it surfaces things unprompted at the start of a conversation: "Your dentist appointment is in 2 hours." "The proposal deadline is tomorrow." The system notices things the user needs to know and tells them at the next natural moment — when the user opens a conversation.

This is the first time Kernos initiates. Every prior interaction required the user to say something first.

## What changes architecturally

A new background kernel process — the `AwarenessEvaluator` — runs on a periodic timer, checks the knowledge store for signals worth surfacing, and queues structured whisper objects. When a conversation opens, the handler checks for pending whispers and injects them into the agent's context. The agent decides how to phrase them naturally. A suppression registry prevents nagging. A new event type (`PROACTIVE_INSIGHT`) records what was surfaced for audit.

## Design principles

1. **Kernel owns facts, LLM owns judgment.** The evaluator finds signals through datetime lookups. The LLM decides how to phrase them and weave them into conversation. (Kit)
2. **Observation-only.** The evaluator whispers to the agent. It never acts on the world — no tool calls, no calendar modifications, no emails. Every action traces through the agent and the gate. (PIE spec)
3. **Silence is correct behavior.** Most cycles, the evaluator has nothing to say. That's the system working correctly. The threshold for surfacing must be high. (PIE spec)
4. **Ambient, not demanding.** Whispers are woven into conversation naturally at session start. No mid-conversation interruption. No push notifications. No alarm-style alerts. (Kernos principle)

---

## Component 1: Data Model

### Whisper dataclass

New file: `kernos/kernel/awareness.py`

```python
@dataclass
class Whisper:
    """A structured insight the evaluator wants the agent to surface."""
    whisper_id: str              # Unique ID: "wsp_{timestamp}_{rand4}"
    insight_text: str            # Natural language framing for the agent
    delivery_class: str          # "stage" or "ambient" (no "interrupt" in 3C)
    source_space_id: str         # Context space where the signal originated
    target_space_id: str         # Where to deliver (usually source; cross-domain = active space)
    supporting_evidence: list[str]  # Underlying data for follow-up questions
    reasoning_trace: str         # Why this was surfaced (agent draws on when user asks)
    knowledge_entry_id: str      # The KnowledgeEntry that triggered this whisper
    foresight_signal: str        # Raw signal from the knowledge entry (stable — used for suppression matching)
    created_at: str              # ISO 8601 UTC
    surfaced_at: str = ""        # When the agent actually received it (empty = pending)
```

### SuppressionEntry dataclass

```python
@dataclass
class SuppressionEntry:
    """Tracks what has been surfaced to prevent nagging."""
    whisper_id: str
    knowledge_entry_id: str      # What triggered the whisper
    foresight_signal: str        # RAW signal from KnowledgeEntry (not formatted insight_text)
    created_at: str              # When the whisper was first created
    resolution_state: str        # "surfaced" | "dismissed" | "acted_on" | "resolved"
    resolved_by: str = ""        # "user_dismissed" | "already_handled" | "entry_expired"
                                 # Note: "knowledge_updated" DELETES the entry (see Component 6)
    resolved_at: str = ""        # When resolution happened
```

### PROACTIVE_INSIGHT event type

Add to `kernos/kernel/event_types.py`:

```python
# --- Phase 3C: Proactive Awareness ---
PROACTIVE_INSIGHT = "proactive.insight"
# Payload: whisper_id, insight_text, delivery_class, source_space_id,
#          target_space_id, knowledge_entry_id, reasoning_trace
```

-----

## Component 2: StateStore Extensions

### query_knowledge_by_foresight()

Add to `StateStore` interface and `JsonStateStore`:

```python
async def query_knowledge_by_foresight(
    self,
    tenant_id: str,
    expires_before: str,       # ISO 8601 — entries expiring before this time
    expires_after: str = "",   # ISO 8601 — entries expiring after this time (optional floor)
    space_id: str = "",        # Filter to a specific space (empty = all spaces)
) -> list[KnowledgeEntry]:
    """Return knowledge entries with active foresight signals expiring in the given window.
    
    An entry is included if:
    - foresight_signal is non-empty
    - foresight_expires is non-empty
    - foresight_expires falls within (expires_after, expires_before]
    - The entry has not been invalidated
    """
```

Implementation in `JsonStateStore`: iterate knowledge entries, filter by `foresight_signal != ""` and `foresight_expires` in the time window. This is the core query for the time pass.

### Whisper and Suppression persistence

Add to `StateStore`:

```python
async def save_whisper(self, tenant_id: str, whisper: Whisper) -> None:
    """Save a pending whisper to the queue."""

async def get_pending_whispers(self, tenant_id: str) -> list[Whisper]:
    """Get all unsurfaced whispers for a tenant."""

async def mark_whisper_surfaced(self, tenant_id: str, whisper_id: str) -> None:
    """Mark a whisper as surfaced (set surfaced_at)."""

async def save_suppression(self, tenant_id: str, entry: SuppressionEntry) -> None:
    """Save a suppression entry."""

async def get_suppressions(
    self, tenant_id: str, 
    knowledge_entry_id: str = "",
    whisper_id: str = "",
    foresight_signal: str = "",
) -> list[SuppressionEntry]:
    """Get suppression entries, optionally filtered by knowledge_entry_id or whisper_id."""

async def delete_suppression(self, tenant_id: str, whisper_id: str) -> None:
    """Delete a suppression entry. Used when knowledge updates clear a suppression."""

async def delete_whisper(self, tenant_id: str, whisper_id: str) -> None:
    """Delete a whisper from the pending queue. Used for queue bounding."""
```

Storage location: `data/{tenant_id}/awareness/whispers.json` and `data/{tenant_id}/awareness/suppressions.json`. Atomic writes (tempfile + os.replace) per the manifest fix pattern.

-----

## Component 3: AwarenessEvaluator

New file: `kernos/kernel/awareness.py` (same file as the dataclasses)

```python
class AwarenessEvaluator:
    """Background kernel process that checks for signals worth surfacing.
    
    Runs on a periodic timer. Produces Whisper objects.
    Does NOT call LLMs (MVP time pass). Does NOT act on the world.
    """
    
    def __init__(
        self,
        state: StateStore,
        events: EventStream,
        interval_seconds: int = 1800,  # 30 minutes default
    ):
        self._state = state
        self._events = events
        self._interval = interval_seconds
        self._running = False
        self._task: asyncio.Task | None = None
    
    async def start(self, tenant_id: str) -> None:
        """Start the periodic evaluator for a tenant."""
        self._running = True
        self._task = asyncio.create_task(self._run_loop(tenant_id))
    
    async def stop(self) -> None:
        """Stop the evaluator."""
        self._running = False
        if self._task:
            self._task.cancel()
    
    async def _run_loop(self, tenant_id: str) -> None:
        """Main loop — run evaluations on interval."""
        while self._running:
            try:
                await self._evaluate(tenant_id)
            except Exception as e:
                logger.error("AwarenessEvaluator error: %s", e)
            await asyncio.sleep(self._interval)
    
    async def _evaluate(self, tenant_id: str) -> None:
        """Run all evaluation passes for a tenant."""
        whispers = await self.run_time_pass(tenant_id)
        
        for whisper in whispers:
            # Check suppression — don't re-surface
            if await self._is_suppressed(tenant_id, whisper):
                logger.info("AWARENESS: suppressed whisper=%s signal=%r",
                           whisper.whisper_id, whisper.insight_text[:80])
                continue
            
            # Save to pending queue
            await self._state.save_whisper(tenant_id, whisper)
            
            # Emit audit event
            await emit_event(
                self._events,
                EventType.PROACTIVE_INSIGHT,
                tenant_id,
                "awareness_evaluator",
                payload={
                    "whisper_id": whisper.whisper_id,
                    "insight_text": whisper.insight_text,
                    "delivery_class": whisper.delivery_class,
                    "source_space_id": whisper.source_space_id,
                    "knowledge_entry_id": whisper.knowledge_entry_id,
                },
            )
            
            logger.info("AWARENESS: queued whisper=%s class=%s signal=%r",
                       whisper.whisper_id, whisper.delivery_class,
                       whisper.insight_text[:80])
        
        # Enforce queue bound — max 10 pending whispers per tenant
        await self._enforce_queue_bound(tenant_id, max_whispers=10)
```

### run_time_pass()

The MVP evaluator. No LLM calls. Pure datetime comparisons.

```python
async def run_time_pass(self, tenant_id: str) -> list[Whisper]:
    """Check for time-anchored signals worth surfacing.
    
    Queries knowledge entries where foresight_expires falls within
    the next 48 hours. Packages each as a whisper.
    """
    now = datetime.now(timezone.utc)
    window_end = now + timedelta(hours=48)
    
    entries = await self._state.query_knowledge_by_foresight(
        tenant_id,
        expires_before=window_end.isoformat(),
        expires_after=now.isoformat(),
    )
    
    whispers = []
    for entry in entries:
        # Calculate urgency from time remaining
        expires_dt = datetime.fromisoformat(entry.foresight_expires)
        hours_remaining = (expires_dt - now).total_seconds() / 3600
        
        delivery_class = "stage" if hours_remaining < 12 else "ambient"
        
        whisper = Whisper(
            whisper_id=generate_whisper_id(),
            insight_text=self._format_time_insight(entry, hours_remaining),
            delivery_class=delivery_class,
            source_space_id=entry.context_space or "",
            target_space_id=entry.context_space or "",  # Same space for time pass
            supporting_evidence=[
                f"Knowledge entry: {entry.id}",
                f"Foresight signal: {entry.foresight_signal}",
                f"Expires: {entry.foresight_expires}",
                f"Hours remaining: {hours_remaining:.1f}",
            ],
            reasoning_trace=(
                f"Time pass detected: '{entry.foresight_signal}' "
                f"expires in {hours_remaining:.1f} hours. "
                f"Source: knowledge entry {entry.id} in {entry.context_space}."
            ),
            knowledge_entry_id=entry.id,
            foresight_signal=entry.foresight_signal,
            created_at=now.isoformat(),
        )
        whispers.append(whisper)
    
    logger.info("AWARENESS: time_pass entries_checked=%d whispers_produced=%d",
               len(entries), len(whispers))
    
    return whispers

def _format_time_insight(self, entry: KnowledgeEntry, hours: float) -> str:
    """Format a foresight signal into natural insight text for the agent."""
    if hours < 2:
        urgency = "very soon"
    elif hours < 6:
        urgency = "in the next few hours"
    elif hours < 24:
        urgency = "today"
    else:
        urgency = "tomorrow"
    
    return (
        f"Upcoming: {entry.foresight_signal}. "
        f"This is relevant {urgency} (expires in ~{hours:.0f} hours). "
        f"Related knowledge: {entry.content[:200]}"
    )
```

### Queue bounding

```python
async def _enforce_queue_bound(self, tenant_id: str, max_whispers: int = 10) -> None:
    """Trim the whisper queue to max_whispers. 
    
    Priority: stage before ambient. Within same class, newest first.
    Excess whispers are silently dropped (not suppressed — they just
    didn't make the cut).
    """
    pending = await self._state.get_pending_whispers(tenant_id)
    if len(pending) <= max_whispers:
        return
    
    # Sort: stage first, then by created_at descending (newest first)
    pending.sort(key=lambda w: (
        0 if w.delivery_class == "stage" else 1,
        w.created_at,  # newer = later ISO string = sorts after
    ), reverse=True)
    
    # Reverse so highest priority is first
    pending.sort(key=lambda w: (
        0 if w.delivery_class == "stage" else 1,
    ))
    
    # Keep the top max_whispers, delete the rest
    keep = set(w.whisper_id for w in pending[:max_whispers])
    for w in pending:
        if w.whisper_id not in keep:
            await self._state.delete_whisper(tenant_id, w.whisper_id)
            logger.info("AWARENESS: trimmed whisper=%s (queue bound %d)",
                       w.whisper_id, max_whispers)
```

Add `delete_whisper(tenant_id, whisper_id)` to the StateStore interface.

### Suppression check

```python
async def _is_suppressed(self, tenant_id: str, whisper: Whisper) -> bool:
    """Check if this whisper has already been surfaced or dismissed.
    
    Suppression is keyed to knowledge_entry_id, not insight text.
    The insight text changes every cycle (countdown updates). The
    knowledge entry ID is stable — if we already surfaced a whisper
    for this entry and nothing changed, suppress.
    """
    suppressions = await self._state.get_suppressions(
        tenant_id,
        knowledge_entry_id=whisper.knowledge_entry_id,
    )
    
    for s in suppressions:
        # Already surfaced for this knowledge entry — suppress
        # (unless resolved by knowledge update, in which case
        # the entry was deleted in Component 6 and won't be here)
        if s.resolution_state == "surfaced":
            return True
        
        # Explicitly dismissed — suppress until knowledge changes
        if s.resolution_state == "dismissed":
            return True
        
        # User acted on it — suppress
        if s.resolution_state == "acted_on":
            return True
        
        # "resolved" entries with resolved_by="knowledge_updated" are
        # DELETED (see Component 6), so they won't appear here.
        # "resolved" entries with resolved_by="entry_expired" suppress.
        if s.resolution_state == "resolved" and s.resolved_by == "entry_expired":
            return True
    
    return False
```

-----

## Component 4: Session-Start Whisper Injection

In `handler.py`, when assembling context for a conversation, check for pending whispers.

### Where it hooks in

The handler already has a cross-domain injection pattern (line ~191):

```python
# 0. Cross-domain injection — background awareness from other spaces (if any)
```

The whisper injection goes in the same area — as a `[PROACTIVE]` block in the context assembly:

```python
async def _get_pending_awareness(self, tenant_id: str, active_space_id: str) -> str:
    """Get pending whispers formatted for the agent's context."""
    whispers = await self._state.get_pending_whispers(tenant_id)
    
    if not whispers:
        return ""
    
    # Filter to whispers targeting this space or with no space target
    relevant = [
        w for w in whispers
        if w.target_space_id == active_space_id
        or w.target_space_id == ""
        or w.source_space_id == active_space_id
    ]
    
    if not relevant:
        return ""
    
    # Sort: stage before ambient
    relevant.sort(key=lambda w: 0 if w.delivery_class == "stage" else 1)
    
    lines = ["## Proactive awareness (surface naturally — do not dump as a list)"]
    lines.append("")
    lines.append(
        "The following signals were detected since the last conversation. "
        "Weave relevant ones into your response naturally. "
        "If the user asks why you're mentioning something, you can draw "
        "on the reasoning trace."
    )
    lines.append("")
    
    for w in relevant:
        lines.append(f"- [{w.delivery_class.upper()}] (id: {w.whisper_id}) {w.insight_text}")
        lines.append(f"  Reasoning: {w.reasoning_trace}")
        lines.append("")
    
    lines.append(
        "If the user says they already know about something or don't want "
        "to hear about it, use dismiss_whisper(whisper_id) to suppress it."
    )
    
    # Mark as surfaced and create suppression entries
    for w in relevant:
        w.surfaced_at = datetime.now(timezone.utc).isoformat()
        await self._state.mark_whisper_surfaced(tenant_id, w.whisper_id)
        
        suppression = SuppressionEntry(
            whisper_id=w.whisper_id,
            knowledge_entry_id=w.knowledge_entry_id,
            foresight_signal=w.foresight_signal,  # Raw signal from knowledge entry
            created_at=w.created_at,
            resolution_state="surfaced",
        )
        await self._state.save_suppression(tenant_id, suppression)
    
    return "\n".join(lines)
```

### Integration point in context assembly

In the handler's `_assemble_space_context()` or equivalent:

```python
# After cross-domain injections, before the main conversation
awareness_block = await self._get_pending_awareness(tenant_id, space_id)
if awareness_block:
    # Inject as a system-level context block
    # Same pattern as cross-domain injection
    context_parts.append(awareness_block)
```

The agent sees this as part of its context — not as a tool result or special message. It reads the signals and decides how to incorporate them into its response naturally.

-----

## Component 5: Evaluator Lifecycle

### Starting the evaluator

In `discord_bot.py` (and any future adapter), after the handler is initialized:

```python
# Start awareness evaluator for each known tenant
# In 3C, we start it for the single active tenant
evaluator = AwarenessEvaluator(
    state=handler._state,
    events=handler.events,
    interval_seconds=int(os.getenv("KERNOS_AWARENESS_INTERVAL", "1800")),
)
await evaluator.start(tenant_id)

# Store reference for cleanup
handler._evaluator = evaluator
```

On shutdown:

```python
if handler._evaluator:
    await handler._evaluator.stop()
```

### Interval configuration

Environment variable: `KERNOS_AWARENESS_INTERVAL` (seconds). Default: 1800 (30 minutes).

For testing, set to 60 (1 minute) to see results faster.

### Multi-tenant consideration

In single-tenant mode, one evaluator per active tenant. The evaluator is keyed to `tenant_id` and only processes that tenant's knowledge store. When multi-tenant arrives, the evaluator runs per tenant on a staggered schedule (avoid all tenants evaluating simultaneously).

-----

## Component 6: Suppression Resolution

### dismiss_whisper kernel tool

Register `dismiss_whisper` as a kernel-managed tool alongside `remember`, `write_file`, etc.

```python
# Tool definition
{
    "name": "dismiss_whisper",
    "description": "Dismiss a proactive insight so it won't be surfaced again. "
                   "Use when the user explicitly says they don't want to hear "
                   "about this topic or have already handled it.",
    "input_schema": {
        "type": "object",
        "properties": {
            "whisper_id": {
                "type": "string",
                "description": "The whisper ID to dismiss (from the proactive awareness block)"
            },
            "reason": {
                "type": "string",
                "enum": ["user_dismissed", "already_handled"],
                "description": "Why this whisper is being dismissed"
            }
        },
        "required": ["whisper_id"]
    }
}
```

Implementation:

```python
async def _handle_dismiss_whisper(
    self, tenant_id: str, whisper_id: str, reason: str = "user_dismissed"
) -> str:
    """Dismiss a whisper — update suppression to prevent re-surfacing."""
    suppressions = await self._state.get_suppressions(
        tenant_id, whisper_id=whisper_id  # Note: needs lookup by whisper_id too
    )
    if suppressions:
        s = suppressions[0]
        s.resolution_state = "dismissed"
        s.resolved_by = reason
        s.resolved_at = datetime.now(timezone.utc).isoformat()
        await self._state.save_suppression(tenant_id, s)
        return f"Dismissed whisper {whisper_id}. Won't bring this up again."
    return f"Whisper {whisper_id} not found in suppression registry."
```

The whisper ID is included in the `[PROACTIVE]` injection block so the agent can reference it when calling dismiss. The agent calls this when the user says things like "I know about that already," "stop reminding me about this," or "I already handled it."

This is a read-effect tool (modifies suppression state, not external world) — it does NOT go through the dispatch gate.

### When knowledge changes

When Tier 2 extraction updates a knowledge entry that has a suppression:

In `coordinator.py` or `llm_extractor.py`, after updating a knowledge entry:

```python
# Knowledge changed — delete suppressions so the evaluator can
# re-surface with updated content. Don't mark "resolved" — delete.
# Less state to manage, simpler logic. (Kit review fix)
suppressions = await state.get_suppressions(
    tenant_id, knowledge_entry_id=entry.id
)
for s in suppressions:
    if s.resolution_state == "surfaced":
        await state.delete_suppression(tenant_id, s.whisper_id)
        logger.info("AWARENESS: cleared suppression whisper=%s reason=knowledge_updated",
                    s.whisper_id)
```

This means: if the user talked about something that updated the knowledge entry, the suppression is removed. Next evaluator cycle, if the signal is still relevant, a new whisper is generated with updated content.

Add `delete_suppression(tenant_id, whisper_id)` to the StateStore interface.

### When foresight expires

Entries past their `foresight_expires` are automatically excluded from `query_knowledge_by_foresight()` — the time window filter handles this. No explicit cleanup needed for the knowledge entries.

For suppression entries: old suppressions can accumulate. Add a cleanup pass to the evaluator that removes suppression entries older than 7 days:

```python
async def _cleanup_old_suppressions(self, tenant_id: str) -> None:
    """Remove suppression entries older than 7 days."""
    # Run once per evaluator cycle, lightweight
```

-----

## Component 7: Logging and Tracing

All evaluator activity uses the `AWARENESS:` prefix:

```python
logger.info("AWARENESS: time_pass entries_checked=%d whispers_produced=%d", ...)
logger.info("AWARENESS: queued whisper=%s class=%s signal=%r", ...)
logger.info("AWARENESS: suppressed whisper=%s signal=%r", ...)
logger.info("AWARENESS: injected whispers=%d for space=%s", ...)
logger.info("AWARENESS: cleanup suppressions_removed=%d", ...)
```

The `PROACTIVE_INSIGHT` event in the event stream provides the audit trail. The whisper's `reasoning_trace` field provides the provenance chain when the user asks "why are you telling me this?"

-----

## What 3C Does NOT Include

- **Autonomous actions** — 3E. The evaluator never acts.
- **Deferred execution / scheduling** — 3E. No "do X tomorrow."
- **Mid-conversation interrupts** — 3E. Whispers arrive at session start only.
- **Gestation queue with graduated reminders** — 3E. No "remind again Sunday if unresolved."
- **External stream ingestion** — Phase 4. Only reads knowledge store + calendar events.
- **Cross-domain bridge detection** — Phase 4. Whispers target their source space.
- **Location awareness** — Phase 4.
- **Learning loop** — Phase 4. No threshold tuning from user responses.
- **Watch Layer / standing orders** — Phase 4.
- **Consolidation daemon / slow thinking** — Phase 4.
- **Pattern pass (anomalous silence, cadence baselines)** — Stretch 2.
- **Pull interface (`query_awareness`)** — Stretch 1.

## What 3C Plants for the Future

- Whisper format has `delivery_class` field ready for "interrupt" in 3E
- Whisper format has `target_space_id` ready for cross-domain bridges
- Suppression `resolved_by` enables user-dismissal vs signal-resolution distinction for the learning loop
- `query_knowledge_by_foresight()` is the foundation for pattern pass queries
- `PROACTIVE_INSIGHT` events feed the learning loop when it arrives
- The evaluator's `_evaluate()` method is a hook for adding `run_pattern_pass()` alongside `run_time_pass()`
- The session-start injection pattern works for both push whispers and pull results

-----

## Implementation Notes

**The evaluator runs even if no conversations are active.** It queues whispers. They sit until the user opens a conversation. This is by design — the evaluator's job is to find signals, not to deliver them. Delivery happens at session start.

**Whisper queue is bounded.** If more than 10 whispers accumulate (user hasn't talked to Kernos in days), only keep the 10 highest-priority (stage before ambient, newest first). The agent shouldn't dump a wall of alerts.

**The agent is NOT instructed to surface every whisper.** The injection says "weave relevant ones into your response naturally." The agent uses judgment — some whispers may not be worth mentioning given the conversation context. This is "LLM owns judgment."

**`foresight_signal` quality matters.** The signals are only as good as what Tier 2 extraction writes. If extraction writes vague signals ("something about Thursday"), the whispers will be vague. This is an extraction quality concern, not a 3C concern. If signal quality is an issue in practice, the fix is in the extraction prompt, not the evaluator.

**Calendar events as signal source.** If Google Calendar MCP is connected, the evaluator could also query upcoming events. For MVP, this is OPTIONAL — the foresight signals from conversation ("dentist appointment tomorrow") are the primary source. If calendar integration is straightforward (call `list-events` with a time window), include it. If it requires new MCP infrastructure, defer.

-----

## Design Decisions

| Decision | Alternative | Why |
|---|---|---|
| Session-start delivery only | Mid-conversation interrupts | Simpler. Avoids interruption semantics. Interrupt class is 3E. (Kit) |
| Time pass only for MVP | Time + pattern passes | Time pass is datetime comparisons, no LLM. Pattern pass needs cadence baselines + model calls. Ship the simpler thing first. (Kit) |
| Suppression in MVP | Add suppression later | Without it, the evaluator nags. Nagging destroys trust faster than missed insights. Non-negotiable. (Kit) |
| Whispers in agent context, not direct messages | Send whispers as messages to the user | The agent decides how to phrase things. PIE principle: evaluator whispers, agent speaks. |
| 30-minute default interval | Per-message evaluation | Cost control. The evaluator doesn't need to run on every message — foresight signals don't change that fast. |
| Bounded whisper queue (max 10) | Unlimited queue | Agent shouldn't dump a wall of alerts after a long absence. |
| `resolved_by` field on suppression | Single resolution state | Costs nothing now. Matters for learning loop later. (Kit) |
| Evaluation runs independently of conversations | Only evaluate when user is active | Signals exist whether or not the user is talking. Queue them. |

-----

## Acceptance Criteria

### MVP (Push + Suppression)

1. **Evaluator runs on interval.** Start bot, verify `AWARENESS: time_pass` logs appear every N seconds (use short interval for testing). Verified.
2. **Foresight signals detected.** Create a knowledge entry with `foresight_signal="Dentist appointment"` and `foresight_expires` set to 6 hours from now. Evaluator detects it. Whisper queued. Verified.
3. **Whisper injected at session start.** Open a conversation after the evaluator has queued a whisper. Agent's response includes the insight naturally — not as a system dump. Verified.
4. **Suppression prevents nagging.** Same whisper is not re-surfaced on the next conversation. Suppression entry exists with `resolution_state="surfaced"`. Verified.
5. **Dismissal suppresses.** User dismisses a whisper (agent marks it). The knowledge entry's whisper is not re-surfaced. Verified.
6. **Resolution clears suppression.** Update the knowledge entry that triggered a whisper. Suppression clears (`resolved_by="knowledge_updated"`). If the signal is still relevant, it can be re-surfaced. Verified.
7. **Expired signals excluded.** Entries past `foresight_expires` are not picked up by the time pass. Verified.
8. **Whisper queue bounded.** Queue more than 10 whispers. Only 10 highest-priority survive. Verified.
9. **PROACTIVE_INSIGHT events emitted.** Event stream contains whisper audit trail. Verified.
10. **Evaluator shutdown clean.** Stop the bot. Evaluator task cancels without errors. Verified.
11. **Atomic persistence.** Whisper and suppression files written atomically (tempfile + os.replace). Verified.

### Stretch 1 (Pull Interface)

12. **`query_awareness` tool registered.** Agent can call `query_awareness(scope="Thursday")` and receive relevant signals. Verified.
13. **Pull returns foresight signals + calendar events.** Scoped by time range. Verified.

### Stretch 2 (Pattern Pass)

14. **`cadence_baseline` on EntityNode.** Field exists, populated by extraction. Verified.
15. **Pattern pass detects anomalous silence.** Entity with `cadence_baseline=1.0` (days) and `last_seen` > 3 days ago. Whisper produced. Verified.

-----

## Live Test Plan

```
Step 0 — Architecture: AwarenessEvaluator exists, Whisper/SuppressionEntry dataclasses exist
Step 1 — Evaluator starts: bot startup creates evaluator, AWARENESS logs appear
Step 2 — Time pass with no signals: evaluator runs, whispers_produced=0
Step 3 — Create foresight signal: remember() or manual entry with foresight_expires in 6 hours
Step 4 — Time pass detects signal: evaluator runs, whispers_produced=1
Step 5 — Whisper queued: pending whispers file exists, contains one whisper
Step 6 — Session-start injection: send a message, agent's response includes the insight
Step 7 — Suppression works: send another message, insight NOT repeated
Step 8 — Dismissal: instruct agent to dismiss, suppression entry updated
Step 9 — Resolution: update the knowledge entry, suppression clears
Step 10 — Expired signal: set foresight_expires to past, evaluator doesn't pick it up
Step 11 — Queue bound: create 15 signals, only 10 whispers in queue
Step 12 — PROACTIVE_INSIGHT event: check event stream for audit entry
Step 13 — Clean shutdown: stop bot, no errors
```

-----

## TAD Updates

- Add "Proactive Awareness" section to `docs/TECHNICAL-ARCHITECTURE.md`
  - AwarenessEvaluator: background process, time pass, whisper packaging
  - Session-start injection: handler integration point
  - Suppression registry: nagging prevention
  - New event type: PROACTIVE_INSIGHT
  - Tracing: AWARENESS: prefix
- Update handler documentation: session-start whisper injection
- Update event types documentation

-----

## Implementation Judgment Guide

This spec contains pseudocode, dataclass sketches, and method signatures. These describe WHAT to build and the contracts between components. Claude Code has authority to adjust HOW when the real codebase suggests a better path. Specifically:

**Follow the spec exactly for:**
- The Whisper and SuppressionEntry data models (field names, types, semantics)
- The time pass query logic (foresight_expires within 48h window)
- Suppression behavior (don't re-surface same signal unless knowledge changes)
- Session-start injection point (not mid-conversation)
- The evaluator running on a periodic timer (not per-message)
- The PROACTIVE_INSIGHT event type and payload shape
- The bounded whisper queue (max 10)
- All acceptance criteria — these are the verification contract

**Use your judgment for:**
- Exact file organization — `kernel/awareness.py` is suggested but if the codebase has a pattern that fits better (e.g., separate files for evaluator vs data models), follow the pattern
- StateStore method signatures — the spec shows `query_knowledge_by_foresight()` but if the existing `query_knowledge()` already supports the needed filtering with minor extension, extend it instead of adding a new method
- How the evaluator gets a reference to connected MCP tools (for optional calendar integration) — follow whatever pattern the handler uses
- The exact injection format in the system prompt — match the style of existing cross-domain injections rather than inventing a new format
- Error handling, logging format details, and edge cases the spec doesn't cover — be thorough, match existing patterns
- How suppression entries are stored — if the state store already has a pattern for small per-tenant JSON files, follow it. If a different storage approach fits better, use it.
- Whether the evaluator lifecycle lives on the handler or is managed separately — follow the pattern that makes startup/shutdown cleanest

**When the spec and the codebase disagree:**
- If a method signature in the spec doesn't match how the StateStore actually works, adapt the spec's intent to the real interface
- If the handler's context assembly works differently than the spec assumes, inject whispers where it actually makes sense — the goal is "agent sees whispers at conversation start," not "whispers go at line 191 specifically"
- If you discover the foresight_signal data isn't being written by Tier 2 extraction as expected, add a note in the test output — don't block the build

**What to flag but not block on:**
- If foresight signals in the existing data are sparse or poorly formatted, note it — the evaluator will work correctly, it just won't have much to surface yet
- If the calendar MCP integration for listing upcoming events is complex, skip it for MVP — conversation-extracted foresight signals are the primary source
- If multi-tenant evaluator scheduling has edge cases, document them — single-tenant is the priority

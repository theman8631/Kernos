# SPEC-3E-B: Time-Triggered Scheduler + manage_schedule

**Status:** DRAFT — Architect proposal. Kit reviewed intent paragraphs. Kabe approval needed.  
**Author:** Architect  
**Date:** 2026-03-22  
**Depends on:** 3E-A (outbound messaging), 3D (dispatch gate), 3C (awareness evaluator)  
**Origin:** 3E chunking. Chunk B — the first "works while you sleep" moment.

---

## Objective

Kernos can only act when the user is talking to it. This spec gives Kernos the ability to execute actions at a specified future time without the user being present. "Remind me to invoice Henderson on Friday at 9am." The user sets it, walks away, Kernos does it when the time comes. The outbound messaging from 3E-A is the delivery mechanism.

**What changes for the user:** Kernos works while you sleep. Reminders fire on time. Recurring checks run on schedule. Timed follow-ups happen automatically. The user says "remind me Friday" in a Tuesday conversation and gets a text on Friday morning.

**What this is NOT:**
- Not event-triggered actions ("text me when Henderson emails") — that's 3E-D
- Not the full PIE expansion — that's 3E-E
- Not standing orders ("whenever X happens, do Y") — those are event triggers, not time triggers
- Not the whisper delivery spectrum upgrade (ambient/stage/interrupt classification) — that's 3E-C, but the `delivery_class` field exists on triggers from the start

---

## The Trigger Data Model

A trigger is a persistent record stored per-instance.

```python
@dataclass
class Trigger:
    trigger_id: str                    # Unique identifier
    tenant_id: str                     # Which instance (KERNOS_INSTANCE_ID)
    member_id: str                     # Who created it (V1: always owner)
    space_id: str                      # Which context space this belongs to

    # Condition
    condition_type: str                # "time" for 3E-B. Future: "event", "state"
    condition: str                     # ISO datetime for one-shot, cron for recurring
    next_fire_at: datetime             # Precomputed next fire time for efficient checking
    recurrence: str | None             # None for one-shot. Cron expression for recurring.

    # Action
    action_type: str                   # "notify", "tool_call", "message"
    action_description: str            # Human-readable: "Remind Kabe to invoice Henderson"
    action_params: dict                # Parameters: {message: "...", tool: "...", args: {...}}

    # Delivery
    notify_via: str | None             # Channel name. None = most recently used.
    delivery_class: str                # "ambient" | "stage" | "interrupt". Default: "stage"

    # Authorization
    authorization_token: str | None    # Pre-authorization from dispatch gate at creation time
    authorization_covenant_id: str | None  # Covenant rule ID that authorizes this action

    # Lifecycle
    status: str                        # "active", "paused", "completed", "failed"
    created_at: datetime
    last_fired_at: datetime | None
    fire_count: int                    # How many times it's fired (for recurring)
    failure_reason: str | None         # Why it failed, if status=failed
    pending_delivery: str | None       # Result text held for delivery on next user message (outbound failure)

    # Audit
    created_by_tool_call: str | None   # The manage_schedule call that created this
```

**Storage:** JSON file per instance at `data/{tenant_id}/state/triggers.json`. Atomic writes (tempfile + os.replace). Same pattern as covenants and soul.

---

## Authorization: Triggers as Covenants

A deferred action is a one-time covenant with a time trigger. When the user says "send Henderson an email tomorrow morning," the system creates:

1. **A trigger record** — fires at the specified time
2. **A scoped covenant rule** — authorizes that specific action at fire time

At fire time, the dispatch gate sees the covenant and allows the action. The gate doesn't need special scheduler logic — it uses the covenant system that already exists.

**Flow at creation time:**
1. Agent calls `manage_schedule create` with the action details
2. Handler evaluates the action through the dispatch gate NOW
3. If the gate says EXPLICIT (clearly authorized): create trigger + covenant
4. If the gate says CONFLICT or DENIED: ask the user for confirmation. On confirmation: create trigger + covenant with approval token
5. The covenant rule is scoped: `source="trigger", trigger_id="{id}", scope="single_use"` or `scope="recurring"` for recurring triggers

**Flow at fire time:**
1. Scheduler detects `next_fire_at <= now`
2. Reads the trigger's `authorization_covenant_id`
3. If covenant exists and is active: execute the action (gate sees covenant, passes)
4. If covenant was removed (user deleted it): skip, set trigger status to `failed`, notify user
5. If the required MCP server is disconnected: set status to `failed`, notify user: "I tried to [action] at [time] but [tool] wasn't connected. Want me to try again?"

**For simple notifications (action_type="notify"):** No gate evaluation needed. Sending a reminder message to the user is always authorized. No covenant created — just the trigger.

---

## The Unified Tick Loop

**Kit's recommendation (accepted):** One scheduler loop with two phases, not separate tasks. Extends the existing awareness evaluator.

The awareness evaluator currently runs on a 30-minute interval checking knowledge entries. This spec extends it with a second phase:

```
Tick (every 60 seconds for triggers, 1800 seconds for awareness):
  Phase 1: Awareness pass (existing 3C behavior)
    - Check knowledge entries with datetime values
    - Produce whispers for approaching events
    - Inject at next session start
    
  Phase 2: Trigger evaluation (NEW)
    - Load active triggers where next_fire_at <= now
    - For each fired trigger:
      - Execute the action
      - Deliver result via handler.send_outbound()
      - Update trigger state (last_fired_at, fire_count, next_fire_at or completed)
    - Log: TRIGGER_FIRE: id={trigger_id} action={action_type} status={success/failed}
```

**Interval:** The awareness pass runs every 1800 seconds (existing). The trigger evaluation runs every 60 seconds (new, more frequent). Implementation: the tick loop runs every 60 seconds. The awareness pass has its own internal timer and only runs every 30th tick (or however the existing interval logic works). Trigger evaluation runs every tick.

**Why not separate loops:** Shared data access (both read from the same tenant data), race condition prevention, simpler to reason about. One async task, not two.

---

## manage_schedule Kernel Tool

Same UX pattern as manage_tools and manage_channels.

```python
MANAGE_SCHEDULE_TOOL = {
    "name": "manage_schedule",
    "description": (
        "Manage scheduled actions — create reminders, recurring tasks, and timed actions. "
        "Use 'list' to see all scheduled items. Use 'create' to schedule something new. "
        "Use 'pause', 'resume', or 'remove' to manage existing schedules."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "create", "update", "pause", "resume", "remove"],
                "description": "The action to perform."
            },
            "trigger_id": {
                "type": "string",
                "description": "The trigger ID (required for update/pause/resume/remove)."
            },
            "description": {
                "type": "string",
                "description": "What this trigger does, in natural language (for create/update)."
            },
            "when": {
                "type": "string",
                "description": "When to fire: ISO datetime, relative time ('in 3 hours', 'tomorrow 9am', 'every Monday 8am'), or cron expression (for create/update)."
            },
            "action_type": {
                "type": "string",
                "enum": ["notify", "tool_call"],
                "description": "Type of action: 'notify' sends a message, 'tool_call' executes a tool (for create)."
            },
            "message": {
                "type": "string",
                "description": "The message to send (for notify action_type)."
            },
            "tool_name": {
                "type": "string",
                "description": "The tool to call (for tool_call action_type)."
            },
            "tool_args": {
                "type": "object",
                "description": "Arguments for the tool call (for tool_call action_type)."
            },
            "notify_via": {
                "type": "string",
                "description": "Channel to deliver on: 'discord', 'sms', or None for default."
            },
            "delivery_class": {
                "type": "string",
                "enum": ["ambient", "stage", "interrupt"],
                "description": "Urgency: 'ambient' (background), 'stage' (natural moment), 'interrupt' (push now). Default: stage."
            }
        },
        "required": ["action"],
        "additionalProperties": false
    }
}
```

Register in `_KERNEL_TOOLS`. `list` in `_KERNEL_READS`. All others in `_KERNEL_WRITES`.

**Gate classification (dynamic):** `list` = read (bypass). `create` with `action_type="notify"` = read bypass (reminders are always authorized — no confirmation dialog for "remind me to check email"). `create` with `action_type="tool_call"` = soft_write (gated — the tool action may need confirmation). `update/pause/resume/remove` = soft_write. Claude Code: implement this as dynamic classification in the manage_schedule handler, NOT as a static entry in _KERNEL_READS/_KERNEL_WRITES. Check the action and action_type to determine gate classification per call.

**Natural language time parsing:** The `when` field accepts natural language ("tomorrow at 9am", "every Friday", "in 3 hours"). The agent parses this into an ISO datetime or cron expression before calling manage_schedule. The agent is good at this — it knows the current date from the system prompt. No separate NLP time parser needed.

---

## Action Execution at Fire Time

When a trigger fires, the execution path depends on `action_type`:

**notify (always authorized):**
```
1. Build the notification message from action_params.message
2. Call handler.send_outbound(tenant_id, member_id, notify_via, message)
3. Log: TRIGGER_FIRE: id={id} action=notify channel={channel} success=True
4. Update trigger: last_fired_at, fire_count, compute next_fire_at or complete
```

**tool_call (covenant-authorized):**
```
1. Check authorization_covenant_id still exists and is active
2. Check the required MCP server is connected
3. If either fails: set status=failed, notify user with explanation
4. Build the tool call from action_params (tool_name, tool_args)
5. Execute via the reasoning service (or direct MCP call with covenant bypass)
6. Deliver result to user via handler.send_outbound()
7. Log: TRIGGER_FIRE: id={id} action=tool_call tool={name} success=True/False
8. Update trigger state
```

**Failure handling:**
- MCP disconnected: status=failed, notify user: "I tried to [action] at [time] but [tool] wasn't connected. Want me to try again?"
- Covenant removed: status=failed, notify user: "The authorization for [action] was removed. Want me to re-schedule?"
- Tool call error: status=failed, notify user with the error. Trigger can be retried.
- Outbound channel unavailable: try fallback channel. If all channels fail, hold the result in a `pending_delivery` field on the trigger record. On next user-initiated message to this tenant, check for pending deliveries and deliver them inline. This prevents silent loss of fired-but-undelivered triggers. Log: `TRIGGER_DELIVERY_PENDING: id={id} reason=all_channels_failed`

---

## Time Expression Handling

The `when` field on manage_schedule is parsed by the agent (LLM), not by a time parser library. The agent converts natural language to structured time:

- "tomorrow at 9am" → ISO datetime in local timezone (from system prompt)
- "in 3 hours" → ISO datetime = now + 3 hours
- "every Monday at 8am" → recurrence with cron expression + next_fire_at
- "every day at 7am" → recurrence with cron expression
- "March 28 at 2pm" → specific ISO datetime

For recurring triggers, `next_fire_at` is recomputed after each fire using the recurrence expression. The implementation should use a cron library (e.g., `croniter`) for cron-based recurrence.

**Timezone:** Uses the server's local timezone (from the system prompt's date injection). The agent sees "Current date and time: Saturday, March 22, 2026 10:30 AM PDT" and interprets "tomorrow 9am" as 9am PDT.

---

## Implementation Order

1. Create Trigger dataclass and TriggerStore (JSON persistence, atomic writes)
2. Implement manage_schedule kernel tool (list/create/update/pause/resume/remove)
3. Wire manage_schedule into _KERNEL_TOOLS, _KERNEL_READS, _KERNEL_WRITES with dynamic gate classification
4. Implement time parsing helpers (ISO datetime to next_fire_at, cron to next_fire_at)
5. Implement trigger-as-covenant creation (on manage_schedule create, create scoped covenant rule)
6. Extend the awareness evaluator tick loop with Phase 2: trigger evaluation
7. Implement trigger fire execution (notify path + tool_call path)
8. Implement failure handling (MCP disconnected, covenant removed, tool error, channel unavailable)
9. Wire outbound delivery through handler.send_outbound()
10. Add TRIGGER_FIRE logging with source, trigger, success
11. Test: create a reminder 2 minutes from now, verify it fires and delivers
12. Test: create a recurring daily trigger, verify next_fire_at recomputes
13. Test: create a tool_call trigger (calendar event), verify covenant-authorized execution
14. Test: disconnect MCP before fire time, verify graceful failure + notification
15. Test: pause and resume a trigger
16. Test: remove a trigger
17. Update docs/behaviors/ with scheduler documentation
18. Update docs/roadmap/whats-next.md — scheduler is shipped

---

## What NOT to Change

- The existing awareness evaluator Phase 1 behavior (whisper production from knowledge entries)
- The dispatch gate — it evaluates actions normally. The covenant created at trigger creation time is what allows fire-time execution.
- The outbound messaging path (3E-A) — triggers use it, don't modify it
- The covenant system — triggers create scoped covenants through the existing covenant manager
- MCP infrastructure

---

## Acceptance Criteria

1. `manage_schedule list` shows all triggers for the instance with status
2. `manage_schedule create` with a future time creates a trigger that fires at that time
3. Notification triggers deliver via outbound messaging (Discord or SMS based on notify_via)
4. Tool call triggers execute with covenant pre-authorization (gate not re-evaluated at fire time)
5. Recurring triggers recompute next_fire_at and continue firing
6. Failed triggers (MCP disconnected, covenant removed) notify the user with an explanation
7. `manage_schedule pause/resume` toggles trigger status correctly
8. `manage_schedule remove` deletes the trigger (shadow archive for audit)
9. Triggers persist across restarts (JSON file, atomic writes)
10. TRIGGER_FIRE logged at INFO with trigger_id, action_type, success
11. Trigger evaluation runs every 60 seconds inside the existing evaluator task
12. Pending delivery: if outbound fails, result held on trigger and delivered on next user message
13. docs/ updated with scheduler documentation
14. All existing tests pass

---

## Live Test

1. Restart bot
2. "Remind me to check my email in 2 minutes" → trigger created (notify, no gate confirmation)
3. Wait 2 minutes → notification arrives on Discord (or SMS if notify_via set)
4. "Show me my schedule" → manage_schedule list shows the completed trigger
5. "Create a calendar event called 'Test' for tomorrow at noon" via manage_schedule with tool_call → verify gate confirmation at creation time
6. Verify the calendar event is created at fire time (covenant-authorized execution)
7. "Every morning at 8am, tell me what's on my calendar today" → recurring trigger with tool_call
8. Verify next morning: Kernos messages with calendar summary (depends on step 5-6 working)
9. "Pause that morning schedule" → trigger paused, doesn't fire
10. "Resume it" → trigger resumes
11. "Cancel it" → trigger removed
12. Regression: normal conversation, calendar operations, awareness whispers all still work

---

## Post-Implementation Checklist

- [ ] All tests pass (existing + new)
- [ ] docs/ updated (new scheduler doc + roadmap update)
- [ ] TRIGGER_FIRE logging with source and trigger
- [ ] State mutation logging maintained (SOUL_WRITE, KNOW_WRITE, COVENANT_WRITE, CAP_WRITE)
- [ ] Live test with Kernos
- [ ] Spec moved to specs/completed/
- [ ] DECISIONS.md NOW block updated

---

## Design Decisions

| Decision | Choice | Why | Who |
|----------|--------|-----|-----|
| Shared tick loop vs separate tasks | Shared loop, two phases | Prevents race conditions, simpler. Kit recommendation. | Kit + Architect |
| Authorization model | Covenant-based pre-authorization | Reuses existing gate + covenant system. No special scheduler auth. | Architect (from Gate Friction design doc) |
| Time parsing | Agent (LLM) parses natural language | Agent already knows the date, good at NLP. No library needed for parsing. Cron library for recurrence computation. | Architect |
| Trigger persistence | JSON file, atomic writes | Same pattern as covenants, soul, knowledge. Consistent. | Architect |
| Failure behavior | Notify user with explanation | User should know when something didn't fire. Option to retry. | Kit |
| delivery_class field | Exists from V1, default "stage" | 3E-C builds the full spectrum. Field exists now to prevent migration. | Architect (from Event-Driven Whispers gap doc) |
| Trigger evaluation interval | 60 seconds | Frequent enough for practical use. Awareness pass keeps its own 1800s interval. | Architect |
| Gate classification | Dynamic per action_type | notify=read bypass (no friction on reminders), tool_call=soft_write (gated). | Kit |
| Outbound failure | Pending delivery queue | Hold result, deliver on next user message. Prevents silent loss. | Kit |

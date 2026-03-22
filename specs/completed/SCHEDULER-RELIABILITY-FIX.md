# Scheduler Reliability Fix — Three Layers

**Status:** APPROVED — Kabe direct to Claude Code  
**Date:** 2026-03-22  
**Source:** Live test of 3E-B scheduler. Root cause analysis by Architect, reviewed by Kit.  
**Priority order:** Layer 2 → Layer 1 → Layer 3. Do them in this order.

---

## Context

The scheduler (3E-B) shipped and the basic flow works — triggers create,
fire on schedule, and deliver via outbound. But live testing surfaced a
recurring pattern: the agent hallucinates having called manage_schedule
instead of actually calling it. This is the same root cause as the
original hallucination detection for create-event. The structural fix
addresses all complex tools, not just manage_schedule.

---

## Layer 2: Fix the hallucination retry path (CRITICAL — affects all tools)

**The current bug:** When hallucination detection catches the agent 
claiming a tool action without calling a tool (iterations=0, 
tool-claiming language detected), it appends a corrective system 
message and retries. Two problems:

1. The hallucinated assistant message stays in the conversation 
   history. The retry sees it and the conversation is now poisoned 
   with a false claim.
2. If the retry response contains tool_use blocks, they are not 
   fed back into the tool loop. The retry text is extracted but 
   tool calls are dropped.

**The fix (from Kit's review):**

When hallucination is detected:
1. **DROP the hallucinated assistant message from conversation history 
   entirely.** Do not append it. Do not tag it. Remove it.
2. Retry cleanly from the user's last message — the retry LLM call 
   should see the same conversation as if the hallucination never 
   happened, plus a system instruction: "You must use the appropriate 
   tool to perform actions. Do not claim to have done something 
   without calling the tool."
3. **If the retry response contains tool_use blocks, feed them into 
   the tool loop for execution.** The retry path must return the full 
   response (text + tool_use), not just extracted text. The tool loop 
   processes the retry response the same way it processes a normal 
   response.

**Where to change:** `kernos/kernel/reasoning.py` in the hallucination 
detection and retry logic. Look for HALLUCINATION_CHECK / 
HALLUCINATION_RETRY.

**Test:** 
- Send "remind me in 2 minutes" — if the agent hallucinates 
  "Scheduled" without calling the tool, the retry should:
  1. Drop the hallucinated response
  2. Retry and actually call manage_schedule
  3. The tool call executes (TRIGGER_CREATE in logs)
  4. The trigger fires 2 minutes later

---

## Layer 1: Simplify manage_schedule create/update to NL description

**The problem:** manage_schedule create has 8+ parameters. The agent 
takes the path of least resistance — generating a conversational 
sentence is easier than constructing correct JSON with action, 
action_type, description, when, message, delivery_class, notify_via, 
tool_name, tool_args. This incentivizes hallucination.

**The fix:** Simplify create and update to accept a natural language 
description. The handler does structured extraction via a Haiku call.

**New schema for create:**
```json
{
  "action": "create",
  "description": "Remind me to invoice Henderson on Friday at 9am"
}
```

That's it. Two fields. The handler receives the description and calls 
Haiku to extract:

```json
{
  "action_type": "notify",        // or "tool_call"
  "when": "2026-03-28T09:00:00",  // parsed datetime in local time
  "message": "Time to invoice Henderson",
  "delivery_class": "stage",      // default
  "recurrence": null,             // or cron expression
  "notify_via": null,             // default to most recent channel
  "tool_name": null,              // for tool_call type
  "tool_args": null               // for tool_call type
}
```

**Haiku extraction prompt:**
```
You are extracting structured schedule data from a natural language 
description. The current date and time is {current_datetime} ({timezone}).

Extract the following fields from this description:
- action_type: "notify" (send a message) or "tool_call" (execute a tool)
- when: ISO datetime string in local time. Convert relative times 
  ("in 2 hours", "tomorrow 9am", "next Friday") to absolute datetimes.
- message: The notification message text (for notify type)
- recurrence: null for one-shot, or describe the pattern 
  ("every day at 8am", "every Monday", "every hour")
- delivery_class: "ambient" (low priority), "stage" (normal, default), 
  or "interrupt" (urgent, push immediately)
- notify_via: null (use default channel) or "discord" or "sms"
- tool_name: The tool to call (for tool_call type only)
- tool_args: Arguments for the tool (for tool_call type only)

Respond with ONLY a JSON object. No explanation.

Description: {description}
```

**Validation:** If the extraction fails to parse (unparseable datetime, 
missing required fields), return an error to the agent: "I couldn't 
parse that schedule request. Can you be more specific about the time?" 
Do NOT create a broken trigger.

**Recurrence handling:** If the extraction returns a recurrence pattern 
like "every day at 8am", convert it to a cron expression using croniter 
or manual mapping. Store the cron in the trigger's recurrence field.

**The existing list/pause/resume/remove actions stay unchanged.** They 
already have simple schemas (action + trigger_id).

**Update the tool schema:** Remove all the extra fields from 
manage_schedule's input_schema for create/update. Keep only action, 
trigger_id (for update/pause/resume/remove), and description 
(for create/update).

```python
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
            "description": "Trigger ID (required for update/pause/resume/remove)."
        },
        "description": {
            "type": "string",
            "description": "What to schedule, in natural language. Include the time and what should happen. Examples: 'Remind me to invoice Henderson on Friday at 9am', 'Every morning at 8am tell me what is on my calendar today', 'In 2 hours send me a message saying time to stretch'"
        }
    },
    "required": ["action"],
    "additionalProperties": false
}
```

**Dynamic gate classification stays the same:** After Haiku extraction, 
check action_type. If notify → read bypass. If tool_call → soft_write.

---

## Layer 3: Raise complete_simple max_tokens + stale trigger cleanup

**max_tokens:** Find the complete_simple call (the lightweight extraction 
LLM call used for Tier 2, dedup, etc.). If max_tokens is set low enough 
that a manage_schedule tool call gets truncated, raise it. Check the 
current value and ensure it can handle the largest reasonable tool call 
response.

**Stale trigger cleanup:** One-shot triggers that have fired successfully 
(fire_count > 0 and recurrence is null) should have their status set to 
"completed" after firing. Currently they stay as "active" with a past 
next_fire_at, cluttering the list.

In evaluate_triggers(), after a one-shot trigger fires successfully:
```python
if not trigger.recurrence:
    trigger.status = "completed"
    logger.info("TRIGGER_COMPLETE: id=%s (one-shot)", trigger.trigger_id)
```

**Defensive guard:** In get_due(), skip triggers where fire_count > 0 
and recurrence is null — they've already fired but weren't marked 
completed (guards against Bug 1 from earlier testing).

---

## Timezone fix

Change _now_iso() in scheduler.py:

BEFORE:
```python
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
```

AFTER:
```python
def _now_iso() -> str:
    return datetime.now().isoformat()
```

The agent writes local time (from the system prompt). The scheduler 
evaluates in local time. No conversion needed for V1. If UTC storage 
is needed later, the conversion happens at write time, not at 
evaluation time.

Also check: the Haiku extraction in Layer 1 produces local time 
datetimes. The current_datetime passed to the extraction prompt should 
use datetime.now(), not datetime.now(timezone.utc).

---

## Verify (in this order)

1. "Remind me in 2 minutes" — agent calls manage_schedule with 
   description only (not 8 params). TRIGGER_CREATE in logs. 
   No hallucination.
2. Trigger fires at correct time (2 minutes later, not immediately).
   TRIGGER_FIRE in logs.
3. Outbound message delivered.
4. After firing, trigger shows as "completed" in manage_schedule list.
5. "Cancel that reminder" or "remove that schedule" — agent calls 
   manage_schedule remove with trigger_id. Trigger disappears from list.
6. Test hallucination path: if agent says "Scheduled" without tool call, 
   HALLUCINATION_CHECK fires, hallucinated message dropped, retry 
   produces actual tool call, trigger created.
7. "Every morning at 8am tell me what's on my calendar" — recurring 
   trigger created with cron recurrence.
8. All existing tests pass.

---

## Update docs/

- docs/behaviors/covenants.md — no changes needed
- docs/roadmap/whats-next.md — note scheduler fixes shipped
- docs/architecture/overview.md — note NL extraction pattern for 
  complex tools (>3-4 fields use description + Haiku extraction)

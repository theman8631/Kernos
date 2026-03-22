# Time-Triggered Scheduler

Kernos can execute actions at a specified future time without the user being present. "Remind me to invoice Henderson on Friday at 9am" — the user sets it, walks away, Kernos does it when the time comes.

## How It Works

The scheduler stores **triggers** — persistent records that fire actions at specified times. The awareness evaluator's tick loop evaluates triggers every 60 seconds.

## Trigger Types

**Notify** (always authorized): Sends a message to the user via outbound messaging. No gate confirmation needed — reminders are always OK.

**Tool call** (covenant-authorized): Executes a tool (create calendar event, send email, etc.). Pre-authorized at creation time via the dispatch gate.

## manage_schedule Tool

| Action | Effect | What it does |
|--------|--------|-------------|
| list | read | Show all scheduled actions with status |
| create | read (notify) / soft_write (tool_call) | Schedule a new action |
| update | soft_write | Change a trigger's timing or content |
| pause | soft_write | Temporarily stop a trigger from firing |
| resume | soft_write | Re-activate a paused trigger |
| remove | soft_write | Delete a trigger |

## Time Expressions

The agent parses natural language times into ISO datetime or cron expressions:
- "tomorrow at 9am" → ISO datetime
- "in 3 hours" → ISO datetime (now + 3h)
- "every Monday at 8am" → cron expression + next_fire_at
- "March 28 at 2pm" → specific ISO datetime

Recurring triggers use cron expressions (via `croniter`). After each fire, `next_fire_at` is recomputed.

## Failure Handling

- **Outbound channel unavailable:** Result held in `pending_delivery` on the trigger. Delivered inline on next user message.
- **Tool call error:** Trigger marked failed. User notified with error details and option to retry.
- **MCP disconnected:** Trigger marked failed. User notified that the tool wasn't available.

## Lifecycle

| Status | Meaning |
|--------|---------|
| active | Trigger will fire at next_fire_at |
| paused | Trigger exists but won't fire until resumed |
| completed | One-shot trigger that has fired |
| failed | Trigger that encountered an error |

## Storage

`data/{tenant_id}/state/triggers.json` — atomic writes (tempfile + os.replace).

## Code Locations

| Component | Path |
|-----------|------|
| Trigger, TriggerStore | `kernos/kernel/scheduler.py` |
| MANAGE_SCHEDULE_TOOL, handle_manage_schedule | `kernos/kernel/scheduler.py` |
| evaluate_triggers | `kernos/kernel/scheduler.py` |
| Tick loop integration | `kernos/kernel/awareness.py` (Phase 2) |

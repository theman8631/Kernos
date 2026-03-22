"""Time-Triggered Scheduler — manage_schedule tool + trigger evaluation.

Triggers are persistent records that fire actions at specified times.
- Notify: send a message to the user (always authorized)
- Tool call: execute a tool with covenant pre-authorization
"""
import json
import logging
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from kernos.kernel.state import StateStore

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Trigger:
    """A persistent scheduled action."""

    trigger_id: str
    tenant_id: str
    member_id: str = ""
    space_id: str = ""

    # Condition
    condition_type: str = "time"         # "time" for 3E-B
    condition: str = ""                  # ISO datetime or cron expression
    next_fire_at: str = ""               # ISO datetime — precomputed next fire time
    recurrence: str = ""                 # Cron expression for recurring. Empty = one-shot.

    # Action
    action_type: str = "notify"          # "notify" or "tool_call"
    action_description: str = ""         # Human-readable
    action_params: dict = field(default_factory=dict)  # {message, tool_name, tool_args, ...}

    # Delivery
    notify_via: str = ""                 # Channel name. Empty = default.
    delivery_class: str = "stage"        # "ambient" | "stage" | "interrupt"

    # Authorization
    authorization_covenant_id: str = ""  # Covenant rule ID authorizing this action

    # Lifecycle
    status: str = "active"              # "active", "paused", "completed", "failed"
    created_at: str = ""
    last_fired_at: str = ""
    fire_count: int = 0
    failure_reason: str = ""
    pending_delivery: str = ""           # Held result if outbound failed

    # Audit
    created_by_tool_call: str = ""


def _trigger_id() -> str:
    return f"trig_{uuid.uuid4().hex[:8]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


class TriggerStore:
    """JSON-on-disk persistence for triggers."""

    def __init__(self, data_dir: str | Path) -> None:
        from kernos.utils import _safe_name
        self._data_dir = Path(data_dir)
        self._safe_name = _safe_name

    def _path(self, tenant_id: str) -> Path:
        return self._data_dir / self._safe_name(tenant_id) / "state" / "triggers.json"

    def _read(self, tenant_id: str) -> list[dict]:
        path = self._path(tenant_id)
        if not path.exists():
            return []
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write(self, tenant_id: str, data: list[dict]) -> None:
        import tempfile
        path = self._path(tenant_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            import os
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, str(path))
        except Exception:
            import os
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    async def save(self, trigger: Trigger) -> None:
        raw = self._read(trigger.tenant_id)
        for i, d in enumerate(raw):
            if d.get("trigger_id") == trigger.trigger_id:
                raw[i] = asdict(trigger)
                self._write(trigger.tenant_id, raw)
                return
        raw.append(asdict(trigger))
        self._write(trigger.tenant_id, raw)

    async def get(self, tenant_id: str, trigger_id: str) -> Trigger | None:
        for d in self._read(tenant_id):
            if d.get("trigger_id") == trigger_id:
                return Trigger(**d)
        return None

    async def list_active(self, tenant_id: str) -> list[Trigger]:
        return [Trigger(**d) for d in self._read(tenant_id) if d.get("status") == "active"]

    async def list_all(self, tenant_id: str) -> list[Trigger]:
        return [Trigger(**d) for d in self._read(tenant_id)]

    async def get_due(self, tenant_id: str, now_iso: str) -> list[Trigger]:
        """Get active triggers where next_fire_at <= now."""
        results = []
        for d in self._read(tenant_id):
            if d.get("status") != "active":
                continue
            nfa = d.get("next_fire_at", "")
            if nfa and nfa <= now_iso:
                results.append(Trigger(**d))
        return results

    async def remove(self, tenant_id: str, trigger_id: str) -> bool:
        raw = self._read(tenant_id)
        new = [d for d in raw if d.get("trigger_id") != trigger_id]
        if len(new) == len(raw):
            return False
        self._write(tenant_id, new)
        return True


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------


def compute_next_fire(recurrence: str, after_iso: str) -> str:
    """Compute the next fire time from a cron expression after a given time.

    Returns ISO datetime string. Returns "" if computation fails.
    """
    try:
        from croniter import croniter
        after = datetime.fromisoformat(after_iso)
        cron = croniter(recurrence, after)
        return cron.get_next(datetime).isoformat()
    except Exception as exc:
        logger.warning("TRIGGER: cron computation failed for %r: %s", recurrence, exc)
        return ""


# ---------------------------------------------------------------------------
# manage_schedule tool definition
# ---------------------------------------------------------------------------

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
                "description": "The action to perform.",
            },
            "trigger_id": {
                "type": "string",
                "description": "The trigger ID (required for update/pause/resume/remove).",
            },
            "description": {
                "type": "string",
                "description": "What this trigger does, in natural language (for create/update).",
            },
            "when": {
                "type": "string",
                "description": (
                    "When to fire: ISO datetime for one-shot, "
                    "cron expression for recurring (for create/update). "
                    "Parse natural language ('tomorrow 9am', 'every Monday 8am') "
                    "into ISO datetime or cron before calling."
                ),
            },
            "action_type": {
                "type": "string",
                "enum": ["notify", "tool_call"],
                "description": "Type of action: 'notify' sends a message, 'tool_call' executes a tool.",
            },
            "message": {
                "type": "string",
                "description": "The message to send (for notify action_type).",
            },
            "tool_name": {
                "type": "string",
                "description": "The tool to call (for tool_call action_type).",
            },
            "tool_args": {
                "type": "object",
                "description": "Arguments for the tool call.",
                "additionalProperties": True,
            },
            "notify_via": {
                "type": "string",
                "description": "Channel to deliver on: 'discord', 'sms', or empty for default.",
            },
            "delivery_class": {
                "type": "string",
                "enum": ["ambient", "stage", "interrupt"],
                "description": "Urgency level. Default: stage.",
            },
            "recurrence": {
                "type": "string",
                "description": "Cron expression for recurring triggers. Empty for one-shot.",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------


async def handle_manage_schedule(
    trigger_store: TriggerStore,
    tenant_id: str,
    member_id: str,
    space_id: str,
    action: str,
    trigger_id: str = "",
    description: str = "",
    when: str = "",
    action_type: str = "notify",
    message: str = "",
    tool_name: str = "",
    tool_args: dict | None = None,
    notify_via: str = "",
    delivery_class: str = "stage",
    recurrence: str = "",
) -> str:
    """Handle the manage_schedule kernel tool."""

    if action == "list":
        return await _list_triggers(trigger_store, tenant_id)

    if action == "create":
        return await _create_trigger(
            trigger_store, tenant_id, member_id, space_id,
            description, when, action_type, message,
            tool_name, tool_args or {}, notify_via, delivery_class, recurrence,
        )

    if action == "pause":
        return await _set_trigger_status(trigger_store, tenant_id, trigger_id, "paused")

    if action == "resume":
        return await _set_trigger_status(trigger_store, tenant_id, trigger_id, "active")

    if action == "remove":
        if not trigger_id:
            return "Error: trigger_id is required for remove."
        removed = await trigger_store.remove(tenant_id, trigger_id)
        if removed:
            logger.info("TRIGGER_REMOVE: id=%s tenant=%s", trigger_id, tenant_id)
            return f"Removed trigger {trigger_id}."
        return f"Error: Trigger '{trigger_id}' not found."

    if action == "update":
        return await _update_trigger(
            trigger_store, tenant_id, trigger_id,
            description, when, message, recurrence,
        )

    return f"Error: Unknown action '{action}'. Use list, create, update, pause, resume, or remove."


async def _list_triggers(store: TriggerStore, tenant_id: str) -> str:
    triggers = await store.list_all(tenant_id)
    if not triggers:
        return "No scheduled actions."

    lines = ["**Scheduled Actions:**\n"]
    for t in triggers:
        status_icon = {"active": "▶", "paused": "⏸", "completed": "✓", "failed": "✗"}.get(t.status, "?")
        recur = f" (recurring: {t.recurrence})" if t.recurrence else ""
        lines.append(
            f"  {status_icon} [{t.trigger_id}] {t.action_description}\n"
            f"    next: {t.next_fire_at[:19] if t.next_fire_at else 'N/A'} | "
            f"type: {t.action_type} | fires: {t.fire_count}{recur}"
        )
    return "\n".join(lines)


async def _create_trigger(
    store: TriggerStore,
    tenant_id: str, member_id: str, space_id: str,
    description: str, when: str, action_type: str, message: str,
    tool_name: str, tool_args: dict, notify_via: str,
    delivery_class: str, recurrence: str,
) -> str:
    if not when:
        return "Error: 'when' is required — provide an ISO datetime or cron expression."
    if not description:
        return "Error: 'description' is required — what should this trigger do?"

    now = _now_iso()
    tid = _trigger_id()

    # Determine next_fire_at
    if recurrence:
        next_fire = compute_next_fire(recurrence, now)
        if not next_fire:
            return f"Error: Could not parse recurrence '{recurrence}' as a cron expression."
    else:
        next_fire = when  # Agent provides ISO datetime

    params: dict = {}
    if action_type == "notify":
        params["message"] = message or description
    elif action_type == "tool_call":
        if not tool_name:
            return "Error: 'tool_name' is required for tool_call action type."
        params["tool_name"] = tool_name
        params["tool_args"] = tool_args

    trigger = Trigger(
        trigger_id=tid,
        tenant_id=tenant_id,
        member_id=member_id,
        space_id=space_id,
        condition_type="time",
        condition=when,
        next_fire_at=next_fire,
        recurrence=recurrence,
        action_type=action_type,
        action_description=description,
        action_params=params,
        notify_via=notify_via,
        delivery_class=delivery_class or "stage",
        status="active",
        created_at=now,
    )

    await store.save(trigger)
    logger.info(
        "TRIGGER_CREATE: id=%s desc=%r next=%s type=%s recurrence=%r",
        tid, description, next_fire[:19] if next_fire else "?", action_type, recurrence,
    )

    recur_note = f" Recurring: {recurrence}." if recurrence else ""
    return (
        f"Scheduled: {description}\n"
        f"Next fire: {next_fire[:19] if next_fire else when}\n"
        f"Type: {action_type} | ID: {tid}{recur_note}"
    )


async def _set_trigger_status(
    store: TriggerStore, tenant_id: str, trigger_id: str, new_status: str,
) -> str:
    if not trigger_id:
        return f"Error: trigger_id is required for {new_status}."
    trigger = await store.get(tenant_id, trigger_id)
    if not trigger:
        return f"Error: Trigger '{trigger_id}' not found."
    trigger.status = new_status
    await store.save(trigger)
    logger.info("TRIGGER_STATUS: id=%s status=%s", trigger_id, new_status)
    return f"Trigger {trigger_id} is now {new_status}."


async def _update_trigger(
    store: TriggerStore, tenant_id: str, trigger_id: str,
    description: str, when: str, message: str, recurrence: str,
) -> str:
    if not trigger_id:
        return "Error: trigger_id is required for update."
    trigger = await store.get(tenant_id, trigger_id)
    if not trigger:
        return f"Error: Trigger '{trigger_id}' not found."

    if description:
        trigger.action_description = description
    if when:
        trigger.condition = when
        if recurrence:
            trigger.recurrence = recurrence
            trigger.next_fire_at = compute_next_fire(recurrence, _now_iso())
        else:
            trigger.next_fire_at = when
    if message and trigger.action_type == "notify":
        trigger.action_params["message"] = message
    if recurrence and not when:
        trigger.recurrence = recurrence
        trigger.next_fire_at = compute_next_fire(recurrence, _now_iso())

    await store.save(trigger)
    logger.info("TRIGGER_UPDATE: id=%s desc=%r next=%s", trigger_id, trigger.action_description, trigger.next_fire_at[:19] if trigger.next_fire_at else "?")
    return f"Updated trigger {trigger_id}."


# ---------------------------------------------------------------------------
# Trigger evaluation — called from the tick loop
# ---------------------------------------------------------------------------


async def evaluate_triggers(
    trigger_store: TriggerStore,
    tenant_id: str,
    handler,  # MessageHandler — for send_outbound and tool execution
) -> int:
    """Evaluate and fire all due triggers. Returns count of triggers fired."""
    now = _now_iso()
    due = await trigger_store.get_due(tenant_id, now)
    fired = 0

    for trigger in due:
        try:
            success = await _fire_trigger(trigger, handler)
            trigger.last_fired_at = now
            trigger.fire_count += 1

            if success:
                if trigger.recurrence:
                    trigger.next_fire_at = compute_next_fire(trigger.recurrence, now)
                    if not trigger.next_fire_at:
                        trigger.status = "completed"
                else:
                    trigger.status = "completed"
            else:
                if not trigger.recurrence:
                    trigger.status = "failed"

            await trigger_store.save(trigger)
            fired += 1

            logger.info(
                "TRIGGER_FIRE: id=%s action=%s status=%s desc=%r",
                trigger.trigger_id, trigger.action_type,
                "success" if success else "failed",
                trigger.action_description,
            )
        except Exception as exc:
            trigger.status = "failed"
            trigger.failure_reason = str(exc)
            await trigger_store.save(trigger)
            logger.error(
                "TRIGGER_FIRE: id=%s action=%s EXCEPTION: %s",
                trigger.trigger_id, trigger.action_type, exc,
            )

    return fired


async def _fire_trigger(trigger: Trigger, handler) -> bool:
    """Execute a trigger's action. Returns True on success."""
    if trigger.action_type == "notify":
        message = trigger.action_params.get("message", trigger.action_description)
        member_id = trigger.member_id or f"member:{trigger.tenant_id}:owner"
        channel = trigger.notify_via or None
        success = await handler.send_outbound(
            trigger.tenant_id, member_id, channel, message,
        )
        if not success:
            # Hold for delivery on next user message
            trigger.pending_delivery = message
            logger.warning(
                "TRIGGER_DELIVERY_PENDING: id=%s reason=outbound_failed",
                trigger.trigger_id,
            )
        return success

    elif trigger.action_type == "tool_call":
        tool_name = trigger.action_params.get("tool_name", "")
        tool_args = trigger.action_params.get("tool_args", {})

        if not tool_name:
            trigger.failure_reason = "No tool_name in action_params"
            return False

        try:
            from kernos.kernel.reasoning import ReasoningRequest
            request = ReasoningRequest(
                tenant_id=trigger.tenant_id,
                conversation_id=f"trigger_{trigger.trigger_id}",
                system_prompt="",
                messages=[],
                tools=[],
                model="",
                trigger="scheduler",
                active_space_id=trigger.space_id,
            )
            result = await handler.reasoning.execute_tool(tool_name, tool_args, request)

            # Deliver result to user
            member_id = trigger.member_id or f"member:{trigger.tenant_id}:owner"
            delivery_msg = f"Scheduled action completed: {trigger.action_description}\n\nResult: {result}"
            channel = trigger.notify_via or None
            success = await handler.send_outbound(
                trigger.tenant_id, member_id, channel, delivery_msg,
            )
            if not success:
                trigger.pending_delivery = delivery_msg
                logger.warning(
                    "TRIGGER_DELIVERY_PENDING: id=%s reason=outbound_failed",
                    trigger.trigger_id,
                )
            return True  # Tool call succeeded even if delivery pending

        except Exception as exc:
            trigger.failure_reason = str(exc)
            # Notify user of failure
            member_id = trigger.member_id or f"member:{trigger.tenant_id}:owner"
            fail_msg = (
                f"I tried to run '{trigger.action_description}' but it failed: {exc}. "
                "Want me to try again?"
            )
            await handler.send_outbound(
                trigger.tenant_id, member_id, trigger.notify_via or None, fail_msg,
            )
            return False

    trigger.failure_reason = f"Unknown action_type: {trigger.action_type}"
    return False

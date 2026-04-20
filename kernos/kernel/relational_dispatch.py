"""Relational messaging dispatcher (RELATIONAL-MESSAGING v5).

Single orchestration point for agent-to-agent messages:

- Permission check against the simplified relationship model.
- Envelope creation with atomic storage.
- Two delivery paths:
    * time_sensitive → immediate push via the adapter layer.
    * elevated / normal → queue for next-turn surfacing on the recipient's
      active turn (picked up through collect_pending_for_member).
- Space-appropriate surfacing:
    * Hint set + match exists in recipient's spaces (not active) → hard
      defer (`space_hint_mismatch`).
    * Hint set + no match anywhere → treat as null-hint (`space_hint_stale`).
    * Hint null → surfaces per the Obvious Benefit Rule in agent judgment.
    * time_sensitive bypasses the hint-deferral rule entirely.
- Expiration sweep (per-urgency TTLs).
- Trace events with reason codes; never the message content.

Agent thinks, kernel enforces. All dispatch permission / state transitions /
thread integrity checks live here — the agent tool is a thin wrapper that
calls into this dispatcher.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from kernos.kernel.relational_messaging import (
    EXPIRATION_BY_URGENCY, INTENTS, URGENCIES, RelationalMessage,
    dispatch_permitted, generate_conversation_id, generate_message_id,
)
from kernos.utils import utc_now

logger = logging.getLogger(__name__)

DEFER_REASON_SPACE_HINT_MISMATCH = "space_hint_mismatch"
DEFER_REASON_SPACE_HINT_STALE = "space_hint_stale"


class DispatchResult:
    """Lightweight result object for dispatcher.send()."""

    def __init__(
        self, *,
        ok: bool,
        message_id: str = "",
        conversation_id: str = "",
        state: str = "",
        error: str = "",
    ) -> None:
        self.ok = ok
        self.message_id = message_id
        self.conversation_id = conversation_id
        self.state = state
        self.error = error

    def to_dict(self) -> dict:
        return {
            "ok": self.ok, "message_id": self.message_id,
            "conversation_id": self.conversation_id,
            "state": self.state, "error": self.error,
        }


class RelationalDispatcher:
    """Orchestrates relational-message send/receive/resolve.

    Holds references to state + instance_db + an outbound-push hook.
    Construction is done once per handler; called per turn.
    """

    def __init__(
        self, *,
        state,
        instance_db,
        outbound_push=None,   # async (instance_id, member_id, text) -> bool
        trace_emitter=None,   # callable(event_name: str, detail: str) — optional
    ) -> None:
        self.state = state
        self.instance_db = instance_db
        self._push = outbound_push
        self._trace = trace_emitter

    # --- Public: send ---

    async def send(
        self, *,
        instance_id: str,
        origin_member_id: str,
        origin_agent_identity: str,
        addressee: str,
        intent: str,
        content: str,
        urgency: str = "normal",
        target_space_hint: str = "",
        conversation_id: str = "",
        reply_to_id: str = "",
    ) -> DispatchResult:
        """Validate + create + route. Returns DispatchResult with outcome."""
        # Basic enum validation
        if intent not in INTENTS:
            return DispatchResult(ok=False, error=f"invalid intent: {intent!r}")
        if urgency not in URGENCIES:
            return DispatchResult(ok=False, error=f"invalid urgency: {urgency!r}")
        if not content or not content.strip():
            return DispatchResult(ok=False, error="content is required")
        if origin_member_id == addressee:
            return DispatchResult(
                ok=False,
                error="cannot send a relational message to yourself",
            )

        # Resolve addressee (member_id or display_name)
        resolved = await self._resolve_member(addressee)
        if resolved is None:
            return DispatchResult(
                ok=False,
                error=f"addressee not found: {addressee!r}",
            )
        if isinstance(resolved, str) and resolved.startswith("AMBIGUOUS:"):
            _, name, ids = resolved.split(":", 2)
            return DispatchResult(
                ok=False,
                error=(
                    f"ambiguous addressee {name!r} — multiple members match "
                    f"({ids}). Ask the user which one, or use a specific "
                    "member_id."
                ),
            )
        addressee_id = resolved["member_id"]

        # Permission check
        perm = "by-permission"
        try:
            perm = await self.instance_db.get_permission(
                origin_member_id, addressee_id,
            )
        except Exception as exc:
            logger.warning("RM_PERMISSION_LOOKUP_FAILED: %s", exc)
        if not dispatch_permitted(perm, intent):
            self._emit(
                "relational_message.rejected",
                f"origin={origin_member_id} addressee={addressee_id} "
                f"intent={intent} reason=permission_{perm}",
            )
            return DispatchResult(
                ok=False,
                error=(
                    f"permission denied: origin's side toward addressee is "
                    f"{perm!r}; intent {intent!r} not allowed."
                ),
            )

        # Build envelope (pending) and persist.
        # Thread-id resolution: explicit conversation_id wins; otherwise if
        # this is a reply_to another envelope, inherit ITS conversation_id
        # so chains stay on one thread without the agent having to copy ids;
        # otherwise start a new thread.
        conv_id = conversation_id
        if not conv_id and reply_to_id:
            try:
                parent = await self.state.get_relational_message(
                    instance_id, reply_to_id,
                )
                if parent is not None:
                    conv_id = parent.conversation_id
            except Exception as exc:
                logger.debug("RM_REPLY_TO_LOOKUP_FAILED: %s", exc)
        if not conv_id:
            conv_id = generate_conversation_id()
        msg = RelationalMessage(
            id=generate_message_id(),
            instance_id=instance_id,
            origin_member_id=origin_member_id,
            origin_agent_identity=origin_agent_identity or "",
            addressee_member_id=addressee_id,
            intent=intent,
            content=content,
            urgency=urgency,
            conversation_id=conv_id,
            state="pending",
            created_at=utc_now(),
            target_space_hint=target_space_hint or "",
            reply_to_id=reply_to_id or "",
        )
        await self.state.add_relational_message(msg)
        self._emit(
            "relational_message.sent",
            f"id={msg.id} origin={origin_member_id} addressee={addressee_id} "
            f"intent={intent} urgency={urgency} conversation={conv_id}",
        )

        # Route by urgency.
        if urgency == "time_sensitive":
            await self._immediate_push(msg)
        # elevated / normal: stay pending; collect on recipient's next turn.

        return DispatchResult(
            ok=True, message_id=msg.id,
            conversation_id=conv_id, state=msg.state,
        )

    # --- Public: pickup ---

    async def collect_pending_for_member(
        self, *,
        instance_id: str,
        member_id: str,
        active_space_id: str,
        recipient_space_ids: list[str] | None = None,
    ) -> list[RelationalMessage]:
        """Pickup queued messages on the recipient's active turn.

        Applies expiration sweep first, then the space-hint rule:
          - time_sensitive bypasses hint deferral.
          - Hint set + match exists in recipient's spaces (not active) →
            defer (reason: space_hint_mismatch).
          - Hint set + no match in recipient's spaces → fallthrough (reason:
            space_hint_stale).
          - Hint null → fallthrough.

        For each message that passes, atomic pending → delivered. Messages
        already delivered-but-not-surfaced are included too (crash recovery).
        """
        # Expire old pendings first so they don't get picked up.
        await self.sweep_expired(instance_id)

        # Candidates: pending or delivered (for recipient).
        candidates = await self.state.query_relational_messages(
            instance_id,
            addressee_member_id=member_id,
            states=["pending", "delivered"],
            limit=200,
        )

        recipient_space_ids = recipient_space_ids or []
        surfaceable: list[RelationalMessage] = []
        for msg in candidates:
            if msg.urgency != "time_sensitive" and msg.target_space_hint:
                hint = msg.target_space_hint
                if hint in recipient_space_ids:
                    if hint != active_space_id:
                        # Hard defer; the hint names a real space but not the
                        # active one.
                        self._emit(
                            "relational_message.deferred",
                            f"id={msg.id} reason={DEFER_REASON_SPACE_HINT_MISMATCH} "
                            f"hint={hint} active={active_space_id}",
                        )
                        continue
                else:
                    # Stale hint — falls through to null-hint path.
                    self._emit(
                        "relational_message.deferred",
                        f"id={msg.id} reason={DEFER_REASON_SPACE_HINT_STALE} "
                        f"hint={hint}",
                    )
                    # Do not `continue` — falls through intentionally.

            # Transition pending → delivered atomically. Already-delivered
            # messages (surfaced==""/resolved_at=="") are re-collected as-is
            # so the agent has the chance to surface them.
            if msg.state == "pending":
                ok = await self.state.transition_relational_message_state(
                    instance_id, msg.id,
                    from_state="pending", to_state="delivered",
                    updates={"delivered_at": utc_now()},
                )
                if ok:
                    msg.state = "delivered"
                    msg.delivered_at = utc_now()
                    self._emit(
                        "relational_message.delivered",
                        f"id={msg.id} path=next_turn addressee={member_id}",
                    )
                else:
                    # Raced with immediate-push or another collect; reload.
                    reloaded = await self.state.get_relational_message(
                        instance_id, msg.id,
                    )
                    if reloaded is None:
                        continue
                    msg = reloaded
                    if msg.state not in ("delivered",):
                        continue
            surfaceable.append(msg)
        return surfaceable

    # --- Public: transitions the handler invokes ---

    async def mark_surfaced(
        self, instance_id: str, message_id: str,
    ) -> bool:
        """delivered → surfaced (end-of-turn commit point)."""
        ok = await self.state.transition_relational_message_state(
            instance_id, message_id,
            from_state="delivered", to_state="surfaced",
            updates={"surfaced_at": utc_now()},
        )
        if ok:
            self._emit(
                "relational_message.surfaced", f"id={message_id}",
            )
        return ok

    async def mark_resolved(
        self,
        instance_id: str,
        message_id: str,
        *,
        from_state: str,
        reason: str = "",
    ) -> bool:
        """Transition to resolved. from_state must be delivered or surfaced.

        `delivered → resolved` direct: agent handled the message entirely
        agent-side (e.g., covenant auto-handles). No user-visible surface,
        no duplicate-surface risk.

        `surfaced → resolved`: agent finished processing the user-visible
        thread.
        """
        if from_state not in ("delivered", "surfaced"):
            return False
        ok = await self.state.transition_relational_message_state(
            instance_id, message_id,
            from_state=from_state, to_state="resolved",
            updates={
                "resolved_at": utc_now(),
                "resolution_reason": reason or "",
            },
        )
        if ok:
            self._emit(
                "relational_message.resolved",
                f"id={message_id} from={from_state} reason={reason or '-'}",
            )
        return ok

    async def sweep_expired(self, instance_id: str) -> int:
        """Expire anything past its urgency-specific TTL.

        Returns the count of envelopes expired. Non-blocking and idempotent.
        """
        now = datetime.now(timezone.utc)
        count = 0
        # Only pending and delivered can expire; surfaced/resolved are
        # either already user-visible or finalized.
        for st in ("pending", "delivered"):
            candidates = await self.state.query_relational_messages(
                instance_id, states=[st], limit=500,
            )
            for msg in candidates:
                ttl = EXPIRATION_BY_URGENCY.get(
                    msg.urgency, EXPIRATION_BY_URGENCY["normal"],
                )
                try:
                    created = datetime.fromisoformat(
                        msg.created_at.replace("Z", "+00:00"),
                    )
                    if created.tzinfo is None:
                        created = created.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    continue
                if (now - created).total_seconds() <= ttl:
                    continue
                ok = await self.state.transition_relational_message_state(
                    instance_id, msg.id,
                    from_state=st, to_state="expired",
                    updates={"expired_at": utc_now()},
                )
                if ok:
                    count += 1
                    self._emit(
                        "relational_message.expired",
                        f"id={msg.id} urgency={msg.urgency} prior_state={st}",
                    )
        return count

    # --- Internals ---

    async def _resolve_member(self, addressee: str):
        """Resolve to {member_id, display_name, role} by id or display name.

        Returns the member dict on unambiguous match, None on no match,
        or the string "AMBIGUOUS:<name>:<ids>" when the display name
        matches more than one member. Callers treat the ambiguous case
        as a send failure that needs disambiguation from the user.
        """
        if not addressee:
            return None
        addressee = addressee.strip()
        try:
            members = await self.instance_db.list_members()
        except Exception as exc:
            logger.warning("RM_RESOLVE_MEMBERS_FAILED: %s", exc)
            return None
        # Exact id match first (ids are globally unique, so never ambiguous).
        for m in members:
            if m.get("member_id") == addressee:
                return m
        # Display-name match (case-insensitive). Multiple matches → ambiguous.
        lo = addressee.lower()
        name_hits = [
            m for m in members
            if (m.get("display_name") or "").lower() == lo
        ]
        if len(name_hits) == 1:
            return name_hits[0]
        if len(name_hits) > 1:
            ids = ",".join(m.get("member_id", "?") for m in name_hits)
            return f"AMBIGUOUS:{addressee}:{ids}"
        return None

    async def _immediate_push(self, msg: RelationalMessage) -> None:
        """Time-sensitive path: atomic pending→delivered + outbound push.

        If no push hook is wired, we still flip the state (the next-turn
        path will pick it up at `delivered`) — the envelope just won't
        reach the recipient out-of-band. That degrades gracefully.
        """
        ok = await self.state.transition_relational_message_state(
            msg.instance_id, msg.id,
            from_state="pending", to_state="delivered",
            updates={"delivered_at": utc_now()},
        )
        if not ok:
            # Someone else (another push, a next-turn scan) already advanced
            # it. Do nothing.
            return
        msg.state = "delivered"
        msg.delivered_at = utc_now()
        self._emit(
            "relational_message.delivered",
            f"id={msg.id} path=immediate_push addressee={msg.addressee_member_id}",
        )
        if self._push is None:
            return
        try:
            await self._push(msg)
        except Exception as exc:
            # Push is best-effort. The envelope is already delivered; the
            # recipient's next turn will surface it anyway.
            logger.warning("RM_IMMEDIATE_PUSH_FAILED: %s", exc)

    def _emit(self, event_name: str, detail: str) -> None:
        if self._trace is None:
            return
        try:
            self._trace(event_name, detail)
        except Exception:
            pass

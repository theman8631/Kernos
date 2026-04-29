"""Workflow action library — bounded set of verbs.

WORKFLOW-LOOP-PRIMITIVE C4. Each verb wraps an existing Kernos
surface; no verb invents new world-effect machinery. The verbs split
into two classes per the spec's verb-split invariant:

**World-effect verbs (action-loop instances).** These actually change
state in the world. Each has an ``execute`` side-effect path and a
``verify`` intent-satisfaction check. Covenant-gated: a configured
covenant_gate callable is consulted before execute; denied gates
short-circuit to ``ActionResult(success=False)``.

  * ``notify_user`` — wraps presence/adapter delivery
  * ``write_canvas`` — wraps canvas write surface
  * ``route_to_agent`` — writes to the configured AgentInbox
    provider; FAILS LOUDLY when no provider is bound
  * ``call_tool`` — wraps the existing tool dispatch primitive;
    verifier is the wrapped tool's own declared verifier
  * ``post_to_service`` — wraps the workshop service registry

**Direct-effect verbs (structural assertions, NOT action-loop
instances).** These mutate internal state only and have a structural
assertion in lieu of an LLM-judged verifier — per the
ACTION-LOOP-PRIMITIVE Anti-Goal of not adding LLM verification to
deterministic operations.

  * ``mark_state`` — versioned internal-state mutation
  * ``append_to_ledger`` — append-only ledger entry

Provider independence: this module MUST NOT reference any specific
inbox backend (URLs, tool names, vendor-specific APIs). The
``route_to_agent`` verb goes through the AgentInbox Protocol; only
the concrete inbox implementations in ``agent_inbox.py`` may carry
backend-specific names. Structural test scans this file for backend
URL fragments and tool-namespace patterns.

Bounded set in v1. New verbs require a separate spec extending the
library — preserves covenant gating, keeps the action surface
auditable.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol

from kernos.kernel.workflows.agent_inbox import (
    AgentInbox,
    AgentInboxUnavailable,
)

logger = logging.getLogger(__name__)


# A covenant gate decides whether a world-effect verb may execute.
# Returns True to permit, False to deny. The engine in C5 injects a
# real evaluator that consults the covenant cohort with the
# synthetic safety context. Tests inject stubs.
CovenantGate = Callable[[Any, str, dict], Awaitable[bool] | bool]


@dataclass
class ActionResult:
    """Uniform return shape for verb execution. Verifier reads
    ``success`` and (for world-effect verbs) cross-checks the
    receipt against the wrapped surface to confirm
    intent-satisfaction."""

    success: bool
    value: Any = None
    error: str | None = None
    receipt: dict = field(default_factory=dict)


class Action(Protocol):
    """Each action verb satisfies this Protocol."""

    action_type: str

    async def execute(self, context: Any, params: dict) -> ActionResult: ...

    async def verify(
        self, context: Any, params: dict, result: ActionResult,
    ) -> bool: ...


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _resolve_covenant(
    gate: CovenantGate | None, context: Any, action_type: str, params: dict,
) -> bool:
    """Resolve the covenant gate. ``None`` means permit. Async + sync
    callables both supported."""
    if gate is None:
        return True
    out = gate(context, action_type, params)
    if asyncio.iscoroutine(out):
        return await out  # type: ignore[no-any-return]
    return bool(out)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# World-effect verbs
# ---------------------------------------------------------------------------


class NotifyUserAction:
    """Deliver a message to a channel via the presence/adapter
    surface. The wrapped delivery callable must return a truthy
    receipt that the verifier checks for ``persisted`` semantics."""

    action_type = "notify_user"

    def __init__(
        self,
        deliver_fn: Callable[..., Awaitable[Any]],
        *,
        covenant_gate: CovenantGate | None = None,
    ) -> None:
        self._deliver = deliver_fn
        self._covenant_gate = covenant_gate

    async def execute(self, context: Any, params: dict) -> ActionResult:
        if not await _resolve_covenant(
            self._covenant_gate, context, self.action_type, params,
        ):
            return ActionResult(success=False, error="covenant_denied")
        try:
            receipt = await self._deliver(
                channel=params["channel"],
                message=params["message"],
                urgency=params.get("urgency", "normal"),
                instance_id=getattr(context, "instance_id", ""),
                member_id=getattr(context, "member_id", ""),
            )
        except KeyError as exc:
            return ActionResult(success=False, error=f"missing_param:{exc.args[0]}")
        except Exception as exc:
            return ActionResult(success=False, error=f"deliver_failed:{exc}")
        return ActionResult(
            success=True,
            value=receipt,
            receipt={"delivered_at": _now()},
        )

    async def verify(
        self, context: Any, params: dict, result: ActionResult,
    ) -> bool:
        # Read-after-write: the wrapped delivery surface returned a
        # receipt; verify the receipt is truthy and that the action
        # marked itself successful. The full read-back-from-channel
        # check is integration-test territory; the unit verifier
        # confirms we didn't silently fail.
        return result.success and bool(result.value)


class WriteCanvasAction:
    """Wraps the existing canvas write surface. ``append`` mode is
    reversible; ``replace`` mode is irreversible (per
    action_classification)."""

    action_type = "write_canvas"

    def __init__(
        self,
        canvas_write_fn: Callable[..., Awaitable[Any]],
        canvas_read_fn: Callable[..., Awaitable[str]],
        *,
        covenant_gate: CovenantGate | None = None,
    ) -> None:
        self._write = canvas_write_fn
        self._read = canvas_read_fn
        self._covenant_gate = covenant_gate

    async def execute(self, context: Any, params: dict) -> ActionResult:
        if not await _resolve_covenant(
            self._covenant_gate, context, self.action_type, params,
        ):
            return ActionResult(success=False, error="covenant_denied")
        canvas_id = params["canvas_id"]
        content = params["content"]
        mode = params.get("append_or_replace", "append")
        try:
            await self._write(
                canvas_id=canvas_id,
                content=content,
                mode=mode,
                instance_id=getattr(context, "instance_id", ""),
            )
        except Exception as exc:
            return ActionResult(success=False, error=f"canvas_write_failed:{exc}")
        return ActionResult(
            success=True,
            receipt={"canvas_id": canvas_id, "mode": mode, "wrote_at": _now()},
        )

    async def verify(
        self, context: Any, params: dict, result: ActionResult,
    ) -> bool:
        if not result.success:
            return False
        try:
            current = await self._read(
                canvas_id=params["canvas_id"],
                instance_id=getattr(context, "instance_id", ""),
            )
        except Exception:
            return False
        # Read-after-write check: replace → exact content; append →
        # content visible somewhere in the current state.
        mode = params.get("append_or_replace", "append")
        if mode == "replace":
            return current == params["content"]
        return params["content"] in current


class RouteToAgentAction:
    """Posts a payload to a configured AgentInbox. Provider
    containment: if no inbox is bound, raises
    ``AgentInboxUnavailable`` rather than silently routing
    elsewhere."""

    action_type = "route_to_agent"

    def __init__(
        self,
        inbox: AgentInbox | None,
        *,
        covenant_gate: CovenantGate | None = None,
    ) -> None:
        self._inbox = inbox
        self._covenant_gate = covenant_gate

    async def execute(self, context: Any, params: dict) -> ActionResult:
        if self._inbox is None:
            raise AgentInboxUnavailable(
                "route_to_agent invoked but no AgentInbox provider is "
                "configured. Bind a concrete inbox provider at action "
                "library construction time."
            )
        if not await _resolve_covenant(
            self._covenant_gate, context, self.action_type, params,
        ):
            return ActionResult(success=False, error="covenant_denied")
        try:
            receipt = await self._inbox.post(
                agent_id=params["agent_id"],
                payload=params["payload"],
                instance_id=getattr(context, "instance_id", ""),
            )
        except Exception as exc:
            return ActionResult(success=False, error=f"inbox_post_failed:{exc}")
        return ActionResult(
            success=True,
            value=receipt,
            receipt={"persisted_id": receipt.persisted_id},
        )

    async def verify(
        self, context: Any, params: dict, result: ActionResult,
    ) -> bool:
        if not result.success or self._inbox is None:
            return False
        try:
            items = await self._inbox.read(
                agent_id=params["agent_id"],
                instance_id=getattr(context, "instance_id", ""),
            )
        except Exception:
            return False
        target_id = result.receipt.get("persisted_id")
        return any(i.persisted_id == target_id for i in items)


class CallToolAction:
    """Wraps the existing tool dispatch primitive. The verifier
    delegates to the tool's own declared verifier — this verb does
    NOT redefine tool verification."""

    action_type = "call_tool"

    def __init__(
        self,
        tool_dispatch_fn: Callable[..., Awaitable[Any]],
        *,
        tool_verifier_fn: Callable[..., Awaitable[bool]] | None = None,
        covenant_gate: CovenantGate | None = None,
    ) -> None:
        self._dispatch = tool_dispatch_fn
        self._tool_verifier = tool_verifier_fn
        self._covenant_gate = covenant_gate

    async def execute(self, context: Any, params: dict) -> ActionResult:
        if not await _resolve_covenant(
            self._covenant_gate, context, self.action_type, params,
        ):
            return ActionResult(success=False, error="covenant_denied")
        try:
            result_value = await self._dispatch(
                tool_id=params["tool_id"],
                args=params.get("args") or {},
                instance_id=getattr(context, "instance_id", ""),
                member_id=getattr(context, "member_id", ""),
            )
        except Exception as exc:
            return ActionResult(success=False, error=f"tool_dispatch_failed:{exc}")
        return ActionResult(
            success=True,
            value=result_value,
            receipt={"tool_id": params["tool_id"], "called_at": _now()},
        )

    async def verify(
        self, context: Any, params: dict, result: ActionResult,
    ) -> bool:
        if not result.success:
            return False
        if self._tool_verifier is None:
            # No tool-specific verifier configured — fall back to the
            # success bit. The wrapped surface is responsible for
            # raising rather than returning falsely-successful values.
            return True
        return await self._tool_verifier(
            tool_id=params["tool_id"], args=params.get("args") or {},
            value=result.value, context=context,
        )


class PostToServiceAction:
    """Wraps the workshop service registry. Each service declares its
    own verifier; this verb's verify() delegates."""

    action_type = "post_to_service"

    def __init__(
        self,
        service_post_fn: Callable[..., Awaitable[Any]],
        *,
        service_verifier_fn: Callable[..., Awaitable[bool]] | None = None,
        covenant_gate: CovenantGate | None = None,
    ) -> None:
        self._post = service_post_fn
        self._service_verifier = service_verifier_fn
        self._covenant_gate = covenant_gate

    async def execute(self, context: Any, params: dict) -> ActionResult:
        if not await _resolve_covenant(
            self._covenant_gate, context, self.action_type, params,
        ):
            return ActionResult(success=False, error="covenant_denied")
        try:
            value = await self._post(
                service_id=params["service_id"],
                payload=params.get("payload") or {},
                instance_id=getattr(context, "instance_id", ""),
            )
        except Exception as exc:
            return ActionResult(success=False, error=f"service_post_failed:{exc}")
        return ActionResult(
            success=True,
            value=value,
            receipt={"service_id": params["service_id"], "posted_at": _now()},
        )

    async def verify(
        self, context: Any, params: dict, result: ActionResult,
    ) -> bool:
        if not result.success:
            return False
        if self._service_verifier is None:
            return True
        return await self._service_verifier(
            service_id=params["service_id"],
            payload=params.get("payload") or {},
            value=result.value, context=context,
        )


# ---------------------------------------------------------------------------
# Direct-effect verbs (structural assertions only)
# ---------------------------------------------------------------------------


class MarkStateAction:
    """Internal state mutation, scoped to instance/member/space/workflow.

    NOT an action-loop. Per ACTION-LOOP-PRIMITIVE Anti-Goal:
    "do not add LLM verification to deterministic operations." The
    structural assertion is "post-mutation read returns the new
    value" — checked by ``verify`` reading the same key back.

    Mutations are versioned per the standing no-destructive-deletes
    principle: each call appends a new entry rather than overwriting.
    The state_store is responsible for the versioning shape.
    """

    action_type = "mark_state"

    def __init__(
        self,
        state_store_set: Callable[..., Awaitable[Any]],
        state_store_get: Callable[..., Awaitable[Any]],
    ) -> None:
        self._set = state_store_set
        self._get = state_store_get

    async def execute(self, context: Any, params: dict) -> ActionResult:
        try:
            await self._set(
                key=params["key"],
                value=params["value"],
                scope=params.get("scope", "instance"),
                instance_id=getattr(context, "instance_id", ""),
            )
        except Exception as exc:
            return ActionResult(success=False, error=f"state_set_failed:{exc}")
        return ActionResult(
            success=True,
            receipt={
                "key": params["key"], "scope": params.get("scope", "instance"),
                "set_at": _now(),
            },
        )

    async def verify(
        self, context: Any, params: dict, result: ActionResult,
    ) -> bool:
        if not result.success:
            return False
        try:
            current = await self._get(
                key=params["key"],
                scope=params.get("scope", "instance"),
                instance_id=getattr(context, "instance_id", ""),
            )
        except Exception:
            return False
        return current == params["value"]


class AppendToLedgerAction:
    """Append a synopsis entry to a workflow's ledger. NOT an
    action-loop — structural assertion is "ledger's last entry
    matches the appended record."

    The ledger surface itself is owned by C5's WorkflowExecution
    layer; this verb only carries the call. Tests inject a stub
    ledger.
    """

    action_type = "append_to_ledger"

    def __init__(
        self,
        ledger_append_fn: Callable[..., Awaitable[Any]],
        ledger_read_last_fn: Callable[..., Awaitable[dict | None]],
    ) -> None:
        self._append = ledger_append_fn
        self._read_last = ledger_read_last_fn

    async def execute(self, context: Any, params: dict) -> ActionResult:
        try:
            await self._append(
                workflow_id=params["workflow_id"],
                entry=params["entry"],
                instance_id=getattr(context, "instance_id", ""),
            )
        except Exception as exc:
            return ActionResult(success=False, error=f"ledger_append_failed:{exc}")
        return ActionResult(
            success=True,
            receipt={
                "workflow_id": params["workflow_id"],
                "appended_at": _now(),
            },
        )

    async def verify(
        self, context: Any, params: dict, result: ActionResult,
    ) -> bool:
        if not result.success:
            return False
        try:
            last = await self._read_last(
                workflow_id=params["workflow_id"],
                instance_id=getattr(context, "instance_id", ""),
            )
        except Exception:
            return False
        if last is None:
            return False
        # Codex doc-batch review: production ledger writers
        # (e.g. WorkflowLedger) inject a `logged_at` timestamp into
        # every appended entry, so a raw equality check against the
        # caller's original entry would always fail in production.
        # Verify by checking that every key/value the caller wrote is
        # present in the read-back record — extra writer-injected
        # fields (logged_at, future audit metadata) don't fail the
        # check.
        if not isinstance(last, dict) or not isinstance(params["entry"], dict):
            return last == params["entry"]
        return all(last.get(k) == v for k, v in params["entry"].items())


# ---------------------------------------------------------------------------
# Library registry
# ---------------------------------------------------------------------------


class ActionLibrary:
    """Registry mapping action_type → Action instance. The execution
    engine looks up verbs by type and dispatches."""

    def __init__(self) -> None:
        self._verbs: dict[str, Action] = {}

    def register(self, action: Action) -> None:
        if action.action_type in self._verbs:
            raise ValueError(
                f"action_type {action.action_type!r} already registered"
            )
        self._verbs[action.action_type] = action

    def get(self, action_type: str) -> Action:
        if action_type not in self._verbs:
            raise KeyError(f"action_type {action_type!r} not registered")
        return self._verbs[action_type]

    def has(self, action_type: str) -> bool:
        return action_type in self._verbs

    def registered_types(self) -> tuple[str, ...]:
        return tuple(self._verbs.keys())


__all__ = [
    "Action",
    "ActionLibrary",
    "ActionResult",
    "AppendToLedgerAction",
    "CallToolAction",
    "CovenantGate",
    "MarkStateAction",
    "NotifyUserAction",
    "PostToServiceAction",
    "RouteToAgentAction",
    "WriteCanvasAction",
]

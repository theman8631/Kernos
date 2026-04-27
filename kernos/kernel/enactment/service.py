"""EnactmentService — branch decision + thin-path rendering (PDI C4).

Consumes a Briefing and routes structurally:

  - decided_action.kind == execute_tool → full machinery (C5-C6)
  - everything else → thin path (render-only)

Render-only kinds (per Kit edit, propose_tool joins the conversational
set because the dispatch happens on the next turn after user confirms,
not in this turn):

  - respond_only
  - defer
  - constrained_response
  - pivot
  - clarification_needed
  - propose_tool

The thin path NEVER dispatches tools. This invariant is enforced
structurally: the thin-path code path takes only the presence renderer
as its dependency. There is no dispatcher reachable from this branch.

Audit category: enactment.terminated. Subtypes:
  - success_thin_path: any non-clarification thin-path render.
  - thin_path_proposal_rendered: propose_tool rendered (proposal is
    awaiting user confirmation; the dispatch happens on the next
    turn's execute_tool decision, NOT here).
  - b1_action_invalidated: full machinery termination (C5-C6).
  - b2_user_disambiguation_needed: full machinery surfaced a B2
    clarification or thin-path rendered a populated-partial_state
    clarification (the latter case — thin-path B2 echo — happens
    when integration emits clarification_needed with a populated
    partial_state because a previous turn's enactment surfaced it).

C4 only emits success_thin_path, thin_path_proposal_rendered, and
b2_user_disambiguation_needed. C5-C6 add b1_action_invalidated and
the per-step / per-tier audit events.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

from kernos.kernel.integration.briefing import (
    ActionKind,
    Briefing,
    ClarificationNeeded,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Termination subtypes
# ---------------------------------------------------------------------------


class TerminationSubtype(str, Enum):
    """Closed enum of enactment.terminated audit subtypes.

    Stable across C4-C6 — adding a value is a schema extension, not
    a tweak. The audit consumer (operator dashboards, friction
    observer) keys on these.
    """

    SUCCESS_THIN_PATH = "success_thin_path"
    THIN_PATH_PROPOSAL_RENDERED = "thin_path_proposal_rendered"
    B1_ACTION_INVALIDATED = "b1_action_invalidated"
    B2_USER_DISAMBIGUATION_NEEDED = "b2_user_disambiguation_needed"


# Kinds that take the thin path (Kit edit — propose_tool included).
_THIN_PATH_KINDS: frozenset[ActionKind] = frozenset({
    ActionKind.RESPOND_ONLY,
    ActionKind.DEFER,
    ActionKind.CONSTRAINED_RESPONSE,
    ActionKind.PIVOT,
    ActionKind.CLARIFICATION_NEEDED,
    ActionKind.PROPOSE_TOOL,
})

# Kinds that take full machinery. Per spec post-Kit-edit: only
# execute_tool. propose_tool was historically considered for full
# machinery and Kit moved it to render-only because its actual
# dispatch happens on the *next* turn after the user confirms.
_FULL_MACHINERY_KINDS: frozenset[ActionKind] = frozenset({
    ActionKind.EXECUTE_TOOL,
})


# ---------------------------------------------------------------------------
# Outcome
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EnactmentOutcome:
    """Result of one enactment turn.

    `text` is the user-facing rendered response. `subtype` is the
    enactment.terminated audit subtype. `decided_action_kind` is the
    forwarded kind for telemetry. `streamed` records whether the
    presence renderer streamed (thin path supports streaming; full
    machinery disables it during execution and re-enables only after
    terminal response).

    `audit_refs` is operator-readable references to the audit entries
    this enactment emitted. Empty in C4 (skeleton); C7 wires the audit
    family fully.
    """

    text: str
    subtype: TerminationSubtype
    decided_action_kind: ActionKind
    streamed: bool = False
    audit_refs: tuple[str, ...] = ()

    @property
    def is_thin_path(self) -> bool:
        return self.subtype in (
            TerminationSubtype.SUCCESS_THIN_PATH,
            TerminationSubtype.THIN_PATH_PROPOSAL_RENDERED,
        )


# ---------------------------------------------------------------------------
# Service-shaped dependencies (Protocols)
# ---------------------------------------------------------------------------


@runtime_checkable
class PresenceRendererLike(Protocol):
    """Renders user-facing text from a Briefing.

    The thin path's only dependency. Implementations bind to a model
    chain and the presence prompt; tests pass stub renderers that
    return canned text. Streaming is the renderer's concern; the
    EnactmentService records whether streaming occurred via the
    `streamed` attribute on the response object.
    """

    async def render(self, briefing: Briefing) -> "PresenceRenderResult": ...


@dataclass(frozen=True)
class PresenceRenderResult:
    """What a PresenceRendererLike returns.

    `text` is the final rendered response. `streamed` records whether
    the renderer streamed mid-generation. Audit/telemetry sources
    this; the spec mandates streaming-disabled-during-full-machinery
    so this flag becomes load-bearing in C5+.
    """

    text: str
    streamed: bool = False


AuditEmitter = Callable[[dict[str, Any]], Awaitable[None]]
"""(audit_entry) → None. enactment.* audit family populates here."""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class EnactmentNotImplemented(NotImplementedError):
    """Raised when a code path is not yet wired (full machinery in
    C5-C6). Distinct subclass so wiring sites can route the error
    cleanly without confusing it with library NotImplementedError."""


# ---------------------------------------------------------------------------
# EnactmentService
# ---------------------------------------------------------------------------


class EnactmentService:
    """Branch decision + thin-path rendering.

    Invariants enforced structurally:
      - The thin path takes only the presence renderer; no
        dispatcher reachable from this branch. The "thin path never
        dispatches tools" rule is therefore not a runtime check, it's
        a code-shape guarantee.
      - The decided_action.kind drives branching; model judgment
        plays no role in path selection.
      - The full machinery branch is a stub in C4; calling it raises
        a clear EnactmentNotImplemented pointing at PDI C5.
    """

    def __init__(
        self,
        *,
        presence_renderer: PresenceRendererLike,
        audit_emitter: AuditEmitter | None = None,
    ) -> None:
        self._presence = presence_renderer
        self._audit = audit_emitter

    async def run(self, briefing: Briefing) -> EnactmentOutcome:
        """Branch on decided_action.kind and route to the right path.

        Branch decision is structural. Full machinery is reserved for
        execute_tool only (Kit edit). Everything else takes the thin
        path. An unrecognised kind is a contract bug — surfaced via
        ValueError so the wiring layer can catch it cleanly.
        """
        kind = briefing.decided_action.kind
        if kind in _FULL_MACHINERY_KINDS:
            return await self._run_full_machinery(briefing)
        if kind in _THIN_PATH_KINDS:
            return await self._run_thin_path(briefing)
        # Defensive — every ActionKind belongs to exactly one set; this
        # branch fires only if a future variant is added without
        # updating the route maps. Failing loudly is the right move.
        raise ValueError(
            f"EnactmentService cannot route decided_action.kind "
            f"{kind.value!r}: not in thin-path or full-machinery sets. "
            f"Update _THIN_PATH_KINDS / _FULL_MACHINERY_KINDS."
        )

    # ----- thin path -----

    async def _run_thin_path(self, briefing: Briefing) -> EnactmentOutcome:
        """Render-only execution.

        No tool dispatch. The presence renderer takes the briefing
        and produces user-facing text. Subtype routing handles the
        special cases:

          - propose_tool → THIN_PATH_PROPOSAL_RENDERED. The proposal
            is awaiting the user's next-turn confirmation; the
            corresponding execute_tool will land on the next turn,
            with its own envelope. This turn renders the proposal
            text only.
          - clarification_needed with populated partial_state →
            B2_USER_DISAMBIGUATION_NEEDED. The previous turn's
            full machinery surfaced this clarification; thin path
            renders the question. Reintegration context lives on
            the briefing's audit refs (C6 wires the storage).
          - clarification_needed with None partial_state → SUCCESS
            (first-pass clarification, integration-initiated).
          - everything else → SUCCESS_THIN_PATH.
        """
        result = await self._presence.render(briefing)
        subtype = self._thin_path_subtype(briefing)
        await self._emit_terminated(briefing, subtype, text=result.text)
        return EnactmentOutcome(
            text=result.text,
            subtype=subtype,
            decided_action_kind=briefing.decided_action.kind,
            streamed=result.streamed,
        )

    @staticmethod
    def _thin_path_subtype(briefing: Briefing) -> TerminationSubtype:
        kind = briefing.decided_action.kind
        if kind is ActionKind.PROPOSE_TOOL:
            return TerminationSubtype.THIN_PATH_PROPOSAL_RENDERED
        if isinstance(briefing.decided_action, ClarificationNeeded):
            # Populated partial_state means the previous turn's full
            # machinery surfaced a B2 clarification; the next turn's
            # integration emitted the variant; thin path renders. The
            # audit subtype reflects the B2 routing for telemetry.
            if briefing.decided_action.partial_state is not None:
                return TerminationSubtype.B2_USER_DISAMBIGUATION_NEEDED
        return TerminationSubtype.SUCCESS_THIN_PATH

    # ----- full machinery (stub until C5) -----

    async def _run_full_machinery(self, briefing: Briefing) -> EnactmentOutcome:
        raise EnactmentNotImplemented(
            "Full machinery (plan + three-question check + five-tier "
            "hierarchy + envelope validation) lands in PDI C5. C4 "
            "ships the skeleton + thin path only."
        )

    # ----- audit emission -----

    async def _emit_terminated(
        self,
        briefing: Briefing,
        subtype: TerminationSubtype,
        *,
        text: str,
    ) -> None:
        """Emit enactment.terminated with the chosen subtype.

        References-not-dumps for plan/briefing payloads (V1
        invariant). C7 expands the audit family; C4 emits only the
        terminated event with the minimal shape.

        Audit emission is best-effort — failures are logged and
        swallowed so an audit-store outage cannot break the user's
        turn.
        """
        if self._audit is None:
            return
        entry = {
            "category": "enactment.terminated",
            "turn_id": briefing.turn_id,
            "integration_run_id": briefing.integration_run_id,
            "decided_action_kind": briefing.decided_action.kind.value,
            "subtype": subtype.value,
            "text_length": len(text),
        }
        try:
            await self._audit(entry)
        except Exception:
            logger.exception("ENACTMENT_AUDIT_EMIT_FAILED")


def build_enactment_service(
    *,
    presence_renderer: PresenceRendererLike,
    audit_emitter: AuditEmitter | None = None,
) -> EnactmentService:
    """Convenience factory mirroring the constructor."""
    return EnactmentService(
        presence_renderer=presence_renderer,
        audit_emitter=audit_emitter,
    )


__all__ = [
    "AuditEmitter",
    "EnactmentNotImplemented",
    "EnactmentOutcome",
    "EnactmentService",
    "PresenceRendererLike",
    "PresenceRenderResult",
    "TerminationSubtype",
    "build_enactment_service",
]

"""Cheap structural shape assertions for the CRB Compiler.

These checks fire inline during ``draft_to_descriptor_candidate`` and
catch *substrate-level* malformations (missing required fields, list
non-emptiness, AST type-correctness) that indicate a Drafter bug.

Capability / provider validation (e.g. "is sms.send connected?",
"does agent_X exist in DAR?") is deferred to STS dry-run per Seam C7
of the CRB spec. The Compiler's job is "produce a syntactically valid
descriptor"; STS's job is "validate that every reference resolves in
the current substrate."

Errors raised by this module surface as
:class:`kernos.kernel.crb.errors.DraftSchemaIncomplete` (missing
required field) or :class:`DraftShapeMalformed` (invalid structure).
Both are operator-diagnostic per Seam C7 — they signal Drafter bugs
that should never reach this layer in production.
"""
from __future__ import annotations

from typing import Any, Iterable

from kernos.kernel.crb.errors import (
    DraftSchemaIncomplete,
    DraftShapeMalformed,
)


# Required fields in the descriptor candidate produced by translation.
# ``predicate`` and ``verifier`` are checked separately because their
# shape involves deeper structural assertions.
_REQUIRED_DESCRIPTOR_KEYS: tuple[str, ...] = (
    "intent_summary",
    "triggers",
    "action_sequence",
    "predicate",
)


def assert_required_fields_present(candidate: dict) -> None:
    """Raise ``DraftSchemaIncomplete`` for the first missing required
    field. The check is order-stable so the operator gets a
    reproducible diagnostic."""
    for key in _REQUIRED_DESCRIPTOR_KEYS:
        if key not in candidate:
            raise DraftSchemaIncomplete(
                f"required descriptor field missing: {key!r}"
            )


def assert_triggers_well_formed(triggers: Any) -> None:
    """Triggers must be a non-empty list of typed trigger objects.
    Each trigger needs at minimum an ``event_type`` field."""
    if not isinstance(triggers, list):
        raise DraftShapeMalformed(
            f"triggers must be a list, got {type(triggers).__name__}"
        )
    if not triggers:
        raise DraftShapeMalformed("triggers list must be non-empty")
    for idx, trig in enumerate(triggers):
        if not isinstance(trig, dict):
            raise DraftShapeMalformed(
                f"triggers[{idx}] must be a dict, got "
                f"{type(trig).__name__}"
            )
        if "event_type" not in trig:
            raise DraftShapeMalformed(
                f"triggers[{idx}] missing required field 'event_type'"
            )


def assert_action_sequence_well_formed(actions: Any) -> None:
    """Action sequence must be a non-empty list of dicts with
    ``action_type``."""
    if not isinstance(actions, list):
        raise DraftShapeMalformed(
            f"action_sequence must be a list, got "
            f"{type(actions).__name__}"
        )
    if not actions:
        raise DraftShapeMalformed("action_sequence list must be non-empty")
    for idx, action in enumerate(actions):
        if not isinstance(action, dict):
            raise DraftShapeMalformed(
                f"action_sequence[{idx}] must be a dict, got "
                f"{type(action).__name__}"
            )
        if "action_type" not in action:
            raise DraftShapeMalformed(
                f"action_sequence[{idx}] missing required field "
                f"'action_type'"
            )


# Permitted predicate AST node shapes. Predicates can be:
# * Boolean literals (``True`` / ``False``)
# * String literal "true"/"false"
# * Dict with ``op`` plus operands appropriate to the op.
_PERMITTED_PREDICATE_OPS: frozenset[str] = frozenset({
    "and", "or", "not",
    "eq", "ne", "lt", "le", "gt", "ge",
    "in", "not_in",
    "contains", "starts_with", "ends_with",
    "exists", "missing",
    "always", "never",
})


def assert_predicate_ast_shape(predicate: Any) -> None:
    """Walk a predicate AST and raise ``DraftShapeMalformed`` for the
    first invalid node. Permits boolean literals, the string forms
    ``"true"`` / ``"false"``, or dicts with a recognized ``op``.
    """
    if predicate is True or predicate is False:
        return
    if predicate in ("true", "false"):
        return
    if not isinstance(predicate, dict):
        raise DraftShapeMalformed(
            f"predicate must be a bool, 'true'/'false', or dict; got "
            f"{type(predicate).__name__}"
        )
    op = predicate.get("op")
    if op is None:
        raise DraftShapeMalformed(
            "predicate dict missing required field 'op'"
        )
    if op not in _PERMITTED_PREDICATE_OPS:
        raise DraftShapeMalformed(
            f"predicate op {op!r} not in permitted set "
            f"{sorted(_PERMITTED_PREDICATE_OPS)}"
        )
    # Recurse into nested predicates for boolean ops.
    if op in ("and", "or"):
        operands = predicate.get("operands")
        if not isinstance(operands, list) or not operands:
            raise DraftShapeMalformed(
                f"predicate op={op!r} requires non-empty 'operands' list"
            )
        for sub in operands:
            assert_predicate_ast_shape(sub)
    elif op == "not":
        operand = predicate.get("operand")
        if operand is None:
            raise DraftShapeMalformed(
                "predicate op='not' requires 'operand' field"
            )
        assert_predicate_ast_shape(operand)


def assert_bounds_shape(bounds: Any) -> None:
    """Bounds must be a dict with non-negative numeric fields when
    present. Soft warning for unreasonably high ``max_executions`` —
    surfaced as the warning is a no-op in v1; just don't crash on it.
    """
    if bounds is None:
        return
    if not isinstance(bounds, dict):
        raise DraftShapeMalformed(
            f"bounds must be a dict, got {type(bounds).__name__}"
        )
    for key in ("iteration_count", "wall_time_seconds", "cost_usd",
                "max_executions"):
        if key in bounds:
            value = bounds[key]
            if not isinstance(value, (int, float)) or value < 0:
                raise DraftShapeMalformed(
                    f"bounds.{key}={value!r} must be a non-negative "
                    f"number"
                )


__all__ = [
    "assert_action_sequence_well_formed",
    "assert_bounds_shape",
    "assert_predicate_ast_shape",
    "assert_required_fields_present",
    "assert_triggers_well_formed",
]

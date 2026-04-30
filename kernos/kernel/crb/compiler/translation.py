"""Deterministic descriptor translation.

``draft_to_descriptor_candidate`` is the production replacement for
Drafter v1's ``compiler_helper_stub``. Pure free function — same
draft -> same descriptor candidate. No LLM, no side effects, no
state.

Mapping (draft fields -> descriptor fields):

* ``draft.intent_summary`` -> ``descriptor.intent_summary`` +
  ``descriptor.metadata.intent_summary``
* ``draft.partial_spec_json.triggers`` -> ``descriptor.triggers``
* ``draft.partial_spec_json.action_sequence`` -> ``descriptor.action_sequence``
* ``draft.partial_spec_json.predicate`` -> ``descriptor.predicate``
* ``draft.partial_spec_json.verifier`` -> ``descriptor.verifier``
* ``draft.partial_spec_json.bounds`` -> ``descriptor.bounds``
* ``draft.partial_spec_json.prev_version_id`` -> ``descriptor.prev_version_id``

Cheap shape assertions fire inline; capability / provider validation
is deferred to STS dry-run per Seam C7. Errors raised:

* :class:`DraftSchemaIncomplete` — required field missing
* :class:`DraftShapeMalformed` — structural shape invalid

Both surface to the operator-diagnostic path; they signal Drafter
bugs and are NOT user-facing.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from kernos.kernel.crb.compiler.shape_assertions import (
    assert_action_sequence_well_formed,
    assert_bounds_shape,
    assert_predicate_ast_shape,
    assert_required_fields_present,
    assert_triggers_well_formed,
)
from kernos.kernel.crb.errors import DraftSchemaIncomplete

if TYPE_CHECKING:  # pragma: no cover
    from kernos.kernel.drafts.registry import WorkflowDraft


def draft_to_descriptor_candidate(draft: "WorkflowDraft") -> dict:
    """Pure deterministic translation. See module docstring.

    Raises:
        DraftSchemaIncomplete: required field missing for descriptor
            production.
        DraftShapeMalformed: structural assertion failed.
    """
    if draft is None:
        raise DraftSchemaIncomplete("draft is required")
    if not draft.instance_id:
        raise DraftSchemaIncomplete("draft.instance_id is required")
    if not draft.intent_summary:
        raise DraftSchemaIncomplete("draft.intent_summary is required")

    spec = dict(draft.partial_spec_json or {})

    # Build the descriptor candidate. Substrate-mandatory fields are
    # only included when present in the draft body — assert_required_
    # fields_present below reports a clean DraftSchemaIncomplete
    # rather than a deeper DraftShapeMalformed.
    candidate: dict = {
        "name": draft.display_name or "untitled-draft",
        "instance_id": draft.instance_id,
        "intent_summary": draft.intent_summary,
    }
    for key in ("triggers", "action_sequence", "predicate"):
        if key in spec:
            candidate[key] = spec[key]

    # Optional pass-through fields.
    if "verifier" in spec:
        candidate["verifier"] = spec["verifier"]
    if "bounds" in spec:
        candidate["bounds"] = spec["bounds"]
    if "prev_version_id" in spec and spec["prev_version_id"]:
        candidate["prev_version_id"] = spec["prev_version_id"]
    if draft.aliases:
        candidate["aliases"] = list(draft.aliases)

    # Carry intent_summary in metadata for downstream consumers that
    # read metadata uniformly (e.g. STS's ContextBriefRegistry).
    metadata = dict(spec.get("metadata") or {})
    metadata.setdefault("intent_summary", draft.intent_summary)
    if metadata:
        candidate["metadata"] = metadata

    # Cheap shape assertions. assert_required_fields_present runs first
    # so missing fields surface as DraftSchemaIncomplete, not the
    # deeper DraftShapeMalformed from a None-typed shape check.
    assert_required_fields_present(candidate)
    assert_triggers_well_formed(candidate["triggers"])
    assert_action_sequence_well_formed(candidate["action_sequence"])
    assert_predicate_ast_shape(candidate["predicate"])
    assert_bounds_shape(candidate.get("bounds"))

    return candidate


__all__ = ["draft_to_descriptor_candidate"]

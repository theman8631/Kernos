"""Deterministic compiler-helper stub for Drafter v1 (DRAFTER spec D7).

The CRB main spec ships the real :func:`draft_to_descriptor_candidate`
translator. v1 needs SOMETHING the cohort can call so the compiler-
boundary invariant is testable: Drafter NEVER translates descriptors
inside its own LLM prompts. Single owner for descriptor translation.

This stub is deterministic pass-through: same draft → same descriptor
candidate. Stateless. No LLM. The CRB main spec replaces this with the
real translator behind the same signature.

Pin (AC #21): Drafter calls this helper for descriptor production;
descriptor JSON MUST NOT appear in any Drafter LLM completion.
"""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from kernos.kernel.drafts.registry import WorkflowDraft


def draft_to_descriptor_candidate(draft: "WorkflowDraft") -> dict:
    """Pass-through stub. Reads ``draft.partial_spec_json`` and returns
    a descriptor candidate dict with WDP-tracked fields populated.

    Determinism: same draft (same ``partial_spec_json`` + same
    metadata) produces the same descriptor candidate. Stateless.

    The CRB main spec replaces this with the real translator that may
    invoke an LLM for descriptor production — but always behind this
    same signature so Drafter never reaches an LLM directly for
    descriptor work.
    """
    if draft is None:
        raise ValueError("draft is required")
    base = dict(draft.partial_spec_json or {})
    # Populate WDP-tracked fields if not already present in
    # partial_spec_json.
    base.setdefault("instance_id", draft.instance_id)
    base.setdefault("name", draft.display_name or "untitled-draft")
    if draft.aliases:
        base.setdefault("aliases", list(draft.aliases))
    if draft.intent_summary:
        base.setdefault("intent_summary", draft.intent_summary)
    return base


__all__ = ["draft_to_descriptor_candidate"]

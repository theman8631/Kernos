"""Canonical SHA-256 hash over a workflow descriptor AST.

The hash is what an approval event commits to. Any field that varies
between proposal and registration without changing what the user
approved must be excluded; everything else is included.

Volatile fields (excluded — registry metadata only):

* ``id``, ``workflow_id``     — assigned at registration
* ``created_at``, ``updated_at``, ``registered_at`` — timestamps
* ``version``                 — sequence/lifecycle marker

Included fields (NOT excluded):

* ``display_name``, ``aliases``, ``intent_summary`` — what the user
  sees and approves
* ``trigger``, ``predicate``, ``verifier``, ``bounds``, all
  ``action_sequence`` entries — executable shape
* ``prev_version_id`` — Kit edit (v1→v2). For modifications, this
  field is set by the Compiler at proposal time and represents user
  intent ("modify THIS specific routine"). Including it in the hash
  means swapping it after approval invalidates the approval.
  Belt-and-suspenders with the modification-target-binding check
  in approval validation Step 5b.

Algorithm (deterministic):

1. Drop every key listed in :data:`DESCRIPTOR_VOLATILE_FIELDS`,
   recursively at every nesting level (``id`` inside an action's
   parameters is also dropped).
2. Recursively sort all dict keys.
3. Serialize to compact UTF-8 JSON with ``separators=(',', ':')``,
   no whitespace, no surrogateescape, ``sort_keys=False`` (we already
   sorted).
4. Compute SHA-256 of the bytes.
5. Return hex digest.

The output is a 64-character lowercase hex string.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any


DESCRIPTOR_VOLATILE_FIELDS: frozenset[str] = frozenset({
    "id",
    "workflow_id",
    "created_at",
    "updated_at",
    "registered_at",
    "version",
})
"""Fields excluded from the canonical descriptor hash.

NOTE (Kit edit, v1 → v2): ``prev_version_id`` is intentionally NOT in
this list. For modifications, ``prev_version_id`` represents user
intent ("modify THIS specific routine") and must change the hash so a
swap attack invalidates the approval."""


def compute_descriptor_hash(descriptor: dict) -> str:
    """Return the canonical SHA-256 hex digest of ``descriptor``.

    Pure function — no I/O. Equivalent descriptors hash identically;
    descriptors that differ in any non-volatile field hash differently.

    Raises:
        TypeError: if ``descriptor`` is not a dict.
    """
    if not isinstance(descriptor, dict):
        raise TypeError(
            f"compute_descriptor_hash expects a dict, got {type(descriptor).__name__}"
        )
    canonical = _canonicalize(descriptor)
    blob = json.dumps(
        canonical, separators=(",", ":"), ensure_ascii=False, sort_keys=False,
    ).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _canonicalize(node: Any) -> Any:
    """Walk the descriptor recursively: strip volatile keys, sort dict
    keys, leave list order intact (lists are part of the executable
    shape and order matters)."""
    if isinstance(node, dict):
        return {
            key: _canonicalize(value)
            for key, value in sorted(node.items())
            if key not in DESCRIPTOR_VOLATILE_FIELDS
        }
    if isinstance(node, list):
        return [_canonicalize(item) for item in node]
    if isinstance(node, tuple):
        return [_canonicalize(item) for item in node]
    return node


__all__ = ["DESCRIPTOR_VOLATILE_FIELDS", "compute_descriptor_hash"]

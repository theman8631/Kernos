"""CRB Compiler — deterministic descriptor translation.

Replaces Drafter v1's ``compiler_helper_stub`` with the production
translator. Pure free functions — no LLM, no I/O, no state.

* :func:`draft_to_descriptor_candidate` — single owner for descriptor
  production. Same draft -> same descriptor candidate.
* :mod:`shape_assertions` — cheap structural checks that fire inline
  during translation (capability/provider validation deferred to STS
  dry-run per spec Seam C7).
"""
from __future__ import annotations

from kernos.kernel.crb.compiler.translation import (
    draft_to_descriptor_candidate,
)

__all__ = ["draft_to_descriptor_candidate"]

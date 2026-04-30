"""CRB error hierarchy.

C1 ships the Compiler errors. C3 fills in ProposalAuthor errors;
C4 fills in ApprovalFlow errors.
"""
from __future__ import annotations


class CRBError(Exception):
    """Base for all CRB-raised errors."""


class CompilerError(CRBError):
    """Base for Compiler-raised errors. Operator-diagnostic per Seam C7
    — these signal Drafter/substrate bugs, NOT user-facing
    conversational issues."""


class DraftSchemaIncomplete(CompilerError):
    """A required draft field is missing for descriptor production.
    Indicates a Drafter bug — the draft was passed to the Compiler
    before all required shaping was complete."""


class DraftShapeMalformed(CompilerError):
    """A structural assertion failed (e.g. predicate AST contains an
    invalid node). Indicates a Drafter bug — Drafter shaped a value
    that should have been caught by recognition validation."""


__all__ = [
    "CRBError",
    "CompilerError",
    "DraftSchemaIncomplete",
    "DraftShapeMalformed",
]

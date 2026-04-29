"""Workflow draft primitive — persistent, conversational drafts of
not-yet-compiled workflows.

Per SPEC-WDP: drafts are different objects than workflows. Drafts
are mutable, incomplete, conversational, and allowed to be invalid.
Workflows are compiled, schema-strict, versioned, dispatchable.
WDP provides the substrate; the Compiler / Drafter cohort / CRB
spec layer on top.
"""

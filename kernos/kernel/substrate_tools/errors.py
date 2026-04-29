"""Substrate Tools error hierarchy.

All STS-raised errors derive from :class:`SubstrateToolsError`. C1
introduces the base + provider-related typed errors; C2 fills in the
approval-validation hierarchy (binding-missing, event-not-found,
authority-spoofed, hash-mismatch, etc.).
"""
from __future__ import annotations


class SubstrateToolsError(Exception):
    """Base for all STS-raised errors."""


# Provider / capability errors (C1).
# Approval-validation errors (C2) live in
# :mod:`kernos.kernel.substrate_tools.registration.approval` once they
# land; they will all derive from :class:`SubstrateToolsError`.


__all__ = ["SubstrateToolsError"]

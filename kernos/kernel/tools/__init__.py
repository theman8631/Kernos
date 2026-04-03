"""Kernel tool schemas and helpers.

Tool schemas are JSON dicts defining the tool's name, description, and input_schema.
Pure helper functions (_read_doc, _read_source) are co-located with their schemas.
Tool HANDLERS remain in ReasoningService for now (they need the full service context).
"""
from kernos.kernel.tools.schemas import (
    INSPECT_STATE_TOOL,
    MANAGE_CAPABILITIES_TOOL,
    READ_DOC_TOOL,
    READ_SOURCE_TOOL,
    READ_SOUL_TOOL,
    REMEMBER_DETAILS_TOOL,
    REQUEST_TOOL,
    UPDATE_SOUL_TOOL,
    SOUL_UPDATABLE_FIELDS,
    read_doc,
    read_source,
)

__all__ = [
    "INSPECT_STATE_TOOL",
    "MANAGE_CAPABILITIES_TOOL",
    "READ_DOC_TOOL",
    "READ_SOURCE_TOOL",
    "READ_SOUL_TOOL",
    "REMEMBER_DETAILS_TOOL",
    "REQUEST_TOOL",
    "UPDATE_SOUL_TOOL",
    "SOUL_UPDATABLE_FIELDS",
    "read_doc",
    "read_source",
]

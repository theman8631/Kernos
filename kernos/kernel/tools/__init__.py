"""Kernel tool schemas and helpers.

Tool schemas are JSON dicts defining the tool's name, description, and input_schema.
Pure helper functions (_read_doc, _read_source) are co-located with their schemas.
Tool HANDLERS remain in ReasoningService for now (they need the full service context).
"""
from kernos.kernel.tools.schemas import (
    DIAGNOSE_LLM_CHAIN_TOOL,
    DIAGNOSE_MESSENGER_TOOL,
    INSPECT_PARCEL_TOOL,
    INSPECT_STATE_TOOL,
    LIST_PARCELS_TOOL,
    MANAGE_CAPABILITIES_TOOL,
    PACK_PARCEL_TOOL,
    READ_DOC_TOOL,
    READ_SOURCE_TOOL,
    READ_SOUL_TOOL,
    REMEMBER_DETAILS_TOOL,
    REQUEST_TOOL,
    RESPOND_TO_PARCEL_TOOL,
    SET_CHAIN_MODEL_TOOL,
    UPDATE_SOUL_TOOL,
    SOUL_UPDATABLE_FIELDS,
    read_doc,
    read_source,
)

__all__ = [
    "DIAGNOSE_LLM_CHAIN_TOOL",
    "DIAGNOSE_MESSENGER_TOOL",
    "INSPECT_PARCEL_TOOL",
    "INSPECT_STATE_TOOL",
    "LIST_PARCELS_TOOL",
    "MANAGE_CAPABILITIES_TOOL",
    "PACK_PARCEL_TOOL",
    "READ_DOC_TOOL",
    "READ_SOURCE_TOOL",
    "READ_SOUL_TOOL",
    "REMEMBER_DETAILS_TOOL",
    "REQUEST_TOOL",
    "RESPOND_TO_PARCEL_TOOL",
    "SET_CHAIN_MODEL_TOOL",
    "UPDATE_SOUL_TOOL",
    "SOUL_UPDATABLE_FIELDS",
    "read_doc",
    "read_source",
]

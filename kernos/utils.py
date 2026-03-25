"""Shared utilities for the KERNOS kernel and persistence layers."""


def _safe_name(s: str) -> str:
    """Convert a string to a safe filesystem name.

    Prevents path traversal and neutralizes dangerous characters.
    tenant_id and conversation_id come from user-controlled input and
    must be treated as untrusted.
    """
    # Remove path traversal
    s = s.replace("..", "")
    # Replace path separators and other dangerous chars
    s = s.replace("/", "_").replace("\\", "_").replace(":", "_")
    # Remove null bytes
    s = s.replace("\x00", "")
    # Ensure non-empty
    if not s or not s.strip():
        s = "_empty_"
    return s

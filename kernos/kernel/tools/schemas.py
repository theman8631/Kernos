"""Kernel tool JSON schemas and pure helper functions."""
REQUEST_TOOL = {
    "name": "request_tool",
    "description": (
        "Request activation of a tool capability that is NOT in your current tool set. "
        "Only use this as a LAST RESORT when no existing tool in your set can do what "
        "you need. If a tool like create-event or list-events is already available to "
        "you, call it directly — do not use request_tool."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "capability_name": {
                "type": "string",
                "description": (
                    "The name of the capability to activate, if known. "
                    "Use 'unknown' if you know what you need but not the exact name."
                ),
            },
            "description": {
                "type": "string",
                "description": (
                    "Thorough description of what you need the tool to do. "
                    "Be exhaustive — include the function needed, the context, "
                    "and why it's needed. This helps match the right tool."
                ),
            },
        },
        "required": ["capability_name", "description"],
    },
}


READ_DOC_TOOL = {
    "name": "read_doc",
    "description": (
        "Read Kernos documentation. Use when you need to understand a capability, "
        "behavior, or how the system works. Your docs are at docs/ — read the "
        "relevant section to answer accurately. "
        "Examples: 'index.md', 'capabilities/web-browsing.md', 'behaviors/covenants.md', "
        "'architecture/memory.md', 'identity/who-you-are.md', 'roadmap/vision.md'"
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Path relative to docs/. "
                    "Examples: 'index.md', 'capabilities/web-browsing.md', "
                    "'behaviors/covenants.md', 'architecture/context-spaces.md'"
                ),
            },
        },
        "required": ["path"],
    },
}


REMEMBER_DETAILS_TOOL = {
    "name": "remember_details",
    "description": (
        "Retrieve exact conversation text from a specific archived source log. "
        "Use after remember() when a Ledger entry includes 'source: log_NNN'. "
        "Optional query narrows to the relevant section within that log. "
        "This is a read-only operation — no state is changed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "source_ref": {
                "type": "string",
                "description": (
                    "The log reference to retrieve, e.g., 'log_003'. "
                    "Get this from a Ledger entry returned by remember()."
                ),
            },
            "query": {
                "type": "string",
                "description": (
                    "Optional keyword to find the relevant section within "
                    "the log. Returns matching lines with surrounding context. "
                    "If omitted, returns the full log (bounded)."
                ),
            },
        },
        "required": ["source_ref"],
    },
}


def read_doc(path: str) -> str:
    """Read a Kernos documentation file from docs/.

    Security: only allows reads within the docs/ directory.
    Rejects paths with '..', absolute paths, or paths outside docs/.
    """
    from pathlib import Path

    if path.startswith("/") or path.startswith("\\"):
        return "Error: Absolute paths are not allowed. Use a relative path like 'capabilities/web-browsing.md'."

    if ".." in path:
        return "Error: Path traversal ('..') is not allowed."

    # Resolve docs/ root relative to the repo
    import importlib
    kernos_root = Path(importlib.import_module("kernos").__file__).parent
    docs_root = kernos_root.parent / "docs"
    target = (docs_root / path).resolve()

    if not str(target).startswith(str(docs_root.resolve())):
        return "Error: Path resolves outside the docs/ directory."

    if not target.exists():
        # List available files to help the agent find the right one
        available = []
        for f in sorted(docs_root.rglob("*.md")):
            available.append(str(f.relative_to(docs_root)))
        hint = "\n".join(f"  - {a}" for a in available[:30])
        return f"Error: File not found: docs/{path}\n\nAvailable docs:\n{hint}"

    if not target.is_file():
        return f"Error: Not a file: docs/{path}"

    return target.read_text(encoding="utf-8")


MANAGE_CAPABILITIES_TOOL = {
    "name": "manage_capabilities",
    "description": (
        "Manage connected services — list, enable, disable, install, or remove capabilities. "
        "Use 'list' to see all services and their connection status. "
        "Use 'enable' or 'disable' to toggle a service on or off. "
        "Use 'install' to add a new MCP server. "
        "Use 'remove' to uninstall a user-added capability "
        "(defaults can only be disabled, not removed). "
        "Note: to see which tools are available, check the TOOLS section in your instructions — "
        "this command manages services, not individual tools."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["list", "enable", "disable", "install", "remove"],
                "description": "The action to perform.",
            },
            "capability": {
                "type": "string",
                "description": "The capability name (required for enable/disable/remove).",
            },
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


READ_SOURCE_TOOL = {
    "name": "read_source",
    "description": (
        "Read Kernos source code. Use when the user asks how something works "
        "technically or wants to see implementation details. Only reads files "
        "within the kernos/ package directory."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Relative path within the kernos/ package. "
                    "Examples: 'kernel/awareness.py', 'kernel/reasoning.py', "
                    "'messages/handler.py', 'capability/registry.py'"
                ),
            },
            "section": {
                "type": "string",
                "description": (
                    "Optional class or function name to extract. "
                    "Examples: 'AwarenessEvaluator', 'run_time_pass', '_gate_tool_call'. "
                    "If omitted, returns the full file."
                ),
            },
        },
        "required": ["path"],
    },
}


READ_SOUL_TOOL = {
    "name": "read_soul",
    "description": (
        "Read your own identity — who you are, your personality, your relationship "
        "with this user. Use this when you want to understand or verify your own state."
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


UPDATE_SOUL_TOOL = {
    "name": "update_soul",
    "description": (
        "Update your own identity — name, emoji, personality notes, communication "
        "style. Use when the user asks you to change something about yourself, or "
        "when you and the user agree on a change."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "field": {
                "type": "string",
                "description": (
                    "The soul field to update. Allowed: agent_name, emoji, "
                    "personality_notes, communication_style."
                ),
            },
            "value": {
                "type": "string",
                "description": "The new value for the field.",
            },
        },
        "required": ["field", "value"],
    },
}


INSPECT_STATE_TOOL = {
    "name": "inspect_state",
    "description": (
        "Inspect what Kernos currently believes is true about this user. "
        "Returns active preferences (with linked triggers/covenants), "
        "active triggers, behavioral rules, key facts, and connected "
        "capabilities. Use this when the user asks 'what notifications "
        "do I have?', 'what preferences are active?', or 'what do you "
        "know about me?'"
    ),
    "input_schema": {
        "type": "object",
        "properties": {},
        "required": [],
    },
}


# LLM-SETUP-AND-FALLBACK admin tools — system space, admin-only.
# Gate-authorized. No LLM call in the handler path.

SET_CHAIN_MODEL_TOOL = {
    "name": "set_chain_model",
    "description": (
        "Admin-only. Change the model used by a (chain, provider) pair in "
        "the on-disk LLM chain config. Validates that the provider is "
        "configured and that the model appears in the provider's /models "
        "response before writing. Emits a CHAIN_MODEL_CHANGED event. "
        "Use this when the user wants to swap the model for a provider "
        "they've already set up, without re-running `kernos setup llm`."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "chain": {
                "type": "string",
                "enum": ["primary", "simple", "cheap"],
                "description": "The chain tier to change.",
            },
            "provider_id": {
                "type": "string",
                "description": (
                    "Provider id from the registry (anthropic, openai, "
                    "google, groq, xai, openrouter, ollama)."
                ),
            },
            "model_id": {
                "type": "string",
                "description": "The new model id (e.g. 'claude-opus-4-7').",
            },
        },
        "required": ["chain", "provider_id", "model_id"],
        "additionalProperties": False,
    },
}


DIAGNOSE_LLM_CHAIN_TOOL = {
    "name": "diagnose_llm_chain",
    "description": (
        "Admin-only. Return a readable view of the current LLM chain "
        "configuration: every chain, providers in fallback order, the "
        "model per provider, the backing storage backend, and whether each "
        "provider has a stored credential. Optionally include recent "
        "FALLBACK_USED events for diagnostic purposes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "include_fallback_events": {
                "type": "boolean",
                "description": (
                    "Also include recent FALLBACK_USED events (last 50). "
                    "Default false."
                ),
            },
        },
        "additionalProperties": False,
    },
}


# Allowed fields for update_soul — lifecycle and user fields are read-only
SOUL_UPDATABLE_FIELDS = {"agent_name", "emoji", "personality_notes", "communication_style"}


def read_source(path: str, section: str = "") -> str:
    """Read Kernos source code. Returns file contents or extracted section.

    Security: only allows reads within the kernos/ package directory.
    Rejects paths with '..', absolute paths, or paths outside kernos/.
    """
    import importlib
    from pathlib import Path

    # Security: reject absolute paths
    if path.startswith("/") or path.startswith("\\"):
        return "Error: Absolute paths are not allowed. Use a relative path like 'kernel/awareness.py'."

    # Security: reject path traversal
    if ".." in path:
        return "Error: Path traversal ('..') is not allowed."

    # Resolve kernos package root
    kernos_root = Path(importlib.import_module("kernos").__file__).parent
    target = (kernos_root / path).resolve()

    # Security: ensure resolved path is within kernos/
    if not str(target).startswith(str(kernos_root)):
        return "Error: Path resolves outside the kernos/ package directory."

    if not target.exists():
        return f"Error: File not found: kernos/{path}"

    if not target.is_file():
        return f"Error: Not a file: kernos/{path}"

    if target.suffix not in (".py", ".md", ".txt", ".json", ".toml", ".yaml", ".yml"):
        return f"Error: Unsupported file type: {target.suffix}"

    content = target.read_text(encoding="utf-8")

    if not section:
        # Cap at 500 lines for full files
        lines = content.split("\n")
        if len(lines) > 500:
            return "\n".join(lines[:500]) + f"\n\n... (truncated — {len(lines)} total lines)"
        return content

    # Extract a class or function section
    lines = content.split("\n")
    start_idx = None
    start_indent = None

    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith(f"class {section}") or stripped.startswith(f"def {section}"):
            start_idx = i
            start_indent = len(line) - len(stripped)
            break
        # Also match async def
        if stripped.startswith(f"async def {section}"):
            start_idx = i
            start_indent = len(line) - len(stripped)
            break

    if start_idx is None:
        return f"Error: Section '{section}' not found in kernos/{path}"

    # Find end: next definition at same or lower indent level
    result_lines = [lines[start_idx]]
    for i in range(start_idx + 1, len(lines)):
        line = lines[i]
        if not line.strip():
            result_lines.append(line)
            continue
        current_indent = len(line) - len(line.lstrip())
        stripped = line.lstrip()
        # Same-level or lower-level class/def = end of section
        if current_indent <= start_indent and (
            stripped.startswith("class ")
            or stripped.startswith("def ")
            or stripped.startswith("async def ")
            or stripped.startswith("# ---")
        ):
            break
        result_lines.append(line)

    # Strip trailing blank lines
    while result_lines and not result_lines[-1].strip():
        result_lines.pop()

    return "\n".join(result_lines)


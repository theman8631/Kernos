"""Universal Tool Catalog — registry of all available tools with one-line descriptions.

Replaces keyword-based CATEGORY_TOOLS. The surfacer LLM reads this catalog
to determine which tools are relevant for a given turn.
"""
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class CatalogEntry:
    """A tool in the universal catalog."""
    name: str              # tool name (unique)
    description: str       # one-line description for surfacer
    source: str            # "kernel" | "mcp:{server}" | "workspace"
    registered_at: str = ""


# Common tools loaded every turn without LLM call (Tier 1)
COMMON_TOOL_NAMES: set[str] = {
    "get-current-time",
    "create-event",
    "list-events",
    "brave_web_search",
    "brave_local_search",
    "remember_details",
    "inspect_state",
    "read_doc",
    "manage_capabilities",
    "request_tool",
}

# Kernel tools that are always in context (not in the catalog — always surfaced)
ALWAYS_SURFACE_KERNEL: set[str] = {
    "request_tool",
    "read_doc",
    "dismiss_whisper",
    "manage_capabilities",
    "remember_details",
    "remember",
}


SURFACER_SCHEMA = {
    "type": "object",
    "properties": {
        "tools": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Tool names relevant to this request",
        },
    },
    "required": ["tools"],
    "additionalProperties": False,
}


class ToolCatalog:
    """Registry of all available tools with one-line descriptions and version tracking."""

    def __init__(self) -> None:
        self._entries: dict[str, CatalogEntry] = {}
        self.version: int = 0

    def register(self, name: str, description: str, source: str, registered_at: str = "") -> None:
        """Register a tool. Increments version if this is a new tool."""
        is_new = name not in self._entries
        self._entries[name] = CatalogEntry(
            name=name, description=description,
            source=source, registered_at=registered_at,
        )
        if is_new:
            self.version += 1
            logger.info("TOOL_CATALOG: registered=%s source=%s version=%d", name, source, self.version)

    def unregister(self, name: str) -> None:
        """Remove a tool. Increments version if the tool existed."""
        if name in self._entries:
            del self._entries[name]
            self.version += 1
            logger.info("TOOL_CATALOG: unregistered=%s version=%d", name, self.version)

    def get(self, name: str) -> CatalogEntry | None:
        return self._entries.get(name)

    def get_all(self) -> list[CatalogEntry]:
        return list(self._entries.values())

    def get_names(self) -> set[str]:
        return set(self._entries.keys())

    def build_catalog_text(self, exclude: set[str] | None = None) -> str:
        """Build a compact text listing of all tools for the surfacer LLM."""
        lines = []
        _exclude = exclude or set()
        for entry in sorted(self._entries.values(), key=lambda e: e.name):
            if entry.name not in _exclude:
                lines.append(f"- {entry.name}: {entry.description}")
        return "\n".join(lines)

    def get_tools_since_version(self, since_version: int) -> list[CatalogEntry]:
        """Get tools added since a given version. Approximation: returns all if version gap exists."""
        # Since we don't track per-entry version, return all entries when version mismatch detected.
        # This is fine — the scan LLM filters for relevance.
        return self.get_all()

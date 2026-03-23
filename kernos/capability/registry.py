"""Capability Registry — the three-tier capability graph.

Tier 1 — Connected: MCP server running, tools discovered, ready to use.
Tier 2 — Available: Known capability, not yet connected. Agent can offer setup.
Tier 3 — Discoverable: Exists in ecosystem. Phase 4 — not implemented.
"""
import dataclasses
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from kernos.capability.client import MCPClientManager
    from kernos.kernel.spaces import ContextSpace


class CapabilityStatus(str, Enum):
    CONNECTED = "connected"        # Working, authenticated, tools available
    AVAILABLE = "available"        # Known, could be connected, not yet set up
    DISCOVERABLE = "discoverable"  # Exists in ecosystem, not configured (Phase 4)
    ERROR = "error"                # Was connected, currently failing
    SUPPRESSED = "suppressed"      # Explicitly uninstalled by user — hidden from prompts
    DISABLED = "disabled"          # MCP server still running, but hidden from LLM tool list


@dataclass
class CapabilityInfo:
    """A capability the system knows about, regardless of connection status."""

    name: str            # "google-calendar", "gmail", "web-search"
    display_name: str    # "Google Calendar", "Gmail", "Web Search"
    description: str     # Human-readable: "Check schedule, list events, find availability"
    category: str        # "calendar", "email", "search", "files", "communication"
    status: CapabilityStatus
    tools: list[str] = field(default_factory=list)          # Tool names when connected
    setup_hint: str = ""                                     # What the agent tells the user
    setup_requires: list[str] = field(default_factory=list) # Required env vars / credentials
    server_name: str = ""                                    # MCP server name (for connected)
    error_message: str = ""                                  # If status is ERROR, what went wrong
    tool_effects: dict[str, str] = field(default_factory=dict)
    # Maps tool_name → effect level: "read" | "soft_write" | "hard_write" | "unknown"
    # Tools not in this dict default to "unknown" (treated as hard_write by Dispatch Interceptor)
    source: str = "default"        # "default" (from known.py) or "user" (installed at runtime)
    universal: bool = False
    # If True, visible in all spaces without explicit activation (system defaults).
    # Set at registration/install time.
    requires_web_interface: bool = False
    # If True, cannot be installed via text/Discord — needs browser OAuth redirect.
    server_command: str = ""           # e.g., "npx"
    server_args: list[str] = field(default_factory=list)   # e.g., ["@cocal/google-calendar-mcp"]
    credentials_key: str = ""         # e.g., "google-calendar" — key file name in secrets/
    env_template: dict[str, str] = field(default_factory=dict)  # e.g., {"GOOGLE_OAUTH_CREDENTIALS": "{credentials}"}


# Calendar read tools — most-called MCP tools, always have full schemas in context.
PRELOADED_TOOLS: set[str] = {
    "list-events",
    "search-events",
    "get-event",
    "get-freebusy",
    "list-calendars",
    "get-current-time",
}


class CapabilityRegistry:
    """Three-tier capability registry. Holds a reference to MCPClientManager for tool lookup."""

    def __init__(self, mcp: "MCPClientManager | None" = None) -> None:
        self._capabilities: dict[str, CapabilityInfo] = {}
        self._mcp = mcp

    def register(self, capability: CapabilityInfo) -> None:
        """Add or update a capability. Stores an independent copy."""
        # TODO: call _update_capabilities_overview() on install/remove (3B+)
        self._capabilities[capability.name] = dataclasses.replace(
            capability,
            tools=list(capability.tools),
            setup_requires=list(capability.setup_requires),
        )

    def get(self, name: str) -> CapabilityInfo | None:
        """Get a specific capability by name."""
        return self._capabilities.get(name)

    def get_by_server_name(self, server_name: str) -> CapabilityInfo | None:
        """Get a capability by its MCP server_name field."""
        for cap in self._capabilities.values():
            if cap.server_name == server_name:
                return cap
        return None

    def get_all(self) -> list[CapabilityInfo]:
        """All known capabilities, any status."""
        return list(self._capabilities.values())

    def get_connected(self) -> list[CapabilityInfo]:
        """Capabilities with active MCP connections and working tools."""
        return [c for c in self._capabilities.values() if c.status == CapabilityStatus.CONNECTED]

    def get_available(self) -> list[CapabilityInfo]:
        """Capabilities that could be connected but aren't yet."""
        return [c for c in self._capabilities.values() if c.status == CapabilityStatus.AVAILABLE]

    def get_disabled(self) -> list[CapabilityInfo]:
        """Capabilities that are disabled (MCP warm, hidden from LLM)."""
        return [c for c in self._capabilities.values() if c.status == CapabilityStatus.DISABLED]

    def disable(self, name: str) -> bool:
        """Disable a capability — hide from LLM but keep MCP warm.

        Returns True if the capability was found and disabled.
        Only CONNECTED capabilities can be disabled.
        """
        cap = self._capabilities.get(name)
        if not cap or cap.status != CapabilityStatus.CONNECTED:
            return False
        cap.status = CapabilityStatus.DISABLED
        logger.info("CAP_WRITE: name=%s action=DISABLE source=registry", name)
        return True

    def enable(self, name: str) -> bool:
        """Re-enable a disabled capability — restore to CONNECTED instantly.

        Returns True if the capability was found and re-enabled.
        Only DISABLED capabilities can be enabled this way.
        """
        cap = self._capabilities.get(name)
        if not cap or cap.status != CapabilityStatus.DISABLED:
            return False
        cap.status = CapabilityStatus.CONNECTED
        logger.info("CAP_WRITE: name=%s action=ENABLE source=registry", name)
        return True

    def get_by_category(self, category: str) -> list[CapabilityInfo]:
        """All capabilities in a category, any status."""
        return [c for c in self._capabilities.values() if c.category == category]

    def get_connected_tools(self) -> list[dict]:
        """Aggregate tool definitions from all connected capabilities (unfiltered).

        Use get_tools_for_space() instead when serving the message handler.
        Kept for backward compatibility.
        """
        if self._mcp:
            return self._mcp.get_tools()
        return []

    def get_connected_capability_names(self) -> list[str]:
        """Names of all connected capabilities. Used by Gate 2 seeding."""
        return [c.name for c in self.get_connected()]

    def get_capability_descriptions(self) -> str:
        """Formatted descriptions of connected capabilities. Used by Gate 2 prompt."""
        connected = self.get_connected()
        if not connected:
            return "No tools currently installed."
        lines = []
        for cap in connected:
            lines.append(f"- {cap.name}: {cap.description}")
        return "\n".join(lines)

    def _visible_capability_names(self, space: "ContextSpace | None") -> set[str]:
        """Which connected capability names are visible in this space.

        System space: everything connected and not disabled.
        Space with active_tools: kernel defaults (always) + universal + explicitly listed.
        Space with empty active_tools: universal only (kernel tools handled separately).
        Disabled capabilities are always excluded — MCP stays warm but tools hidden.
        """
        if space and space.space_type == "system":
            return {
                c.name for c in self._capabilities.values()
                if c.status == CapabilityStatus.CONNECTED
            }

        visible: set[str] = set()
        for cap in self._capabilities.values():
            if cap.status != CapabilityStatus.CONNECTED:
                continue
            if cap.universal:
                visible.add(cap.name)
        if space and space.active_tools:
            for name in space.active_tools:
                cap = self._capabilities.get(name)
                if cap and cap.status == CapabilityStatus.CONNECTED:
                    visible.add(name)
        return visible

    def get_tools_for_space(self, space: "ContextSpace | None" = None) -> list[dict]:
        """Return MCP tool definitions visible for this space.

        Replaces get_connected_tools() in the message handler — same format,
        filtered by space visibility.
        """
        if not self._mcp:
            return []

        tool_defs_by_server = self._mcp.get_tool_definitions()
        visible_names = self._visible_capability_names(space)

        result = []
        for cap_name in visible_names:
            cap = self._capabilities.get(cap_name)
            if not cap or cap.status != CapabilityStatus.CONNECTED:
                continue
            server_tools = tool_defs_by_server.get(cap.server_name, [])
            result.extend(server_tools)
        return result

    def get_tool_schema(self, tool_name: str) -> dict | None:
        """Get the full schema for a specific tool by name."""
        if not self._mcp:
            return None
        for tool in self._mcp.get_tools():
            if tool["name"] == tool_name:
                return tool
        return None

    def get_preloaded_tools(self, space: "ContextSpace | None" = None) -> list[dict]:
        """Get full schemas for pre-loaded MCP tools only (calendar reads).

        Respects space visibility — only returns tools from visible capabilities.
        """
        visible_names = self._visible_capability_names(space)
        if not self._mcp:
            return []
        result = []
        tool_defs_by_server = self._mcp.get_tool_definitions()
        for cap_name in visible_names:
            cap = self._capabilities.get(cap_name)
            if not cap or cap.status != CapabilityStatus.CONNECTED:
                continue
            for tool in tool_defs_by_server.get(cap.server_name, []):
                if tool["name"] in PRELOADED_TOOLS:
                    result.append(tool)
        return result

    def get_all_tool_names(self, space: "ContextSpace | None" = None) -> set[str]:
        """Get all available MCP tool names for a space (for directory validation)."""
        visible_names = self._visible_capability_names(space)
        if not self._mcp:
            return set()
        names: set[str] = set()
        tool_defs_by_server = self._mcp.get_tool_definitions()
        for cap_name in visible_names:
            cap = self._capabilities.get(cap_name)
            if not cap or cap.status != CapabilityStatus.CONNECTED:
                continue
            for tool in tool_defs_by_server.get(cap.server_name, []):
                names.add(tool["name"])
        return names

    def build_tool_directory(self, space: "ContextSpace | None" = None) -> str:
        """Build a compact tool directory for the system prompt.

        Lists available capabilities by category with one-line descriptions.
        Full schemas are NOT included — they load on demand via lazy loading.
        """
        visible_names = self._visible_capability_names(space)
        visible_connected = [
            c for c in self._capabilities.values()
            if c.status == CapabilityStatus.CONNECTED and c.name in visible_names
        ]
        available = [
            c for c in self._capabilities.values()
            if c.status == CapabilityStatus.AVAILABLE
        ]

        if not visible_connected and not available:
            return (
                "CURRENT CAPABILITIES — only claim these:\n"
                "- Conversation: answer questions, discuss topics, help think through problems.\n"
                "That is ALL you can do right now."
            )

        lines = ["AVAILABLE TOOLS:"]

        for cap in visible_connected:
            tool_count = len(cap.tool_effects) if cap.tool_effects else "?"
            lines.append(f"• {cap.display_name}: {cap.description} ({tool_count} tools)")

        if available:
            lines.append("")
            lines.append("NOT YET CONNECTED (offer to set up if asked):")
            for cap in available:
                lines.append(f"• {cap.display_name}: {cap.description}")

        lines.append("")
        lines.append("To use any tool, call it by name. The system loads it automatically.")
        lines.append("You cannot do anything beyond what's listed above. Be honest about limits.")

        return "\n".join(lines)

    def build_capability_prompt(self, space: "ContextSpace | None" = None) -> str:
        """Build the CAPABILITIES section of the system prompt.

        System space: all connected capabilities shown.
        Other spaces: only visible capabilities shown (universal + active_tools).
        AVAILABLE (not disabled) capabilities shown so agent can offer setup.
        Disabled capabilities excluded from both connected and available lists.
        """
        available = [
            c for c in self._capabilities.values()
            if c.status == CapabilityStatus.AVAILABLE
        ]
        if space and space.space_type == "system":
            return self._build_prompt_for_capabilities(self.get_connected(), available)

        visible_names = self._visible_capability_names(space)
        visible_connected = [
            c for c in self._capabilities.values()
            if c.status == CapabilityStatus.CONNECTED and c.name in visible_names
        ]
        return self._build_prompt_for_capabilities(visible_connected, available)

    def _build_prompt_for_capabilities(
        self,
        connected: list[CapabilityInfo],
        available: list[CapabilityInfo],
    ) -> str:
        """Build the prompt string from capability lists."""
        if not connected and not available:
            return (
                "CURRENT CAPABILITIES — only claim these:\n"
                "- Conversation: answer questions, discuss topics, help think through problems, brainstorm ideas.\n"
                "That is ALL you can do right now. You cannot check calendars, set reminders, send emails, "
                "do web research, manage files, or take any actions. "
                "If asked about these, be honest that you don't have those capabilities yet — "
                "don't pretend or make things up. It's fine to mention that more capabilities are coming."
            )

        parts: list[str] = []

        if connected:
            lines = ["CONNECTED CAPABILITIES — you can use these:"]
            for cap in connected:
                lines.append(f"- {cap.display_name}: {cap.description}")
            parts.append("\n".join(lines))
        else:
            parts.append(
                "CURRENT CAPABILITIES — only claim these:\n"
                "- Conversation: answer questions, discuss topics, help think through problems."
            )

        if available:
            lines = ["AVAILABLE CAPABILITIES — not connected yet, offer to set these up if the user asks:"]
            for cap in available:
                hint = f' Setup: "{cap.setup_hint}"' if cap.setup_hint else ""
                lines.append(f"- {cap.display_name}: {cap.description}.{hint}")
            parts.append("\n".join(lines))

        parts.append("You cannot do anything beyond what's listed above. Be honest about limits.")
        return "\n\n".join(parts)

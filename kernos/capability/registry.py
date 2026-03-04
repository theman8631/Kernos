"""Capability Registry — the three-tier capability graph.

Tier 1 — Connected: MCP server running, tools discovered, ready to use.
Tier 2 — Available: Known capability, not yet connected. Agent can offer setup.
Tier 3 — Discoverable: Exists in ecosystem. Phase 4 — not implemented.
"""
import dataclasses
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kernos.capability.client import MCPClientManager


class CapabilityStatus(str, Enum):
    CONNECTED = "connected"        # Working, authenticated, tools available
    AVAILABLE = "available"        # Known, could be connected, not yet set up
    DISCOVERABLE = "discoverable"  # Exists in ecosystem, not configured (Phase 4)
    ERROR = "error"                # Was connected, currently failing


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


class CapabilityRegistry:
    """Three-tier capability registry. Holds a reference to MCPClientManager for tool lookup."""

    def __init__(self, mcp: "MCPClientManager | None" = None) -> None:
        self._capabilities: dict[str, CapabilityInfo] = {}
        self._mcp = mcp

    def register(self, capability: CapabilityInfo) -> None:
        """Add or update a capability. Stores an independent copy."""
        self._capabilities[capability.name] = dataclasses.replace(
            capability,
            tools=list(capability.tools),
            setup_requires=list(capability.setup_requires),
        )

    def get(self, name: str) -> CapabilityInfo | None:
        """Get a specific capability by name."""
        return self._capabilities.get(name)

    def get_all(self) -> list[CapabilityInfo]:
        """All known capabilities, any status."""
        return list(self._capabilities.values())

    def get_connected(self) -> list[CapabilityInfo]:
        """Capabilities with active MCP connections and working tools."""
        return [c for c in self._capabilities.values() if c.status == CapabilityStatus.CONNECTED]

    def get_available(self) -> list[CapabilityInfo]:
        """Capabilities that could be connected but aren't yet."""
        return [c for c in self._capabilities.values() if c.status == CapabilityStatus.AVAILABLE]

    def get_by_category(self, category: str) -> list[CapabilityInfo]:
        """All capabilities in a category, any status."""
        return [c for c in self._capabilities.values() if c.category == category]

    def get_connected_tools(self) -> list[dict]:
        """Aggregate tool definitions from all connected capabilities.

        Delegates to MCPClientManager. Returns same format as mcp.get_tools().
        """
        if self._mcp:
            return self._mcp.get_tools()
        return []

    def build_capability_prompt(self) -> str:
        """Build the CAPABILITIES section of the system prompt from registry data.

        Connected capabilities: listed with descriptions and tool instructions.
        Available capabilities: listed with setup hints so the agent can offer them.
        """
        connected = self.get_connected()
        available = self.get_available()

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

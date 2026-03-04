# SPEC-1B3: Capability Graph Formalization

**Status:** READY FOR IMPLEMENTATION
**Depends on:** 1B.2 (Reasoning Service) — complete
**Objective:** Transform the flat tool list into a structured capability registry with three tiers: connected (working now), available (could be connected), and discoverable (exists in ecosystem, future). After this, the agent always knows what it can do, what it could do, and how to get there. Adding a new capability means adding a registry entry — not editing the system prompt builder.

**What this solves right now:**

Today, when the user asks "can you check my email?" the agent says "I can't do that." After 1B.3, it says "I have an email integration available — want me to help you connect it?" That's the difference between a tool the user outgrows and an OS that grows with them.

Today, the system prompt builder has hardcoded calendar detection (`"calendar" in n.lower()`). After 1B.3, the prompt is built from structured capability metadata. Adding email or web search means adding a registry entry, not touching the prompt builder.

**Zero-cost-path:** With one connected capability (calendar), the registry is a thin wrapper. `get_connected_tools()` returns the same tool list as today's `get_tools()`. The registry adds value through the available tier and structured metadata, not through computational overhead on the hot path.

---

## Component 1: Capability Data Model

**New file:** `kernos/capability/registry.py`

```python
from dataclasses import dataclass, field
from enum import Enum


class CapabilityStatus(str, Enum):
    CONNECTED = "connected"       # Working, authenticated, tools available
    AVAILABLE = "available"       # Known, could be connected, not yet set up
    DISCOVERABLE = "discoverable" # Exists in ecosystem, not configured (Phase 4)
    ERROR = "error"               # Was connected, currently failing


@dataclass
class CapabilityInfo:
    """A capability the system knows about, regardless of connection status."""

    name: str                     # "google-calendar", "gmail", "web-search"
    display_name: str             # "Google Calendar", "Gmail", "Web Search"
    description: str              # Human-readable: "Check schedule, list events, find availability"
    category: str                 # "calendar", "email", "search", "files", "communication"
    status: CapabilityStatus
    tools: list[str] = field(default_factory=list)          # Tool names when connected
    setup_hint: str = ""          # What the agent tells the user: "I'll need access to your Google account"
    setup_requires: list[str] = field(default_factory=list) # ["GOOGLE_OAUTH_CREDENTIALS_PATH"]
    server_name: str = ""         # MCP server name (for connected capabilities)
    error_message: str = ""       # If status is ERROR, what went wrong
```

**Key design decisions:**

- `name` is the identifier. Unique across the registry.
- `display_name` and `description` are what the agent uses in conversation and the system prompt. The agent says "Google Calendar" not "google-calendar."
- `category` enables grouping. The system prompt can say "Calendar capabilities: ..." and "Email capabilities: ..." without parsing tool names.
- `setup_hint` is what the agent tells the user when they ask about an available capability. "I can connect to your Gmail — I'll need access to your Google account. Want me to walk you through it?"
- `setup_requires` is the list of env vars or credentials needed. Not shown to the user — used by the kernel to determine if a capability CAN be connected. If `GOOGLE_OAUTH_CREDENTIALS_PATH` is set but email MCP isn't registered, the kernel knows the credential exists but the server isn't configured.
- `tools` is populated from MCP tool discovery for connected capabilities. Empty for available/discoverable.

---

## Component 2: Capability Registry

**Same file:** `kernos/capability/registry.py`

```python
class CapabilityRegistry:
    """The three-tier capability graph.

    Tier 1 — Connected: MCP server running, tools discovered, ready to use.
    Tier 2 — Available: Known capability, not yet connected. Agent can offer setup.
    Tier 3 — Discoverable: Exists in ecosystem. Phase 4 — not implemented.
    """

    def __init__(self) -> None:
        self._capabilities: dict[str, CapabilityInfo] = {}

    def register(self, capability: CapabilityInfo) -> None:
        """Add or update a capability in the registry."""
        self._capabilities[capability.name] = capability

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

        Returns the same format as MCPClientManager.get_tools() — this is
        the backward-compatible path. Anything that called mcp.get_tools()
        can call registry.get_connected_tools() instead.
        """
        # Delegates to MCPClientManager internally — see Component 3
        ...

    def build_capability_prompt(self) -> str:
        """Build the CAPABILITIES section of the system prompt from registry data.

        Connected capabilities: listed with descriptions and tool instructions.
        Available capabilities: listed with setup hints so the agent can offer them.
        """
        ...
```

**`build_capability_prompt()` output example:**

```
CONNECTED CAPABILITIES — you can use these:
- Google Calendar: Check your schedule, list events, find availability. Always use calendar tools when asked about schedule, events, or appointments — never guess from memory.

AVAILABLE CAPABILITIES — not connected yet, offer to set these up if the user asks:
- Gmail: Read, categorize, and draft email responses. Setup: "I can connect to your Gmail — I'll need access to your Google account."
- Web Search: Search the internet for current information. Setup: "I can add web search — want me to set that up?"

You cannot do anything beyond what's listed above. Be honest about limits.
```

This replaces the entire hardcoded `if has_calendar:` block in the handler's `_build_system_prompt()`.

---

## Component 3: Registry + MCPClientManager Integration

The registry wraps MCPClientManager but doesn't replace it. The MCP client still manages connections and tool calls. The registry adds the metadata layer.

**Modified file:** `kernos/capability/client.py`

Add a method to MCPClientManager:

```python
def get_tool_definitions(self) -> dict[str, list[dict]]:
    """Return tool definitions grouped by server name.

    Returns: {"google-calendar": [{"name": "get-events", ...}, ...]}
    """
    result: dict[str, list[dict]] = {}
    for tool in self._tools:
        server = self._tool_to_session.get(tool["name"], "unknown")
        result.setdefault(server, []).append(tool)
    return result
```

This lets the registry know which tools belong to which capability (server).

**Registry initialization pattern (in app.py / discord_bot.py startup):**

```python
from kernos.capability.registry import CapabilityRegistry, CapabilityInfo, CapabilityStatus

registry = CapabilityRegistry()

# Register available capabilities (always present, regardless of config)
registry.register(CapabilityInfo(
    name="gmail",
    display_name="Gmail",
    description="Read, categorize, and draft email responses",
    category="email",
    status=CapabilityStatus.AVAILABLE,
    setup_hint="I can connect to your Gmail — I'll need access to your Google account.",
    setup_requires=["GOOGLE_OAUTH_CREDENTIALS_PATH"],
))

registry.register(CapabilityInfo(
    name="web-search",
    display_name="Web Search",
    description="Search the internet for current information",
    category="search",
    status=CapabilityStatus.AVAILABLE,
    setup_hint="I can add web search — want me to set that up?",
    setup_requires=[],
))

# Register and connect Google Calendar
registry.register(CapabilityInfo(
    name="google-calendar",
    display_name="Google Calendar",
    description="Check your schedule, list events, find availability",
    category="calendar",
    status=CapabilityStatus.AVAILABLE,  # starts as available
    setup_hint="I can connect to your Google Calendar.",
    setup_requires=["GOOGLE_OAUTH_CREDENTIALS_PATH"],
    server_name="google-calendar",
))

# After MCP connect_all():
# Update connected capabilities with discovered tools
for server_name, tools in mcp_manager.get_tool_definitions().items():
    cap = registry.get(server_name)
    if cap:
        cap.status = CapabilityStatus.CONNECTED
        cap.tools = [t["name"] for t in tools]
    # If connect failed, the capability stays AVAILABLE (or set to ERROR)
```

**`get_connected_tools()` implementation in registry:**

```python
def get_connected_tools(self, mcp: MCPClientManager) -> list[dict]:
    """Delegates to MCPClientManager for actual tool definitions."""
    return mcp.get_tools()
```

Or simpler — the registry holds a reference to the MCPClientManager:

```python
class CapabilityRegistry:
    def __init__(self, mcp: MCPClientManager | None = None) -> None:
        self._capabilities: dict[str, CapabilityInfo] = {}
        self._mcp = mcp

    def get_connected_tools(self) -> list[dict]:
        if self._mcp:
            return self._mcp.get_tools()
        return []
```

**Decision: The registry holds a reference to the MCPClientManager.** This keeps the interface clean — callers ask the registry for everything. The registry delegates to MCP for tool definitions and tool execution. The MCP client becomes an implementation detail of the capability layer.

---

## Component 4: System Prompt Refactoring

**Modified file:** `kernos/messages/handler.py`

Replace the entire capability section of `_build_system_prompt()`.

**Before:** Handler calls `self.mcp.get_tools()`, parses tool names looking for "calendar" or "event," builds hardcoded capability text.

**After:** Handler calls `self.registry.build_capability_prompt()` which returns a complete capability section built from structured metadata.

The function signature changes:

```python
# Before
def _build_system_prompt(message: NormalizedMessage, tools: list[dict] | None = None) -> str:

# After
def _build_system_prompt(message: NormalizedMessage, capability_prompt: str) -> str:
```

The handler's `process()` method changes:

```python
# Before
tools = self.mcp.get_tools()
system_prompt = _build_system_prompt(message, tools)

# After
tools = self.registry.get_connected_tools()
capability_prompt = self.registry.build_capability_prompt()
system_prompt = _build_system_prompt(message, capability_prompt)
```

The handler still passes `tools` to the ReasoningRequest — the reasoning service needs the actual tool definitions for the API call. But the system prompt no longer parses tool names.

**What the handler constructor gains:**

```python
def __init__(
    self,
    mcp: MCPClientManager,
    conversations: ConversationStore,
    tenants: TenantStore,
    audit: AuditStore,
    events: EventStream,
    state: StateStore,
    reasoning: ReasoningService,
    registry: CapabilityRegistry,    # NEW
) -> None:
```

Note: The handler still takes `mcp` for now because the reasoning service (which owns the tool-use loop) uses it. When the registry fully wraps MCP, the handler can drop the direct MCP reference. But that's a future cleanup, not a 1B.3 requirement.

---

## Component 5: State Store Integration

**On capability status changes**, update the tenant's profile.

During startup, after capabilities are resolved (connected or remained available), update each tenant's capabilities in the State Store:

```python
# In handler._ensure_tenant_state() or startup code:
profile.capabilities = {
    cap.name: cap.status.value
    for cap in registry.get_all()
}
```

This means `./kernos-cli profile <tenant_id>` shows:

```json
"capabilities": {
    "google-calendar": "connected",
    "gmail": "available",
    "web-search": "available"
}
```

**Emit capability events from the registry** when status changes. The MCPClientManager already emits `capability.connected` and `capability.error` events. The registry should also emit when capabilities transition between statuses. However, for 1B.3 where status changes only happen at startup, the existing MCPClientManager events are sufficient. Runtime status changes (OAuth expiration, server crash) come in later phases.

---

## Component 6: Known Capabilities Definition

**New file:** `kernos/capability/known.py`

A catalog of capabilities the system knows about. This is the "available" tier — capabilities that exist and could be connected. Separate file so adding a new capability is adding an entry to this list, not editing startup code.

```python
"""Known capabilities catalog.

Adding a new capability to KERNOS:
1. Add a CapabilityInfo entry here
2. Add the MCP server registration in app.py/discord_bot.py (if server exists)
3. The registry handles the rest — system prompt, State Store, CLI
"""

from kernos.capability.registry import CapabilityInfo, CapabilityStatus

KNOWN_CAPABILITIES: list[CapabilityInfo] = [
    CapabilityInfo(
        name="google-calendar",
        display_name="Google Calendar",
        description="Check your schedule, list events, find availability. "
                    "Always use calendar tools when asked about schedule, events, "
                    "or appointments — never guess from memory.",
        category="calendar",
        status=CapabilityStatus.AVAILABLE,
        setup_hint="I can connect to your Google Calendar — I'll need access to your Google account.",
        setup_requires=["GOOGLE_OAUTH_CREDENTIALS_PATH"],
        server_name="google-calendar",
    ),
    CapabilityInfo(
        name="gmail",
        display_name="Gmail",
        description="Read, categorize, and draft email responses",
        category="email",
        status=CapabilityStatus.AVAILABLE,
        setup_hint="I can connect to your Gmail — I'll need access to your Google account.",
        setup_requires=["GOOGLE_OAUTH_CREDENTIALS_PATH"],
        server_name="gmail",
    ),
    CapabilityInfo(
        name="web-search",
        display_name="Web Search",
        description="Search the internet for current information",
        category="search",
        status=CapabilityStatus.AVAILABLE,
        setup_hint="I can add web search — want me to set that up?",
        setup_requires=[],
        server_name="",
    ),
]
```

**Startup code uses this:**

```python
from kernos.capability.known import KNOWN_CAPABILITIES

registry = CapabilityRegistry(mcp=mcp_manager)
for cap in KNOWN_CAPABILITIES:
    registry.register(cap)

# After connect_all(), update connected capabilities
...
```

Adding a new capability is: add entry to `known.py`, add MCP server registration if the server exists. The system prompt, CLI, and State Store all pick it up automatically.

---

## Component 7: CLI Extension

**Modified file:** `kernos/cli.py`

Add a `capabilities` subcommand:

```bash
./kernos-cli capabilities
```

Output:

```
────────────────────────────────────────────────────────────
  Capability Registry
────────────────────────────────────────────────────────────

  [CONNECTED] Google Calendar
      Check your schedule, list events, find availability
      Tools: get-current-time, list-calendars, get-events, ...
      Server: google-calendar

  [AVAILABLE] Gmail
      Read, categorize, and draft email responses
      Setup: I can connect to your Gmail — I'll need access to your Google account.
      Requires: GOOGLE_OAUTH_CREDENTIALS_PATH

  [AVAILABLE] Web Search
      Search the internet for current information
      Setup: I can add web search — want me to set that up?
```

**Note:** This command reads from the known capabilities catalog and MCP state, not from a tenant's State Store. Capability availability is system-wide (which servers are running), not per-tenant. Per-tenant capability state in the profile is an informational mirror.

**Implementation:** The CLI can import `KNOWN_CAPABILITIES` and check which servers are actually connected by looking at the MCP manager state, or simply read the known catalog and display statuses. Since the CLI runs outside the bot process, it can't check live MCP connections — it reads from the catalog and shows the configured state. This is sufficient for development inspection.

---

## Component 8: File Structure

```
kernos/capability/
├── __init__.py
├── client.py              # MCPClientManager — add get_tool_definitions()
├── known.py               # NEW — KNOWN_CAPABILITIES catalog
└── registry.py            # NEW — CapabilityInfo, CapabilityStatus, CapabilityRegistry
```

---

## Acceptance Criteria

1. **CapabilityInfo dataclass** exists with all fields: name, display_name, description, category, status, tools, setup_hint, setup_requires, server_name, error_message.
2. **CapabilityRegistry** provides get_connected(), get_available(), get_by_category(), get_connected_tools(), build_capability_prompt().
3. **Known capabilities catalog** (`known.py`) lists at least three capabilities: google-calendar, gmail, web-search.
4. **System prompt no longer parses tool names.** The hardcoded `"calendar" in n.lower()` detection is gone from handler.py. `build_capability_prompt()` generates the capability section.
5. **System prompt includes AVAILABLE capabilities** so the agent can offer them.
6. **Handler constructor accepts registry.** The handler uses `self.registry` for tools and prompt building.
7. **State Store updated:** TenantProfile.capabilities populated with `{name: status}` for all known capabilities.
8. **CLI `capabilities` command** shows all known capabilities with status, description, and setup info.
9. **MCPClientManager.get_tool_definitions()** returns tools grouped by server name.
10. **Existing tests pass** — no regressions. Handler tests may need to provide a mock registry.
11. **Bot starts and responds to messages** — calendar queries still work.
12. **Adding a new capability requires only:** one entry in `known.py` + MCP server registration if applicable. No system prompt changes, no handler changes.
13. **Event payloads unchanged** — capability.connected events still have the same structure.

---

## Tests

**New file:** `tests/test_registry.py`

- Test CapabilityInfo creation with all fields
- Test CapabilityRegistry.register() and get()
- Test get_connected() returns only CONNECTED capabilities
- Test get_available() returns only AVAILABLE capabilities
- Test get_by_category() filters correctly
- Test get_connected_tools() delegates to MCPClientManager (mock)
- Test build_capability_prompt() includes connected capabilities with descriptions
- Test build_capability_prompt() includes available capabilities with setup hints
- Test build_capability_prompt() with no capabilities returns conversation-only text
- Test build_capability_prompt() with only available (none connected) mentions available only

**Updated file:** `tests/test_handler.py`

- Handler tests now provide a mock CapabilityRegistry
- System prompt tests verify capability section comes from registry, not hardcoded
- Verify handler works with registry that has zero connected capabilities (conversation-only mode)

---

## What 1B.3 deliberately does NOT build

- **Discoverable tier** — Phase 4 marketplace. The CapabilityStatus enum includes it but nothing populates it.
- **Runtime capability installation** — user says "connect my email" and the agent walks through OAuth. Phase 2. The available tier surfaces that it COULD be connected; the actual connection is still manual.
- **Dependency resolution** — the outline mentions capability prerequisites. `setup_requires` captures env var dependencies but there's no runtime resolution engine. Phase 2.
- **Per-tenant capability customization** — all tenants see the same available capabilities. Tenant-specific capability configuration (different MCP servers per tenant) comes with multi-tenancy in 1B.6.
- **Dynamic capability health monitoring** — detecting when a connected capability goes down mid-operation. The error status exists but runtime health checks don't.

---

## Live Verification

**Live verification: REQUIRED** — system prompt changes affect every message.

### Step 1: Cold start
1. Restart the bot
2. Check logs for capability registry initialization
3. Verify calendar shows as CONNECTED with tool names

### Step 2: System prompt verification
1. Send: "What can you do?"
2. The agent should describe calendar capabilities AND mention available capabilities (email, web search)
3. This is the key change — before 1B.3, it would only mention calendar

### Step 3: Available capability awareness
1. Send: "Can you check my email?"
2. Agent should NOT say "I can't do that"
3. Agent SHOULD say something like "I have email available but it's not connected yet — want me to help set that up?"

### Step 4: Calendar regression
1. Send: "What's on my schedule today?"
2. Verify real calendar data returns (same as before)

### Step 5: CLI verification
1. Run: `./kernos-cli capabilities`
2. Verify google-calendar shows as CONNECTED with tools listed
3. Verify gmail and web-search show as AVAILABLE

### Step 6: State Store verification
1. Run: `./kernos-cli profile <tenant_id>`
2. Verify capabilities dict shows `{"google-calendar": "connected", "gmail": "available", "web-search": "available"}`

---

*Spec ready for review. After approval, founder commits to specs/ and triggers Claude Code.*

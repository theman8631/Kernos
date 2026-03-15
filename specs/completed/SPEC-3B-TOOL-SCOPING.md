# SPEC-3B: Per-Space Tool Scoping

**Status:** READY FOR REVIEW
**Depends on:** SPEC-3A Per-Space File System (complete), CapabilityRegistry (Phase 1B)
**Design source:** System Space + MCP Architecture brainstorm (Kabe + Kit, 2026-03-14)
**Objective:** Scope tool visibility per context space so each space only sees tools relevant to its domain. Provision the system space as the agent's control panel. Add `request_tool` for on-demand tool activation.

**What changes for the user:** The D&D space stops showing invoice tools. The Business space stops showing map-drawing tools. Each space feels purpose-built for its domain. When a space needs a tool it doesn't have, the agent activates it silently or directs the user to the system space for installation.

**What changes architecturally:** ContextSpace gains an `active_tools` field. CapabilityRegistry gains a `universal` flag and space-aware filtering on `build_capability_prompt()`. A system space is auto-created at tenant provisioning. Gate 2 space creation expands to include tool seeding. A new `request_tool` kernel meta-tool handles on-demand activation.

**What this is NOT:**
- Not MCP installation (that's 3B+ — a separate spec)
- Not MCP discovery or browsing (that's 3B+)
- Not credential management (that's 3B+)
- Not the Dispatch Interceptor (that's 3D)

-----

## Component 1: System Space

Auto-created at tenant provisioning alongside Daily. A singleton context space dedicated to system configuration and management.

### Creation

```python
# In handler._get_or_init_soul() or _ensure_tenant_state(), after Daily creation:

system_space = ContextSpace(
    id=f"space_{uuid4().hex[:8]}",
    tenant_id=tenant_id,
    name="System",
    description=(
        "System configuration and management. Install and manage tools, "
        "view connected capabilities, get help with how the system works."
    ),
    space_type="system",
    status="active",
    posture=(
        "Precise and careful. Configuration changes affect the whole system. "
        "Confirm before modifying system settings or tool configurations."
    ),
    created_at=_now_iso(),
    last_active_at=_now_iso(),
    is_default=False,
    # System space sees all installed tools — active_tools is ignored
)
```

### Characteristics

- `space_type: "system"` — new type alongside daily, project, domain, managed_resource
- Cannot be archived or deleted (enforced in LRU sunset — exempt like Daily)
- The router recognizes system-management intent ("what tools do I have," "connect my email," "install something new") and routes there
- Always sees ALL installed tools regardless of `active_tools` — it's the one space where the full registry is visible
- Pre-loaded with two documentation files (via 3A):
  - `capabilities-overview.md` — what tools are currently connected, what each does in one sentence. Dynamic: updated when tools are installed/removed.
  - `how-to-connect-tools.md` — a static guide explaining the install flow.

### Router awareness

Add "system" to the router's space list with its description. System-management messages ("what tools do I have," "set up email access," "how does this work") route there naturally via the LLM router — the description provides the routing signal. No special routing logic needed beyond what 2B-v2 already provides.

-----

## Component 2: `active_tools` on ContextSpace

**Modified file:** `kernos/kernel/spaces.py`

```python
@dataclass
class ContextSpace:
    # ... existing fields ...
    active_tools: list[str] = field(default_factory=list)
    # List of capability names visible to this space.
    # Empty list = system defaults (kernel tools + universal MCP tools).
    # System space ignores this field — always sees everything.
```

### System defaults definition

When `active_tools` is empty, the space sees system defaults:

- **Kernel tools (always available):** `remember`, `write_file`, `read_file`, `list_files`, `delete_file`, `request_tool`
- **Universal MCP tools:** Any capability with `universal=True` in the registry
- **Nothing else** — restricted MCP tools require explicit activation via seeding or `request_tool`

This means:
- Fresh tenant, nothing installed → kernel tools only
- Calendar installed as universal → kernel tools + calendar everywhere
- Calendar installed as restricted → kernel tools everywhere, calendar in system space + wherever activated
- D&D space with `active_tools=["google-calendar"]` → kernel tools + calendar (explicit)

### Serialization

The `active_tools` field serializes as a JSON list of strings in the existing ContextSpace JSON storage. Empty list on deserialization for existing spaces (backward compatible — all existing spaces get system defaults).

-----

## Component 3: `universal` Flag on CapabilityRegistry

**Modified file:** `kernos/capability/registry.py`

```python
@dataclass
class CapabilityInfo:
    # ... existing fields ...
    universal: bool = False
    # If True, this capability is included in system defaults —
    # visible to every space without explicit activation.
    # Set at registration/install time.
```

### Default universality for known capabilities

```python
# In known.py — update KNOWN_CAPABILITIES:
# Calendar is universal (useful in most spaces)
# Future capabilities set universal based on their nature at install time
```

The `universal` flag is set at install time. The config file specifies it, the registry holds and enforces it at runtime. When `build_capability_prompt()` runs, it checks: is this capability in the active space's `active_tools`, or is it `universal=True`, or is this the system space? If any are true, include the capability's tools.

-----

## Component 4: Space-Aware `build_capability_prompt()`

**Modified file:** `kernos/capability/registry.py`

The existing `build_capability_prompt()` generates the tool list for the system prompt. Add space-aware filtering:

```python
def build_capability_prompt(
    self, space: ContextSpace | None = None,
) -> str:
    """Generate the CAPABILITIES section of the system prompt.

    Filters tools by space visibility:
    - System space: all connected tools (no filtering)
    - Space with active_tools: kernel tools + universal + explicitly listed
    - Space with empty active_tools: kernel tools + universal (system defaults)
    """
    if space and space.space_type == "system":
        # System space sees everything
        return self._build_prompt_unfiltered()

    visible_capabilities = set()

    # Kernel tools are always visible (handled separately — not in registry)

    # Universal capabilities
    for cap in self._capabilities.values():
        if cap.universal and cap.status == CapabilityStatus.CONNECTED:
            visible_capabilities.add(cap.name)

    # Space-specific active tools
    if space and space.active_tools:
        for tool_name in space.active_tools:
            if tool_name in self._capabilities:
                cap = self._capabilities[tool_name]
                if cap.status == CapabilityStatus.CONNECTED:
                    visible_capabilities.add(cap.name)

    return self._build_prompt_filtered(visible_capabilities)


def _build_prompt_filtered(self, visible: set[str]) -> str:
    """Build capability prompt showing only visible capabilities."""
    # Same format as current build_capability_prompt(),
    # but only includes capabilities in the visible set.
    # AVAILABLE capabilities still shown as "could be connected"
    # to let the agent know what's possible.
    ...

def _build_prompt_unfiltered(self) -> str:
    """Build capability prompt showing all capabilities (system space)."""
    # Current build_capability_prompt() behavior — no changes
    ...
```

### Handler integration

```python
# In handler.process(), when building the system prompt:

capability_prompt = self.registry.build_capability_prompt(space=active_space)
```

This is one parameter addition to an existing call. The filtering happens inside the registry. No other handler changes for tool visibility.

-----

## Component 5: Smart Seeding at Space Creation

**Modified file:** `kernos/messages/handler.py` — `_trigger_gate2()`

When Gate 2 creates a new space, the LLM call that names and describes the space also recommends which installed MCP tools should be pre-activated. One call, expanded output schema.

### Expanded Gate 2 schema

```python
GATE2_SCHEMA = {
    "type": "object",
    "properties": {
        "create_space": {"type": "boolean"},
        "name": {"type": "string"},
        "description": {"type": "string"},
        "reasoning": {"type": "string"},
        "recommended_tools": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Capability names from the installed list that are relevant to this space"
        }
    },
    "required": ["create_space", "name", "description", "reasoning", "recommended_tools"],
    "additionalProperties": False
}
```

### Gate 2 prompt expansion

```python
# In _trigger_gate2(), add installed tool list to the prompt:

installed_tools = self.registry.get_connected_capability_names()
tool_descriptions = self.registry.get_capability_descriptions()

user_content = (
    f"Messages about this topic:\n{self._format_messages(tagged_messages)}\n\n"
    f"Installed tools available for activation:\n{tool_descriptions}\n\n"
    f"Based on the space name and description, populate recommended_tools "
    f"with capability names likely to be useful for this space."
)
```

### Seeding on creation

```python
# After Gate 2 creates the space:

if parsed.get("recommended_tools"):
    new_space.active_tools = [
        t for t in parsed["recommended_tools"]
        if t in installed_tools  # Only seed tools that actually exist
    ]
```

A Business space gets calendar and email. A D&D space gets system defaults only. The LLM makes a best guess from the space name and description — "from the context name and description a best guess is notably beneficial" (Kabe).

-----

## Component 6: `request_tool` Meta-Tool

**Modified files:** `kernos/kernel/reasoning.py`, new function in `kernos/capability/registry.py`

A kernel-managed tool the agent calls when it discovers it needs a capability not in its active set.

### Tool definition

```python
REQUEST_TOOL = {
    "name": "request_tool",
    "description": (
        "Request activation of a tool capability for the current context space. "
        "Use this when you need a tool that isn't currently available. "
        "Describe what you need thoroughly — what the tool should do, why you need it, "
        "and what context it's for. This helps the system find the right match."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "capability_name": {
                "type": "string",
                "description": (
                    "The name of the capability to activate, if known. "
                    "Use 'unknown' if you know what you need but not the exact name."
                )
            },
            "description": {
                "type": "string",
                "description": (
                    "Thorough description of what you need the tool to do. "
                    "Be exhaustive — include the function needed, the context, "
                    "and why it's needed. This helps match the right tool."
                )
            }
        },
        "required": ["capability_name", "description"]
    }
}
```

### Kernel routing

```python
# In ReasoningService, tool call handling:

elif tool_name == "request_tool":
    result = await self._handle_request_tool(
        tenant_id, active_space_id,
        tool_args.get("capability_name", "unknown"),
        tool_args.get("description", ""),
    )
```

### Implementation

```python
async def _handle_request_tool(
    self, tenant_id: str, space_id: str,
    capability_name: str, description: str,
) -> str:
    """Handle a request_tool call.

    1. If capability_name matches an installed capability: activate silently
    2. If capability_name is 'unknown': fuzzy match against registry using description
    3. If not installed: direct user to system space
    """
    registry = self._registry

    # Exact match
    if capability_name != "unknown":
        cap = registry.get(capability_name)
        if cap and cap.status == CapabilityStatus.CONNECTED:
            # Activate for this space
            await self._activate_tool_for_space(tenant_id, space_id, capability_name)
            tools = [t for t in cap.tools]
            return (
                f"Activated '{cap.name}' for this space. "
                f"Available tools: {', '.join(tools)}. "
                f"You can use these now."
            )

    # Fuzzy match — check all connected capabilities against the description
    # Sort by universal first — prefer universally available tools over restricted ones
    candidates = sorted(
        [c for c in registry.get_all() if c.status == CapabilityStatus.CONNECTED],
        key=lambda c: (not c.universal, c.name),  # universal=True sorts first
    )
    best_match = None
    for cap in candidates:
        # Check if capability name or any tool name appears in description
        desc_lower = description.lower()
        if (cap.name.lower() in desc_lower or
            any(tool.lower() in desc_lower for tool in cap.tools)):
            best_match = cap
            break

    if best_match:
        await self._activate_tool_for_space(tenant_id, space_id, best_match.name)
        tools = [t for t in best_match.tools]
        return (
            f"Found and activated '{best_match.name}' for this space. "
            f"Matched capability: {best_match.name}. "
            f"Available tools: {', '.join(tools)}. "
            f"You can use these now."
        )

    # Not installed — direct to system space
    return (
        f"I don't have a tool matching '{capability_name}' installed. "
        f"To get new tools set up, you'd need to head to system settings. "
        f"Want me to help you find the right tool there?"
    )


async def _activate_tool_for_space(
    self, tenant_id: str, space_id: str, capability_name: str,
) -> None:
    """Add a capability to a space's active_tools list."""
    space = await self._state.get_context_space(tenant_id, space_id)
    if space and capability_name not in space.active_tools:
        space.active_tools.append(capability_name)
        await self._state.update_context_space(
            tenant_id, space_id, {"active_tools": space.active_tools}
        )
```

### Response includes `matched_capability`

The response text always names what was activated so the agent knows exactly what it got. This closes the loop Kit flagged — no ambiguity about whether the match was correct.

### Name matching approach

Kit flagged that `capability_name` assumes the agent knows the registry name. The implementation handles this:
- **Exact name known:** Direct match against registry. Fast path.
- **Name is "unknown":** Fuzzy match — check if any capability name or tool name appears in the description. Simple string matching, not LLM. Covers "I need to schedule something" matching against a capability whose tools include "create-event."
- **No match at all:** Direct user to system space. The agent doesn't guess.

For v1, this string matching is sufficient. If it proves too loose or too strict in practice, a Haiku-class matching call can be added later — but string matching against a small registry (typically 5-20 capabilities) should work well.

-----

## Component 7: Tool Installation Scoping

When a new MCP server is installed (via 3B+ future spec), the installation handler:

1. Adds the capability to the system-wide registry
2. Sets `universal` based on the install config
3. Auto-activates in the system space (system space always sees everything — this is automatic)
4. Does NOT retroactively update other spaces' `active_tools`

Other spaces get the tool via:
- **Smart seeding** when new spaces are created after installation
- **`request_tool`** when existing spaces discover they need it
- **Universal flag** if the tool is marked universal at install time

Installing and activating are separate events. This prevents tools appearing in spaces that never asked for them.

This component is a design note for 3B+, not implementation work in 3B. The scoping infrastructure built in 3B supports this flow — 3B+ just uses it.

-----

## Component 8: Documentation Files

At system space creation, two files are written via the FileService:

### `capabilities-overview.md`

```python
async def _write_capabilities_overview(
    self, tenant_id: str, system_space_id: str,
) -> None:
    """Write/update the capabilities overview doc in the system space."""
    connected = self.registry.get_connected_capabilities()
    available = self.registry.get_available_capabilities()

    content = "# Connected Tools\n\n"
    if connected:
        for cap in connected:
            universal_tag = " (available everywhere)" if cap.universal else ""
            content += f"- **{cap.name}**{universal_tag}: {cap.description}\n"
            content += f"  Tools: {', '.join(cap.tools)}\n"
    else:
        content += "No tools connected yet.\n"

    content += "\n# Available to Connect\n\n"
    if available:
        for cap in available:
            content += f"- **{cap.name}**: {cap.description}\n"
    else:
        content += "No additional tools available.\n"

    await self.files.write_file(
        tenant_id, system_space_id,
        "capabilities-overview.md", content,
        "What tools are connected and available — updated on changes",
    )
```

This file is updated whenever a tool is installed or removed. In 3B, the file is written at system space creation. The update trigger for install/remove events ships with 3B+. Claude Code should add `# TODO: call _update_capabilities_overview() on install/remove` at the relevant registry mutation points (`register()`, `unregister()`, `promote()`) so the 3B+ implementer knows where to wire it.

### `how-to-connect-tools.md`

Static content written at system space creation:

```
# How to Connect Tools

Tools extend what I can do — connect your calendar, email, documents,
and more. Each tool is an MCP server that runs alongside the system.

## What's Connected
Check capabilities-overview.md for the current list, or just ask me
"what tools do I have?"

## Adding a New Tool
To connect a new tool, tell me what you need:
- "I need access to my Google Calendar"
- "Can you connect to my email?"
- "I need a tool for [description]"

I'll walk you through the setup.

## Tool Visibility
Tools are available where they're useful. Your D&D space won't show
invoice tools. Your Business space won't show game tools. If you need
a tool in a specific space, just ask — I'll activate it.
```

-----

## Implementation Order

1. **ContextSpace model** — add `active_tools: list[str]` field
2. **CapabilityInfo model** — add `universal: bool` field
3. **System space creation** — auto-provision at tenant init, `space_type: "system"`, exempt from LRU
4. **Documentation files** — write capabilities-overview.md and how-to-connect-tools.md at system space creation
5. **`build_capability_prompt()` filtering** — space-aware tool visibility
6. **Handler integration** — pass `active_space` to `build_capability_prompt()`
7. **Gate 2 expansion** — expanded schema with `recommended_tools`, tool seeding at space creation
8. **`request_tool` meta-tool** — tool definition, kernel routing, activation logic, fuzzy matching
9. **Tests** — system space creation, tool filtering (system/default/explicit/universal), Gate 2 seeding, request_tool (exact/fuzzy/not-found), LRU exemption
10. **Live test**

-----

## What Claude Code MUST NOT Change

- File system (3A) — only uses file tools to write documentation
- Compaction system (2C)
- Retrieval system (2D) — remember() unchanged
- Router logic (2B-v2) — system space routes naturally via description
- Entity resolution (2A)
- Soul data model
- Existing tool-use loop in ReasoningService — only add request_tool to the KERNEL_TOOLS intercept

-----

## Acceptance Criteria

1. **System space auto-created.** New tenant gets Daily + System spaces at provisioning. System space has correct posture, description, and `space_type: "system"`. Verified via `kernos-cli spaces`.

2. **System space exempt from LRU.** With ACTIVE_SPACE_CAP spaces, the system space is never archived. Verified.

3. **System space sees all tools.** When in system space, `build_capability_prompt()` returns all connected tools regardless of `active_tools`. Verified.

4. **Tool filtering works for non-system spaces.** D&D space with `active_tools=["google-calendar"]` sees kernel tools + calendar only. No other MCP tools. Verified.

5. **Empty active_tools = system defaults.** A space with `active_tools=[]` sees kernel tools + universal MCP tools. Not everything, not nothing. Verified.

6. **Universal flag works.** Calendar with `universal=True` appears in all spaces including those with empty `active_tools`. A restricted tool with `universal=False` appears only in system space + spaces that explicitly list it. Verified.

7. **Gate 2 seeds active_tools.** New space created via Gate 2 has `active_tools` populated based on LLM recommendation. Business space gets calendar/email. D&D space gets defaults only. Verified.

8. **request_tool activates installed capability.** Agent calls `request_tool("google-calendar", "I need to schedule a session")` → capability activated for this space, tools listed in response. `active_tools` updated on ContextSpace. Verified.

9. **request_tool fuzzy matches.** Agent calls `request_tool("unknown", "I need to schedule an event on the calendar")` → matches google-calendar by tool name presence in description. Verified.

10. **request_tool handles not-installed.** Agent calls `request_tool("map-drawing", "I need to draw a map")` → no match → response directs user to system space. Verified.

11. **Documentation files exist in system space.** capabilities-overview.md and how-to-connect-tools.md present in system space file directory. Verified via `kernos-cli files`.

12. **Backward compatible.** Existing spaces with no `active_tools` field get system defaults (kernel tools + universal). No behavior change for existing tenants. Verified.

13. **Silent activation.** When request_tool activates a capability, no user-facing notification is generated. The agent just gets the tools and proceeds. Verified.

14. **All existing tests pass.** New tests cover all components.

-----

## Live Verification

Follow the Live Testing Protocol in `tests/live/PROTOCOL.md`.

### Test Table

| Step | Action | Expected |
|---|---|---|
| 1 | `kernos-cli spaces <tenant>` | System space exists alongside Daily. Type: system. |
| 2 | Send: "What tools do I have?" | Routes to system space. Agent lists all connected capabilities. |
| 3 | Switch to D&D space. Send: "What can you do here?" | Agent describes capabilities based on D&D active_tools (defaults or seeded). Should NOT list business-only tools. |
| 4 | In D&D: "I need to check my calendar for our next session" | If calendar is universal: agent has calendar tools, proceeds. If not: agent calls request_tool, calendar activates, agent proceeds. |
| 5 | `kernos-cli spaces <tenant>` after step 4 | D&D space `active_tools` now includes google-calendar (if it was activated via request_tool). |
| 6 | Switch to Business space. Check available tools. | Should have calendar (universal or seeded). Should NOT have D&D-specific tools. |
| 7 | Send: "I need a tool for drawing maps" | Agent calls request_tool. No match (map tool not installed). Agent directs to system space. |
| 8 | Create a new space (trigger Gate 2). Check active_tools. | Space has recommended_tools seeded based on its description. |
| 9 | `kernos-cli files <tenant> <system_space_id>` | Shows capabilities-overview.md and how-to-connect-tools.md. |
| 10 | In system space: "Read the capabilities overview" | Agent reads the file and summarizes connected/available tools. |

Write results to `tests/live/LIVE-TEST-3B.md`.

-----

## Design Decisions This Spec Encodes

| Decision | Choice | Why |
|---|---|---|
| System space auto-created | Like Daily, singleton, can't be archived | Always exists. User has a control panel from day one. No setup required. |
| active_tools on ContextSpace | Not a separate file | Needed every message for tool filtering. Field access is immediate. File I/O per message is wrong tradeoff. (Kit confirmed) |
| universal flag on CapabilityRegistry | Not on config file | Registry already tracks connection state and tools. "Visible everywhere" is a registry-level property. Config sets it, registry enforces it. (Kit confirmed) |
| Empty active_tools = system defaults | Not "show everything" | Two-level model is only meaningful if the default is scoped. Kernel tools + universal = the safe, useful base. (Kit) |
| Smart seeding at Gate 2 | One LLM call, expanded schema | Saves future tokens if spaces have to wire tools up later. Presents likely-relevant tools before need arises. (Kabe) |
| New MCP → system space only | No retroactive activation | Installing and activating are separate events. Prevents tools appearing in spaces that never asked. (Kit) |
| Silent activation via request_tool | No per-space "Activated!" notification | All context spaces feel like the same agent. Behavioral restrictions are covenant rules, not activation prompts. (Kabe) |
| request_tool description should be exhaustive | Spend tokens upfront on description | Gets the right tool on first match. Saves repeated attempts. (Kabe) |
| Fuzzy matching via string presence | Not LLM call | Registry is small (5-20 capabilities). String matching against names and tool names is fast and sufficient. LLM matching added later if needed. |
| Two documentation files | Not a manual | capabilities-overview.md (dynamic) + how-to-connect-tools.md (static). Orientation, not a manual. (Kit: "Anything beyond that is noise.") |
| 3B is scoping only, not installation | Split per Kit recommendation | Per-space scoping and MCP installation are different problems. Scoping is the gate — nothing works cleanly without it. |

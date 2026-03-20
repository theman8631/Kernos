# SPEC-3K: Unified Capability Registry — Defaults = Pre-Installed, Not Special-Cased

## OBJECTIVE

known.py currently hardcodes capabilities as Python objects. User-installed MCPs go through 3B+ infrastructure. This spec unifies them — all capabilities live in one registry with the same enable/disable mechanics.

## DESIGN

1. Add source field to CapabilityInfo: "default" or "user"
2. Add status field: "enabled", "disabled", "available", "error"
3. On first boot/tenant creation: install defaults from known.py manifest into the runtime registry (read once, not on every startup)
4. On startup: migration step — check for new defaults in manifest not yet in tenant registry, add as "available"
5. MCP server lifecycle on disable: server stays WARM (process keeps running, just hidden from LLM tool list). Re-enable is instant. Do NOT shut down MCP processes on disable.
6. Implement manage_tools kernel tool with actions: list, enable, disable, install, remove
   - list: gate bypass (read)
   - enable/disable/install/remove: soft_write
   - remove rejects source="default" capabilities with error message
   - install routes through existing 3B+ flow
7. Modify tool list sent to LLM to filter out disabled capabilities
8. Remove any code path that treats known.py differently from user-installed MCPs

## MANAGE_TOOLS TOOL DEFINITION

name: "manage_tools"
description: "Manage your capabilities — list, enable, disable, install, or remove tools. Use 'list' to see all available capabilities and their status. Use 'enable' or 'disable' to toggle capabilities on or off. Use 'install' to add a new MCP server. Use 'remove' to uninstall a user-added capability (defaults can only be disabled, not removed)."
input_schema:
  action: string enum ["list", "enable", "disable", "install", "remove"] required
  capability: string (required for enable/disable/remove)
  additionalProperties: false

Register in _KERNEL_TOOLS. list in _KERNEL_READS, others in _KERNEL_WRITES.

## DO NOT CHANGE

- MCP connection/discovery mechanics (3B+ infrastructure)
- Dispatch gate tool_effects evaluation
- Per-space tool scoping (3B)
- Existing MCP server configurations

## ACCEPTANCE CRITERIA

1. manage_tools list returns both default and user-installed capabilities with source and status
2. Disabling calendar removes calendar tools from LLM tool list
3. Re-enabling calendar restores tools instantly (no reconnection)
4. manage_tools remove google-calendar returns error
5. known.py read only at tenant initialization, not every startup
6. New defaults in manifest appear as "available" on existing tenants after restart
7. Disabled capabilities keep MCP server process running
8. docs/capabilities/overview.md updated — unified registry, "pre-installed" not "built-in"
9. All existing tests pass

## TESTS ADDED

1. manage_tools list shows all capabilities with source and status
2. Disable default → tools absent from LLM list → manage_tools shows disabled
3. Re-enable default → tools reappear instantly
4. manage_tools remove default → returns error
5. Install test MCP, remove it, verify gone from registry
6. Disable user-added → same flow works
7. Wipe tenant, restart → defaults appear in registry
8. Add new entry to known.py, restart → existing tenant gets it as "available"
9. Disable capability → MCP process still running → re-enable → no process restart

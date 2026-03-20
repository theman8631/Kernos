# Capabilities Overview

Capabilities are external tools that Kernos connects to via the Model Context Protocol (MCP). Each capability is an MCP server that the system connects to, discovers tools from, and routes tool calls through.

## Unified Capability Model

All capabilities — pre-installed defaults and user-added tools — are managed through the same mechanism. The only difference between a default capability (like Google Calendar) and one the user adds later is that defaults come pre-installed. The user or agent can disable any capability, default or otherwise. All capabilities appear in one unified management list.

There is no "built-in" vs "external" distinction. Everything is an MCP server. Everything can be enabled, disabled, installed, or uninstalled through the same interface.

## Pre-Installed Capabilities

These come ready to connect:

| Capability | What it does | Universal? |
|-----------|-------------|-----------|
| Google Calendar | Read schedule, create/update/delete events, find availability | Yes |
| Gmail | Read, categorize, draft, and send email | No |
| Web Search (Brave) | Structured web search returning titles, URLs, snippets | No |
| Web Browser (Lightpanda) | Navigate and read any web page, run JavaScript | Yes |

**Universal** means the capability is available in every context space by default. Non-universal capabilities must be activated per-space.

## How Connection Works

1. **Registration** — capabilities are registered in the `CapabilityRegistry` with server command, args, and credentials requirements.
2. **Connection** — at startup, registered servers connect via MCP and their tools are discovered.
3. **Tool Discovery** — each MCP server exposes a set of tools. These are added to the tool list available to the agent.
4. **Credential Setup** — some capabilities require API keys or OAuth tokens. The secure credential handoff intercepts the next user message as a secret (never entering the LLM context) and uses it to connect.

## Runtime Install/Uninstall

Users can add new MCP servers at runtime:

- Tell the agent what tool you need ("I need access to my Notion")
- The agent uses `request_tool` to search for or activate a capability
- If a new MCP server needs to be installed, the system handles connection and tool discovery
- Configuration is persisted in `mcp-servers.json` in the system space

Uninstalling suppresses a capability (status becomes `SUPPRESSED`) — configuration is preserved for potential re-enable.

## Tool Scoping

Tools are scoped per context space. Each space has an `active_tools` list:

- **Universal capabilities** (like calendar) are available in every space
- **Non-universal capabilities** must be explicitly activated for a space
- The `request_tool` meta-tool activates a capability for the current space
- Gate 2 (space creation) can recommend tools when creating a new space

## Effect Classification

Every tool call is classified by its effect level:

- **read** — information retrieval, bypasses dispatch gate
- **soft_write** — creates/modifies data, dispatch gate evaluates
- **hard_write** — high-impact action (send email, delete), dispatch gate evaluates with higher scrutiny

Effect classifications come from `tool_effects` on each `CapabilityInfo`, or default to `hard_write` if unknown.

## Capability Status

Each capability has a status:

- **AVAILABLE** — registered but not connected
- **CONNECTED** — connected and tools discovered
- **ERROR** — connection failed
- **SUPPRESSED** — user uninstalled (config preserved)

## Code Locations

| Component | Path |
|-----------|------|
| CapabilityRegistry | `kernos/capability/registry.py` |
| MCPClientManager | `kernos/capability/client.py` |
| Known capabilities catalog | `kernos/capability/known.py` |
| Tool scoping | `kernos/capability/registry.py` (get_tools_for_space) |
| Runtime install flow | `kernos/messages/handler.py` |

# SPEC-3B+: MCP Installation

**Status:** READY FOR REVIEW
**Depends on:** SPEC-3A File System (system space files), SPEC-3B Tool Scoping (registry, active_tools, system space), SPEC-3D Dispatch Interceptor (gate)
**Design source:** Brainstorm: System Space + MCP Architecture (Kabe + Kit, 2026-03-14), Plug Alignment Audit (2026-03-15), Kabe secure credential brainstorm
**Objective:** Let users install and connect MCP servers through natural conversation in the system space. First real external integration. Config persists across restarts. Credentials never enter the conversation pipeline.

**What changes for the user:** User says "connect my calendar" → the system walks them through setup → credentials are collected securely → the tool is connected and available. All from a text conversation. No config files, no command line, no manual setup. Disconnecting is equally conversational.

**What changes architecturally:** MCP configs persist as a JSON file in the system space. MCPClientManager gains `connect_one()` and `disconnect_one()` for runtime connections. A handler-level secure input mode intercepts credentials before they enter the pipeline. The startup flow merges known.py defaults with persisted configs. New EventTypes for tool installation tracking.

**What this is NOT:**
- Not MCP discovery/marketplace browsing (that's 4A)
- Not OAuth flows (deferred to 4C — requires web interface for browser redirect)
- Not auto-installation by the agent (user-initiated only)
- Not credential rotation or management (future work)

-----

## Component 1: Config Persistence

**New file:** `mcp-servers.json` in the system space via 3A file primitives.

### Schema

```json
{
  "servers": {
    "google-calendar": {
      "display_name": "Google Calendar",
      "command": "npx",
      "args": ["@cocal/google-calendar-mcp"],
      "credentials_key": "google-calendar",
      "env_template": {
        "GOOGLE_OAUTH_CREDENTIALS": "{credentials}"
      },
      "universal": true,
      "tool_effects": {
        "list-events": "read",
        "create-event": "soft_write",
        "delete-event": "hard_write"
      }
    }
  },
  "uninstalled": ["some-tool-user-removed"]
}
```

### Key design choices

- **Credentials referenced by capability name.** `credentials_key` points to the file in `secrets/{tenant_id}/{capability_name}.key`. The `env_template` shows how to inject the credential value into the MCP server's environment — `{credentials}` is replaced with the actual value at connect time. The agent sees `credentials_key: "google-calendar"` but never sees the value.
- **`uninstalled` list** suppresses known.py entries the user explicitly disconnected. Explicit user action overrides the default catalog. (Kit)
- **File is written via FileService** — same `write_file` path as any other file. The agent in the system space can read it, and the kernel writes it during install/uninstall.

### Startup merge flow

```python
# In discord_bot.py on_ready():

# 1. Load known.py defaults (AVAILABLE status)
for cap in KNOWN_CAPABILITIES:
    registry.register(dataclasses.replace(cap))

# 2. Load persisted config from system space
mcp_config = await files.read_file(tenant_id, system_space_id, "mcp-servers.json")
if mcp_config:
    config = json.loads(mcp_config)
    
    # 3. Suppress uninstalled entries
    for name in config.get("uninstalled", []):
        cap = registry.get(name)
        if cap:
            cap.status = CapabilityStatus.SUPPRESSED  # New status
    
    # 4. Register + connect persisted servers
    for name, server_config in config.get("servers", {}).items():
        resolved_env = resolve_credentials(server_config, tenant_id)
        mcp_manager.register_server(
            name,
            StdioServerParameters(
                command=server_config["command"],
                args=server_config["args"],
                env=resolved_env,
            ),
        )

# 5. Connect all registered servers
await mcp_manager.connect_all()

# 6. Promote connected servers in registry
for server_name, tools in mcp_manager.get_tool_definitions().items():
    cap = registry.get(server_name)
    if cap:
        cap.status = CapabilityStatus.CONNECTED
        cap.tools = [t["name"] for t in tools]
```

### New CapabilityStatus: SUPPRESSED

```python
class CapabilityStatus(str, Enum):
    CONNECTED = "connected"
    AVAILABLE = "available"
    DISCOVERABLE = "discoverable"
    SUPPRESSED = "suppressed"  # User explicitly uninstalled — don't show
    ERROR = "error"
```

SUPPRESSED capabilities don't appear in the system prompt, `build_capability_prompt()`, or capabilities-overview.md. They still exist in the registry so they can be re-installed.

-----

## Component 2: `connect_one()` and `disconnect_one()`

**Modified file:** `kernos/capability/client.py`

```python
async def connect_one(self, server_name: str) -> bool:
    """Connect a single MCP server at runtime.
    
    Same connection logic as connect_all() but for one server.
    Returns True on success, False on failure.
    """
    if server_name not in self._server_params:
        return False
    
    params = self._server_params[server_name]
    try:
        server = Server(name=server_name, params=params)
        await server.connect()
        self._servers[server_name] = server
        
        # Discover tools
        tools = await server.list_tools()
        self._tool_map[server_name] = tools
        
        return True
    except Exception as exc:
        logger.warning("Failed to connect %s: %s", server_name, exc)
        return False


async def disconnect_one(self, server_name: str) -> bool:
    """Disconnect a single MCP server at runtime.
    
    Returns True on success, False if server wasn't connected.
    """
    if server_name not in self._servers:
        return False
    
    try:
        await self._servers[server_name].disconnect()
    except Exception:
        pass  # Best effort disconnect
    
    del self._servers[server_name]
    self._tool_map.pop(server_name, None)
    return True
```

### Registry integration after connect/disconnect

```python
# After successful connect_one():
cap = registry.get(server_name)
if cap:
    cap.status = CapabilityStatus.CONNECTED
    cap.tools = [t["name"] for t in mcp_manager.get_tool_definitions().get(server_name, [])]

# After successful disconnect_one():
cap = registry.get(server_name)
if cap:
    cap.status = CapabilityStatus.SUPPRESSED  # User explicitly removed — not AVAILABLE
    cap.tools = []
```

-----

## Component 3: Secure Credential Handoff

### The problem

The agent needs the user's API key to connect an MCP server. Credentials must NEVER enter the conversation pipeline — not the context window, not Tier 2 extraction, not KnowledgeEntries, not compaction, not the message store.

### Agent script (Kit: spec must prescribe exact wording)

The agent follows this script when credentials are needed. This is enforced via the system space template/prompt:

```
SECURE CREDENTIAL COLLECTION SCRIPT:

When you need an API key or credential from the user:

1. Explain what's needed and where to get it:
   "To connect [Tool Name], you'll need an API key from [provider].
    Go to [URL], navigate to [steps], and copy the key."

2. Instruct the user to enter secure input mode:
   "When you have your key ready, reply with exactly:
    secure api
    
    This will put the system into secure mode — your NEXT message
    after that will go directly to encrypted storage and won't be
    seen by any agent."

3. After the user sends "secure api" and then their key, the system
   handles the rest. You'll be told whether the connection succeeded.

NEVER ask the user to paste their API key directly in conversation.
ALWAYS use the secure api flow.
```

### Handler-level intercept

**Modified file:** `kernos/messages/handler.py`

```python
# New state on handler (per-tenant, in-memory)
_secure_input_state: dict[str, SecureInputState] = {}

@dataclass
class SecureInputState:
    capability_name: str
    expires_at: datetime
    # Set when "secure api" is detected
    # Cleared after credential is received or on timeout

# In handler.process(), BEFORE any normal processing:

async def process(self, msg: NormalizedMessage) -> str:
    tenant_id = msg.tenant_id
    
    # Check for active secure input mode
    if tenant_id in self._secure_input_state:
        state = self._secure_input_state[tenant_id]
        
        # Check timeout (10 minutes)
        if datetime.now(timezone.utc) > state.expires_at:
            del self._secure_input_state[tenant_id]
            # Kabe: timeout must notify the user
            return (
                "The secure input session timed out after 10 minutes. "
                "Your message was processed normally (not stored as a credential). "
                "Say 'secure api' again when you're ready to send your key."
            )
        
        # INTERCEPT: consume credential, never store message
        credential_value = msg.content.strip()
        cap_name = state.capability_name
        del self._secure_input_state[tenant_id]
        
        # Write to secrets directory — NOT to conversation store
        await self._store_credential(tenant_id, cap_name, credential_value)
        
        # Attempt connection
        success = await self._connect_after_credential(tenant_id, cap_name)
        if success:
            return (
                f"Key stored securely. {cap_name} is now connected! "
                f"You can start using it right away."
            )
        else:
            return (
                f"Key stored, but I couldn't connect to {cap_name}. "
                f"The key might be invalid, or the service might be down. "
                f"Try again or check the key."
            )
    
    # Check for "secure api" trigger
    if msg.content.strip().lower() == "secure api":
        # Determine which capability needs credentials
        # The agent should have set this in conversation context
        cap_name = await self._infer_pending_capability(tenant_id)
        if not cap_name:
            return (
                "I'm not sure which tool you're setting up. "
                "Head to system settings and start the connection process first."
            )
        
        self._secure_input_state[tenant_id] = SecureInputState(
            capability_name=cap_name,
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
        )
        
        return (
            f"Secure input mode active for {cap_name}. "
            f"Your next message will NOT be seen by any agent — "
            f"it will go directly to encrypted storage as your {cap_name} API key. "
            f"Send your key now."
        )
    
    # Normal processing continues...
    return await self._process_normal(msg)
```

### Capability inference

When the user says "secure api", the handler needs to know which capability's credential is being collected. The agent should have been walking the user through setup in the system space — the most recent system space conversation will reference the capability.

```python
async def _infer_pending_capability(self, tenant_id: str) -> str | None:
    """Infer which capability is being set up from recent conversation context.
    
    Checks recent messages in the system space for capability references.
    Falls back to checking if any AVAILABLE capability has been discussed.
    """
    # Check recent messages in the system space for capability mentions
    system_space = await self._get_system_space(tenant_id)
    if not system_space:
        return None
    
    recent = await self.conversations.get_recent(
        tenant_id, system_space.id, limit=5,
    )
    
    available = self.registry.get_available()
    for cap in available:
        for msg in recent:
            if cap.name.lower() in msg.content.lower() or \
               cap.display_name.lower() in msg.content.lower():
                return cap.name
    
    return None
```

### Credential storage

```python
async def _store_credential(
    self, tenant_id: str, capability_name: str, value: str,
) -> None:
    """Store a credential in the secrets directory.
    
    Secrets live OUTSIDE the data directory.
    Never readable by the agent. Never in any context window.
    """
    secrets_dir = Path(self._secrets_dir) / _safe_name(tenant_id)
    secrets_dir.mkdir(parents=True, exist_ok=True)
    
    secret_path = secrets_dir / f"{capability_name}.key"
    secret_path.write_text(value.strip())
    
    # Set restrictive permissions
    secret_path.chmod(0o600)
```

### Credential resolution at startup

```python
def resolve_credentials(
    server_config: dict, tenant_id: str,
) -> dict[str, str]:
    """Resolve credential references to actual values.
    
    Reads the credential file keyed by capability name from secrets/.
    Injects the value into the env_template.
    Falls back to environment variables.
    """
    credentials_key = server_config.get("credentials_key", "")
    env_template = server_config.get("env_template", {})
    resolved = {}
    
    # Load credential value
    credential_value = ""
    if credentials_key:
        secret_path = Path(SECRETS_DIR) / _safe_name(tenant_id) / f"{credentials_key}.key"
        if secret_path.exists():
            credential_value = secret_path.read_text().strip()
    
    # Apply to env template
    for key, template in env_template.items():
        if "{credentials}" in template:
            if credential_value:
                resolved[key] = template.replace("{credentials}", credential_value)
            else:
                # Fall back to environment variable with same name as key
                resolved[key] = os.getenv(key, "")
        else:
            resolved[key] = template
    
    return resolved
```

-----

## Component 4: Installation Flow

The full install flow, orchestrated between the agent (in system space) and the kernel:

```
User: "Connect my calendar"
  → Router sends to system space (system-management intent)
  
Agent: "I can connect Google Calendar. You'll need an API key from 
        Google Cloud Console. Go to console.cloud.google.com, 
        create a project, enable the Calendar API, and create 
        credentials. Copy the JSON key file contents.
        
        When you have your key ready, reply with exactly:
        secure api
        
        This will put the system into secure mode — your NEXT message
        after that will go directly to encrypted storage and won't be
        seen by any agent."

User: "secure api"
  → Handler intercepts, sets secure input mode
  
Handler: "Secure input mode active for google-calendar. Your next 
          message will NOT be seen by any agent — it will go directly 
          to encrypted storage as your google-calendar API key. 
          Send your key now."

User: [pastes API key]
  → Handler intercepts, writes to secrets/, calls connect_one()
  
Handler: "Key stored securely. google-calendar is now connected! 
          You can start using it right away."
  → Registry updated, capabilities-overview.md refreshed
  → tool.installed event emitted

Agent: [resumes normal] "Calendar is live. I can check your schedule, 
        create events, and find free time. What would you like to do?"
```

### Post-connection kernel actions

After `connect_one()` succeeds:

```python
async def _connect_after_credential(
    self, tenant_id: str, capability_name: str,
) -> bool:
    """Connect an MCP server after credentials are stored."""
    # Get server config from known.py or existing config
    cap = self.registry.get(capability_name)
    if not cap:
        return False
    
    # Build server parameters with resolved credentials
    server_config = self._build_server_config(cap, tenant_id)
    
    # Register and connect
    self.mcp.register_server(capability_name, server_config)
    success = await self.mcp.connect_one(capability_name)
    
    if success:
        # Update registry
        tools = self.mcp.get_tool_definitions().get(capability_name, [])
        cap.status = CapabilityStatus.CONNECTED
        cap.tools = [t["name"] for t in tools]
        
        # Persist config to system space
        await self._persist_mcp_config(tenant_id)
        
        # Refresh capabilities-overview.md
        system_space = await self._get_system_space(tenant_id)
        if system_space:
            await self._write_capabilities_overview(tenant_id, system_space.id)
        
        # Emit event
        await emit_event(
            self.events, EventType.TOOL_INSTALLED,
            tenant_id, "mcp_installer",
            payload={
                "capability_name": capability_name,
                "tool_count": len(cap.tools),
                "universal": cap.universal,
            },
        )
    
    return success
```

### Persisting config

```python
async def _persist_mcp_config(self, tenant_id: str) -> None:
    """Write current MCP config to system space's mcp-servers.json."""
    system_space = await self._get_system_space(tenant_id)
    if not system_space:
        return
    
    # Build config from current registry state
    config = {"servers": {}, "uninstalled": []}
    
    for cap in self.registry.get_all():
        if cap.status == CapabilityStatus.CONNECTED and cap.server_name:
            config["servers"][cap.name] = {
                "display_name": cap.display_name,
                "command": self._get_server_command(cap.name),
                "args": self._get_server_args(cap.name),
                "env": self._get_server_env_refs(cap.name),
                "universal": cap.universal,
                "tool_effects": cap.tool_effects,
            }
        elif cap.status == CapabilityStatus.SUPPRESSED:
            config["uninstalled"].append(cap.name)
    
    await self.files.write_file(
        tenant_id, system_space.id,
        "mcp-servers.json",
        json.dumps(config, indent=2),
        "MCP server configurations — managed by the system",
    )
```

-----

## Component 5: Uninstall / Disconnect

```python
async def _disconnect_capability(
    self, tenant_id: str, capability_name: str,
) -> bool:
    """Disconnect an MCP server and update all state."""
    success = await self.mcp.disconnect_one(capability_name)
    
    if success:
        # SUPPRESSED — user explicitly removed it.
        # AVAILABLE means "never explicitly removed."
        # SUPPRESSED means "user disconnected, don't show in catalog."
        cap = self.registry.get(capability_name)
        if cap:
            cap.status = CapabilityStatus.SUPPRESSED
            cap.tools = []
        
        # Update persisted config (adds to uninstalled list)
        await self._persist_mcp_config(tenant_id)
        
        # Refresh capabilities-overview.md
        system_space = await self._get_system_space(tenant_id)
        if system_space:
            await self._write_capabilities_overview(tenant_id, system_space.id)
        
        # Emit event
        await emit_event(
            self.events, EventType.TOOL_UNINSTALLED,
            tenant_id, "mcp_installer",
            payload={"capability_name": capability_name},
        )
    
    return success
```

### Credentials NOT deleted

On disconnect, credentials in `secrets/` are preserved. If the user reconnects later, they don't need to re-enter them. The agent can offer: "Want me to remove your stored credentials too?" — that's a separate explicit action.

### Spaces unaffected

Spaces that had this capability in `active_tools` keep the entry — but the tool won't appear in their tool list since it's no longer CONNECTED. If the user reconnects, the tools reappear without reconfiguration.

-----

## Component 6: Known Catalog Transition

`known.py` becomes the "available but not connected" catalog:

- **Before 3B+:** `known.py` is the only source. Hardcoded at startup.
- **After 3B+:** Two sources merge. `known.py` provides defaults. `mcp-servers.json` provides user-configured state.
- **Merge precedence:** `mcp-servers.json` wins on conflict. If a user changed a setting, it persists.
- **SUPPRESSED entries:** known.py entries that appear in the `uninstalled` list are suppressed from AVAILABLE. User who disconnected calendar doesn't see it reappearing.
- **Future (4A):** known.py shrinks as discovery from ClawHub/marketplace expands. Eventually known.py may only contain first-party integrations.

### OAuth-gated capabilities

Some MCP servers require OAuth (browser redirect). Discord/SMS can't do this.

```python
@dataclass
class CapabilityInfo:
    # ... existing fields ...
    requires_web_interface: bool = False
    # If True, cannot be installed via text/Discord.
    # Agent explains: "This needs a browser login. It'll work 
    # once we have a web interface."
```

Google Calendar and Gmail should be marked `requires_web_interface: True` if they use OAuth. API-key-based alternatives can be installed via text.

The agent's system prompt includes: "If a capability requires a web interface to connect, explain that it can't be set up in this channel yet and will be available when the web interface ships."

-----

## Component 7: Event Types

**Modified file:** `kernos/kernel/events.py`

```python
class EventType(str, Enum):
    # ... existing types ...
    TOOL_INSTALLED = "tool.installed"
    TOOL_UNINSTALLED = "tool.uninstalled"
```

Payload for both: `capability_name`, `tool_count` (installed only), `universal` (installed only).

-----

## Component 8: System Prompt Addition

Add to the system space posture/template:

```
You can help users connect and manage their tools. When a user wants 
to connect a new tool:

1. Identify the capability from the known catalog
2. Explain what's needed (API key, account setup, etc.)
3. Walk them through getting the credential
4. For the credential handoff, instruct them: "When you have your key 
   ready, reply with exactly: secure api"
5. The system handles the rest — you'll be told if it succeeded

NEVER ask users to paste API keys directly in conversation.
ALWAYS use the "secure api" flow for credentials.

If a capability requires a web interface (marked in the catalog), 
explain that it can't be set up in this channel yet.
```

-----

## Implementation Notes

**`_infer_pending_capability()` is best-effort.** If the handler can't determine which capability needs credentials when "secure api" is sent, it asks. This is a graceful fallback, not a failure. The common case (user was just discussing a specific tool in system space) will match correctly.

**Secrets directory location.** Configurable via `KERNOS_SECRETS_DIR` env var. Default: `./secrets/` (sibling to `./data/`, not inside it). The handler needs this path at construction time.

**Config file written by kernel, not agent.** The agent doesn't call `write_file("mcp-servers.json", ...)` directly. The kernel's install/uninstall functions write the config. The agent CAN `read_file("mcp-servers.json")` to show the user what's configured.

**Timeout notification (Kabe).** On 10-minute secure input timeout, the handler sends a message explaining what happened. Without this, the user's next message (hours later) would be processed normally and they'd wonder why their key didn't work.

**`server_name` on CapabilityInfo (Kit).** `_persist_mcp_config` references `cap.server_name` but verify this field exists on the dataclass. It's in `known.py` entries but may need explicit `server_name: str = ""` if not already present. Claude Code should verify and add if missing.

-----

## Implementation Order

1. **CapabilityStatus.SUPPRESSED** — new enum value
2. **`requires_web_interface` on CapabilityInfo** — new field, update known.py
3. **`connect_one()` and `disconnect_one()`** — MCPClientManager runtime connection
4. **Secrets directory** — `_store_credential()`, `resolve_credentials()`, directory creation
5. **SecureInputState + handler intercept** — mode flag, "secure api" detection, timeout, credential consumption
6. **`_infer_pending_capability()`** — system space conversation scan
7. **`_connect_after_credential()`** — registry update, config persist, capabilities-overview refresh, event emit
8. **Config persistence** — `_persist_mcp_config()`, `mcp-servers.json` schema, read on startup
9. **Startup merge flow** — known.py + mcp-servers.json + suppress uninstalled + connect
10. **`_disconnect_capability()`** — disconnect, suppress, persist, event emit
11. **EventTypes** — TOOL_INSTALLED, TOOL_UNINSTALLED
12. **System prompt addition** — secure credential collection script
13. **Tests** — connect_one/disconnect_one, secure input mode (trigger, timeout, consumption, normal-after-timeout), config persistence (write, read, merge, suppress), credential storage (write, resolve, isolation), capability inference, startup merge order, SUPPRESSED status filtering, requires_web_interface handling
14. **Live test**

-----

## What Claude Code MUST NOT Change

- Compaction system (2C)
- Retrieval system (2D)
- Router logic (2B-v2)
- Entity resolution (2A)
- Dispatch Interceptor (3D) — new tools are gated automatically
- Tool Scoping (3B) — install uses existing activation infrastructure
- File system (3A) — config files use existing file primitives
- Soul data model

-----

## Acceptance Criteria

1. **connect_one() connects a server at runtime.** Register a server, call connect_one(), verify tools are discovered and registry is CONNECTED. Verified.

2. **disconnect_one() disconnects a server at runtime.** Connected server, call disconnect_one(), verify registry set to SUPPRESSED, tools cleared, added to uninstalled list. Verified.

3. **"secure api" activates secure input mode.** User sends "secure api" → handler returns secure mode message → next message bypasses pipeline entirely. Verified by checking no conversation store entry, no LLM call, no extraction.

4. **Credential stored in secrets directory.** After secure input, credential file exists at `secrets/{tenant_id}/{capability_name}.key` with correct permissions. Verified.

5. **Credential NEVER enters pipeline.** The message containing the API key does not appear in: conversation store, event stream, knowledge entries, audit log. Verified by inspecting all stores after credential submission.

6. **10-minute timeout works.** Set secure mode, wait > 10 minutes, send a message → message processed normally (not consumed as credential). User receives timeout explanation. Verified.

7. **Timeout notification sent (Kabe).** On timeout, user receives a message explaining the session expired. Verified.

8. **Config persists across restart.** Install a capability, write config, simulate restart (re-run startup merge), verify capability reconnects from persisted config. Verified.

9. **Uninstalled entries suppressed.** Disconnect a capability, verify it appears in `uninstalled` list, verify it doesn't show as AVAILABLE in system prompt or capabilities-overview.md. Verified.

10. **Startup merge order correct.** known.py defaults + mcp-servers.json overlay + suppress uninstalled. Persisted config wins on conflict. Verified.

11. **capabilities-overview.md refreshed.** After connect/disconnect, the system space doc reflects current state. Verified.

12. **tool.installed event emitted.** After successful connection, event stream contains TOOL_INSTALLED with capability_name and tool_count. Verified.

13. **tool.uninstalled event emitted.** After disconnection, event stream contains TOOL_UNINSTALLED. Verified.

14. **Credentials preserved on disconnect.** After disconnect, `secrets/{tenant_id}/{capability_name}.key` still exists. Verified.

15. **OAuth capability handled gracefully.** Capability with `requires_web_interface: True` → agent explains it can't be set up in this channel. No secure input mode triggered. Verified.

16. **Agent uses prescribed script.** In system space, when credential is needed, agent says "reply with exactly: secure api" — not "paste your key here." Verified by inspecting agent response.

17. **All existing tests pass.** New tests cover all components.

-----

## Live Verification

Follow the Live Testing Protocol in `tests/live/PROTOCOL.md`.

### Test Table

| Step | Action | Expected |
|---|---|---|
| 1 | Send: "I want to connect a new tool" (route to system space) | Agent lists available capabilities from known catalog. |
| 2 | Send: "Connect Google Calendar" | Agent identifies capability, explains setup requirements. If OAuth-required: explains limitation. If API-key: begins credential collection script. |
| 3 | Send: "secure api" | Handler intercepts. Returns secure mode message with capability name and instructions. |
| 4 | Send: [test API key value] | Handler intercepts. Credential stored in secrets/. Connection attempted. Response indicates success or failure. |
| 5 | Check secrets directory | File exists at `secrets/{tenant_id}/google-calendar.key` (or equivalent). Permissions 600. |
| 6 | Check conversation store | The API key message does NOT appear in any conversation record. |
| 7 | Check capabilities-overview.md in system space | Reflects newly connected tool. |
| 8 | Check event stream | TOOL_INSTALLED event present with correct payload. |
| 9 | Switch to another space. Send: "What's on my calendar?" | If calendar connected successfully: tools available (via universal or request_tool). Gate fires for writes. |
| 10 | Return to system space. Send: "Disconnect calendar" | Capability disconnected. Registry downgraded. Config updated. capabilities-overview.md refreshed. |
| 11 | Check mcp-servers.json | Uninstalled list contains the disconnected capability. |
| 12 | Simulate restart (re-run startup merge) | Persisted configs load correctly. Uninstalled entries suppressed. |

Write results to `tests/live/LIVE-TEST-3B+.md`.

-----

## Post-Implementation Checklist

Claude Code must complete ALL of the following before marking this spec done:

- [ ] All tests pass (existing + new)
- [ ] Spec file moved to `specs/completed/SPEC-3B+-MCP-INSTALLATION.md`
- [ ] `DECISIONS.md` NOW block updated (status, owner, action, test count)
- [ ] `docs/TECHNICAL-ARCHITECTURE.md` updated:
  - New section: MCP Installation (config persistence, connect_one/disconnect_one, secure credential handoff, startup merge)
  - Update MCPClientManager section: connect_one, disconnect_one
  - Update CapabilityStatus: SUPPRESSED
  - Update CapabilityInfo: requires_web_interface
  - Update Handler section: secure input mode, credential intercept
  - Update EventType list: TOOL_INSTALLED, TOOL_UNINSTALLED
  - Update "Last updated" line
  - Update test count
- [ ] Live test results written to `tests/live/LIVE-TEST-3B+.md`
- [ ] Live test script at `tests/live/run_3b_plus_live.py`
- [ ] Any new audit findings documented

-----

## Design Decisions This Spec Encodes

| Decision | Choice | Why |
|---|---|---|
| Config in system space as JSON file | Not env vars, not database | Readable by agent (for display), writable by kernel (for install), persists via 3A file primitives. Credentials referenced by capability name, not env var names. (Kit bug fix) |
| Credential keyed by capability name | Not by env var ref name | `_store_credential` writes `{capability_name}.key`, `resolve_credentials` reads by same key. One convention, no mismatch. (Kit bug fix) |
| Disconnect = SUPPRESSED | Not AVAILABLE | User explicitly removed it. AVAILABLE = "never explicitly removed." SUPPRESSED = hidden from catalog, in uninstalled list. (Kit) |
| Credential isolation via handler intercept | Not conversation-level | "secure api" → mode flag → next message bypasses entire pipeline. Credentials never reach LLM, extraction, or storage. (Kabe) |
| Agent prescribes exact trigger phrase | "reply with exactly: secure api" | Reliable detection. Users won't remember arbitrary phrases without clear instruction. (Kit) |
| 10-minute timeout with notification | Not infinite, not silent | Without timeout: stale mode consumes wrong message. Without notification: user doesn't know why key failed. (Kit + Kabe) |
| SUPPRESSED status for uninstalled | Not deleted from registry | User action overrides catalog default. Suppressed entries can be re-installed. (Kit) |
| OAuth deferred to 4C | Not blocked on web interface | Discord/SMS can't do browser redirects. Mark capabilities as requires_web_interface. API-key installs work now. (Kit) |
| tool.installed/uninstalled events | Same pattern as DISPATCH_GATE | Audit trail, trace data, future automation. Lightweight. (Kit) |
| Credentials preserved on disconnect | Not deleted | User may reconnect. Explicit "remove my credentials" is a separate action. |
| Kernel writes config, agent reads it | Agent doesn't write mcp-servers.json directly | Install is a kernel operation. Agent provides the UX. Clean separation. |
| Startup merge: known.py → config → suppress → connect | Explicit ordering | Persisted configs win. Uninstalled entries don't reappear. Deterministic. |

# SPEC: Lazy Tool Loading — Usage-Based Schema Loading with Session Decay

**Status:** DRAFT — Kit reviewed intent paragraphs. Kabe approval needed.  
**Author:** Architect  
**Date:** 2026-03-22  
**Depends on:** 3B (capability registry), Reasoning Service (tool loop)  
**Replaces:** Per-space active_tools pre-configuration from 3B  
**Prerequisite for:** Gmail integration, any future MCP additions  
**Type:** Foundational change to how tools are presented to the agent.

---

## Objective

Replace the always-present full tool schema injection (~11,800 tokens on every message) with a compact directory (~400 tokens) plus on-demand schema loading when tools are actually used. Tools load on first use, stay loaded for the session, and unload at session boundary.

**Token savings:** 84% reduction on messages that don't use heavy tools. The worst case (all tools loaded) is identical to today.

**Why now:** Adding Gmail (16 tools, ~3,000+ tokens) without lazy loading pushes tool context to ~15,000 tokens per message. Unsustainable. This is a prerequisite for any tool expansion.

---

## The Model

1. **Every space starts with a compact tool directory** in the system prompt showing what's available by category with one-line descriptions. ~400 tokens.

2. **Kernel tools are always loaded** — remember, manage_schedule, read_doc, read_file, list_files, write_file, delete_file, manage_covenants, manage_channels, manage_tools, read_soul, update_soul, dismiss_whisper. These are small (~1,500 tokens total) and used constantly.

3. **Calendar READ tools are pre-loaded** — list-events, search-events, get-event, get-freebusy, list-calendars, get-current-time. These are the most-called MCP tools and relatively small (~1,500 tokens). Avoids first-use round-trip on the most common operations.

4. **All other tools are lazy-loaded.** When the agent generates a tool_use block for a tool that's in the directory but not loaded, the tool loop catches it, loads the full schema, and re-runs the LLM call with the schema available. One extra round-trip on first use, transparent to the agent and user.

5. **Tools stay loaded for the session.** Second and subsequent uses are direct — no extra round-trip. The loaded set is per-space, in-memory.

6. **Tools unload at session boundary** — conversation window reset, compaction, or server restart. Fresh session = fresh directory = tools load as needed.

---

## Token Budget

| State | Tool tokens | vs Today |
|-------|------------|----------|
| Today (all 37 schemas always) | ~11,800 | baseline |
| Directory + kernel + calendar reads (baseline) | ~3,400 | -71% |
| + calendar writes loaded | ~8,900 | -25% |
| + browser loaded | ~10,400 | -12% |
| + everything (worst case) | ~11,800 | same |
| Future: + Gmail (16 tools, lazy) | ~3,400 baseline | vs ~15,000 if always-present |

---

## Implementation

### Component 1: The Tool Directory

Replace the capability prompt section of the system prompt. Instead of injecting full schemas for all connected capabilities, inject a compact directory.

In `kernos/capability/registry.py`, add `build_tool_directory()`:

```python
def build_tool_directory(self, space=None) -> str:
    """Build a compact tool directory for the system prompt.
    
    Lists available capabilities by category with one-line descriptions.
    Full schemas are NOT included — they load on demand.
    """
    lines = ["AVAILABLE TOOLS:"]
    
    # Group capabilities by category
    by_category = {}
    for cap in self._capabilities.values():
        if cap.status == CapabilityStatus.CONNECTED:
            cat = cap.category or "other"
            by_category.setdefault(cat, []).append(cap)
    
    for category, caps in sorted(by_category.items()):
        for cap in caps:
            tool_count = len(cap.tool_effects) if cap.tool_effects else "?"
            lines.append(f"• {cap.display_name}: {cap.description} ({tool_count} tools)")
    
    lines.append("")
    lines.append("To use any tool, call it by name. The system loads it automatically.")
    
    return "\n".join(lines)
```

### Component 2: The Loaded Set

Track which tools are loaded per-space in memory. On the reasoning service or handler:

```python
# Per-space loaded tool tracking
self._loaded_tools: dict[str, set[str]] = {}  # space_id -> set of tool names

def get_loaded_tools(self, space_id: str) -> set[str]:
    """Get the set of tool names currently loaded for a space."""
    return self._loaded_tools.get(space_id, set())

def load_tool(self, space_id: str, tool_name: str):
    """Add a tool to the loaded set for a space."""
    if space_id not in self._loaded_tools:
        self._loaded_tools[space_id] = set()
    self._loaded_tools[space_id].add(tool_name)

def clear_loaded_tools(self, space_id: str):
    """Clear loaded tools for a space (session boundary)."""
    self._loaded_tools.pop(space_id, None)
```

### Component 3: Tool List Assembly

Modify the tool list assembly to use the loaded set instead of all schemas:

In `kernos/kernel/reasoning.py` or wherever the tool list is assembled for LLM calls:

```python
def assemble_tool_list(self, space_id: str, registry, kernel_tools) -> list[dict]:
    """Assemble the tool list for an LLM call.
    
    Always includes: kernel tools + pre-loaded tools.
    Conditionally includes: tools in the loaded set for this space.
    """
    tools = []
    
    # Always: kernel tools (full schemas)
    tools.extend(kernel_tools)
    
    # Always: pre-loaded MCP tools (calendar reads)
    for tool_def in registry.get_preloaded_tools():
        tools.append(tool_def)
    
    # Loaded: tools that have been used in this space's session
    loaded = self.get_loaded_tools(space_id)
    for tool_name in loaded:
        tool_def = registry.get_tool_schema(tool_name)
        if tool_def:
            tools.append(tool_def)
    
    return tools
```

### Component 4: The Load-on-Use Mechanism

In the reasoning service's tool loop, when the agent generates a tool_use block for an unknown tool:

```python
# In the tool loop, when processing a tool_use block:
tool_name = block.name

# Check if this tool is in the loaded set or always-loaded
if tool_name not in known_tools:
    # Check the directory — is this tool available?
    tool_def = registry.get_tool_schema(tool_name)
    
    if tool_def is None:
        # Tool doesn't exist at all
        result = f"Tool '{tool_name}' is not available."
    else:
        # Tool exists but wasn't loaded. Load it and re-run.
        self.load_tool(space_id, tool_name)
        logger.info(
            "TOOL_LOAD: tool=%s space=%s (first use, re-running with schema)",
            tool_name, space_id
        )
        
        # Re-run the LLM call with the schema now available
        # The agent will regenerate with correct parameters
        updated_tools = self.assemble_tool_list(space_id, registry, kernel_tools)
        
        # Add a system hint so the agent knows the tool is now available
        retry_messages = messages + [{
            "role": "user",
            "content": f"[SYSTEM] The tool '{tool_name}' is now loaded. "
                       f"Please proceed with your {tool_name} call."
        }]
        
        response = await self._provider.complete(
            messages=retry_messages,
            tools=updated_tools,
            max_tokens=max_tokens,
            system=system_prompt,
        )
        
        # Process the retry response through the tool loop normally
        # (it should now contain a correct tool_use block)
```

### Component 5: Pre-Loaded Tool Configuration

In the registry, mark which tools are pre-loaded (always have full schemas in context):

```python
# In known.py or registry configuration:
PRELOADED_TOOLS = {
    # Calendar reads — most-called MCP tools, small schemas
    "list-events",
    "search-events", 
    "get-event",
    "get-freebusy",
    "list-calendars",
    "get-current-time",
}
```

Kernel tools don't need to be in this set — they're always included by the kernel tool assembly, not the MCP tool assembly.

### Component 6: Session Boundary Cleanup

On session boundary events (server restart, compaction, long inactivity), clear the loaded set:

```python
# On server restart: _loaded_tools starts empty (in-memory, not persisted)
# On compaction: clear_loaded_tools(space_id)
# On conversation window reset: clear_loaded_tools(space_id)
```

No persistence needed. Fresh session = fresh directory.

### Component 7: Registry Methods

Add to `CapabilityRegistry`:

```python
def get_preloaded_tools(self) -> list[dict]:
    """Get full schemas for pre-loaded tools only."""
    result = []
    for tool_name in PRELOADED_TOOLS:
        schema = self.get_tool_schema(tool_name)
        if schema:
            result.append(schema)
    return result

def get_tool_schema(self, tool_name: str) -> dict | None:
    """Get the full schema for a specific tool by name."""
    # Look through connected capabilities for this tool
    for cap in self._capabilities.values():
        if cap.status == CapabilityStatus.CONNECTED:
            for tool in cap.discovered_tools:
                if tool["name"] == tool_name:
                    return tool
    return None

def get_all_tool_names(self) -> set[str]:
    """Get all available tool names (for directory validation)."""
    names = set()
    for cap in self._capabilities.values():
        if cap.status == CapabilityStatus.CONNECTED:
            for tool in cap.discovered_tools:
                names.add(tool["name"])
    return names
```

---

## What Changes from 3B

| 3B Concept | Lazy Tools Replacement |
|------------|----------------------|
| active_tools per space (pre-configured) | loaded_tools per space (usage-driven) |
| universal flag on capabilities | Pre-loaded set (calendar reads) — based on usage frequency, not capability flag |
| request_tool meta-tool | Automatic load-on-use — agent just calls the tool, system loads it |
| build_capability_prompt (all schemas) | build_tool_directory (compact) + assemble_tool_list (loaded only) |

The `universal` flag on CapabilityInfo can be deprecated. The `active_tools` field on ContextSpace can be repurposed or deprecated. The `request_tool` kernel tool is no longer needed (load-on-use replaces it).

---

## What NOT to Change

- Kernel tool handling — these are always loaded, processed in the handler, no change
- Dispatch gate — evaluates tool calls after generation, independent of loading
- The MCP client manager — still connects to servers, discovers tools. Lazy loading is about PRESENTATION, not CONNECTION.
- Tool effects classification — still read/soft_write/hard_write
- The evaluator tick loop — scheduler and whispers don't go through the schema loading path

---

## Logging

| Log line | When |
|----------|------|
| `TOOL_LOAD: tool={name} space={space}` | Tool loaded on first use |
| `TOOL_DIRECTORY: tools={n} preloaded={n} loaded={n}` | At start of each reasoning call |
| `TOOL_UNLOAD: space={space} cleared={n}` | Session boundary cleanup |

---

## Acceptance Criteria

1. System prompt shows compact tool directory instead of full schemas for non-preloaded tools
2. Kernel tools always have full schemas in the tool list
3. Calendar read tools always have full schemas (pre-loaded)
4. Calendar write tools (create-event, update-event, delete-event) NOT in tool list until used
5. When agent calls an unloaded tool, it's loaded and the call succeeds (one extra round-trip)
6. Second call to the same tool in the same space is direct (no extra round-trip)
7. After server restart, loaded sets are empty (fresh directory)
8. Token count per message decreases significantly (~71% baseline reduction)
9. TOOL_LOAD logged at INFO on first use
10. All existing tool-calling functionality works (calendar, search, browse, schedule, remember)
11. All existing tests pass

---

## Live Test

1. Restart bot
2. "What's on my calendar today?" — list-events works immediately (pre-loaded). Console shows NO TOOL_LOAD.
3. "Create an event called Test for tomorrow at 2pm" — first use of create-event. Console shows `TOOL_LOAD: tool=create-event`. Event created successfully (may take one extra LLM call).
4. "Update that event to 3pm" — update-event is NOT pre-loaded but create-event load triggered it. Check if update-event also needs loading.
5. "Search the web for Anthropic" — brave_web_search. Check if pre-loaded or lazy.
6. "Remind me in 2 minutes" — manage_schedule, kernel tool, always loaded. No TOOL_LOAD.
7. Check token counts in console: ctx_tokens_est should be significantly lower than previous sessions (~20k → ~12k for same conversation).
8. Regression: all scheduler, whisper, covenant, awareness functionality still works.

---

## Design Decisions

| Decision | Choice | Why | Who |
|----------|--------|-----|-----|
| Decay mechanism | Session boundary (Option B) | Simplest, covers common case. No per-tool tracking. | Kit |
| Pre-load calendar reads | Yes | Most-called MCP tools, 1,500 tokens, avoids first-use round-trip | Kit |
| Agent awareness of unloaded tools | Let it try, kernel catches and loads | Transparent to agent, no explicit load_tool call needed | Kit |
| Hallucination impact | Expected decrease | Less noise = clearer decisions. Directory gives WHICH, schema gives HOW | Kit |
| Replaces active_tools from 3B | Yes | Usage-based is better than pre-configured. Tools self-organize. | Architect + Kabe |
| Replaces universal flag | Yes | Pre-loaded set based on usage frequency, not capability flag | Architect |
| Replaces request_tool | Yes | Automatic load-on-use is simpler than explicit request | Architect |

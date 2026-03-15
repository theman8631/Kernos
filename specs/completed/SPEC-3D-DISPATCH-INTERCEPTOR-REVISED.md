# SPEC-3D: Dispatch Interceptor

**Status:** READY FOR REVIEW
**Depends on:** SPEC-2D NL Contract Parser (CovenantRules), SPEC-3B Tool Scoping (tool_effects)
**Design source:** Brainstorm: 3D Dispatch Interceptor — Permission Model (Kabe + Kit, 2026-03-14)
**Objective:** Gate write/action tool calls before execution. Reads pass silently. Writes require either an explicit user instruction in the current message, a matching covenant rule, or user confirmation. The gate cannot be bypassed by model inference.

**What changes for the user:** The agent stops acting without permission on anything that changes state. Calendar reads happen freely. Calendar writes get checked — if the user said "book it for 4pm" in this message, it proceeds. If there's a standing covenant ("always add calendar entries from email"), it proceeds. Otherwise, the agent asks first. Over time, the gate gets quieter as covenants accumulate through normal use.

**What changes architecturally:** A gate function inserted into the tool-use loop in ReasoningService, between tool call proposal and tool execution. Uses `tool_effects` from CapabilityInfo to classify calls. Three-step authorization check: explicit instruction → covenant/permission lookup → ask user. One Haiku call for covenant matching when needed. Permission overrides stored on TenantProfile.

**What this is NOT:**
- Not proactive behavior gating (3C builds on this but adds its own trigger mechanisms)
- Not MCP installation (3B+)
- Not a replacement for covenant rules — the gate uses them, doesn't create them
- Not per-space permissions — gate is system-wide

-----

## The Two Failure Modes This Prevents

1. **Inference overshoot:** User is thinking out loud about a future change. Agent infers intent and acts. User wasn't ready. The gate fires before execution — user sees what the agent wants to do and can deny it.

2. **Ambiguous permission scope:** User says "check this inbox, should we just delete it all?" Agent doesn't know if that's rhetorical or an instruction. Without a clear covenant, the gate defaults to ask. Ambiguity resolves to caution, not action.

-----

## Two Layers of Protection

**Layer 1 — Covenant contract:** A developed understanding between agent and user about when to act and when to ask. Built over time through conversation. "Always add calendar entries when I say so." "Never delete email without explicit confirmation." This layer grows richer over months and reduces gate friction organically.

**Layer 2 — The gate:** Mechanical fallback when no covenant applies. Per-capability permission settings. Always fires in the absence of explicit permission. Cannot be bypassed by model inference.

The covenant layer reduces gate noise over time. The gate layer ensures safety before the covenant is established. Both are necessary.

-----

## Component 1: Gate Classification

Every tool call is classified using `tool_effects` from CapabilityInfo before the gate logic runs.

```python
def _classify_tool_effect(
    self, tool_name: str, active_space: ContextSpace | None,
) -> str:
    """Classify a tool call's effect level.

    Returns: "read", "soft_write", "hard_write", or "unknown"
    Kernel tools have hardcoded classifications.
    MCP tools use tool_effects from CapabilityInfo.
    Unknown defaults to "hard_write" (safe default).
    """
    # Kernel tools — hardcoded
    KERNEL_READS = {"remember", "list_files", "read_file", "request_tool"}
    KERNEL_WRITES = {"write_file", "delete_file"}

    if tool_name in KERNEL_READS:
        return "read"
    if tool_name in KERNEL_WRITES:
        return "soft_write"

    # MCP tools — look up from registry
    for cap in self._registry.get_all():
        if tool_name in cap.tool_effects:
            return cap.tool_effects[tool_name]
        if tool_name in cap.tools and tool_name not in cap.tool_effects:
            return "unknown"  # Tool exists but no effect declared

    return "unknown"  # Not found at all
```

### Effect levels

| Effect | Gate behavior | Examples |
|---|---|---|
| `read` | Always bypass. Silent. | list-events, search-email, remember, list_files, read_file, request_tool |
| `soft_write` | Gate by default. Can be set to always-allow. | create-event, send-email, write_file |
| `hard_write` | Gate by default. Can be set to always-allow. | delete-event, delete-email, delete_file |
| `unknown` | Treated as hard_write. Safe default. | Any tool without a declared effect level |

-----

## Component 2: The Gate — Three-Step Authorization

When a write tool call is intercepted (soft_write, hard_write, or unknown), the gate runs this logic:

```python
async def _gate_tool_call(
    self,
    tool_name: str,
    tool_input: dict,
    effect: str,
    user_message: str,
    tenant_id: str,
    active_space_id: str,
) -> GateResult:
    """Three-step authorization for write tool calls.

    Returns GateResult with: allowed (bool), reason (str), method (str)
    """
    # Step 0: Check must_not covenants FIRST — they override everything
    # A must_not rule ("never send emails without asking") blocks even
    # explicit instructions. This MUST run before the fast path.
    if await self._has_prohibiting_covenant(tool_name, tenant_id, active_space_id):
        return GateResult(
            allowed=False,
            reason="covenant_prohibited",
            method="must_not_block",
            proposed_action=self._describe_action(tool_name, tool_input),
        )

    # Step 1: Explicit instruction in current message (fast path, no LLM)
    if self._explicit_instruction_matches(tool_name, tool_input, user_message):
        return GateResult(allowed=True, reason="explicit_instruction", method="fast_path")

    # Step 2: Permission override or covenant authorization (one Haiku call)
    auth = await self._check_permission_or_covenant(
        tool_name, tool_input, tenant_id, active_space_id,
    )
    if auth.allowed:
        return auth

    # Step 3: Ask user — don't act
    return GateResult(
        allowed=False,
        reason="no_authorization",
        method="ask_user",
        proposed_action=self._describe_action(tool_name, tool_input),
    )
```

### Step 0: Prohibitive Covenant Check (must_not rules)

```python
async def _has_prohibiting_covenant(
    self, tool_name: str, tenant_id: str, active_space_id: str,
) -> bool:
    """Check if any must_not covenant rule prohibits this tool call.

    must_not rules override EVERYTHING — including explicit instructions.
    "Never send emails without asking me first" blocks even when the
    user says "send this email" because the covenant is prohibitive.
    This runs BEFORE the fast path to prevent bypass.
    """
    cap_name = self._get_capability_for_tool(tool_name)
    rules = await self._state.query_covenant_rules(
        tenant_id,
        context_space_scope=[active_space_id, None],
        active_only=True,
    )
    for rule in rules:
        if rule.rule_type != "must_not":
            continue
        # Check if the rule's description references this capability or tool
        desc_lower = rule.description.lower()
        if cap_name and cap_name.lower() in desc_lower:
            return True
        if tool_name.lower() in desc_lower:
            return True
        # Check for domain keywords (email, calendar, etc.)
        domain_keywords = self._get_domain_keywords(tool_name)
        if any(kw in desc_lower for kw in domain_keywords):
            return True
    return False
```

This check is fast — it's a structured data lookup against CovenantRules, no LLM call. It only checks `must_not` rules. If a prohibitive rule matches, the gate blocks immediately and the fast path never runs.

### Step 1: Explicit Instruction Check (fast path)

```python
def _explicit_instruction_matches(
    self, tool_name: str, tool_input: dict, user_message: str,
) -> bool:
    """Check if the user's current message contains a direct instruction
    for this specific action.

    Imperative verb + this tool's domain = fast path.
    No LLM call. Simple signal matching.
    """
    msg_lower = user_message.lower()

    # Tool-specific instruction signals
    TOOL_SIGNALS: dict[str, list[str]] = {
        # Calendar writes
        "create-event": ["schedule", "book", "set up", "add to calendar", "make an appointment",
                         "create event", "put on my calendar", "block time", "add meeting"],
        "update-event": ["reschedule", "move", "change", "update event", "push back", "move to"],
        "delete-event": ["cancel", "remove event", "delete event", "take off calendar"],
        # Email writes
        "send-email": ["send", "email", "write to", "reply to", "forward"],
        "delete-email": ["delete email", "trash", "remove email"],
        # File writes (kernel tools — already handled by delete_file principle,
        # but included for completeness)
        "write_file": ["create file", "write", "save", "draft"],
        "delete_file": ["delete", "remove", "get rid of", "trash", "clean up",
                        "clear out", "throw away", "discard", "drop", "nuke", "wipe", "erase"],
    }

    signals = TOOL_SIGNALS.get(tool_name, [])
    return any(signal in msg_lower for signal in signals)
```

This is the same pattern as `_check_delete_allowed` from 3A — keyword matching against the current message. For the common case ("book an appointment for 4pm"), this fires immediately with no LLM call.

### Step 2: Permission Override or Covenant Authorization

```python
async def _check_permission_or_covenant(
    self,
    tool_name: str,
    tool_input: dict,
    tenant_id: str,
    active_space_id: str,
) -> GateResult:
    """Check permission overrides (fast) then covenant rules (one Haiku call).

    Permission overrides are checked first — they're a dict lookup.
    Covenant rules require one Haiku call to determine if a rule applies.
    """
    # Check permission overrides first (fast, no LLM)
    cap_name = self._get_capability_for_tool(tool_name)
    if cap_name:
        tenant = await self._state.get_tenant_profile(tenant_id)
        if tenant and tenant.permission_overrides:
            permission = tenant.permission_overrides.get(cap_name)
            if permission == "always-allow":
                return GateResult(
                    allowed=True, reason="permission_override",
                    method="always_allow",
                )
            # "ask" or "allow-once" don't bypass — fall through to covenant check

    # Check covenant rules (one Haiku call)
    rules = await self._state.query_covenant_rules(
        tenant_id,
        context_space_scope=[active_space_id, None],  # space + global
        active_only=True,
    )

    if not rules:
        return GateResult(allowed=False, reason="no_covenants", method="none")

    # Ask Haiku: does any covenant rule authorize this action?
    action_desc = self._describe_action(tool_name, tool_input)
    rules_text = "\n".join(
        f"- [{r.rule_type}] {r.description} (scope: {r.context_space or 'global'})"
        for r in rules
    )

    result = await self._reasoning.complete_simple(
        system_prompt=(
            "You are checking whether a proposed agent action is authorized by "
            "any of the user's standing rules (covenants). "
            "Answer ONLY with: YES, NO, or AMBIGUOUS.\n"
            "YES = a rule explicitly covers this action.\n"
            "NO = no rule covers this action.\n"
            "AMBIGUOUS = a rule might cover this but the scope is unclear."
        ),
        user_content=(
            f"Proposed action: {action_desc}\n\n"
            f"Active covenant rules:\n{rules_text}"
        ),
        max_tokens=16,
        prefer_cheap=True,
    )

    answer = result.strip().upper()
    if answer == "YES":
        return GateResult(allowed=True, reason="covenant_authorized", method="haiku_check")

    # NO or AMBIGUOUS → don't act
    return GateResult(
        allowed=False,
        reason="covenant_denied" if answer == "NO" else "covenant_ambiguous",
        method="haiku_check",
    )
```

### Step 3: Ask User

When neither explicit instruction nor covenant/permission authorizes the action, the gate blocks and surfaces the proposed action to the user.

```python
# In the tool-use loop, when gate returns allowed=False:

if not gate_result.allowed:
    blocked_message = (
        f"[SYSTEM] Action blocked by the dispatch gate. "
        f"Proposed: {gate_result.proposed_action}. "
        f"Reason: no explicit instruction or standing rule authorizes this. "
        f"Ask the user for permission before proceeding. "
        f"If they confirm, you may offer to create a standing rule."
    )
    # Append BEFORE continuing — model must receive this feedback
    tool_results.append({
        "type": "tool_result",
        "tool_use_id": block.id,
        "content": blocked_message,
    })
```

The model then naturally explains to the user: "I wanted to [action] but I don't have permission yet. Should I go ahead?" If the user confirms, the model re-proposes the tool call. This time, the explicit instruction check in Step 1 catches the confirmation ("yes, do it" / "go ahead" / "book it").

### Confirmation creates covenant opportunity

After a user confirms a blocked action, the agent can offer: "Should I always do this without asking?" If yes → create a CovenantRule via the NL Contract Parser. The gate gets quieter through normal use.

```python
CONFIRMATION_SIGNALS = [
    "yes", "go ahead", "do it", "proceed", "confirmed", "approve",
    "that's fine", "ok", "sure", "yep", "yeah",
]

# Add to TOOL_SIGNALS as a universal fallback for any tool:
# If the previous assistant message proposed an action and the user confirms,
# the confirmation is treated as an explicit instruction for that specific action.
```

-----

## Component 3: Permission Overrides on TenantProfile

**Modified file:** `kernos/persistence/` (TenantProfile model)

```python
@dataclass
class TenantProfile:
    # ... existing fields ...
    permission_overrides: dict[str, str] = field(default_factory=dict)
    # Maps capability_name → "ask" | "allow-once" | "always-allow"
    # Default (not in dict) = "ask"
    # "always-allow" bypasses gate for all write tools in this capability
    # "allow-once" not yet implemented — reserved for future use
    # "ask" = explicit default, same as not being in the dict
```

### Permission levels

| Level | Behavior |
|---|---|
| `ask` (default) | Gate fires every time. Agent proposes, user approves or denies. |
| `always-allow` | Bypass gate for this capability's write tools. |
| `allow-once` | Reserved for future use. Not implemented in 3D. |

### Setting permissions

Permissions are managed via the system space. The user says "always allow calendar writes" → the agent (in the system space) updates the tenant's `permission_overrides`. This uses the NL Contract Parser pattern — the agent recognizes a permission instruction, the kernel updates the TenantProfile.

```python
# In the system space, when the user sets a permission:

async def _set_permission_override(
    self, tenant_id: str, capability_name: str, level: str,
) -> None:
    tenant = await self._state.get_tenant_profile(tenant_id)
    if tenant:
        tenant.permission_overrides[capability_name] = level
        await self._state.save_tenant_profile(tenant_id, tenant)
```

### Revocability

All permission overrides are revocable at any time via the system space. "Turn off auto-approve for calendar" → `permission_overrides.pop("google-calendar")`. Nothing is permanent.

-----

## Component 4: Integration into ReasoningService Tool-Use Loop

**Modified file:** `kernos/kernel/reasoning.py`

The gate inserts between tool call proposal and tool execution:

```python
# In the tool-use loop, after extracting tool_name and tool_input:

for block in response.content:
    if block.type != "tool_use":
        continue

    tool_name = block.name
    tool_input = block.input or {}

    # Classify the tool's effect level
    effect = self._classify_tool_effect(tool_name, request.active_space)

    # Gate check for write tools
    if effect in ("soft_write", "hard_write", "unknown"):
        gate_result = await self._gate_tool_call(
            tool_name, tool_input, effect,
            request.input_text, request.tenant_id,
            request.active_space_id,
        )

        # Emit gate event for tracing
        await emit_event(
            self._events, EventType.DISPATCH_GATE,
            request.tenant_id, "dispatch_interceptor",
            payload={
                "tool_name": tool_name,
                "effect": effect,
                "allowed": gate_result.allowed,
                "reason": gate_result.reason,
                "method": gate_result.method,
            },
        )

        logger.info(
            "GATE: tool=%s effect=%s allowed=%s reason=%s method=%s",
            tool_name, effect, gate_result.allowed,
            gate_result.reason, gate_result.method,
        )

        if not gate_result.allowed:
            # Return blocked message as tool_result — model MUST receive this
            result = (
                f"[SYSTEM] Action blocked by dispatch gate. "
                f"Proposed: {gate_result.proposed_action}. "
                f"No explicit instruction or standing rule authorizes this. "
                f"Ask the user for permission before proceeding. "
                f"If they confirm, you may offer to create a standing rule."
            )
            # IMPORTANT: Append blocked result BEFORE continuing so the model
            # gets feedback. Without this, the gate fires silently and the
            # model can't explain anything to the user.
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": result,
            })
            continue

    # Reads and authorized writes proceed normally
    # ... (existing tool execution code)
```

### GateResult dataclass

```python
@dataclass
class GateResult:
    allowed: bool
    reason: str  # "explicit_instruction", "permission_override", "covenant_authorized",
                 # "covenant_denied", "covenant_ambiguous", "no_covenants", "no_authorization"
    method: str  # "fast_path", "always_allow", "haiku_check", "ask_user", "none"
    proposed_action: str = ""  # Human-readable description of what was blocked
```

### Action description helper

```python
def _describe_action(self, tool_name: str, tool_input: dict) -> str:
    """Generate a human-readable description of a tool call."""
    # Summarize the action based on tool name and key input fields
    if tool_name == "create-event":
        summary = tool_input.get("summary", "an event")
        start = tool_input.get("start", "unspecified time")
        return f"Create calendar event: '{summary}' at {start}"
    elif tool_name == "send-email":
        to = tool_input.get("to", "someone")
        subject = tool_input.get("subject", "no subject")
        return f"Send email to {to}: '{subject}'"
    elif tool_name == "delete_file":
        name = tool_input.get("name", "a file")
        return f"Delete file: {name}"
    elif tool_name == "write_file":
        name = tool_input.get("name", "a file")
        return f"Write/update file: {name}"
    else:
        # Generic description
        return f"Execute {tool_name} with {json.dumps(tool_input)[:200]}"
```

-----

## Component 5: Persona / Roleplay Safety

Persona context does not modify gate behavior. All write actions surface to the kernel layer before execution regardless of active persona or roleplay context. The blocked message is always from `[SYSTEM]`, never in-character. The soul is not a persona.

This requires no special implementation — the gate runs in ReasoningService (kernel layer), not in the conversation model. The model reads `[SYSTEM]` messages and breaks character to relay them. This is already the behavior for kernel tool results.

-----

## Component 6: New EventType

**Modified file:** `kernos/kernel/events.py`

```python
class EventType(str, Enum):
    # ... existing types ...
    DISPATCH_GATE = "dispatch.gate"
    # Payload: tool_name, effect, allowed, reason, method
```

Tracing integration: `GATE:` prefix in logger output (consistent with ROUTE:, TOOL_LOOP:, FILE_*, KERNEL_TOOL:, REMEMBER: prefixes from the tracing infrastructure).

-----

## Component 7: delete_file Gate Consolidation

The `_check_delete_allowed()` principle enforcement from 3A is now a subset of the dispatch gate. The gate handles all write tool calls, including `delete_file`. The 3A delete check can be consolidated into the gate's Step 1 (explicit instruction check) — the `delete_file` signals are already in the TOOL_SIGNALS dict.

```python
# In reasoning.py, the delete_file intercept:

# BEFORE (3A):
# if tool_name == "delete_file":
#     delete_allowed = self._check_delete_allowed(request.input_text)
#     if not delete_allowed: ...

# AFTER (3D):
# The gate handles this via _classify_tool_effect("delete_file") → "soft_write"
# → _gate_tool_call() → Step 1 checks TOOL_SIGNALS["delete_file"] → same signals
# The separate _check_delete_allowed() is removed.
# delete_file is still a kernel tool — it's routed through the kernel intercept
# for execution, but gated like any other write tool.
```

This consolidation means there's one gate mechanism, not two. The delete_file signals list moves into TOOL_SIGNALS. The principle is the same — user must explicitly request deletion — but it's enforced by the same code path as every other write tool.

-----

## Implementation Notes

**Confirmation as explicit instruction:** When the gate blocks an action, the model explains what it wanted to do. If the user says "yes, do it" — the model re-proposes the same tool call. Step 1 needs to recognize confirmations as explicit instructions for the previously proposed action. The CONFIRMATION_SIGNALS list covers this. The implementation should check: if the previous assistant message contained a `[SYSTEM] Action blocked` message AND the current user message is a confirmation, treat it as an explicit instruction for the blocked tool. This avoids needing a TTL or state — the conversation itself is the context.

**Cost:** One Haiku call per gated write tool call ONLY when no explicit instruction is found in Step 1. Most tool calls triggered by direct user requests will fast-path. The Haiku call is the fallback for indirect or covenant-authorized actions. Expected cost impact: negligible — most writes follow direct instructions.

**Haiku response parsing:** `result.strip().upper()` against exact strings "YES"/"NO"/"AMBIGUOUS". If Haiku returns a verbose response, it falls through to `covenant_ambiguous` — safe failure direction. `max_tokens=16` constrains output. Add a comment in code noting this intentional behavior.

**Confirmation handling for unknown tools:** `TOOL_SIGNALS.get(tool_name, [])` returns `[]` for tools not in the dict, so confirmations on unknown tools won't match Step 1. The confirmation check must be explicit: if the previous assistant message contained `[SYSTEM] Action blocked` AND the current user message matches `CONFIRMATION_SIGNALS`, treat it as an explicit instruction regardless of tool name.

**No per-message LLM cost:** The gate only fires on write tool calls. Read calls bypass entirely. Messages with no tool calls have zero gate overhead. This preserves the Phase 3 constraint.

-----

## Implementation Order

1. **GateResult dataclass** — data model for gate outcomes
2. **_classify_tool_effect()** — tool effect classification (kernel + registry)
3. **_has_prohibiting_covenant()** — Step 0: must_not rules block before fast path
4. **_explicit_instruction_matches()** — Step 1 fast path with TOOL_SIGNALS
5. **_check_permission_or_covenant()** — Step 2 with permission lookup + Haiku covenant check
6. **_gate_tool_call()** — orchestrator calling Steps 0-3
7. **_describe_action()** — human-readable action descriptions
8. **Integration into tool-use loop** — insert gate between proposal and execution. CRITICAL: append blocked result to tool_results before continue.
9. **permission_overrides on TenantProfile** — field, serialization, backward compat
10. **DISPATCH_GATE EventType** — event emission + GATE: trace logging
11. **Consolidate delete_file** — remove _check_delete_allowed, move signals to TOOL_SIGNALS
12. **Confirmation handling** — recognize confirmations after blocked actions
13. **Tests** — gate classification, fast path, must_not blocking explicit instructions, covenant check (authorized/denied/ambiguous), permission override, gate integration in tool loop, blocked message received by model, confirmation recognition, read bypass, unknown effect handling, delete_file consolidation
14. **Live test**

-----

## What Claude Code MUST NOT Change

- Compaction system (2C)
- Retrieval system (2D) — remember() is classified as "read", bypasses gate
- Router logic (2B-v2)
- Entity resolution (2A)
- File system (3A) — file tools get gated via the universal mechanism, 3A's delete_file principle is consolidated into the gate
- Tool scoping (3B) — scoping determines what tools are visible, the gate determines what tools can execute
- NL Contract Parser (2D) — the gate uses covenant rules, doesn't create them
- Soul data model

-----

## Acceptance Criteria

1. **Read tools bypass gate.** `remember()`, `list_files`, `read_file`, `request_tool`, `list-events` → no gate check, no event, silent pass. Verified.

2. **Write tools gate by default.** `create-event`, `send-email`, `write_file` → gate fires. Verified by checking DISPATCH_GATE event emission.

3. **Unknown tools treated as hard_write.** A tool not in any capability's tool_effects → classified as "unknown" → gated. Verified.

4. **Fast path works.** User says "book a meeting for 4pm" → agent calls create-event → Step 1 matches "book" → gate allows, no Haiku call. Verified by checking gate_result.method == "fast_path".

5. **Covenant authorization works.** Standing covenant "always add calendar entries when I say so" + agent proposes create-event from email context → Step 2 Haiku call → YES → allowed. Verified.

6. **must_not covenants block even explicit instructions.** User has covenant "never send emails without asking me first." User says "send this email." Fast path would match "send" — but Step 0 catches the must_not rule first and blocks. Verified by checking gate_result.reason == "covenant_prohibited".

6. **Covenant denial works.** No matching covenant → Step 2 returns NO → gate blocks → agent asks user. Verified.

7. **Covenant ambiguity → ask.** Ambiguously worded covenant → Step 2 returns AMBIGUOUS → gate blocks (safe default). Verified.

8. **Permission override works.** `permission_overrides["google-calendar"] = "always-allow"` → all calendar write tools bypass gate. Verified.

9. **Gate blocks and surfaces action.** Blocked tool call → model receives `[SYSTEM] Action blocked` → model explains to user what it wanted to do. Verified by inspecting the model's response after a block.

10. **Confirmation after block.** Gate blocks create-event → agent explains → user says "yes, go ahead" → agent re-proposes → Step 1 matches confirmation → allowed. Verified.

11. **Covenant creation opportunity.** After user confirms a blocked action, agent offers "should I always do this without asking?" User says yes → CovenantRule created. Verified.

12. **delete_file consolidated.** 3A's `_check_delete_allowed()` removed. delete_file is gated through the universal dispatch gate. Same signals, same behavior. Verified by checking delete_file still requires user instruction.

13. **DISPATCH_GATE events emitted.** Every gate check emits an event with tool_name, effect, allowed, reason, method. Verified in event stream.

14. **GATE: trace logging.** Every gate check logs at INFO level with GATE: prefix. Verified in test output.

15. **Persona safety.** In roleplay context, write tool calls still gate at the kernel level. `[SYSTEM]` message is not in-character. Verified.

16. **System-wide permissions.** Permission set once applies to all spaces. Calendar always-allow in Business space also allows in D&D space. Verified.

17. **All existing tests pass.** New tests cover all components.

-----

## Live Verification

Follow the Live Testing Protocol in `tests/live/PROTOCOL.md`.

### Test Table

| Step | Action | Expected |
|---|---|---|
| 1 | Send: "What's on my calendar today?" | Agent calls list-events. Read tool → bypasses gate. No GATE event. |
| 2 | Send: "Book a meeting with Henderson for Thursday at 2pm" | Agent calls create-event. Fast path → "book" matches. GATE event: allowed=True, method=fast_path. |
| 3 | (Context where agent wants to create an event without explicit instruction — e.g., agent reads an email and infers a meeting) | Gate fires. No explicit instruction. Covenant check. If no covenant → blocked. Agent asks user. |
| 4 | Respond: "Yes, go ahead" to step 3 block | Agent re-proposes. Confirmation matches. GATE: allowed=True, method=fast_path. |
| 5 | Respond: "Always allow calendar writes" | Permission override set on TenantProfile. Verified via state inspection. |
| 6 | Repeat step 3 scenario | Gate checks permission override → always-allow → bypasses. GATE: method=always_allow. |
| 7 | Send: "Never send emails without asking me first" | CovenantRule created: must_not, email sends. |
| 8 | (Context where agent wants to send email) | Gate fires. Covenant matches must_not → blocked regardless. |
| 9 | "Delete the campaign notes" in D&D space | delete_file gated via universal gate. "Delete" matches in TOOL_SIGNALS. Allowed. Verified delete_file consolidation works. |
| 10 | Check event stream for DISPATCH_GATE events | All gate decisions logged with tool, effect, allowed, reason, method. |

Write results to `tests/live/LIVE-TEST-3D.md`.

-----

## Post-Implementation Checklist

Claude Code must complete ALL of the following before marking this spec done:

- [ ] All tests pass (existing + new)
- [ ] Spec file moved to `specs/completed/SPEC-3D-DISPATCH-INTERCEPTOR.md`
- [ ] `DECISIONS.md` NOW block updated (status, owner, action, test count)
- [ ] `docs/TECHNICAL-ARCHITECTURE.md` updated:
  - New section: Dispatch Interceptor (gate classification, three-step auth, GateResult)
  - Update Reasoning Service section: gate integration in tool-use loop
  - Update TenantProfile: `permission_overrides` field
  - Update EventType list: DISPATCH_GATE
  - Update kernel tools section: note that write tools are gated
  - Update "Last updated" line
  - Update test count
  - Update "What Doesn't Exist Yet" section
- [ ] Live test results written to `tests/live/LIVE-TEST-3D.md`
- [ ] Live test script at `tests/live/run_3d_live.py`
- [ ] Any new audit findings documented in live test report

-----

## Design Decisions This Spec Encodes

| Decision | Choice | Why |
|---|---|---|
| The covenant layer IS the gate | Not separate certainty check | LLM-checking-LLM creates compound error risk. Covenant check is a structured data lookup + one Haiku classification, not inference checking inference. (Kabe + Kit) |
| Reads always bypass | `tool_effects == "read"` → silent | Connecting a tool means granting read access. Gating reads makes integrations useless. (Kit) |
| Fast path via keyword matching | No LLM call for direct instructions | "Book a meeting for 4pm" → immediate. Most writes follow direct instructions. The Haiku call is the fallback, not the common path. |
| Permission overrides on TenantProfile | Not in file, not per-space | Read on every gate check — needs to be fast. System-wide: "I want ALL of you to have access to my calendar" is one setting. (Kabe) |
| Per-capability permissions | Not per-effect-category | "Always allow calendar writes" is natural. "Always allow all soft_writes" is not how humans think. (Kit confirmed) |
| Ambiguity → ask | AMBIGUOUS treated as NO | Safe default. Better to ask once too many than to act once wrong. (Kabe + Kit) |
| Confirmation = explicit instruction | Not TTL, not state | The conversation itself is the context. Previous blocked message + "yes" = permission for that action. No separate approval state. |
| Gate shrinks through use | Confirmation → covenant creation opportunity | Every "yes, go ahead" is a chance to create a standing rule. The gate gets quieter organically, not through configuration. (Kit) |
| Persona doesn't modify gate | [SYSTEM] always kernel-level | Roleplay context cannot bypass safety. The soul is not a persona. (Kabe + Kit) |
| Consolidate delete_file into gate | One mechanism, not two | 3A's delete principle is a subset of the dispatch gate. Same signals, same behavior, one code path. |

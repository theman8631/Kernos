# 3D HOTFIX: Dispatch Gate Redesign

## Context
Live testing revealed a kill chain of failures in the dispatch gate:
1. Keyword fast path missed natural language ("make an entry" not in signal list)
2. Haiku covenant check truncated → returned ambiguous → blocked
3. Gate was stateless — user confirmation disappeared between turns
4. must_not structured lookup blocked actions the user explicitly overrode
5. Sync Anthropic client blocked the event loop on 429 retries
6. complete_simple returned "{}" sentinel on truncation for plain-text calls
7. Response parser matched "EXPLICIT" inside denial explanations

The gate has been incrementally patched during testing. This directive
consolidates everything into the final correct design and ensures the
implementation matches.

## The Final Gate Design — Two Steps

The gate is TWO steps. No keyword matching. No structured covenant lookup.
One mechanical check (token), one language-understanding check (lightweight model).

```
Step 1: Approval token check
        If tool_input contains _approval_token → validate:
        - Token exists in _approval_tokens dict
        - Not expired (< 5 minutes)
        - Not already used
        - tool_name matches
        - tool_input hash matches (excluding _approval_token field)
        If valid → mark used, execute immediately. No model call.
        If invalid → treat as fresh tool call, fall through to Step 2.

Step 2: Lightweight model evaluation (sole authority)
        One LLM call. Sees everything. Makes unified judgment.
        Returns: EXPLICIT / AUTHORIZED / CONFLICT / DENIED
```

That's it. Two steps. Everything else is gone.

## What Gets REMOVED (if not already)

- TOOL_SIGNALS dict — delete entirely
- _explicit_instruction_matches() — delete entirely  
- _has_prohibiting_covenant() — delete entirely (Step 0 is gone)
- Any structured keyword matching against covenant descriptions
- Any separate must_not check before the model call
- _check_delete_allowed() from 3A (already consolidated but verify gone)

## Step 1: Approval Token (mechanical, no LLM)

```python
@dataclass
class ApprovalToken:
    token_id: str          # uuid hex[:12]
    tool_name: str
    tool_input_hash: str   # md5 of json-serialized tool_input (excluding _approval_token)
    issued_at: datetime
    used: bool = False

# Stored in-memory on ReasoningService
_approval_tokens: dict[str, ApprovalToken] = {}
```

When a tool call includes `_approval_token` in its input:
1. Extract token_id
2. Look up in _approval_tokens
3. Validate: exists, not used, < 5 min old, tool_name matches, hash matches
4. If valid: mark used=True, strip _approval_token from tool_input, execute
5. If invalid: ignore token, run Step 2 normally

The agent re-submits tool calls with the token after user confirmation.
The gate is stateless — tokens are verification, not state.

## Step 2: Lightweight Model Evaluation

One LLM call per write tool call. This is the ONLY correctness mechanism.

### System prompt for the gate model:

```
You are a security gate checking whether an agent's proposed action is 
authorized. You have access to the user's recent messages, the agent's 
reasoning for the action, and the user's standing behavioral rules 
(covenants).

Evaluate and answer with ONE of these words:

EXPLICIT — The user directly asked for this action in their recent messages.
AUTHORIZED — A standing covenant rule explicitly covers this action, and 
             the agent's reasoning is consistent with the evidence.
CONFLICT — The user asked for this action, BUT a restriction (must_not 
           rule) also applies. The user may be knowingly overriding the 
           restriction. Surface this tension to the user.
DENIED — The user did not ask for this, and no covenant authorizes it.

Important:
- If the user explicitly addresses a restriction ("no need to review, 
  just send it"), that is an override — return EXPLICIT, not CONFLICT.
- If the user asks for an action and a must_not rule exists but the user 
  did NOT address the restriction, return CONFLICT.
- If the agent's reasoning claims the user asked for something but the 
  recent messages don't support that claim, return DENIED.
- When in doubt, return DENIED. It is always safe to ask.

Answer with ONLY one word. Nothing else.
```

### User content for the gate model:

```python
user_content = (
    f"Recent user messages (oldest to newest):\n{recent_messages_text}\n\n"
    f"Agent's reasoning for this action:\n{agent_reasoning}\n\n"
    f"Proposed action: {tool_name}\n"
    f"Tool description: {tool_description}\n"  
    f"Action details: {action_description}\n\n"
    f"Active covenant rules:\n{rules_text}"
)
```

### Where each piece comes from:

- **recent_messages_text**: Last 3-5 USER messages from the conversation 
  store. User turns only — no agent responses, no [SYSTEM] messages.
  Format: `- "message content"` per line.

- **agent_reasoning**: The text content from the LLM's response BEFORE 
  the tool call. When the main model reasons about why it should call a 
  tool, that reasoning text precedes the tool_use block. Extract it and 
  pass it to the gate. If there's no text before the tool_use block, 
  pass "No explicit reasoning provided."

- **tool_description**: From CapabilityInfo or MCPClientManager tool 
  definitions. The human-readable description of what this tool does.
  For kernel tools, hardcode descriptions:
    - write_file: "Create or update a text file in the current space"
    - delete_file: "Delete a file from the current space"
    - (read tools don't reach the gate)

- **action_description**: Human-readable summary of the specific action.
  Build from tool_name + tool_input. Same as _describe_action().

- **rules_text**: ALL active covenant rules for this tenant (space-scoped 
  + global). Format: `- [rule_type] description (scope: space/global)`

### Response parsing:

```python
first_word = result.strip().split()[0].upper() if result.strip() else ""
if first_word == "EXPLICIT":
    return GateResult(allowed=True, reason="explicit_instruction", ...)
elif first_word == "AUTHORIZED":
    return GateResult(allowed=True, reason="covenant_authorized", ...)
elif first_word == "CONFLICT":
    return GateResult(allowed=False, reason="covenant_conflict", ...)
else:  # DENIED or anything unexpected
    return GateResult(allowed=False, reason="denied", ...)
```

First word ONLY. Everything after is verbose explanation that gets logged 
but not parsed. This prevents the bug where "EXPLICIT" appearing inside 
a denial explanation caused a false approval.

### max_tokens: 128

Haiku sometimes returns verbose responses. 128 gives margin. Only the 
first word matters — truncation of explanation is harmless.

### Model call:

Use complete_simple with NO output_schema. Plain text completion.
Verify: has_schema=False in the GATE_HAIKU debug log.

## Gate Integration in Tool-Use Loop

```python
# In the tool-use loop, after extracting tool_name and tool_input:

effect = self._classify_tool_effect(tool_name, active_space)

if effect == "read":
    # Bypass gate entirely. Execute.
    pass

elif effect in ("soft_write", "hard_write", "unknown"):
    
    # Step 1: Token check
    token_id = tool_input.pop("_approval_token", None)
    if token_id:
        token = self._approval_tokens.get(token_id)
        if token and not token.used and token.tool_name == tool_name:
            input_hash = _hash_tool_input(tool_input)
            if token.tool_input_hash == input_hash:
                age = (now - token.issued_at).total_seconds()
                if age < 300:  # 5 minutes
                    token.used = True
                    # Execute immediately — user already approved
                    # (fall through to normal execution below)
                    pass  # token valid
                else:
                    token_id = None  # expired
            else:
                token_id = None  # hash mismatch
        else:
            token_id = None  # not found or already used
    
    if not token_id:
        # Step 2: Lightweight model evaluation
        gate_result = await self._evaluate_gate(
            tool_name, tool_input, effect,
            request.input_text,
            request.tenant_id,
            request.active_space_id,
            request.conversation_id,
            agent_reasoning,  # text from LLM before tool_use block
        )
        
        # Emit trace event
        await emit_event(...)
        logger.info("GATE: tool=%s effect=%s allowed=%s reason=%s",
                     tool_name, effect, gate_result.allowed, gate_result.reason)
        
        if not gate_result.allowed:
            # Issue approval token
            token = ApprovalToken(
                token_id=uuid4().hex[:12],
                tool_name=tool_name,
                tool_input_hash=_hash_tool_input(tool_input),
                issued_at=now,
            )
            self._approval_tokens[token.token_id] = token
            
            # Build detailed reason for [SYSTEM] message
            if gate_result.reason == "covenant_conflict":
                system_msg = (
                    f"[SYSTEM] Action paused — conflict with standing rule. "
                    f"Proposed: {gate_result.proposed_action}. "
                    f"Conflicting rule: {gate_result.conflicting_rule}. "
                    f"The user may be overriding this rule. Ask for "
                    f"clarification. If they confirm, re-submit with "
                    f"_approval_token: '{token.token_id}'. "
                    f"Also offer to update or remove the conflicting rule."
                )
            else:  # denied
                system_msg = (
                    f"[SYSTEM] Action blocked — no authorization found. "
                    f"Proposed: {gate_result.proposed_action}. "
                    f"The user's recent messages do not request this action "
                    f"and no covenant rule covers it. "
                    f"Ask the user if they'd like you to proceed. "
                    f"If they confirm, re-submit with "
                    f"_approval_token: '{token.token_id}'. "
                    f"You may also offer to create a standing rule."
                )
            
            # IMPORTANT: Append to tool_results BEFORE continue
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": system_msg,
            })
            continue

# Execute the tool (reads, token-approved writes, and model-approved writes)
```

## GateResult Dataclass

```python
@dataclass
class GateResult:
    allowed: bool
    reason: str      # "explicit_instruction", "covenant_authorized", 
                     # "covenant_conflict", "denied"
    method: str      # "token", "model_check"
    proposed_action: str = ""
    conflicting_rule: str = ""  # For CONFLICT — which rule conflicts
    raw_response: str = ""      # Full model response for logging
```

## Agent Reasoning Extraction

When the main model's response contains text blocks before a tool_use 
block, that text is the agent's reasoning. Extract it:

```python
agent_reasoning = ""
for block in response.content:
    if block.type == "text":
        agent_reasoning += block.text
    elif block.type == "tool_use":
        break  # reasoning comes before tool calls
if not agent_reasoning:
    agent_reasoning = "No explicit reasoning provided."
```

## CONFLICT Response — What the Agent Does

When the gate returns CONFLICT, the agent should:
1. Explain what it wanted to do
2. Surface the conflicting rule
3. Offer THREE paths (Kit: always three, not two):
   a. Respect the rule this time (e.g., "show you the draft first")
   b. Override the rule this time (e.g., "just send it now")
   c. Update the rule permanently (e.g., "change the rule so I don't ask next time")

This third option is the gate-friction-as-covenant-creation pattern.

Example agent response:
"I was going to send that email, but you have a standing rule: 
'don't send emails without review first.' Three options:
1. I can show you the draft first (respecting the rule)
2. I can just send it this time (one-time override)
3. I can update the rule so I stop asking — want me to change it?"

## Tool Effect Classification (unchanged from 3D)

```python
KERNEL_READS = {"remember", "list_files", "read_file", "request_tool"}
KERNEL_WRITES = {"write_file", "delete_file"}

# MCP tools: use tool_effects from CapabilityInfo
# Unknown tools: default to "hard_write" (gated)
```

## Event + Logging (unchanged from 3D)

```python
EventType.DISPATCH_GATE  # payload: tool_name, effect, allowed, reason, method
logger.info("GATE: tool=%s effect=%s allowed=%s reason=%s", ...)
logger.info("GATE_MODEL: max_tokens=%d, has_schema=%s, rules=%d", ...)
logger.info("GATE_MODEL: raw_response=%r", result[:200])
```

## What Stays the Same

- Read tools bypass entirely (no change)
- tool_effects classification (no change)  
- DISPATCH_GATE events (no change)
- Trace logging with GATE: prefix (no change)
- Permission overrides on TenantProfile (no change — but now checked 
  BY the model, not as a separate step)

Wait — clarification on permission_overrides: The model now sees all 
covenants. Permission overrides ("always-allow: google-calendar") should 
be included in the rules_text as a covenant-like entry:
  - [always-allow] google-calendar write actions (system-wide permission)
The model treats it like any other authorization. No separate lookup.

## Tests

Remove or update:
- Tests for TOOL_SIGNALS / _explicit_instruction_matches (deleted)
- Tests for _has_prohibiting_covenant (deleted)
- Tests for separate must_not Step 0 (deleted)

Add/update:
- Token lifecycle: issue, validate, expire (>5 min), single-use, hash mismatch
- Model gate: user explicitly asks → EXPLICIT
- Model gate: covenant covers action → AUTHORIZED  
- Model gate: user asks but must_not conflicts → CONFLICT
- Model gate: no authorization → DENIED
- Model gate: user explicitly overrides must_not ("no need to review") → EXPLICIT
- Model gate: agent reasoning doesn't match evidence → DENIED
- Model gate: recent messages provide context for vague current message
- Model gate: non-English instruction → EXPLICIT (works in any language)
- First-word parsing: "DENIED\n\nThe user's message..." → DENIED not EXPLICIT
- Blocked result appended to tool_results before continue
- CONFLICT surfaces the conflicting rule in the [SYSTEM] message
- Permission overrides included in rules_text

## Post-Implementation

- Update docs/TECHNICAL-ARCHITECTURE.md: 
  - Dispatch Interceptor section: two-step design, no keyword matching
  - Remove references to TOOL_SIGNALS, fast path, Step 0
  - Add: agent reasoning extraction, CONFLICT response type
- Update DECISIONS.md: gate redesign decision from testing
- Run full test suite
- Do NOT run a live test script — Kabe is testing live manually

# Confirmation Redesign: Kernel-Owned Replay

## Context
The agent never uses approval tokens. It makes fresh tool calls every 
time instead of injecting _approval_token into tool_input. User confirmed 
3 times ("BOTH!", "2", "DELETE POTATOS!") — action never executed. 
Infinite loop.

Root cause: asking an LLM to do mechanical bookkeeping (remember hex 
strings, reconstruct exact parameters, inject foreign fields). LLMs are 
bad at this. This is kernel work, not agent work.

## The Fix: Kernel-Owned Replay

The kernel handles confirmation. The agent never touches tokens.

### New: PendingAction state on MessageHandler

```python
@dataclass
class PendingAction:
    tool_name: str
    tool_input: dict          # exact parameters from the blocked call
    proposed_action: str      # human-readable description
    conflicting_rule: str     # for CONFLICT — which rule
    gate_reason: str          # "covenant_conflict" or "denied"
    expires_at: datetime      # 5 minutes from creation
    
# Multiple actions can be pending (e.g., delete both file + calendar event)
# Stored as a list, indexed by position
_pending_actions: dict[str, list[PendingAction]] = {}  # tenant_id → list
```

### Flow

1. Gate blocks a tool call → kernel stores PendingAction(s) on the handler

2. Agent receives [SYSTEM] message (NO tokens exposed):
   ```
   [SYSTEM] Action blocked — conflict with standing rule.
   Proposed: Delete file 'potato.md'.
   Conflicting rule: Never delete or archive data without owner awareness.
   Pending action index: 0
   
   Ask the user to confirm. If they confirm, include [CONFIRM:0] in 
   your response. If they confirm ALL pending actions, include 
   [CONFIRM:ALL]. Also offer three options:
   1. Respect the rule (don't do it)
   2. Override this time (confirm the action)
   3. Update the rule permanently
   ```
   
   For multiple blocked actions in the same turn:
   ```
   [SYSTEM] Action blocked — conflict with standing rule.
   Proposed: Delete calendar event 'Potato' at 4:00 PM.
   Conflicting rule: Never delete or archive data without owner awareness.
   Pending action index: 0
   
   [SYSTEM] Action blocked — conflict with standing rule.
   Proposed: Delete file 'potato.md'.
   Conflicting rule: Never delete or archive data without owner awareness.
   Pending action index: 1
   
   Ask the user which to confirm. Include [CONFIRM:0], [CONFIRM:1], 
   or [CONFIRM:ALL] in your response based on their answer.
   ```

3. Agent communicates naturally with user:
   "I was going to delete both the Potato calendar event and potato.md, 
    but you have a rule about deleting without awareness. Three options:
    1. Don't delete (respect the rule)
    2. Delete them this time
    3. Update the rule so I stop asking"

4. User says "2" or "both" or "delete them" or "DELETE POTATOS!"

5. Agent understands the confirmation and includes the signal:
   "Got it — deleting both. [CONFIRM:ALL]"
   Or for selective: "Deleting just the file. [CONFIRM:1]"

6. Handler intercepts at TOP of process(), BEFORE normal pipeline 
   (same position as SecureInputState check):
   
   ```python
   # Check for pending action confirmations
   if tenant_id in self._pending_actions:
       pending = self._pending_actions[tenant_id]
       
       # Check for [CONFIRM:N] or [CONFIRM:ALL] in assistant response
       # NOTE: This check happens AFTER the reasoning call returns,
       # not before. The agent processes the user's message, and if 
       # the agent's response contains [CONFIRM:N], we execute.
       # See "WHERE THE CHECK HAPPENS" below.
   ```

### WHERE THE CHECK HAPPENS

IMPORTANT: The confirmation check is NOT at the top of process() like 
SecureInputState. The flow is:

1. User says "yes" or "delete them" or "2"
2. Normal pipeline runs — agent sees the user's message + the prior 
   [SYSTEM] blocked messages in conversation history
3. Agent produces a response that includes [CONFIRM:0] or [CONFIRM:ALL]
4. AFTER the reasoning call returns, handler scans the agent's response 
   text for [CONFIRM:N] patterns
5. If found: execute the stored PendingAction(s), append results to 
   the response
6. Strip [CONFIRM:N] from the response text before sending to user

```python
# After reasoning returns response_text:
if tenant_id in self._pending_actions:
    pending = self._pending_actions[tenant_id]
    confirm_pattern = re.compile(r'\[CONFIRM:(\d+|ALL)\]')
    matches = confirm_pattern.findall(response_text)
    
    if matches:
        actions_to_execute = []
        for match in matches:
            if match == "ALL":
                actions_to_execute = list(range(len(pending)))
                break
            else:
                idx = int(match)
                if 0 <= idx < len(pending):
                    actions_to_execute.append(idx)
        
        # Execute confirmed actions
        results = []
        for idx in actions_to_execute:
            action = pending[idx]
            if datetime.now() < action.expires_at:
                result = await self._execute_tool(
                    action.tool_name, action.tool_input, request)
                results.append(f"Executed: {action.proposed_action}")
                logger.info("CONFIRM_EXECUTE: tool=%s idx=%d", 
                           action.tool_name, idx)
            else:
                results.append(f"Expired: {action.proposed_action}")
                logger.warning("CONFIRM_EXPIRED: tool=%s idx=%d", 
                              action.tool_name, idx)
        
        # Clear pending actions
        del self._pending_actions[tenant_id]
        
        # Strip [CONFIRM:N] from response before sending to user
        response_text = confirm_pattern.sub('', response_text).strip()
        
        # Append execution results
        if results:
            response_text += "\n\n" + "\n".join(results)
    
    else:
        # Agent responded without confirming — user said something 
        # else or changed topic. Clear pending actions.
        del self._pending_actions[tenant_id]
        logger.info("PENDING_CLEARED: tenant=%s reason=no_confirm_signal",
                    tenant_id)
```

### Storing PendingActions when gate blocks

In the tool-use loop, where the gate currently appends the [SYSTEM] 
blocked message:

```python
if not gate_result.allowed:
    # Store pending action
    if tenant_id not in self._pending_actions:
        self._pending_actions[tenant_id] = []
    
    pending_idx = len(self._pending_actions[tenant_id])
    self._pending_actions[tenant_id].append(PendingAction(
        tool_name=tool_name,
        tool_input=dict(tool_input),  # copy
        proposed_action=gate_result.proposed_action,
        conflicting_rule=gate_result.conflicting_rule,
        gate_reason=gate_result.reason,
        expires_at=datetime.now() + timedelta(minutes=5),
    ))
    
    # Build [SYSTEM] message — NO tokens
    if gate_result.reason == "covenant_conflict":
        system_msg = (
            f"[SYSTEM] Action blocked — conflict with standing rule. "
            f"Proposed: {gate_result.proposed_action}. "
            f"Conflicting rule: {gate_result.conflicting_rule}. "
            f"Pending action index: {pending_idx}. "
            f"\nAsk the user to confirm. If they confirm, include "
            f"[CONFIRM:{pending_idx}] in your response. "
            f"Also offer three options: "
            f"1. Respect the rule (don't do it) "
            f"2. Override this time (confirm) "
            f"3. Update the rule permanently."
        )
    else:  # denied
        system_msg = (
            f"[SYSTEM] Action blocked — no authorization found. "
            f"Proposed: {gate_result.proposed_action}. "
            f"Pending action index: {pending_idx}. "
            f"\nAsk the user if they want to proceed. If they confirm, "
            f"include [CONFIRM:{pending_idx}] in your response. "
            f"You may also offer to create a standing rule."
        )
```

### Agent system prompt addition

Add to the posture/capability section:

```
## Confirmed actions
When the dispatch gate blocks a tool call, you'll receive a [SYSTEM] 
message describing what was blocked and why. Your job is to communicate 
this to the user naturally and ask for their decision.

If the user confirms, include the signal [CONFIRM:N] in your response 
where N is the pending action index from the [SYSTEM] message. For 
multiple actions, include multiple signals or [CONFIRM:ALL] for all.

Example:
- You receive: "[SYSTEM] Action blocked. Pending action index: 0"
- User says: "yes, go ahead"
- You respond: "Deleting potato.md now. [CONFIRM:0]"

The kernel handles execution. You never need to re-call the tool.
For CONFLICT blocks, always offer three options:
1. Respect the rule
2. Override this time  
3. Update the rule permanently
```

### Token mechanism — KEEP as programmatic fallback

Do NOT remove Step 1 (token check) from the gate. It stays for 
programmatic/API callers who CAN inject tokens reliably. But:
- Remove token IDs from agent-facing [SYSTEM] messages
- Remove any agent instructions about token injection
- The gate's Step 1 is now a programmatic interface only

### Clearing stale pending actions

Pending actions are cleared when:
1. Agent responds with [CONFIRM:N] → execute and clear
2. Agent responds WITHOUT any [CONFIRM] signal → clear immediately
   (user changed topic or said something else)
3. 5-minute timeout (checked at execution time, not on a timer)

Rule 2 is important: if the user starts talking about something 
completely different, the pending actions clear. The agent can 
re-raise later if needed: "By the way, I still had a pending delete 
from earlier — did you want to address that?"

### Tests

- PendingAction stored on gate block
- Agent response with [CONFIRM:0] executes stored action
- Agent response with [CONFIRM:ALL] executes all stored actions
- Agent response without [CONFIRM] clears pending actions
- Expired pending action (>5 min) not executed
- Multiple pending actions: selective confirmation [CONFIRM:1] only
- [CONFIRM:N] stripped from response text before sending to user
- Token mechanism still works for programmatic callers (Step 1)
- [SYSTEM] messages do NOT contain token IDs
- Agent system prompt includes confirmed-actions section

### Post-implementation
- Update docs/TECHNICAL-ARCHITECTURE.md Dispatch Interceptor section
- Update DECISIONS.md
- Run full test suite

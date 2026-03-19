# SPEC: LLM-Based Covenant Validation

**Status:** Ready for implementation  
**Author:** Architect (Claude Project)  
**Implementer:** Claude Code  
**Date:** 2026-03-18  
**Principle:** LLM over keyword matching. Algorithmic similarity scoring is the wrong tool for semantic validation.

---

## Objective

Replace algorithmic covenant dedup/contradiction detection with a single Haiku LLM call that validates the entire covenant set after every rule write. The LLM sees all active rules and identifies conflicts, redundancies, merge opportunities, and poorly worded rules — things embedding similarity thresholds fundamentally cannot catch.

**What changes for the user:** Covenants self-clean. When the user says "share your thought process" and later says "stop narrating your reasoning," the system recognizes these contradict and resolves it. Duplicate rules from different sessions get merged automatically. Poorly worded rules get cleaned up.

---

## Prerequisite: Resolve the Dual-Path Problem

**There are currently two paths that create covenant rules from user natural language. This must be collapsed to one.**

**Path 1 (kernel, background):** Tier 2 extraction in `coordinator.py` runs after every message. It detects behavioral instructions ("never do X", "always confirm Y") and creates covenant rules automatically via the NL contract parser.

**Path 2 (agent, foreground):** The agent (Sonnet) reads the same message, recognizes it as a behavioral instruction, and either creates a rule via `manage_covenants update` or asks "Should I add that as a rule?" — duplicating what Tier 2 already did.

Result: "Never talk about unicorns" can create two identical rules — one from each path. The previous consolidation_check was patching over this by deduplicating after the fact.

**Resolution: One path, one owner.**

- **Tier 2 extraction is the sole creator of covenant rules from conversation.** It's the right layer — specialized extraction call, runs on every message, purpose-built for this.
- **The agent does NOT create rules from the same input.** Remove covenant creation from the agent's reflexive behavior. The agent should not offer "Should I add that as a rule?" — the kernel already did it.
- **`manage_covenants` tool is for management only:** `list`, `remove`, `update` (editing existing rules on explicit user request). NOT for creating new rules from conversation.
- **The system prompt should instruct the agent** that covenant rules are automatically extracted from behavioral instructions — the agent doesn't need to create them. If the user says "never do X," the agent acknowledges it ("Got it, I won't do X") and trusts that the kernel captured the rule.

**Implementation:**
1. Remove any `manage_covenants` action type that creates new rules (if one exists). The tool should only support `list`, `remove`, `update`.
2. Update the system prompt / reference material to tell the agent: "Behavioral instructions are automatically captured as covenant rules. You don't need to create them. Use manage_covenants to view or edit existing rules when the user asks."
3. Verify that Tier 2 extraction is the only code path that calls the NL contract parser to create rules.

This must be done before or alongside the validation work below. Without it, validation catches the duplicate after the fact instead of preventing it.

---

## Design

### Trigger

`validate_covenant_set()` fires **after every successful covenant rule write**. This includes:
- New rule creation (from Tier 2 extraction / NL contract parser — the sole creation path)
- Rule update via `manage_covenants update` (user explicitly editing an existing rule)
- Does NOT fire after `manage_covenants remove` (removal can't create conflicts)
- Does NOT fire during startup migration (startup cleanup is a separate, fast path)

### The Validation Call

A single `complete_simple(prefer_cheap=True)` call (Haiku) with ALL active covenant rules for the tenant. The covenant set is small (10-30 rules for a heavy user) — fits easily in one call.

**System prompt:**

```
You are a covenant rule validator for a personal AI assistant. You will receive 
a set of active behavioral rules. Your job is to identify problems and recommend 
actions.

For each problem found, return ONE action:

- MERGE: Two or more rules say the same thing in different words. Keep the best-
  worded version, supersede the others. Return the rule_id to keep and the 
  rule_ids to supersede.

- CONFLICT: Two rules contradict each other (e.g., a MUST and a MUST_NOT about 
  the same behavior). Return both rule_ids and a plain-English description of 
  the conflict for the user to resolve.

- REWRITE: A single rule is poorly worded, vague, or could be clearer. Return 
  the rule_id and a suggested improved description.

- NO_ISSUES: The covenant set is clean. No action needed.

Be thorough. Check every rule against every other rule. Common patterns:
- "share thought process" vs "don't narrate reasoning" = CONFLICT
- Four rules all about the same person's contact info = MERGE  
- "do the thing with the stuff" = REWRITE

Respond with JSON only. No other text.
```

**User message:**

```
Active covenant rules for this tenant:

{for each active rule}
- rule_id: {rule.id}
  type: {rule.rule_type} (MUST / MUST_NOT / PREFERENCE)
  description: {rule.description}
  context_space: {rule.context_space or "global"}
{end for}

The most recently written rule is: {new_rule.id}

Identify any MERGE, CONFLICT, or REWRITE issues. If the set is clean, return NO_ISSUES.
```

**Expected structured output:**

```json
{
  "actions": [
    {
      "type": "MERGE",
      "keep_rule_id": "rule_5b68eab6",
      "supersede_rule_ids": ["rule_56b42e97", "rule_81975a28", "rule_110deb25"],
      "reason": "All four rules contain Sarah Henderson's contact information"
    },
    {
      "type": "CONFLICT",
      "rule_ids": ["rule_fb5a47fc", "rule_b770a84a"],
      "description": "MUST share thought process contradicts MUST NOT narrate reasoning"
    },
    {
      "type": "REWRITE",
      "rule_id": "rule_abc123",
      "current_description": "do the thing with emails",
      "suggested_description": "Always confirm before sending emails to external contacts"
    }
  ]
}
```

### Action Handling

For each action returned by the validation call:

**MERGE:**
- Auto-execute. Supersede the duplicates (set `superseded_by` to the kept rule's ID).
- Emit `covenant.rule.merged` event for each superseded rule.
- Log: `COVENANT_VALIDATE: merged {N} duplicates into {keep_rule_id}: {reason}`
- No whisper needed — this is cleanup, not a decision.

**CONFLICT:**
- Do NOT auto-resolve. The user must decide.
- Create a whisper via the awareness system: `PROACTIVE_INSIGHT` event with delivery_class `stage` (surfaces at next conversation start).
- Whisper content: "I noticed a conflict in your rules: {description}. Which one do you want me to keep?" Include both rule_ids so the agent can offer `manage_covenants remove` for the loser.
- Log: `COVENANT_VALIDATE: conflict detected between {rule_ids}: {description}`

**REWRITE:**
- Auto-execute. Create a new rule with the improved description, supersede the old one.
- Emit `covenant.rule.rewritten` event.
- Log: `COVENANT_VALIDATE: rewrote {rule_id}: "{old}" → "{new}"`
- No whisper needed unless the rewrite changes meaning (which it shouldn't — the prompt asks for clarity improvements, not semantic changes).

**NO_ISSUES:**
- Log: `COVENANT_VALIDATE: set clean ({N} active rules)`
- No action needed.

### Error Handling

- If the Haiku call fails (timeout, rate limit, parse error): log warning and skip. The covenant set is unchanged. Validation will run again on the next write.
- If the JSON response is malformed: log warning, skip. Same rationale.
- If a referenced rule_id doesn't exist or is already superseded: skip that action, log warning.
- Never block the rule write on validation failure. The rule is already written. Validation is post-write cleanup.

---

## Implementation

### New function in `covenant_manager.py`

```python
async def validate_covenant_set(
    state_store,
    event_stream, 
    reasoning_service,
    tenant_id: str,
    new_rule_id: str,
    awareness_evaluator=None,  # optional, for whisper delivery
) -> dict:
    """
    Post-write LLM validation of the full covenant set.
    
    Fires after every covenant rule creation/update.
    Identifies conflicts, merges, and rewrites via a single Haiku call.
    Auto-resolves merges and rewrites. Forces whisper for conflicts.
    
    Returns: {"merges": int, "conflicts": int, "rewrites": int}
    """
```

**Steps:**
1. Load all active rules: `state_store.get_contract_rules(tenant_id, active_only=True)`
2. If <= 1 active rule, return immediately (nothing to validate)
3. Build the prompt with all active rules, marking `new_rule_id` as the most recent
4. Call `reasoning_service.complete_simple(system=SYSTEM_PROMPT, user=USER_MSG, prefer_cheap=True)`
5. Parse JSON response
6. Execute actions (merge/conflict-whisper/rewrite)
7. Return action counts

### Integration point in `coordinator.py` (sole rule creation path)

After Tier 2 extraction writes a new covenant rule, call `validate_covenant_set()`. This is a fire-and-forget async call — do not block the user response on validation.

Find the location where `covenant.rule.created` events are emitted after a new rule is written. After that event emission, add:

```python
# Post-write validation (async, non-blocking)
asyncio.create_task(
    validate_covenant_set(
        state_store=self.state_store,
        event_stream=self.event_stream,
        reasoning_service=self.reasoning,
        tenant_id=tenant_id,
        new_rule_id=new_rule.id,
        awareness_evaluator=self.awareness_evaluator,
    )
)
```

### Integration point in `reasoning.py` (manage_covenants update)

After a `manage_covenants update` action writes the new replacement rule, add the same fire-and-forget call.

After a `manage_covenants remove` action — do NOT call validation. Removal can't create conflicts.

### Whisper delivery for conflicts

If `awareness_evaluator` is provided and a CONFLICT is detected, create a whisper:

```python
from kernos.kernel.awareness import Whisper  # or however whispers are created

whisper = Whisper(
    insight=f"Covenant conflict: {conflict['description']}",
    delivery_class="stage",  # surfaces at next conversation start
    source_context_space=None,  # system-level
    target_context_space=None,  # agent picks up globally
    supporting_evidence=[
        f"Rule {rid}: {get_rule_description(rid)}" 
        for rid in conflict['rule_ids']
    ],
    reasoning_trace="Detected by post-write covenant validation",
)
```

If `awareness_evaluator` is None (e.g., during testing), log the conflict but don't attempt whisper delivery.

### What to remove / deprecate

- `check_semantic_duplicate()` in `covenant_manager.py` — replaced by MERGE detection in validation call. Can be removed entirely.
- `check_contradiction()` in `covenant_manager.py` — replaced by CONFLICT detection in validation call. Can be removed entirely.
- Embedding similarity thresholds (0.85 dedup, 0.70 contradiction) — no longer used for covenant validation.
- Keep `consolidation_check()` if it does anything beyond dedup/contradiction (check first). If it only does dedup/contradiction, it's fully replaced.
- **Any `manage_covenants` action that creates new rules from scratch** — the tool should only support `list`, `remove`, `update`. If there's a `create` or `add` action, remove it.
- **Any agent system prompt language encouraging the agent to create covenant rules** — replace with language saying rules are auto-extracted, agent manages existing rules only.

**Keep the startup migration** (`run_covenant_cleanup`) as a fast, zero-LLM-cost safety net for exact/near-exact duplicates (word overlap >= 0.80). It runs once at boot and catches obvious dupes without an API call. The LLM validation handles everything else at write time.

---

## Events

New events emitted:

```python
# Merge (auto-resolved)
{
    "event_type": "covenant.validation.merge",
    "data": {
        "kept_rule_id": str,
        "superseded_rule_ids": list[str],
        "reason": str,
    }
}

# Conflict (whisper forced)
{
    "event_type": "covenant.validation.conflict",
    "data": {
        "rule_ids": list[str],
        "description": str,
        "whisper_created": bool,
    }
}

# Rewrite (auto-resolved)
{
    "event_type": "covenant.validation.rewrite",
    "data": {
        "old_rule_id": str,
        "new_rule_id": str,
        "old_description": str,
        "new_description": str,
    }
}
```

---

## Tests

### Unit tests for `validate_covenant_set()`

1. **MERGE detection:** 4 rules about "Sarah Henderson contact info" with slight wording variations → merges into 1, supersedes 3.
2. **CONFLICT detection:** MUST "share thought process" + MUST_NOT "narrate reasoning" → conflict detected, whisper created.
3. **REWRITE detection:** Vague rule "do email thing" → rewritten to something clearer.
4. **NO_ISSUES:** Clean set of 5 non-overlapping rules → no actions taken.
5. **Mixed:** Set with 1 conflict + 2 merges → both handled in single call.
6. **Single rule:** Only 1 active rule → skips validation entirely.
7. **LLM failure:** Haiku call times out → logs warning, returns gracefully, no rules changed.
8. **Malformed JSON:** LLM returns invalid JSON → logs warning, returns gracefully.
9. **Invalid rule_id:** LLM references a rule_id that doesn't exist → skips that action, processes others.
10. **Fire-and-forget:** Validation runs async and does not block the user response.

### Integration tests

11. **End-to-end creation flow:** User says "never delete emails" → rule created → validation fires → no issues (clean add).
12. **End-to-end conflict:** User says "always share reasoning" then "stop narrating" → second rule triggers validation → conflict whisper created → whisper surfaces on next conversation start.
13. **End-to-end merge:** User says "remember Henderson's number is 555-1234" across 3 sessions → third creation triggers validation → merges the duplicates.

---

## Cost

- One Haiku call per covenant rule write. Typical user creates 1-3 rules per session. Cost: negligible.
- Startup migration remains zero-LLM-cost (word overlap only).
- No change to per-message costs. Validation is per-rule-write, not per-message.

---

## What This Does NOT Change

- The dispatch gate (3D) — still evaluates covenants at tool-call time. Unchanged.
- The system prompt covenant injection — still reads active rules. Unchanged.
- `manage_covenants list/remove` — unchanged.
- Startup migration — unchanged (fast safety net).
- Rule schema (`CovenantRule` dataclass) — unchanged.

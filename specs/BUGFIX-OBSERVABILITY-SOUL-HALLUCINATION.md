# Bug Fix: State Mutation Observability + Soul Bypass + Hallucination Correction

**Status:** APPROVED — Kabe direct to Claude Code  
**Date:** 2026-03-20  
**Priority:** Urgent — security bypass and user-visible fabrication  
**Principle:** Every state change must be traceable in the console to its source and trigger. If we can't tell from the console why something changed, the logging is insufficient.

---

## Fix 1: State Mutation Logging (the root fix)

We discovered a soul field changed without update_soul being called, and had to investigate to figure out why. That investigation should have been unnecessary — the console should have told us immediately.

**Every state mutation path must log at INFO level with source and trigger.** Add logging to ALL write paths that don't already have it:

### Soul writes
Anywhere soul.json is written — `save_soul()` in state_json.py, and anywhere else soul fields are modified:
```
SOUL_WRITE: field={field} value={value} source={calling_function} trigger={cause}
```
Example: `SOUL_WRITE: field=emoji value=🔥 source=tier2_extraction trigger=msg_42`
Example: `SOUL_WRITE: field=agent_name value=Rex source=update_soul trigger=user_tool_call`

### Knowledge entry writes
In the extraction/dedup pipeline — `_write_entry`, `_write_entry_enhanced`, fact dedup ADD:
```
KNOW_WRITE: id={entry_id} action={ADD|UPDATE|NOOP} source={calling_function} trigger={cause}
```
(Fact dedup already logs `Fact dedup: ADD/NOOP` — enhance it with source and trigger context)

### Covenant writes
In covenant_manager — create, merge, rewrite, remove:
```
COVENANT_WRITE: id={rule_id} action={CREATE|MERGE|REWRITE|REMOVE} source={function} trigger={cause}
```
(Covenant validation already logs MERGE/REWRITE — enhance with source context)

### Capability state changes
In the registry — enable, disable, install, remove:
```
CAP_WRITE: name={capability} action={ENABLE|DISABLE|INSTALL|REMOVE} source={function} trigger={cause}
```

### The pattern
Every `save_*()` or state-modifying function should accept or infer a `source` string and a `trigger` string. The source is the function name. The trigger is the upstream cause (user_tool_call, tier2_extraction, compaction, awareness_pass, etc.).

---

## Fix 2: Investigate and close the soul bypass

With Fix 1 in place, the investigation becomes trivial. But while you're in the code:

**Find everywhere that can write to soul fields.** Grep for:
- `save_soul(` 
- Any direct modification of Soul dataclass fields followed by a save
- Anywhere in the Tier 2 extraction pipeline (`llm_extractor.py`, `coordinator.py`) that touches soul fields

**The expected result:** `update_soul` kernel tool should be the ONLY path that modifies soul fields at runtime. If Tier 2 extraction is also writing to soul (e.g., extracting user_name or emoji from conversation and saving it), that path needs to go through the kernel tool and the gate — not bypass it.

**If Tier 2 extraction IS writing to soul:** Remove that code path. Soul updates should be explicit (through update_soul tool) or happen during compaction/graduation (which is a kernel-managed lifecycle event, not a per-message extraction). The gate exists for a reason — soul changes should be gated.

**If Tier 2 extraction is NOT writing to soul:** Then something else changed the emoji. Log everything and re-test to find the actual path.

---

## Fix 3: Hallucination detection → corrective action

Currently: hallucination checker detects `iterations=0` when agent claims tool use, logs a warning, and prefixes the response with `[SYSTEM NOTE: The following response was generated without actual tool execution]`.

**Problem:** The user still sees "Created!" or "Done!" The note is easily missed. The agent fabricated an action and the user may believe it happened.

**Fix:** When hallucination is detected, instead of just tagging the response:

1. **Do not send the hallucinated response to the user.**
2. **Inject a corrective system message** into the conversation and retry:
   ```
   [SYSTEM: Your previous response claimed to perform an action but no tool was called. 
   Do NOT claim actions were completed without actually calling the tool. 
   If you need to call a tool, call it. If you cannot perform the action, say so honestly.]
   ```
3. **Retry the LLM call** with the corrective message appended. The agent gets a second chance to actually call the tool or honestly say it can't.
4. **If the retry also hallucinates** (iterations=0 again), send a user-facing message: "I tried to do that but wasn't able to execute the action. Can you try asking again?"
5. **Log both attempts:** `HALLUCINATION_RETRY: original_response={preview} retry_succeeded={bool}`

This turns a silent fabrication into an honest failure or a successful retry.

---

## Update docs/

- `docs/architecture/overview.md` — add section on state mutation logging convention
- `docs/behaviors/dispatch-gate.md` — note hallucination detection and retry behavior

---

## Verify

1. After Fix 1: Ask Kernos to change its emoji. Console should show `SOUL_WRITE: field=emoji value=🔥 source={whatever} trigger={whatever}` — immediately revealing which path did the write.
2. After Fix 2: The ONLY `SOUL_WRITE` entries should come from `source=update_soul` (explicit tool call) or `source=compaction` (lifecycle). No `source=tier2_extraction`.
3. After Fix 3: Ask Kernos to "remind me to buy milk" (without a reminder tool). It should NOT say "Done!" — it should either honestly say it can't, or actually create a calendar event.
4. All existing tests pass.

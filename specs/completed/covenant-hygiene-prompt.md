# Task: Covenant Hygiene — Dedup & Contradiction Detection

## Context

The NL Contract Parser creates covenant rules when the user gives behavioral instructions ("never do X", "always check before Y"). The problem: it never checks existing rules before creating new ones. This causes:

1. **Duplicates** — same rule created multiple times (e.g. "divorce" covenant appeared 4x)
2. **Contradictions** — user says "share your thought process" (creates MUST), later says "stop sharing thought process" (creates MUST NOT), but the original MUST is never removed
3. **Near-duplicates** — two rules that say essentially the same thing in different words and would be cleaner as one

See Audit & Cleanup item: "Covenant management: duplicates, contradictions, no pruning"

## Phase 1: Investigate

Before writing any code, investigate and report back:

1. **Find the NL Contract Parser.** Where does covenant creation happen? Trace the flow from when the agent detects a behavioral instruction to when the rule is persisted. Show me the key files and functions.

2. **Show me the covenant rule schema.** What fields does a CovenantRule have? How are they stored? Where are they read back?

3. **Find existing dedup logic.** There was a word-overlap check (>= 0.8) added for KnowledgeEntry dedup during 2D. Does anything similar exist for covenant rules? If so, why isn't it catching these cases?

4. **Show me the current covenant list** for the test tenant, so I can see examples of the duplicates and contradictions in the wild.

5. **How does the gate read covenants?** The dispatch gate (3D) reads covenant rules to evaluate tool calls. How does it retrieve them? This matters because the fix must not break gate evaluation.

Report findings before proceeding to Phase 2.

## Phase 2: Implement

The fix has two parts. Both run at covenant creation time (when the NL Contract Parser is about to add a new rule), NOT as a background batch process.

### Part A: Consolidation Check

When a new covenant rule is about to be created, use a lightweight LLM call (Haiku via `complete_simple(prefer_cheap=True)`) to check the new rule against ALL existing covenant rules for the tenant. The prompt should be something like:

```
You are checking whether a new behavioral rule should be added to an existing set of rules, or whether it's redundant with rules that already exist.

Existing rules:
{format existing rules as numbered list with rule_type + description}

Proposed new rule:
{new_rule.rule_type}: {new_rule.description}

Respond with exactly one of:
- ADD — the new rule is genuinely new and should be added as-is
- MERGE {n} — the new rule and existing rule #{n} express the same intent and should be consolidated. Provide a merged_description that captures both.
- REPLACE {n} — the new rule supersedes/updates existing rule #{n} (e.g. user changed their mind). The old rule should be deactivated and the new one added.

Respond in JSON:
{"action": "ADD"} 
or {"action": "MERGE", "existing_rule_number": 3, "merged_description": "..."}
or {"action": "REPLACE", "existing_rule_number": 3}
```

**On MERGE:** Update the existing rule's description to the merged version. Do NOT create a new rule. Emit a covenant.merged event.

**On REPLACE:** Soft-deactivate the old rule (set active=false, never delete). Create the new rule. Emit a covenant.replaced event.

**On ADD:** Proceed as normal.

### Part B: Contradiction Detection

If the Haiku call in Part A doesn't catch it (or as a separate check), also detect contradictions: a new MUST that conflicts with an existing MUST_NOT on the same capability, or vice versa.

When a contradiction is detected, do NOT silently resolve it. Instead:
- Do NOT add the new rule yet
- Return a signal to the active agent that a contradiction exists
- The agent should ask the user: "You previously told me to [old rule]. Now you're saying [new rule]. Which should I go with?"
- Once the user clarifies, apply the result (typically REPLACE)

The key insight: the agent handles the user conversation, the kernel handles the rule mechanics. The kernel detects the contradiction and tells the agent "ask the user about this." The agent asks naturally. The user's response triggers the resolution.

### Implementation constraints:
- Use `complete_simple(prefer_cheap=True)` for the Haiku check — same pattern as the gate
- Structured output for reliable JSON parsing (use the same patterns as other Haiku calls in the codebase)
- Emit events for all covenant mutations (merged, replaced, contradiction_detected, contradiction_resolved)
- Never delete rules — soft-deactivate (active=false). This is a system-wide invariant.
- The check must not add meaningful latency to the user experience — it runs in parallel with covenant confirmation, or as part of the existing creation flow
- Write tests for: exact duplicate, near-duplicate that should merge, contradiction, genuinely new rule that should ADD

## What NOT to do

- Do NOT build a `manage_covenants` tool or trust dashboard — that's future work
- Do NOT build compaction-based pruning — the Haiku check at creation time is sufficient for now
- Do NOT change how the gate reads covenants — the gate is working, don't touch it
- Do NOT add scoring or confidence thresholds — this is a binary check (same/different, contradicts/doesn't), not a ranking system

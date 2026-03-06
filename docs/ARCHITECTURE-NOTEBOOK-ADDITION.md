# Architecture Notebook Addition — March 4, 2026

> Append these sections to KERNOS-ARCHITECTURE-NOTEBOOK.md in the appropriate locations.

---

## Add to Section 4 (Phase 2-3: Context Spaces)

### Cross-Context Episodic Memory

**Source:** OSBuilder feedback on the agent identity model.

The context spaces model organizes knowledge by domain (D&D, legal, business). But some of the most important memory cuts across all domains — it's relational and temporal, not domain-specific.

"What was I stressed about last week?" can't be answered from any single context space. The answer spans: a work deadline (business context), a missed appointment (calendar context), and a comment about not sleeping well (personal context). The user experienced one week; the system bucketed it into domains.

**Design requirement:** The State Store needs a cross-context episodic layer — a timeline of the user's life that any context space can query. Memory projectors should write both domain-specific knowledge (to context spaces) and cross-cutting episodic entries (to a shared timeline). The episodic layer answers "what happened" questions. Context spaces answer "what do I know about X domain" questions.

This connects to the open memory architecture problem. The memory system needs at minimum two retrieval paths: domain-scoped (for context-specific depth) and temporal-episodic (for cross-cutting life narrative).

---

## Add to Section 4 (Phase 2-3: Context Spaces)

### Per-Context Behavioral Contracts on Shared Tools

**Source:** OSBuilder feedback on tool scoping.

A tool that exists in multiple context spaces may need different behavioral guardrails in each. Calendar in the D&D context ("do I have time to play tonight?") and calendar in the legal context ("schedule client meeting") is the same underlying tool, but the behavioral contract differs:

- D&D context: calendar is read-only reference, casual usage, no confirmation needed
- Legal context: calendar modifications require confirmation, scheduling involves client communication protocols

The behavioral contract model needs **context-space-level overrides**, not just global rules. A contract rule could be:

```
rule_type: "must"
capability: "calendar"
context_space: "legal"  # Only applies in this context
description: "Always confirm before scheduling client meetings"
```

Without context-space scoping, you either over-restrict (D&D can't casually check the calendar) or under-restrict (playful D&D posture can schedule client meetings). Neither is acceptable.

**For 1B.5:** The ContractRule dataclass should have room for an optional `context_space` field (reserved, not implemented). Global rules (context_space = None) apply everywhere. Context-scoped rules override within their space.

---

## Add to Section 9 (Real Scenarios) or new Section 12

### Shared-Agent Scenarios

Three distinct patterns emerged for multi-tenant access to a single agent identity:

**Pattern 1: Household — shared personality, individual contracts**

A husband and wife both communicate with the same agent. The soul (personality, values, relational style) is shared — it's one entity that knows the family. But each person has individual behavioral contracts. Perhaps one spouse manages business email through the agent; the other doesn't have access to business communications but can manage the shared family calendar.

**Architecture implication:** The soul belongs to a *workspace*, not a tenant. Both spouses are tenants within the same workspace. They share the agent's personality and shared resources (family calendar, household knowledge), but contracts are per-tenant. The agent knows who it's talking to and adjusts permissions accordingly, not personality.

**Pattern 2: Plumber's clients — owner plus scoped external access**

The plumber's clients text the agent's number to schedule appointments. They interact with the same agent (same personality, same business knowledge) but with radically restricted access. Clients can request scheduling, ask about availability, and get confirmations. They cannot read the plumber's email, modify prices, or access private business data.

**Architecture implication:** External contacts are tenants with minimal contracts — mostly "read-only public capabilities." The agent's personality is consistent (professional, helpful) but its contract model is completely different per tenant class. The Blueprint's external contact handling section already sketches this, but the workspace model makes it cleaner: the plumber's workspace has an owner tenant and multiple client tenants, each with scoped contracts.

**Pattern 3: Demo — completely separate tenants**

When showcasing the system, each new phone number or Discord account should get a completely separate tenant with a fresh hatch process. No shared state, no shared soul, no cross-contamination.

**Architecture implication:** This already works with the current `derive_tenant_id()` logic — each sender identity gets its own tenant_id. No workspace sharing. The demo case is the default behavior, not a special mode.

**Implementation timeline:**

Pattern 3 works today. Patterns 1 and 2 require the workspace abstraction — a layer between "system" and "tenant" where shared resources live. This is Phase 2 work, but the soul data model reserves `workspace_id` from 1B.5 to avoid retrofitting.

**Open questions:**
- How does the agent's greeting differ when it recognizes tenant A vs. tenant B in the same workspace? Same personality, but "hey Sarah" vs. "hey Mike" — does the user context portion of the soul need per-tenant variants within a shared workspace?
- For the plumber's clients: when a new unknown number texts, does the agent hatch a new client tenant automatically, or does it route to a generic "public-facing" tenant first?
- Workspace administration: who can add/remove tenants from a workspace? Only the owner? Can ownership be shared?

---

## Add to Section 5 (Phase 2-3: Behavioral Contracts)

### Bootstrap Consolidation Pattern

**Source:** Founder feedback on bootstrap fadeout design.

The bootstrap prompt should not disappear on a hard message count. The right trigger is **soul maturity** — when the soul has accumulated enough substance to carry the relationship without training wheels.

Before the bootstrap leaves the system prompt, the agent gets a **consolidation moment**: a reasoning call that asks it to review its bootstrap principles against its actual experience with this user, and migrate anything worth preserving into its permanent personality notes and user context.

This is analogous to a junior employee who's been following the onboarding manual for their first few weeks, then reaches a point where they've internalized the principles and no longer needs the manual on their desk. The wisdom isn't lost — it's become part of who they are.

**The consolidation call preserves Moss's concern:** Moss flagged that deleting BOOTSTRAP.md after first light means the agent can't audit its own formation. In KERNOS, the formation conversation lives permanently in the Event Stream. The consolidation step ensures the agent's identity isn't dependent on re-reading its birth instructions — the relevant guidance has been absorbed into the soul. The bootstrap content is always recoverable for audit, but no longer actively shaping behavior.

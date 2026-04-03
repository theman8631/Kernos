## NOW

**Status:** Phase 6A in progress. 1467 tests.
**Owner:** Founder
**Action:** SPEC-6A-3 shipped (state introspection). Next: 6A-4 (preference parser) or live validation.
**Tests:** 1467

> **Rule:** This block is always the first thing in the file. Whoever completes a step updates it before handing off. Format is always: Status (what), Owner (who), Action (next thing to do).

> **What this file is:** Claude Code's entry point. Read this first, then execute the Active Spec. Full planning and roadmap live in Notion. Architecture lives in `docs/TECHNICAL-ARCHITECTURE.md`.

---

## Active Spec

None currently active. Next spec will be assigned by founder.

---

## Phase Summary

| Phase | What | Tests | Completed |
|-------|------|-------|-----------|
| 1A | First Spark — SMS/Discord, Calendar MCP, persistence | 65 | 2026-03-01 |
| 1B | The Kernel — events, state, reasoning, capabilities, tasks, templates, projectors, isolation | 297 | 2026-03-06 |
| 2 | Memory + Context — entity resolution, space routing, compaction, retrieval | 627 | 2026-03-14 |
| 3 | Agent Workspace — files, tools, MCP, gate, awareness, covenants, identity, docs | 1039 | 2026-03-20 |
| 4A | Hardening — triggers, timezone, convlog locking, handler hygiene | 1330 | 2026-03-26 |
| 4B | Code Health — knowledge filtering, gate calibration, template cleanup, receipts | 1359 | 2026-03-27 |
| 4C | Structural Refactor — handler decompose, reasoning extract, handler protocol | 1359 | 2026-03-28 |
| 4D | Context Quality — ledger architecture, selective injection, fact harvest, tool surfacing | 1392 | 2026-03-31 |
| RT1 | Runtime Hardening — result budgeting, concurrency, timeouts, retry, turn serialization, closeout | 1429 | 2026-04-02 |
| 6A-1 | Preference State — first-class Preference type, persistence, migration from KnowledgeEntry | 1443 | 2026-04-02 |
| 6A-2 | Preference Linkage — source_preference_id on Trigger/CovenantRule, reconciliation cascade | 1453 | 2026-04-02 |
| 6A-3 | State Introspection — user truth view, operator state view, inspect_state tool, /status | 1467 | 2026-04-02 |

---

## Where Things Live

- **Full roadmap + priority stack:** Notion (Kernos Roadmap — Canonical)
- **Kit reviews + design documents:** Notion (Kit Reviews)
- **As-built architecture:** `docs/TECHNICAL-ARCHITECTURE.md`
- **Kernel design:** `docs/KERNEL-ARCHITECTURE-OUTLINE.md`
- **Completed specs:** `specs/completed/`
- **Design documents:** `docs/DESIGN-*.md`

---

## Key Architectural Decisions (permanent reference)

These are load-bearing decisions that Claude Code should always respect:

1. **Handler never knows about platform adapters; adapters never know about the handler.** All communication through NormalizedMessage.
2. **Every piece of state keyed to tenant_id.** Multi-tenancy from day one.
3. **No destructive deletions.** Shadow archive for user data. Internal operational artifacts (whispers, expired tokens) can be cleaned up.
4. **Conservative by default, expansive by permission.** Loss-cost gate: APPROVE / CONFIRM / CLARIFY.
5. **Behavioral contracts are the safety mechanism, not access restriction.** "Agent thinks, kernel enforces."
6. **Cognitive UI grammar:** RULES / NOW / STATE / RESULTS / ACTIONS / MEMORY / CONVERSATION — rebuilt every turn.
7. **Cohort agent architecture:** One principal reasoning agent surrounded by bounded specialized mediators (Router, Shaper, Surfacer, Gate, Harvester, Budgeter, etc.). Each should be bypassable.
8. **Turn serialization invariant:** For any (tenant, space) pair, only one turn may own reasoning and side effects at a time. Per-space mailbox/runner pattern.
9. **Depth should be recoverable, not always loaded.** Memory, facts, and tools are selectively loaded per-turn.
10. **Provider neutral.** Use "lightweight model" or "cheap model" instead of "Haiku." The cheap model varies by provider configuration.

---

*When Claude Code finishes a spec: update the NOW block, increment test count, move spec to specs/completed/.*

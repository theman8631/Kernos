## NOW

**Status:** 3D-HOTFIX-v2 COMPLETE — Live test 14/14 PASS. Gate fully redesigned: three-step (token → permission_override → model), CONFLICT response type, per-tool-call agent reasoning, permission_overrides mechanical bypass. Bug fixed: _get_capability_for_tool now checks tool_effects. 855 tests.
**Owner:** Founder / Architect
**Action:** Decide next Phase 3 spec.
**Tests:** 855
**Planning:** All roadmap planning is in Notion. This file is the execution bridge only.

> **Rule:** This block is always the first thing in the file. Whoever completes a step updates it before handing off. Format is always: Status (what), Owner (who: Founder / Architect / Claude Code), Action (the single next thing to do). If you're opening this file and wondering what to do, start here.

> **What this file is:** The execution bridge between planning and implementation. Claude Code reads this file first, then executes the Active Spec. **Planning and roadmap decisions live in Notion** — not here. This file tracks: current status (NOW block), phase completion (tracker), and architectural decisions made (decision log).
>
> For as-built architecture: see `docs/TECHNICAL-ARCHITECTURE.md`. For kernel design: see `docs/KERNEL-ARCHITECTURE-OUTLINE.md`. If something in this file conflicts with those documents, this file wins (it represents more recent decisions).

---

## Phase Status Tracker

### Phase 3: Agent Workspace

| ID | Spec | Status | Verified | Notes |
|---|---|---|---|---|
| 3A | Per-Space File System | COMPLETE | 2026-03-14 | Four kernel-managed file tools (write/read/list/delete), soft delete, manifest, compaction integration, Discord upload handling. 63 new tests. 711 total. |
| 3B | Per-Space Tool Scoping | COMPLETE | 2026-03-15 | System space auto-provisioned, active_tools on ContextSpace, universal flag on capabilities, space-aware build_capability_prompt() + get_tools_for_space(), Gate 2 seeding, request_tool kernel meta-tool, LRU exemption. 36 new tests. 747 total. |
| 3B+ | MCP Installation | COMPLETE | 2026-03-15 | Secure "secure api" credential handoff, CapabilityStatus.SUPPRESSED, connect_one/disconnect_one, mcp-servers.json in system space, startup merge, tool.installed/uninstalled events, requires_web_interface flag. 47 new tests. 849 total. |
| 3D | Dispatch Interceptor | COMPLETE | 2026-03-15 | Gate in tool-use loop, must_not covenants block before fast path, explicit instruction fast path, permission overrides + covenant authorization (Haiku), DISPATCH_GATE events, delete_file consolidated into universal gate. 55 new tests. 802 total. |
| 3D-HOTFIX | Dispatch Gate Redesign | COMPLETE | 2026-03-15 | Async Anthropic client (FIX 1), Haiku as sole authority — no keyword fast path (FIX 2), ApprovalToken single-use confirmation flow (FIX 3), tool description in model prompt + detailed failure reasons (FIX 4). 851 tests. |
| 3D-HOTFIX-v2 | Gate Full Redesign (spec-driven) | COMPLETE | 2026-03-16 | Three-step gate (token → permission_override → model), CONFLICT response type, agent reasoning extraction, recent messages in prompt, permission_overrides as mechanical bypass (not in rules_text). Bug: _get_capability_for_tool checks tool_effects. Live test 14/14 PASS. 855 tests. |

---

### Phase 2: Memory + Context Intelligence

| ID | Spec | Status | Verified | Notes |
|---|---|---|---|---|
| 2.0 | Schema Foundation Sprint | COMPLETE | 2026-03-06 | All Phase 2 data models planted; ContextSpace auto-created on soul init |
| 2A | Entity Resolution + Fact Dedup | COMPLETE | 2026-03-08 | Three-tier cascade, three-zone dedup, Voyage AI embeddings, NOOP reinforcement |
| 2A-PATCH | Relationship-Role Entity Linking | COMPLETE | 2026-03-13 | role_match Tier 1, entity upgrade, split reconciliation. Self-healed live split. |
| 2B | Context Space Routing (v1 — algorithmic) | SUPERSEDED | 2026-03-08 | Replaced by 2B-v2 |
| 2B-v2 | Context Space Routing (LLM Router) | COMPLETE | 2026-03-10 | LLM router (Haiku), tagged message stream, space thread assembly, cross-domain injection, Gate 1/2 space creation, session exit |
| 2C | Context Space Compaction | COMPLETE | 2026-03-13 | Two-layer compaction (Ledger + Living State), token tracking, domain-adaptive editorial judgment, rotation + archival |
| 2D | Active Retrieval + NL Contract Parser | COMPLETE | 2026-03-14 | `remember` kernel tool, RetrievalService (knowledge + entity + archive search), NL Contract Parser, quality score ranking replaces FSRS-6. 627 tests, 13/14 live steps passed. |

---

### Phase 1A: First Spark — COMPLETE

All deliverables live-verified. Full pipeline working: message in via Discord, Claude processes with MCP tools, response out. Calendar capability live. Persistence survives restart. Architecture cleanly separates adapter / handler / capability. SMS path ready when Twilio A2P clears.

| ID | Deliverable | Status | Verified | Notes |
|---|---|---|---|---|
| 1A.1 | Evaluate AIOS codebase | COMPLETE | 2026-02-27 | Decision: reference-only, not fork |
| 1A.2 | SMS gateway + normalized messaging | CODE COMPLETE | — | Tests pass. Live SMS blocked on Twilio A2P registration |
| 1A.2b | Discord adapter | COMPLETE | 2026-02-28 | Primary testing channel |
| 1A.3 | Google Calendar via MCP | COMPLETE | 2026-02-28 | Real calendar data via Discord |
| 1A.4 | Basic persistence | COMPLETE | 2026-03-01 | Three-store separation, shadow archive, auto-provisioning |

### Phase 1B: The Kernel — COMPLETE

Building the kernel layer that transforms a chatbot-with-tools into an intelligent operating system. See `docs/KERNEL-ARCHITECTURE-OUTLINE.md` for the full design.

| ID | Deliverable | Status | Verified | Notes |
|---|---|---|---|---|
| 1B.1 | Event Stream + State Store | COMPLETE | 2026-03-03 | 79 events captured, cost tracking live, 7 default contract rules, CLI inspection tools |
| 1B.2 | Reasoning Service abstraction | COMPLETE | 2026-03-03 | Provider ABC, AnthropicProvider, ReasoningService owns tool-use loop. Handler imports zero SDK code. |
| 1B.3 | Capability Graph formalization | COMPLETE | 2026-03-03 | Three-tier registry, known.py catalog, build_capability_prompt(), CLI capabilities command. |
| 1B.4 | Task Engine (minimal) | COMPLETE | 2026-03-03 | Task dataclass + lifecycle, TaskEngine wraps reasoning, task.created/completed/failed events, handler delegates via engine. |
| 1B.5 | Agent templates + CLI | COMPLETE | 2026-03-05 | Template + Soul datamodels, hatch process, template-driven prompt assembly, CLI soul/contracts/capabilities fixes. |
| 1B.6 | Tenant isolation verification + test suite | COMPLETE | 2026-03-05 | 65 new tests across test_isolation.py + test_kernel_integrity.py; _safe_name hardened + consolidated; update_knowledge/update_contract_rule tenant-scoped |
| 1B.7 | Memory Projectors | COMPLETE | 2026-03-06 | Two-tier extraction: Tier 1 rule-based (sync, zero cost), Tier 2 LLM (async background). Dedup, confidence precedence, corrections/supersedes chain. 70 new tests in test_projectors.py. |

### Phase 1B Completion Criteria (from Blueprint + outline)

- Can spawn agents that run concurrently, access shared memory safely, use MCP tools, respect permission boundaries
- Kernel survives restart with state intact
- Two tenant instances on same machine cannot see each other's data
- Every reasoning call logged with model, tokens, cost, duration
- Zero-cost-path principle: simple messages add no perceptible latency over 1A

---

## Live Verification Policy

Every deliverable that adds or changes user-facing capability requires a live test before it is marked complete. Automated tests prove the code works in isolation. Live tests prove it works in the world.

**Structural rule:** Every spec that has user-facing changes MUST include a "Live Verification" section with:
- Prerequisites (accounts, credentials, setup needed)
- Step-by-step deployment instructions
- A test table: what to send, what to expect
- Troubleshooting for common failures

**Data structure review:** For kernel infrastructure deliverables (1B.1+), the founder inspects actual data structures with their own eyes — event payloads, state store records, cost logs — and verifies each field earns its place before the deliverable is marked complete.

The architect produces verification steps as part of each spec. Claude Code does not execute them — the founder does. A deliverable is not COMPLETE until live verification passes.

Every live verification includes an Agent Awareness test as the first step. On a cold session, the agent must correctly identify itself, its platform, its available tools, and its trust context.

If a deliverable is purely internal (refactoring, test infrastructure, documentation), live verification is not required. The architect will note "Live verification: N/A" in the spec.

---

## Active Spec

SPEC-3A complete. No active spec. Founder to decide next Phase 3 spec.

---

## Decisions Made

### 2026-03-15: 3D-HOTFIX-v2 — Gate Full Redesign (spec-driven)

Implemented `specs/3D-HOTFIX-GATE-REDESIGN.md` in full. The gate is now definitively correct.

**Three-step gate:**
- Step 1: Token check (mechanical — single-use ApprovalToken, 5-min TTL, input hash)
- Step 2: Permission override (mechanical — `always-allow` bypasses model entirely; no LLM call; zero cost for high-volume automation)
- Step 3: Model evaluation — sole correctness authority (EXPLICIT / AUTHORIZED / CONFLICT / DENIED)

**New: CONFLICT response** — when user asks for an action AND a `must_not` rule applies but the user didn't address it. Agent surfaces three options: respect the rule, one-time override (with token), or update/remove the rule permanently.

**New: Agent reasoning extraction** — text from the LLM's response before the tool_use block is extracted and passed to the model gate, so the gate can evaluate whether the agent's reasoning aligns with the user's actual request.

**New: Recent messages context** — last 5 user turns from conversation history passed to model gate, so vague follow-up messages ("again?") are understood in context.

**Parser:** First-word-only parsing. "DENIED\n\nThe EXPLICIT request..." → DENIED (not allowed). Prevents false approvals from verbose denial explanations.

**Permission overrides:** NOT included in rules_text. They bypass the model as Step 2. This prevents 50 emails with `always-allow` from triggering 50 model calls.

**Removed:** `_has_prohibiting_covenant`, `_get_domain_keywords`, `_DOMAIN_KEYWORDS`. The model handles must_not detection — no structured pre-check.

- **Modified files:** `kernos/kernel/reasoning.py`, `tests/test_dispatch_gate.py`
- **Spec moved to:** `specs/completed/3D-HOTFIX-GATE-REDESIGN.md`
- **Tests:** 855 total passing.

### 2026-03-15: 3D HOTFIX — Dispatch Gate Redesign

Four critical fixes shipped together after live testing revealed the gate failed on natural-language requests:

- **FIX 1 — Async Anthropic client:** `anthropic.Anthropic` → `anthropic.AsyncAnthropic`. Sync client calls `time.sleep()` on 429 retries, blocking the asyncio event loop and causing Discord heartbeat failure → session invalidation.

- **FIX 2 — Haiku as sole authority:** Removed the keyword fast path entirely (`_TOOL_SIGNALS`, `_explicit_instruction_matches`). The gate now has three steps: (0) must_not covenant check, (1) approval token check, (2) permission override, (3) Haiku answers EXPLICIT/AUTHORIZED/DENIED. No keyword list — Haiku handles all languages and natural-language phrasings. Tool description from MCP manifest passed to Haiku so the gate works for any future tool without configuration. max_tokens raised from 16 to 64 (was causing truncation).

- **FIX 3 — ApprovalToken:** When the gate blocks, an `ApprovalToken` (UUID hex[:12], MD5 hash of tool_input) is issued and included in the [SYSTEM] message. Agent re-submits with `_approval_token: '{token_id}'` after user confirms. Token is single-use, 5-minute TTL, input-hash-verified.

- **FIX 4 — Transparent failure reasons:** `must_not_block` includes rule description. DENIED with rules: `covenant_ambiguous (N rules evaluated, Haiku response: '...')`. DENIED with no rules: `denied — user message does not request this action and no covenant matches`.

- **Modified files:** `kernos/kernel/reasoning.py`, `tests/test_dispatch_gate.py`, `tests/test_reasoning.py`, `tests/test_app.py`, `tests/test_files.py`
- **Tests:** 851 total passing.

### 2026-03-15: SPEC-3B — Per-Space Tool Scoping — COMPLETE

- **What:** System space auto-provisioned at tenant init alongside Daily. `active_tools: list[str]` added to ContextSpace — empty = system defaults (kernel tools + universal MCP tools). `universal: bool` flag on CapabilityInfo — google-calendar set universal=True. `build_capability_prompt(space=)` and `get_tools_for_space(space=)` added to CapabilityRegistry for space-aware filtering. System space sees all tools; non-system spaces see only universal + explicitly activated. Gate 2 expanded schema includes `recommended_tools` — LLM seeds `active_tools` at space creation. `request_tool` kernel meta-tool: exact match, fuzzy match (string presence), not-installed redirect. `_activate_tool_for_space()` updates `active_tools` in state. LRU archiving exempts system space. Documentation files (`capabilities-overview.md`, `how-to-connect-tools.md`) written to system space at creation. Router excludes system space from routing candidates. Backward compat: existing spaces load with `active_tools=[]`.
- **New files:** `tests/test_tool_scoping.py` (36 tests)
- **Modified files:** `spaces.py` (active_tools field), `state_json.py` (_CONTEXT_SPACE_FIELDS updated, _load_context_space default), `capability/registry.py` (universal flag, space-aware methods), `capability/known.py` (universal=True for google-calendar), `kernel/reasoning.py` (REQUEST_TOOL, _KERNEL_TOOLS, _handle_request_tool, _activate_tool_for_space, set_registry, set_state), `messages/handler.py` (system space creation, _write_system_docs, LRU exemption, Gate 2 expansion, get_tools_for_space, build_capability_prompt(space=)), `kernel/router.py` (system space excluded from routing candidates)
- **Tests:** 36 new tests in test_tool_scoping.py. Total: 747 passing.
- **Live verified:** 13/13 steps passed. System space auto-created (space_5a7b039c). Both doc files present. request_tool intercepted and returned not-installed redirect. LRU exemption confirmed. All 6 existing spaces load with active_tools=[] (backward compat). See `tests/live/LIVE-TEST-3B.md`.
- **Finding:** System space excluded from LLM router candidates — messages with "system settings" wording route to Daily/current space instead of System space. Low impact (kernel tools work anywhere); fix is adding System space to router's active_spaces list in a future pass.
- **Full spec:** `specs/completed/SPEC-3B-TOOL-SCOPING.md`

### 2026-03-14: SPEC-3A — Per-Space File System — COMPLETE

- **What:** Four kernel-managed tools (write_file, read_file, list_files, delete_file) routed through ReasoningService intercept. Per-space file directories at `data/{tenant_id}/spaces/{space_id}/files/`. Soft delete moves files to `.deleted/` with timestamp — nothing permanently deleted. File manifest in `.manifest.json` (fast path for list_files, source for compaction Living State). Compaction integration: manifest injected into compact() input, FILES section guidance added to COMPACTION_SYSTEM_PROMPT. Discord attachment handling: text files downloaded by bot, stored in context, handler processes them via _handle_file_upload(). Delete principle enforcement: kernel checks user message for delete signals — agent cannot self-initiate deletion. ContextSpace gains `max_file_size_bytes` / `max_space_bytes` config hooks (None defaults, enforcement deferred). CLI: `kernos-cli files <tenant_id> <space_id>`.
- **New files:** `kernos/kernel/files.py` (FileService + FILE_TOOLS), `tests/test_files.py` (63 tests)
- **Modified files:** `spaces.py` (config hooks), `reasoning.py` (KERNEL_TOOLS set, set_files(), input_text on ReasoningRequest, _check_delete_allowed(), file tool routing), `compaction.py` (manifest injection, FILES guidance), `template.py` (file tool awareness in operating principles), `handler.py` (FileService wired, FILE_TOOLS added, _handle_file_upload(), attachment context processing, input_text on request), `discord_bot.py` (text attachment download + context injection), `cli.py` (files command)
- **Tests:** 63 new tests in test_files.py. Total: 711 passing.
- **Live verified:** 9/11 steps passed. Henderson NDA draft created in Business space. D&D session log (3155 bytes, pip-session-log.md) created in D&D space. Cross-space isolation confirmed bidirectionally. CLI working. 2 failures were test design issues (fresh conversation with no routing context for D&D space) — not implementation bugs. See `tests/live/LIVE-TEST-3A.md`.
- **Full spec:** `specs/completed/SPEC-3A-FILE-SYSTEM.md`

### 2026-03-13: Pre-2D Diagnostic Audit — Findings and Decisions

Full diagnostic audit of the kernel before Phase 2D. Six questions investigated; eight decisions recorded.

- **DECIDED — Soul user_context eliminated.** `soul.user_context` was an append-only string with no dedup — 24 lines of contradictory facts ("Lives in Portland" AND "Lives in Seattle"). Replaced by KnowledgeEntry query at prompt-build time. `_build_system_prompt()` now queries active user-subject KEs with archetype in (structural, identity, habitual). Soul becomes the relationship layer only (name, communication style, personality). Field retained on dataclass for serialization compat, marked deprecated.

- **DECIDED — Personality evolution on rotation.** `soul.personality_notes` rewritten via one Haiku call on compaction rotation. Prompt: describe patterns, not events. "Approaches problems by mapping them to familiar frameworks" is a pattern. "Mentioned late-night coding on March 6" is an event — excluded. Preserves stable core; revision, not replacement. Fires only on rotation — infrequent, cheap, deliberate.

- **DECIDED — Embedding pipeline fixed.** Three gaps: (1) `_apply_correction()` bypassed embeddings entirely — now routes through embedding pipeline. (2) `_write_entry_enhanced()` had zero retries — now retries once with 2s delay before fallback. (3) No way to fix historical gaps — added `kernos-cli backfill-embeddings <tenant_id>`. Backfill run: 29/29 entries embedded, 0 failed. Coverage: 67/67 (was 38/67).

- **DECIDED — Dead weight fields: keep for now, 2D decides.** `salience`, `foresight_signal`, `foresight_expires` are written by Tier 2 extraction but never read in production code (CLI display only). Foresight fields were designed for the retrieval tool — 2D will either wire them into retrieval ranking or cut them. Do not remove yet.

- **DECIDED — `compute_retrieval_strength()` is orphaned, 2D wires it.** The FSRS-6 power-law decay formula exists in `state.py` and uses `lifecycle_archetype`, `storage_strength`, `last_reinforced_at` — but is only called from CLI (`cli.py:120`). Not integrated into context assembly or knowledge injection. The retrieval tool in 2D becomes the consumer. If 2D finds a simpler ranking sufficient, the function gets removed.

- **DECIDED — Entities do not reach the conversation model.** EntityNodes inform Tier 2 extraction only (coreference resolution via `_build_entity_context()`). After user_context elimination, user facts come from KnowledgeEntries at prompt-build time. Entity relationships, aliases, and graph structure are invisible to the conversation model. Identity edges (SAME_AS, MAYBE_SAME_AS) are not traversed at inference time. 2D's retrieval tool surfaces entity data when the agent searches. Prompt-build may also include lightweight entity context alongside user KEs — 2D decides.

- **DECIDED — NL Contract Parser is unbuilt.** No code path creates covenant rules from conversation. `CovenantRule.source` field supports three values (default/user_stated/evolved) but only "default" has a code path. The one `user_stated` rule in live data (`rule_test_scoped`) was manually inserted for SPEC-2B testing. 2D builds the NL Contract Parser or defers it.

- **DECIDED — Keep dirty test data.** Existing contradictions (location conflicts in soul), split entities (Liana SAME_AS merge, Sarah MAYBE_SAME_AS), and embedding backfill results are valuable for resilience testing. Do not wipe tenant data before 2D.

### 2026-03-13: SPEC-2C — Context Space Compaction System — COMPLETE

- **What:** Replaces naive `_truncate_to_budget()` with a two-layer compaction system per context space. Every space gets a Ledger (append-only historical record with domain-appropriate editorial judgment) and a Living State (rewritten each cycle to reflect current truth). Token tracking via `cumulative_new_tokens` triggers compaction when the ceiling is reached. The ceiling is computed from model capacity minus instructions, context definition, and existing history. Document rotation seals full documents as archives with index summaries. Adaptive headroom reduces conversation headroom by 5% if rotation rate exceeds 20%.
- **New files:** `kernos/kernel/tokens.py` (TokenAdapter ABC + AnthropicTokenAdapter + EstimateTokenAdapter), `kernos/kernel/compaction.py` (CompactionState dataclass + CompactionService + COMPACTION_SYSTEM_PROMPT), `tests/test_compaction.py` (45 tests), `tests/live/run_2c_live.py`, `tests/live/run_2c_phase2.py`
- **Modified files:** `kernos/kernel/event_types.py` (3 compaction event types), `kernos/persistence/base.py` + `json_file.py` (`include_timestamp` param on `get_space_thread()`), `kernos/messages/handler.py` (CompactionService init, token tracking after response, compaction trigger, headroom init on daily/Gate 2 spaces, `_assemble_space_context()` rewrite for compaction-aware context), `kernos/cli.py` (`compaction` command)
- **Tests:** 568 passing (516 existing + 52 new compaction tests).
- **Live verified:** D&D space — 2 compactions fired correctly with domain-appropriate narrative Ledger entries. Daily space — 1 compaction with operational/task-oriented Ledger entries. Same COMPACTION_SYSTEM_PROMPT produces distinctly different editorial voices per domain. Historical context query successfully recalled compacted history. Graceful degradation: AnthropicTokenAdapter falls back to EstimateTokenAdapter when API credits exhausted. See `tests/live/LIVE-TEST-2C.md`.
- **Full spec:** `specs/SPEC-2C-COMPACTION.md`

### 2026-03-10: SPEC-2B-v2 — LLM Context Space Routing — COMPLETE

- **What:** Replaces the previous algorithmic router (alias/entity/MRA matching) with an LLM-based router (Haiku) that reads message meaning. Every message gets `space_tags` stored on its conversation record — the full message stream is tagged rather than routed to a single space. Spaces are reconstructed per-domain from the tagged stream. Cross-domain injections appear in the system prompt as background context (not fake dialogue). Spaces create themselves organically via two gates: Gate 1 counts how many messages are tagged toward an unnamed topic cluster; Gate 2 fires an LLM call at the threshold to judge whether it's a real domain and generate name + description. Session exit maintenance fires when focus shifts away from a non-daily space, updating its description with what happened that session. LRU sunset caps active spaces at 40.
- **Supersedes:** SPEC-2B (algorithmic router). ContextSpace model fields `routing_keywords`, `routing_entity_ids`, `routing_aliases`, `suggestion_suppressed_until` removed — LLM reads descriptions, not keyword lists.
- **New files:** `tests/test_routing.py` (complete rewrite, 45 tests), `tests/live/LIVE-TEST-2B-v2.md`, `tests/live/run_live_test_2b_v2.py`
- **Modified files:** `spaces.py` (dead fields removed), `persistence/base.py` (get_recent_full, get_space_thread, get_cross_domain_messages), `persistence/json_file.py` (implement new methods), `state.py` (topic hint ABC), `state_json.py` (topic hint impl), `reasoning.py` (prefer_cheap=True → Haiku model), `router.py` (LLMRouter replaces ContextSpaceRouter), `handler.py` (full process() rewrite + _assemble_space_context, _run_session_exit, _trigger_gate2, _enforce_space_cap), `cli.py` (remove alias args)
- **Tests:** 516 passing (471 existing + 45 new routing tests).
- **Live verified:** Haiku router working per message, Gate 1 counting (dnd_campaign 1→15), Gate 2 created `space_fbdace10` with LLM-generated description, multi-tagging on cross-domain messages, space thread assembly showing coherent D&D conversation, return-to-D&D routing correct. See `tests/live/LIVE-TEST-2B-v2.md`.
- **Design note:** Topic hints (snake_case strings emitted by the router for emerging topics) are how Gate 1 counts before a space exists. Gate 2 clears hints on creation or decline. Space names may start as raw topic hint strings — session exit maintenance refines them over time.
- **Full spec:** `specs/completed/SPEC-2B-v2-CONTEXT-ROUTING.md`

### 2026-03-08: SPEC-2B — Context Space Routing — COMPLETE

- **What:** Algorithmic context space routing activates the ContextSpace infrastructure planted in SPEC-2.0. Messages route to spaces via a three-check cascade: (1) space name/alias match in message text, (2) entity ownership — a mentioned entity belongs to a specific space, (3) MRA fallback — most recently active space, flagged as `confident=False`. The system prompt gains posture injection for non-daily spaces (plain English working style override with "does not override core values" label). Covenant rules load scoped: space-specific rules + global rules, excluding other spaces' rules. Space switches prepend a `[Switched from: X]` annotation to the LLM's input. Knowledge entries extracted in non-daily spaces get `context_space` set; user-level structural/identity facts are always global. Daily-only tenants have zero behavior change from Phase 1B.
- **New files:** `kernos/kernel/router.py` (ContextSpaceRouter), `tests/test_routing.py` (26 tests)
- **Modified files:** `state.py` (last_active_space_id on TenantProfile, query_covenant_rules ABC), `state_json.py` (query_covenant_rules with context_space_scope filtering), `handler.py` (router init, routing in process(), posture + scoped rules in _build_system_prompt(), handoff annotation, active_space_id to projectors), `coordinator.py` (active_space_id param), `llm_extractor.py` (active_space_id param, context_space on _write_entry/_write_entry_enhanced, _space_for_entry scoping logic), `cli.py` (create-space command)
- **Tests:** 26 new tests in test_routing.py. Total: 497 passing.
- **Live verified:** Alias routing (confident=True), MRA fallback (confident=False), space switch events with correct payload, posture injection (207-char system prompt difference), scoped rule filtering (8 vs 7 rules), knowledge scoping (Mike Sullivan fact tagged to Test Project space), handoff annotation on switch. See `tests/live/LIVE-TEST-2B.md`.
- **Design note:** MRA fallback is "sticky" — without explicit routing signals, the router stays in the current space. This is by design; 2C's context assembly can correct routing for ambiguous messages.
- **Full spec:** `specs/SPEC-2B-CONTEXT-SPACE-ROUTING.md`

### 2026-03-13: SPEC-2A-PATCH — Relationship-Role Entity Linking — COMPLETE

- **What:** Fixed the split-entity problem where "my wife" and "Liana" created two separate EntityNodes. Three changes: (1) Extraction prompt updated — name+role together ("my wife Liana") now produces ONE entity with both fields, not two. (2) Tier 1 resolution gains `role_match` check — when incoming entity has a `relationship_type`, existing role-named entities ("user's wife") are matched and upgraded: real name becomes canonical, role becomes alias. Role_match runs before exact name matching to ensure split entities always route through reconciliation. (3) Split reconciliation — if a role entity AND a separate named entity both exist, the next "my wife Liana" mention merges them: knowledge entries migrated, duplicate deactivated, SAME_AS edge created. (4) Two coordinator/extractor bugs fixed: EntityResolver was only instantiated with VOYAGE_API_KEY present; `enhanced` flag gated entity resolution behind embeddings. Both fixed — entity resolution (Tier 1 deterministic) now always active regardless of Voyage.
- **Modified files:** `kernos/kernel/projectors/llm_extractor.py` (prompt addition, `resolve_entities` flag split from `enhanced`, `relationship_type` passed to resolver, `role_match` in ENTITY_MERGED events), `kernos/kernel/resolution.py` (`_role_forms()` helper, `relationship_type` param on `resolve()` + `_tier1_resolve()`, `role_match` Tier 1 check, `_apply_role_match()` upgrade + split reconciliation), `kernos/kernel/projectors/coordinator.py` (EntityResolver always instantiated for Tier 1)
- **Tests:** 7 new tests in `test_entity_resolution.py`. Total: 523 passing.
- **Live verified:** Live split (ent_cb0bed64 "user's wife" + ent_decd315d "Liana") self-healed on first "My wife Liana is amazing" message. entity.merged event emitted (role_match). SAME_AS edge created (confidence=1.0). See `tests/live/LIVE-TEST-2A-PATCH.md`.
- **Note on AC6 (knowledge entry linkage):** Historical KEs from the 2A live test have empty `entity_node_id` — a known data gap from before the field was populated. The reconciliation correctly migrates entries that ARE linked; back-filling historical entries deferred (non-blocking).
- **Full spec:** `specs/SPEC-2A-PATCH-ROLE-LINKING.md`

### 2026-03-07: SPEC-2A — Entity Resolution + Fact Deduplication — COMPLETE

- **What:** Three-tier entity resolution cascade (deterministic → multi-signal scoring → LLM) + three-zone fact deduplication (ADD / NOOP / LLM-classify). "Present, don't presume" principle implemented — name collision with mismatched context creates MAYBE_SAME_AS edge rather than auto-merging. Embeddings via Voyage AI voyage-3-lite stored separately in `embeddings.json` per tenant (keeps knowledge.json clean). NOOP reinforcement strengthens existing facts (reinforcement_count + storage_strength). Entity context injected into Tier 2 extraction prompt for coreference resolution. Graceful degradation: enhanced path requires VOYAGE_API_KEY; legacy hash-only dedup if absent.
- **New files:** `kernos/kernel/embeddings.py`, `kernos/kernel/embedding_store.py`, `kernos/kernel/resolution.py`, `kernos/kernel/dedup.py`, `tests/test_entity_resolution.py`
- **Modified files:** `entities.py` (6 new EntityNode fields), `state.py` (get_knowledge_entry, updated query_entity_nodes + save_identity_edge), `state_json.py` (implementations + per-tenant identity edges), `llm_extractor.py` (entity context injection, dual path, enhanced EXTRACTION_SCHEMA), `coordinator.py` (service initialization from VOYAGE_API_KEY), `cli.py` (entities command), `pyproject.toml` (rapidfuzz, jellyfish, voyageai deps)
- **Tests:** 45 new tests in test_entity_resolution.py. Total: 471 passing.
- **Full spec:** `specs/completed/SPEC-2A-ENTITY-RESOLUTION.md`

### 2026-03-06: Phase 1B — The Kernel — COMPLETE

- **What:** All 7 deliverables verified. 369+ tests passing. Kernel has: event stream, state store, reasoning service abstraction, capability graph, task engine, agent soul with personality and identity, behavioral contracts, tenant isolation, and memory projectors. The agent hatches, learns, remembers, and will graduate its bootstrap. Standard met: functional value of OpenClaw or better on the kernel layer, with architectural elements (kernel-owned memory, behavioral contracts, multi-tenancy, shadow archive) that exceed it.

### 2026-03-06: Phase 1B.7 — Basic Memory Projectors complete

- **What:** Two-tier extraction pipeline: Tier 1 rule-based (name + style, sync, zero cost), Tier 2 LLM-based (entities, facts, preferences, corrections, async). Soul learns from conversations. Bootstrap graduation enabled via soul maturity signals. KnowledgeEntry gains durability, content_hash, supersedes fields. ReasoningService gains `complete_simple()` for kernel infrastructure calls. Name correction verified live (John → JT).

### 2026-03-05: Phase 1B.7 — Memory Projectors code complete

- **What:** Two-tier memory extraction pipeline that transforms raw conversation into structured soul and knowledge store entries — zero extra latency for the user.
- **Tier 1 (synchronous, zero LLM cost):** Rule-based extraction of `user_name` and `communication_style` from user messages. Fires before the response is returned. Conservative: only writes to empty soul fields; corrections require Tier 2's contextual understanding. False-positive name protection (`_FALSE_POSITIVE_NAMES` blocklist). Emits `knowledge.extracted` event when fields are updated.
- **Tier 2 (async background, LLM):** `asyncio.create_task` fires `run_tier2_extraction()` after response is assembled. LLM extracts entities, facts, preferences, and corrections. Deduplication via SHA256[:16] content hash (O(1) lookup). Confidence precedence: stated entries displace inferred; inferred entries are silently discarded if a stated entry for the same subject already exists. Corrections create supersedes chains — old entry marked `active=False`, new entry references old ID. Permanent user-subject facts appended to `soul.user_context`.
- **Bootstrap consolidation:** `_consolidate_bootstrap()` called on soul maturity (user_name + user_context + communication_style + interaction_count ≥ 10). Uses `complete_simple()` to summarize personality into `soul.personality_notes`, then sets `bootstrap_graduated=True`.
- **complete_simple():** New `ReasoningService` method for kernel infrastructure calls — no tools, no history, no task events. Stateless single-shot LLM call used by Tier 2 and consolidation.
- **Name ask:** `_maybe_append_name_ask()` appends "By the way — what should I call you?" to the first response if the soul has no `user_name` and the response doesn't already ask.
- **Soul field added:** `emoji: str = ""` — self-chosen identity marker that emerges from conversation.
- **New files:** `kernos/kernel/projectors/__init__.py`, `rules.py`, `llm_extractor.py`, `coordinator.py`.
- **Tests:** 70 new tests in `tests/test_projectors.py`. Total: 367 passing.
- **Full spec:** `specs/completed/SPEC-1B7-MEMORY-PROJECTORS.md`

### 2026-03-05: Phase 1B.6 — Tenant Isolation Verification + Test Suite code complete

- **What:** Exhaustive proof that multi-tenancy isolation holds across every data structure. No new features — verification and two security fixes.
- **Security fixes:** `update_knowledge` and `update_contract_rule` previously scanned all tenant directories to find an entry by ID — a cross-tenant data mutation vulnerability. Both now require `tenant_id` and scope exclusively to that tenant's directory.
- **_safe_name hardening:** Three duplicate `_safe_name` implementations consolidated into one shared `kernos/utils.py`. Hardened to strip `..` (path traversal), null bytes, and handle empty strings. All three callers (`events.py`, `persistence/json_file.py`, `state_json.py`) now import from there.
- **New tests:** `tests/test_isolation.py` (43 tests) — cross-tenant isolation for conversations, events, state store (profile, soul, knowledge, contracts, summaries), tenant store, audit store, path traversal attempts, malformed inputs, _safe_name coverage. `tests/test_kernel_integrity.py` (22 tests) — restart survival (soul, profile, contracts, events, summaries, knowledge), event completeness and ordering, cost tracking accuracy, shadow archive, behavioral contract defaults.
- **Total tests:** 297 passing (up from 232).
- **Full spec:** `specs/completed/SPEC-1B6-TENANT-ISOLATION.md`

### 2026-03-04: Phase 1B.5 — Agent Templates + CLI code complete

- **What:** The hardcoded "You are Kernos, a personal intelligence assistant" prompt is replaced by a template-driven, soul-aware system. Every tenant's first interaction creates a `Soul` (unhatched); after the first successful response the soul is marked `hatched=True` and an `agent.hatched` event is emitted. Subsequent interactions load the persisted soul and include the bootstrap prompt until `bootstrap_graduated` is set (triggered when all four maturity signals are present: user_name, user_context, communication_style, and interaction_count ≥ 10).
- **Key new files:** `kernos/kernel/soul.py` (Soul dataclass), `kernos/kernel/template.py` (AgentTemplate + PRIMARY_TEMPLATE with operating principles, default personality, bootstrap prompt). Both are reachable via `kernos.kernel.*` — no import isolation violations.
- **State Store additions:** `get_soul` / `save_soul` abstract methods added to StateStore ABC; implemented in `JsonStateStore` as `{data_dir}/{tenant_id}/state/soul.json`. `ContractRule` gains `context_space: str | None = None` (reserved for Phase 2 scoped contracts).
- **Handler changes:** `process()` now calls `_get_or_init_soul()` before reasoning, loads contract rules from State Store, passes all five args to `_build_system_prompt()`. `_post_response_soul_update()` handles hatch marking, interaction counting, and maturity-gated bootstrap graduation (no consolidation reasoning call in 1B.5 — maturity gate only, as specified).
- **CLI additions:** `soul <tenant_id>` — inspect hatched soul; `contracts <tenant_id>` — grouped display by type (MUST/MUST NOT/PREFERENCE/ESCALATION); `contract` kept for backwards compatibility. `capabilities` fixed: removed env-var inference, removed invented "CONFIGURED" label, uses CapabilityStatus vocabulary only; optional `--tenant` arg reads from persisted profile for accurate runtime status.
- **Events added:** `agent.hatched`, `agent.bootstrap_graduated` added to EventType enum.
- **Known gap (documented in spec):** `user_name` auto-extraction from conversation not implemented — memory projector work (Phase 2). Bootstrap will remain active until user_name is manually set or memory projectors are built.
- **Tests:** 27 new tests in `tests/test_soul.py`. 232 total passing.
- **Full spec:** `specs/completed/SPEC-1B5-AGENT-TEMPLATES.md`

### 2026-03-03: Phase 1B.4 — Task Engine (minimal) complete

- **What:** Every piece of work in the system now flows through a `Task`. The `TaskEngine` wraps `ReasoningService`, emitting `task.created` and `task.completed`/`task.failed` lifecycle events. Handler creates a `Task` dataclass for every inbound message and delegates to `engine.execute(task, request)`, reading `task.result_text` as the response. Engine re-raises reasoning errors; handler still catches them for user-facing friendly messages.
- **Key structures:** `Task` dataclass (id, type, tenant_id, conversation_id, status, priority, lifecycle timestamps, input_text, result_text, error_message, metrics). `TaskType` (REACTIVE_SIMPLE). `TaskStatus` (PENDING/RUNNING/COMPLETED/FAILED). `TaskPriority` constants (integer levels). `generate_task_id()` produces `task_{ts_us}_{rand4}` — lexicographically sortable.
- **Task ID format:** `task_{microseconds_since_epoch}_{4_random_hex_chars}` — sortable and collision-resistant.
- **Event types added:** `task.created`, `task.completed`, `task.failed`.
- **CLI:** `./kernos-cli tasks <tenant_id>` shows task lifecycle from events. `capabilities` command now loads `.env` so CONFIGURED shows correctly.
- **Zero-cost path:** For reactive-simple tasks (100% of current traffic), engine is one function call wrapping existing reasoning flow. No routing, no decomposition overhead.
- **Full spec:** `specs/completed/SPEC-1B4-TASK-ENGINE.md`

### 2026-03-03: Phase 1B.3 — Capability Graph formalization complete

- **What:** Transformed the flat tool list into a three-tier capability registry (connected/available/discoverable). Handler's hardcoded `"calendar" in n.lower()` detection replaced by structured metadata from `CapabilityRegistry.build_capability_prompt()`.
- **Key structures:** `CapabilityInfo` (name, display_name, description, category, status, tools, setup_hint, setup_requires, server_name). `CapabilityRegistry` with `get_connected()`, `get_available()`, `get_by_category()`, `get_connected_tools()`, `build_capability_prompt()`. `KNOWN_CAPABILITIES` catalog in `known.py` — three entries (google-calendar, gmail, web-search).
- **System prompt:** Now includes AVAILABLE capabilities so the agent can offer to set them up. Agent says "I have email available — want me to help connect it?" instead of "I can't do that."
- **Adding a new capability:** One entry in `known.py` + MCP server registration. No handler changes, no prompt changes.
- **CLI:** `./kernos-cli capabilities` shows full registry with status, description, setup info.
- **State Store:** Tenant profiles now sync capability status on every message (`capabilities: {"google-calendar": "connected", "gmail": "available", ...}`).
- **Full spec:** `specs/completed/SPEC-1B3-CAPABILITY-GRAPH.md`

### 2026-03-03: Phase 1B.1 — Event Stream and State Store live-verified

- **What:** Introduced two foundational kernel primitives. Event Stream (typed, append-only, multi-reader event log) and State Store (indexed knowledge model with four domains: tenant profile, user knowledge, behavioral contracts, conversation summaries).
- **Key structures:** Event with 6 required fields (id, type, tenant_id, timestamp, source, payload). 14 event types covering message lifecycle, reasoning, tools, capabilities, and system. KnowledgeEntry with provenance chains. 7 default conservative behavioral contract rules per tenant.
- **Migration approach:** Additive — both old stores and new systems write. Nothing broke.
- **Live verification findings:** Cost tracking working ($0.70 for 19 API calls). ~12K input tokens per call (system prompt + 21 messages + 13 tools). Minor fixes applied: metadata duplication removed, startup/capability events wired.
- **Full spec:** `specs/completed/SPEC-1B1-EVENT-STREAM-STATE-STORE.md`

### 2026-03-03: Context Spaces — transparent multi-context routing (design decision, Phase 2)

- **What:** The user has one conversation. The kernel maintains multiple context windows behind it. Each managed resource, project, hobby domain, or life thread gets its own isolated context with accumulated depth. The kernel routes each inbound message to the correct context based on content — the user never explicitly switches.
- **Key design:** Free handoff annotations (algorithmic, zero LLM cost) on every context switch. Agent self-service via `query_context` tool for cross-context retrieval when annotation is insufficient. No agent-to-agent telephone. No lightweight kernel model pre-filtering every message.
- **Cost gradient:** free annotation → cheap State Store retrieval → rare user clarification. Complexity scales with actual ambiguity.
- **Not built in 1B.** Captured in architecture outline and future considerations. Primitives from 1B.1 (Event Stream, State Store, managed resources) support it.

### 2026-03-01: Discord adapter added as 1A.2b — primary testing channel

- **What:** Twilio A2P 10DLC registration takes days to weeks. Discord adapter added to unblock live testing.
- **SMS status:** Twilio adapter built, tested, ready. When A2P clears, SMS lights up with zero code changes.

### 2026-02-28: Live Verification Policy adopted

- **What:** Every deliverable with user-facing changes requires live testing before marked complete.
- **Structure:** Architect produces steps, Claude Code doesn't execute them, founder does.

### 2026-02-27: Google Calendar MCP — adopting nspady/google-calendar-mcp

- **Package:** `@cocal/google-calendar-mcp` (npm, run via npx)
- **License:** MIT. Most mature Google Calendar MCP server (964 stars).
- **Phase 2 note:** Re-evaluate `taylorwilsdon/google_workspace_mcp` (covers Calendar + Gmail + Drive in one server) when adding email agent.

### 2026-02-27: AIOS — reference-only, not fork

- **Decision:** Use AIOS as reference architecture. Too academic to fork cleanly. Rebuild kernel modules from scratch using its design patterns.

### 2026-02-27: DECISIONS.md created as execution bridge

- **What:** Bridge between planning and execution. Founder and architect plan here, Claude Code executes against Active Spec.

---

## Phase 2 Preparation

- **SPEC-2.0 complete + ContextSpace addition:** Schema Foundation Sprint done. All Phase 2 data models planted including ContextSpace (daily space auto-created on soul init). See `specs/completed/SPEC-2.0-SCHEMA-FOUNDATION.md`.
- **Research produced:** Four research papers in `research/` covering structured outputs, entity resolution, memory staleness, and behavioral contract enforcement.
- **Technical Architecture Document:** Needed before Phase 2 spec writing — captures the as-built kernel architecture, interfaces, data flows, and extension points.
- **Research lead:** Kit (OSBuilder) established as research lead for Phase 2 inputs.

---

## Completed Specs

Full specifications for completed phases have been moved to `specs/completed/` for reference. They are not active execution context.

---

*Last updated: 2026-03-15 (SPEC-3B complete — 747 tests passing. Per-Space Tool Scoping live-verified.)*

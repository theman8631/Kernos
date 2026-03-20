# SPEC-3J: Kernos Self-Documentation — docs/ as Source of Truth

**Status:** APPROVED — Kabe direct to Claude Code  
**Author:** Architect  
**Date:** 2026-03-19  
**Principle:** "Know thyself." Every feature Kernos has, it should understand. The docs are how it learns itself.

---

## Objective

Replace the `reference.py` string blob with proper documentation in `docs/` — nested sections, organized for quick retrieval, serving three consumers from one source of truth:

1. **The agent** — reads docs to understand itself
2. **Developers** — reads docs in the repo to understand the system
3. **Users** — same docs published as web reference when the web UI ships

The docs cover what's ACTUALLY LIVE, plus the vision and roadmap — so Kernos understands what it's part of and where it's headed. Unresolved future speculation is excluded. If it's not decided, it's not in the docs.

Write from two perspectives: (1) an agent in the system trying to understand what it can do, and (2) a human using the system trying to understand how it works.

---

## docs/ Directory Structure

```
docs/
├── index.md                    # What is Kernos? Vision, principles, the one-paragraph description
├── architecture/
│   ├── overview.md              # How Kernos works at a high level (kernel, handler, adapters)
│   ├── context-spaces.md        # What context spaces are, instance vs space hierarchy
│   ├── memory.md                # Knowledge entries, entities, retrieval, compaction
│   ├── soul.md                  # The two-layer identity model (template + soul.json)
│   └── event-stream.md          # Events, audit trail, append-only
├── capabilities/
│   ├── overview.md              # What capabilities are, how MCPs connect, unified registry model
│   ├── calendar.md              # Google Calendar — what it can do, what tools are available
│   ├── web-browsing.md          # Lightpanda — browsing AND searching, how to use it
│   ├── file-system.md           # Per-space files — write, read, list, delete
│   ├── memory-tools.md          # remember(), knowledge retrieval, entity search
│   └── sms.md                   # Twilio SMS — status, what it enables
├── behaviors/
│   ├── covenants.md             # What covenants are, auto-capture (Tier 2), manage_covenants tool
│   ├── dispatch-gate.md         # How the gate works, what gets confirmed vs executed
│   ├── proactive-awareness.md   # 3C — what it does, whispers, time pass, what's live NOW
│   └── instruction-types.md     # Behavioral constraints vs automation rules (standing orders)
├── identity/
│   ├── who-you-are.md           # You are Kernos. Platform identity. Name is a starting point.
│   ├── soul-system.md           # soul.json, what fields exist, how identity evolves
│   └── onboarding.md            # The first meeting, guided improv, competence first
└── roadmap/
    ├── vision.md                # The one-paragraph vision. Where this is all going.
    ├── whats-next.md            # Decided next steps: trigger system, outbound messaging, scheduler
    └── future.md                # Broad decided directions: multi-user, web UI, PIE expansion, cost
```

---

## Capability Management Principle

Default tools (calendar, email, browser, web search) are installed through the SAME mechanism as user-added tools. The only difference is they come pre-installed. The user or agent can disable any default just like disabling something they added themselves. All tools — defaults and user-added — appear in one unified management list.

`docs/capabilities/overview.md` MUST document this as the capability management model. Do not describe defaults as "built-in" — describe them as "pre-installed."

---

## Content Principles

- **Write for the agent first.** The primary reader is Kernos trying to understand itself. Clear, direct, no jargon-for-jargon's-sake. If Kernos reads `capabilities/web-browsing.md` it should immediately know it can search the web using its browser.
- **Only what's live or decided.** If a feature is shipped, document it accurately. If a direction is decided, include it in roadmap. If it's still being discussed, it's not in docs.
- **Each doc is self-contained.** The agent should be able to read one file and understand that topic. Cross-references are links, not dependencies.
- **Nest for quick retrieval.** The agent doesn't need to read all of docs/. It reads the specific section relevant to the current question. "How does my memory work?" → `docs/architecture/memory.md`. "Can I search the web?" → `docs/capabilities/web-browsing.md`.
- **Version reality, not aspiration.** Each capability doc states what works TODAY. A "Planned" section at the bottom can note decided future enhancements, but the main content is current truth.

---

## How the Agent Accesses Docs

Two mechanisms, complementary:

**1. System prompt summary (slim).** A short section in the system prompt — NOT the full reference blob. Just: "Your documentation is at docs/. Read the relevant section when you need to understand yourself. Start with docs/index.md for an overview." Plus a one-line summary per major section so the agent knows where to look.

**2. Kernel tool: `read_doc(path)`.** Always available. Gate classification: read (bypass). NOT gated behind developer mode — self-knowledge is always available. When asked "can you search the web?" the agent reads `docs/capabilities/web-browsing.md` and answers accurately.

The system prompt shrinks significantly. The reference.py blob is replaced by a concise directory listing plus the operating principles and covenant injection that must always be in-prompt.

---

## What Stays in the System Prompt

- Operating principles (from template.py — always-on values)
- Soul personality and user context (from soul.json — per-user identity)
- Active covenants (from state store — behavioral constraints)
- Capability list with status (from registry — what tools are available)
- Bootstrap prompt (if not yet graduated)
- Docs directory hint: "Your full documentation is in docs/. Read the relevant section when you need to understand a capability or behavior."

Everything else moves to docs/ and is read on demand.

---

## What reference.py Becomes

reference.py is NOT deleted — it becomes a thin index that generates the docs directory hint for the system prompt. The detailed content moves to docs/ files. reference.py might still hold a few always-in-prompt items (confirm protocol explanation, tool-specific usage notes too small for their own doc file), but the bulk of the content migrates.

---

## System Space Docs — DEPRECATED

Per-tenant system space files (`how-i-work.md`, `kernos-reference.md`, `capabilities-overview.md`, `how-to-connect-tools.md`) are DEPRECATED by this spec. They were a stopgap that drifts and double-maintains. With docs/ + read_doc, the agent reads the canonical source directly.

**Claude Code:** Remove the provisioning step that creates these files for new tenants. Existing tenants' copies become stale naturally — no migration needed.

---

## Roadmap Section Content (decided, not speculation)

**docs/roadmap/vision.md:**

"Kernos is a personal intelligence that lives in the cloud, works around the clock, and earns your trust through thousands of correct small actions — not by asking you to believe in it, but by proving itself one kept promise at a time. You text a phone number and within an hour you have an agent that knows your calendar, remembers your clients, watches for things you'd miss, and handles the work you shouldn't have to think about — whether you're a plumber managing appointments or a parent running a household. It starts as Kernos but becomes yours: it learns your voice, your priorities, your boundaries, and it respects them not because it's told to but because the infrastructure beneath it makes violation impossible. It's an open system — install any tool, connect any service, share access with your family or your team — but it's your system, running on your terms, with security that protects you from the world without protecting the world from you. The vision isn't an assistant you talk to. It's a second brain that works while you sleep, that knows what you meant even when you said it wrong, and that gets better not because someone shipped an update but because it lived another day with you."

**docs/roadmap/whats-next.md** contains decided next steps:
- Unified trigger system (time/event/state conditions → actions)
- Outbound messaging (reach user unprompted, notify_via channel selection)
- manage_schedule tool (unified trigger management)
- Twilio SMS connection (A2P approved, adapter exists)
- Whisper delivery spectrum upgrade (ambient/stage/interrupt)

**docs/roadmap/future.md** contains broad decided directions:
- Multi-user instances with trust tiers
- Web interface for real users
- PIE expansion (observe everything — collision/anomaly/opportunity/pattern passes)
- Multi-LLM routing and cost optimization
- Customer-facing spaces with disclosure
- Standing orders and event-triggered automation
- Unified capability registry (defaults = pre-installed, not special-cased)

None of these include implementation details — just the direction and why it matters.

---

## Implementation Order

1. Create the docs/ directory structure
2. Write each doc from current knowledge (Architecture Notebook, Blueprint, shipped specs, live test results, decisions). Write from two perspectives: agent understanding itself, and human understanding the system.
3. Create `read_doc(path)` kernel tool — always available, read bypass, not dependent on developer mode
4. Update system prompt assembly in handler.py to inject docs directory hint instead of reference.py blob
5. Slim down reference.py to thin index + always-in-prompt items
6. Remove system space doc provisioning (how-i-work.md, kernos-reference.md, capabilities-overview.md, how-to-connect-tools.md) from tenant initialization
7. Verify: fresh hatch, ask Kernos about each major capability — it should read the relevant doc and answer accurately

---

## Acceptance Criteria

1. docs/ directory exists with the nested structure above
2. Each doc accurately reflects current live state
3. System prompt is shorter (reference blob removed, docs hint added)
4. `read_doc(path)` kernel tool works and is always available (not developer-mode-gated)
5. Kernos can answer "what capabilities do you have?" by reading docs/
6. Kernos can answer "can you search the web?" by reading docs/capabilities/web-browsing.md
7. Kernos can explain context spaces, covenants, proactive awareness accurately
8. Kernos can describe the vision and what's coming next
9. System space doc provisioning removed from tenant initialization
10. reference.py is slimmed to index + always-in-prompt items only
11. All existing tests pass

---

## What Claude Code MUST NOT Do

- Do not dump unresolved future speculation into docs
- Do not make docs access depend on developer mode — self-knowledge is always available
- Do not rewrite operating_principles or bootstrap_prompt (those stay in template.py)
- Do not remove covenant injection from system prompt (covenants must always be in-prompt for gate enforcement)
- Do not create docs for features that aren't shipped
- Do not describe default tools as "built-in" — describe them as "pre-installed" with the same enable/disable mechanics as user-added tools

---

## Post-Implementation Checklist

- [ ] All tests pass (existing + new)
- [ ] docs/ directory created with full structure
- [ ] read_doc kernel tool implemented and working
- [ ] System prompt slimmed (reference blob → docs hint)
- [ ] System space doc provisioning removed
- [ ] reference.py slimmed to thin index
- [ ] Spec moved to specs/completed/
- [ ] DECISIONS.md NOW block updated
- [ ] docs/TECHNICAL-ARCHITECTURE.md updated

---

## Mandatory Standard Going Forward

Every future spec that ships MUST update the relevant docs/ section. This is now in the Spec Delivery Standards page as a mandatory post-implementation step. If a spec ships without updating docs/, the spec is not complete.

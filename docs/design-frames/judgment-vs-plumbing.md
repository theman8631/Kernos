# Judgment-vs-Plumbing

> The pattern: LLM calls do semantic work. Python does deterministic work. They never overlap.

## The pattern in one sentence

Every turn of Kernos involves many decisions. The ones that require reading meaning — *what is this message actually asking for? what does this member care about? does this response honor the declared covenant?* — are made by LLM calls. The ones that are lookups, mutations, or routing — *which context space does this member_id belong to? what is the current permission profile between A and B? which tool id maps to which handler function?* — are made by Python.

The two layers never overlap. Judgment calls never do bookkeeping. Plumbing never makes semantic guesses.

## What counts as judgment

Judgment work is anything that requires understanding what something means in context. In Kernos, this is done by bounded cohort agents:

- **Routing** — the Router cohort reads an incoming message and decides which context space it belongs to. "Which space" is a judgment about the meaning of the message, not a lookup against fixed keywords.
- **Welfare evaluation** — the Messenger cohort judges whether a cross-member response serves the disclosing member's welfare. No permission matrix can produce this judgment; only a read of the content and the declared covenants can.
- **Fact reconciliation** — when the fact store compresses at a compaction boundary, an LLM call reads the new facts against the existing store and produces a reconciled structured set. Dedupe happens by construction, not by string matching.
- **Friction observation** — the Friction Observer cohort reads the recent turn trace and judges whether a pattern of friction warrants surfacing a proposed improvement.
- **Disclosure gating** — when information with sensitivity metadata flows toward the agent's context, an LLM judgment filters what surfaces based on what's relevant to the current turn.

Each of these is a bounded cohort: narrow input contract, narrow output contract, does one kind of judgment.

## What counts as plumbing

Plumbing is everything that doesn't require understanding meaning. In Kernos, this is always Python:

- **Permission lookup.** Given two `member_id`s, return the permission profile between them. This is a database query, never an LLM call.
- **Space state.** Given an `active_space_id`, write it into the handler context so the next phase can read it.
- **Tool dispatch.** Given a tool name and arguments, call the corresponding Python function. The routing from name to function is a dictionary lookup.
- **Effect classification.** Given a tool name, return whether its effects are `read`, `soft_write`, or `hard_write`. Statically declared, deterministic, looked up.
- **Conversation logging.** Write a message to the per-(instance, space, member) log file on disk. Completely mechanical.
- **Fact writes.** Serializing a reconciled fact set to the store. No judgment — the judgment already happened.

None of this work enters an LLM context. It doesn't need to.

## Why this matters

Mixing the two produces predictable failure modes.

**When plumbing leaks into LLM context:** tool dispatch decisions become LLM guesses. "Should I call the calendar tool?" becomes an LLM call instead of a direct invocation of the tool the agent already chose. Token usage explodes, latency climbs, and determinism goes out the window for decisions that had no judgment component to begin with.

**When judgment gets pushed into Python:** semantic routing reduces to keyword matching. "Is this message about work?" becomes a regex. Edge cases pile up. The system gets brittle exactly where it needs to be adaptive.

The split is not a stylistic choice. It's a correctness property: the system stays fast and deterministic on the work that has no judgment component, and stays adaptive on the work where meaning matters.

## The architectural invariants

Two invariants hold across the codebase:

**The principal agent never sees cohort reasoning.** When the Router decides a turn belongs to the work space, the principal agent doesn't see the Router's prompt, reasoning, or internal decision. It sees the effect: the active space is now the work space. Judgment outputs become plumbing inputs; judgment reasoning stays inside the cohort.

**Cohorts never see each other's reasoning.** The Messenger doesn't see what the Router decided. The Router doesn't see the Friction Observer's notes. Each cohort has a deliberately minimal input contract — its `ExchangeContext` or equivalent — and sees only what it needs to make its one judgment. Cross-cohort communication is Python state, not LLM context passing.

Both invariants are enforced at the call-site architecture level, not by convention. The Python that invokes cohorts structurally cannot pass cohort outputs into other cohorts as prompt context; the cohorts have no side channels.

## Related

- **[Skill Model](skill-model-lens.md)** — how the tool catalog composes with this pattern
- **[Action Loop](action-loop.md)** — the turn shape that alternates judgment and plumbing
- **[Cohort architecture](../architecture/cohort-and-judgment.md)** — the full architectural treatment

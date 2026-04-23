# Roadmap

What's next for Kernos. Near-term specs are concrete; farther-out direction is directional. Specific dates are not promised; the ordering reflects current priorities.

## In flight

- **Messenger prompt iteration.** The Messenger cohort's welfare-judgment prompt is undergoing structured iteration against the full eval library. Each round tightens the prompt on the weakest-performing scenarios and verifies no regression on previously passing ones.
- **Documentation arc.** The architecture contribution pages, V2 direction essays, and reference docs that make Kernos a legible portfolio project.

## Near-term — V1 completion

Specs that complete V1 as a production-shaped personal agent runtime:

- **Workspace sandbox hardening.** Subprocess isolation improvements, declared-capability gating, and descriptor signing for workspace-built tools. Closes a category of potential safety gap in the agentic-workspace feature.
- **Handler pipeline decomposition.** The current turn handler is a single large module. Decomposing it into explicit six-phase pipeline modules makes the pipeline visible in code and unlocks per-phase instrumentation.
- **Event stream foundation.** Durable per-instance event stream backed by SQLite. The substrate for ambient behaviors — standing orders, external-signal adapters, event-triggered actions — all of which consume from a shared event stream rather than each spec inventing its own ingestion.
- **Situation model v1.** A per-member structured situation representation consumed by the router, awareness evaluator, and Messenger. Today these primitives each build their own situation view; v1 consolidates.
- **External signal adapters.** Adapters for the external world — calendar polling, email ingestion, weather, transit. Each adapter writes into the event stream with sensitivity-tagged, schema-consistent events.
- **Event-triggered actions.** Standing orders expressed as event-pattern-plus-action pairs. The user declares rules like "when my calendar shows a gap of more than 2 hours on a work day, nudge me about the project I said I wanted to focus on." The event stream plus the situation model make this tractable.
- **Graph memory v1.** Adding relational structure between facts. The Facts store today is a flat keyed space; v1 adds typed edges for relationships like "is-a-milestone-of", "depends-on", "supersedes". Unlocks deeper reasoning over memory.
- **Cohort registry.** Cleaning up the ad-hoc registration of cohorts into a declared catalog with startup-time validation.

## Longer horizon — V2 begins

Work that opens the V2 trajectory. See [V2 direction](v2/direction.md) for the architectural vision.

- **SPEC-COGNITION-KERNEL-V1.** The foundational V2 spec. Establishes the continuous-process substrate and World Model primitive without changing observable V1 behavior.
- **Reflection-pass v1.** Periodic World Model re-read with structured state-change emissions.
- **Projection-pass v1.** Forward modeling against current World Model with candidate-signal emissions.
- **Relevance filter v1.** The surface-or-silence decision layer. The spec that makes the pipeline able to produce any outbound signal at all.
- **Bounded autonomy v1.** Auto / confirm / decline classification for forward-modeled actions. Integrates with the V1 dispatch gate's effect classification.
- **Fused streams — calendar, email, location, artifact state.** Each stream ships as its own adapter spec with principal-configurable privacy scopes.
- **Principal modeling v1.** Structured capture of member preferences, patterns, and decision tendencies with the same supersession discipline the Facts store uses.

## Farther still

Not actively specified but on the architectural horizon:

- **Voice adapter.** Audio input/output with prosody-aware synthesis. The complexity isn't in TTS/STT — it's in the turn-shape changes voice introduces (no scrollback, different interruption semantics, different sensitivity profile for overheard speech).
- **Deliberate tool crafting.** Today the agentic workspace lets the agent write tools on demand. Deliberate tool crafting extends this into a workflow where the agent identifies recurring friction, designs a durable tool interface, tests it against synthetic cases, promotes it, refines it over use, and deprecates it when replaced. The agent as toolsmith.
- **Plan recomposition.** Currently plans are authored and stepped through. Recomposition lets the agent observe that a plan's assumptions have changed mid-execution and propose structural edits to the plan's remaining steps.
- **Multi-instance coordination.** Today each Kernos instance serves one household's worth of members. Multi-instance coordination lets instances federate for legitimate cross-household use cases (shared family calendars across households, etc.) without collapsing the per-instance isolation property.

## What we're not building

Some things are explicitly out of scope, not just deprioritized:

- **Hosted multi-tenant service.** Kernos is a self-hosted personal runtime. A hosted version would be a different product with different architectural constraints; it's not on this roadmap.
- **General-purpose ambient AI platform.** Kernos serves personal-agent use cases. Extending into general ambient computing (home automation hub, enterprise chatbot platform, etc.) would compromise the personal-agent focus.
- **Autonomous agents in the dramatic sense.** Even as V2 adds continuous cognition, the action-band discipline keeps the agent bounded. Auto-band actions are reversible and covenant-clean. There is no "turn it loose" mode on the roadmap.
- **Plugin marketplace.** Third-party capability expansion is valuable but introduces governance and safety questions that are out of scope for the current architecture. A marketplace would require structural changes to how capabilities are declared and gated.

## How this roadmap changes

This roadmap is a snapshot, not a commitment. Priorities shift based on what shipped specs reveal, what friction observations surface, and what the V2 direction requires next. The ordering within sections is considered; the timing is directional. Check the GitHub repo for active spec work.

## Related

- **[V2 direction](v2/direction.md)** — the architectural vision the longer-horizon work composes toward
- **[V1 is the alignment substrate](v2/alignment-substrate.md)** — why V1's architecture is the right foundation for V2
- **[README](../README.md)** — project overview

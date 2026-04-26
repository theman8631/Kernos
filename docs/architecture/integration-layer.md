# Integration Layer

Foundational primitive for Kernos's four-layer cognition architecture:

```
cohorts → integration → presence → (expression, future)
```

This doc covers the substrate shipped by the **INTEGRATION-LAYER-V1** spec:
the briefing schema and the integration runner. The fan-out runner, cohort
adapters, presence decoupling, and live wiring are subsequent specs in the
arc — they target this contract.

## What integration is

Integration is the runtime layer that takes everything cohorts produce
on a turn — memory retrievals, gardener observations, pattern heuristics,
covenant findings, weather, etc. — plus the conversation thread, the
surfaced tool catalog, and the active context-space metadata, and produces
a single structured **briefing** for presence.

Presence sees the briefing. Presence does not see raw cohort outputs, the
tool catalog, memory dumps, or covenant lists. Everything passed through
integration was either reflected in the briefing or filtered.

The architectural payoff: **integration is where structural safety lives**.
Restricted material (secret covenants, hidden memory, cross-space data)
shapes integration's decision but never appears quoted in the briefing.
Presence acts on behavioral instruction, not on the secret.

## Briefing schema

The briefing is a frozen, JSON-serialisable artifact:

| Field | Type | Notes |
|---|---|---|
| `relevant_context` | `tuple[ContextItem, ...]` | What integration deems relevant for this turn |
| `filtered_context` | `tuple[FilteredItem, ...]` | What it considered and dismissed (audit trail) |
| `decided_action` | tagged union (six variants) | What presence should do |
| `presence_directive` | `str` | Bounded prose; presence-safe behavioral framing |
| `audit_trace` | `AuditTrace` | References + telemetry; not raw content |
| `turn_id` | `str` | Joins the briefing to the turn |
| `integration_run_id` | `str` | Joins to the audit log |

### ContextItem

`source_type` (free-form, dotted convention), `source_id` (stable
reference), `summary` (presence-safe distillation), `confidence`
(0.0–1.0). `summary` MUST be presence-safe — never a quote of restricted
source content.

### FilteredItem

`source_type`, `source_id`, `reason_filtered`. No summary — the filtered
audit trail records what was weighed and why it was dismissed; auditors
cross-reference by `source_id`.

### DecidedAction (tagged union, exactly one)

| Variant | Fields | Presence behavior |
|---|---|---|
| `respond_only` | — | Conversational reply; no tool action |
| `execute_tool` | `tool_id`, `arguments`, `narration_context` | Execute the call; gate already cleared |
| `propose_tool` | `tool_id`, `arguments`, `reason` | Surface confirmation to user |
| `constrained_response` | `constraint`, `satisfaction_partial` | Partial satisfaction under a limit |
| `pivot` | `reason`, `suggested_shape` | Different shape than literal request |
| `defer` | `reason`, `follow_up_signal` | Acknowledge + signal delay |

Schema enforces tagged-union semantics: exactly one variant per briefing,
unknown `kind` rejected.

### AuditTrace

References + run telemetry, never raw content:

- `cohort_outputs` — list of cohort_run_id strings
- `tools_called_during_prep` — list of read-only tool invocation refs
- `iterations_used` — how many integration loop turns ran
- `budget_state` — five flags for which guardrails (if any) hit
- `fail_soft_engaged` — true when the runner returned the minimal fallback
- `phase_durations_ms` — milliseconds per named phase (acceptance #12)
- `notes` — free-form; runner records fail-soft cause here

## CohortOutput contract

Cohort adapters produce CohortOutputs to feed integration:

| Field | Type | Notes |
|---|---|---|
| `cohort_id` | `str` | "memory", "weather", "gardener", etc. |
| `cohort_run_id` | `str` | Stable reference for audit |
| `output` | `dict` | Cohort-specific shape; integration interprets |
| `visibility` | `Visibility` (tagged) | `Public()` or `Restricted(reason)` |
| `produced_at` | `str` (ISO 8601) | When the cohort emitted |

Restricted outputs may shape integration's decision but their content
must never be quoted in briefing text fields. Integration translates
them into behavioral instruction; the runner enforces this with a
post-finalize redaction-invariant check that refuses any briefing whose
text contains substrings from a Restricted cohort's payload.

## Internal phases

One runtime layer with named regions for instrumentation handles:

1. **Collect** — bundle inputs into the integration prompt
2. **Filter** — distinguish signal from noise; populate relevant + filtered
3. **Integrate** — weave context; loop with read-only tool calls if needed
4. **Decide** — pick a `decided_action` variant
5. **Brief** — emit the structured artifact (apply redaction)

Phases are not separate prompts or model calls; they're regions of the
same iterative loop, observable via `audit_trace.phase_durations_ms`.

## Iterative prep loop

The runner runs an iterative loop. Each iteration the model either:

- **calls a read-only retrieval tool** to gather more information
  (gate_classification: `read` only — `soft_write` / `hard_write` /
  `delete` rejected), OR
- **calls `__finalize_briefing__`** (synthetic tool whose schema mirrors
  the Briefing fields) to terminate the loop with the structured output.

The model's prompt explains the four-layer model, the read-only
restriction, the redaction invariant, and the depth-scaling guidance.
Cohort outputs and surfaced tools (with surfacing rationale) appear in
the user message; the system prompt is static so prompt-cache hits.

## Depth guardrails

Configurable limits per `IntegrationConfig`:

- `max_iterations` (default 5)
- `integration_timeout_seconds` (default 30.0)
- `max_summarized_cohort_entries` (default 20)
- `max_filtered_entries` (default 50)
- `max_integration_tokens` (per-call max_tokens; default 2048)

Hitting any limit flips the matching `BudgetState` flag.

## Fail-soft fallback

If integration fails, errors, or exceeds budget, the runner returns a
minimal **respond_only** briefing:

```
presence_directive: "integration prep was incomplete; respond
conservatively and acknowledge limited context if relevant."
```

Raw cohort inputs are never returned as a fallback — the briefing is
always the interface. Audit emit fires with `success: false` so downstream
auditors can see fail-soft engagement; `BudgetState` and `notes` carry
the cause.

Fail-soft triggers:

- `max_iterations` exhausted
- `integration_timeout` exceeded
- model produced no `tool_use` block
- model attempted a non-read tool (read-only violation)
- model attempted an unsurfaced tool
- briefing validation failed (schema mismatch, empty directive, etc.)
- redaction-invariant violated (briefing text quoted Restricted content)
- any unexpected exception

## Audit log

Each run emits one entry under audit category `integration.briefing`:

```
{
  "audit_category": "integration.briefing",
  "briefing": <full briefing dict>,
  "success": <bool>,
  "error": <str>
}
```

Member-scoped, ephemeral (audit-only — not durable memory), references
only (no raw cohort payload dumps). Wiring this to the existing
tool_audit substrate is part of `INTEGRATION-WIRE-LIVE`.

## Architectural placement

```
kernos/kernel/integration/
├── __init__.py        # public API
├── briefing.py        # Briefing, CohortOutput, Visibility, AuditTrace, BudgetState, …
├── runner.py          # IntegrationRunner, IntegrationInputs, IntegrationConfig, SurfacedTool
└── template.py        # system prompt + __finalize_briefing__ tool schema
```

The runner is opt-in callable (acceptance criterion #13). Nothing in the
existing reasoning loop calls it yet. The contract is parameterized over
its inputs (chain caller, read-only dispatcher, audit emitter) so the
runner is testable in isolation and wireable later without restructure.

## Spec arc following this primitive

Construction work that follows, each its own spec:

1. **COHORT-FAN-OUT-RUNNER** — per-turn parallel execution of cohort
   adapters; produces CohortOutputs matching this contract
2. **COHORT-ADAPT-GARDENER** — wrap GardenerDecision as CohortOutput
3. **COHORT-ADAPT-MEMORY** — decouple memory retrieval from being a
   model-decided tool call
4. **COHORT-ADAPT-PATTERNS** — surface pattern heuristics as a standalone
   cohort
5. **COHORT-ADAPT-COVENANT** — split covenant validation's judgment-vs-
   apply concerns; emit findings as Restricted CohortOutputs
6. **PRESENCE-DECOUPLING** — separate generation from the reasoning tool
   loop; presence consumes briefings
7. **INTEGRATION-WIRE-LIVE** — wire fan-out → integration → presence into
   the live turn pipeline; end-to-end ship

The contract this spec defines is the stable surface those specs target.

# Integration Wire-Live

Production-wiring spec that closes the gap PDI's loud-error guardrail
surfaced. Wires concrete implementations of the four Protocol-typed
service hooks (Planner / StepDispatcher / DivergenceReasoner /
PresenceRenderer), the EnactmentOutcome → ReasoningResult translation
seam, audit emission compatibility, and TurnRunner wiring into
`server.py`.

After this spec ships, `features.use_decoupled_turn_runner=True` is
safe — turns route through the new path successfully;
`TurnRunnerNotWired` no longer fires when wiring is in place.

## Composition with PDI

PDI shipped 11 structural-impossibility invariants. IWL preserves
all 11 and adds the concrete hook implementations. None of the
invariants relax.

| PDI invariant | IWL preserves via |
|---|---|
| Feature flag default OFF, legacy unchanged | reasoning.py routing unchanged when flag off |
| Thin path NEVER dispatches tools | Hook absence on thin-path code path |
| Streaming disabled inside full machinery | Protocol return types lack `streamed` field |
| `enactment.plan_created` emits BEFORE first dispatch | Order preserved in EnactmentService loop |
| Tier classification as explicit pure function | classify_routing untouched |
| Vocabulary locked at 7 SignalKinds | New synthetic tools enumerate exactly the seven |
| No same-turn integration re-entry | EnactmentService still has no integration_service param |
| Reintegration caps at construction | ReintegrationContext untouched |
| Friction observer write-only | Friction Protocol untouched |
| Envelope validation at every plan-changing tier | Tier handlers preserved |
| `enactment.*` audit family complete + references-not-dumps | Audit shape preserved |

## The four concrete hooks

### Planner (`kernos/kernel/enactment/planner.py`)

Conforms to `PlannerLike.create_plan(PlanCreationInputs) ->
PlanCreationResult`. Tool catalog filtered to
`inputs.briefing.action_envelope.allowed_operations` (Kit-corrected
access path). Defense-in-depth filters layered on top:
`allowed_tool_classes`, `forbidden_moves`.

Synthetic finalize tool `__finalize_plan__` carries the locked
7-signal vocabulary as a closed JSON-schema enum. Plan parsing
raises `PlannerError` on structural failure so EnactmentService
routes through B1.

Stamps `created_via`:
- `"initial"` when `prior_plan_id` is empty
- `"tier_4_reassemble"` when `prior_plan_id` is set

`StaticToolCatalog` ships as the v1 default; production wiring binds
the workshop registry as the catalog source. v1 same-model default —
the chain caller is the same callable integration uses by default.

### StepDispatcher (`kernos/kernel/enactment/dispatcher.py`)

Conforms to `StepDispatcherLike.dispatch(StepDispatchInputs) ->
StepDispatchResult`. The shipped six fields exactly: `completed`,
`output`, `failure_kind`, `error_summary`, `corrective_signal`,
`duration_ms`. No invented fields.

Operation resolution at dispatch time uses PDI C1's
`operation_resolver`. Per-operation timeout uses
`OperationClassification.timeout_ms` enforced via `asyncio.wait_for`.
Default fallback `DEFAULT_TOOL_TIMEOUT_MS=30s` when the operation's
timeout is 0/unset.

Tool executor is dependency-injected via the `ToolExecutor` Protocol.
Production wiring provides an executor that bridges to the workshop
dispatch primitive in ReasoningService. Single source of truth
across legacy and new path.

Trace-sink seam: dispatcher appends per-call entries in the legacy
shape (`{name, input, success, result_preview}`) into a list shared
with ReasoningService. The handler drains via
`drain_tool_trace()` after `reason()` returns.

**Drain-ordering invariant:** dispatcher only appends. Never drains.
Never clears. The handler owns the drain.

### DivergenceReasoner (`kernos/kernel/enactment/divergence_reasoner.py`)

Conforms to `DivergenceReasonerLike` four-method surface:

- `judge_divergence(inputs) -> DivergenceJudgment`
- `emit_modified_step(inputs) -> Step`
- `emit_pivot_step(inputs) -> Step`
- `formulate_clarification(inputs) -> ClarificationFormulationResult`

Per Kit edit: deterministic-vs-prose split lives INSIDE
`judge_divergence`. When structured signals all passed AND dispatch
completed, the method short-circuits without invoking the model.
Otherwise the prose path fires with explicit divergence framing.

Per-method synthetic tools (`__judge_divergence__`,
`__emit_modified_step__`, `__emit_pivot_step__`,
`__formulate_clarification__`). Modified-step and pivot-step schemas
list the locked 7-signal vocabulary in expectation structures.
Clarification schema enumerates the closed `ambiguity_type` enum
(target | parameter | approach | intent | other).

ClarificationFormulationResult caps NOT enforced in the reasoner —
PDI C1's ClarificationPartialState dataclass enforces them at
construction time. Over-cap output raises BriefingValidationError
loudly so the model can be tuned.

### PresenceRenderer (`kernos/kernel/enactment/presence_renderer.py`)

Conforms to `PresenceRendererLike.render(briefing) ->
PresenceRenderResult`. Awaited, NOT AsyncIterator (Kit edit).

Single renderer with kind-aware prompting. Branches structurally on
`briefing.decided_action.kind` to pick the system prompt:

- respond_only / defer / constrained_response / pivot
- propose_tool (renders proposal awaiting user confirmation; does
  NOT execute)
- clarification_needed first-pass
- execute_tool (full machinery terminal)

**B1 / B2 structural safety (Kit edit, load-bearing):**

Dedicated frozen dataclasses `B1RenderInputs` and `B2RenderInputs`
structurally exclude `discovered_information`. The renderer's
`render_b1` and `render_b2` entry points consume these dedicated
input types. `B2RenderInputs.from_partial_state(question,
partial_state)` factory takes a ClarificationPartialState (which
DOES carry discovered_information per PDI C1's contract) and drops
the field on the floor by construction.

The unsafe `discovered_information` lives in audit / reintegration
only, where it's referenced by `audit_refs`. Sentinel test pin
seeds discovered_information with `"RESTRICTED_SENTINEL_XYZ"` and
verifies the sentinel is absent from BOTH the renderer's prompt
input AND the rendered output text.

## Translation seam

`enactment_outcome_to_reasoning_result(...)` lives in
`kernos/kernel/response_delivery.py`. Targets the live ReasoningResult
fields ONLY:

```python
@dataclass
class ReasoningResult:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    duration_ms: int
    tool_iterations: int
```

No `tool_calls`, `assistant_content`, `stop_reason`, `provider`, or
`event_id` fields. Public ReasoningResult shape NOT widened by this
spec.

Translation aggregates tokens / cost across all hook model calls in
the turn (integration + planner + per-step divergence + presence) via
the shared `AggregatedTelemetry` accumulator. The wiring layer wraps
each hook's chain caller with `wrap_chain_caller_with_telemetry`
binding the SAME telemetry instance per turn.

`tool_iterations`: 0 for thin-path turns; equals the dispatch count
for full-machinery turns. Wiring layer increments via
`ProductionResponseDelivery.increment_tool_iteration()` after each
StepDispatcher invocation.

## Audit emission compatibility

**No-double-count invariant (Kit edit, locked):** inner hook model
calls do NOT emit `reasoning.*` events. Synthetic outer
`reasoning.request` / `reasoning.response` events emitted ONCE per
turn at the TurnRunner boundary by `ProductionResponseDelivery`.
Test pin verifies count of `reasoning.response` per new-path turn
equals exactly 1.

Synthetic event shape:
- `reasoning.request`: emitted at turn start with
  `trigger="turn_runner"` distinguishing from legacy emissions.
- `reasoning.response`: emitted at turn end with aggregated payload
  (tokens, cost, duration, model, termination_subtype,
  decided_action_kind, `turn_completed_via="decoupled"`).

Legacy `reasoning.*` event consumers (cost tracking, downstream
tools, audit replay) work unchanged on both paths because the
synthetic outer event matches legacy shape.

## `reasoning.py` minimal additive change

Per Kit edit (locked): `reasoning.py` is MINIMALLY EXTENDED, not
unchanged. The constructor accepts an optional `trace_sink: list[dict]`
parameter. When provided, the underlying `_turn_tool_trace` list IS
that injected list — shared with the new path's StepDispatcher. The
reasoning loop logic itself is unchanged.

`drain_tool_trace()` clears in-place rather than swapping for a new
list, so the shared reference stays valid across turns.

The handler's persistence seam works identically across paths: it
calls `drain_tool_trace()` once after `reason()` returns; the drain
returns entries from whichever path produced them.

## `server.py` production wiring

Construction order (acceptance criterion #23):
1. `chains` from `build_chains_from_env()` (existing).
2. Per-turn telemetry accumulator (`AggregatedTelemetry`).
3. Hook-specific chain callers wrapped with
   `wrap_chain_caller_with_telemetry`.
4. Concrete hooks: `Planner`, `StepDispatcher`,
   `DivergenceReasoner`, `PresenceRenderer`.
5. `IntegrationService` (PDI C3-shipped).
6. `EnactmentService` (PDI C4/C5/C6-shipped) wired with the four
   hooks.
7. `CohortFanOutRunner` with registered cohort adapters.
8. `ProductionResponseDelivery`.
9. `TurnRunner` (PDI C2-shipped constructor — NOT widened).
10. `ReasoningService` with `turn_runner=` and `trace_sink=`.

The cohort registration in v1 covers the covenant cohort
unconditionally (its only dependency is `StateStore`, which is
already wired in `server.py`). Gardener and memory cohort
registration is a follow-up enhancement that lands when their
service dependencies (`GardenerService` / `RetrievalService`) are
restructured out of `MessageHandler`'s lazy-init path.

## Feature flag semantics

`KERNOS_USE_DECOUPLED_TURN_RUNNER` env var (PDI C2-shipped). Default
OFF. When set AND a TurnRunner is wired, `ReasoningService.reason()`
routes to the decoupled path. With this spec shipped, flipping the
flag is safe — production turns route through the new path.

Per-turn flag check; operators can flip without restarting.

## Equivalence telemetry

`ProductionResponseDelivery` emits
`turn_completed_via="decoupled"` on the synthetic
`reasoning.response` event. Legacy path's `reasoning.response` events
do not carry this field today; equivalence-test infrastructure can
distinguish path origin by either presence/absence of the field or by
`trigger="turn_runner"`.

Per-turn duration_ms is on the same event, enabling latency overhead
measurement on representative scenarios.

## Architectural pins (load-bearing tests)

- ReasoningResult shape unchanged: exactly the seven shipped fields.
  Pin via `dataclasses.fields` introspection.
- Synthetic `reasoning.response` count per new-path turn = 1
  (no-double-count invariant).
- B1RenderInputs / B2RenderInputs do NOT carry
  `discovered_information` (structural redaction pin).
- B2 sentinel test: `RESTRICTED_SENTINEL_XYZ` absent from prompt
  input AND rendered output.
- `ProductionResponseDelivery` has no `_trace_sink` attribute and no
  `drain` / `drain_tool_trace` methods (drain-ordering invariant).
- Chain wrapper has no event-emission parameters (no-double-count).
- DivergenceJudgment / ClarificationFormulationResult have no
  `streamed` field (streaming-disabled-by-construction).

## Files

```
kernos/kernel/enactment/planner.py
kernos/kernel/enactment/dispatcher.py
kernos/kernel/enactment/divergence_reasoner.py
kernos/kernel/enactment/presence_renderer.py
kernos/kernel/response_delivery.py
kernos/kernel/reasoning.py                  (minimal additive trace_sink seam)
kernos/server.py                            (production wiring)

tests/test_enactment_planner.py
tests/test_enactment_dispatcher.py
tests/test_enactment_divergence_reasoner.py
tests/test_enactment_presence_renderer.py
tests/test_response_delivery.py
tests/test_iwl_integration.py               (end-to-end smoke)
```

## Rollout

1. **IWL ships** (this batch): hooks + translation seam +
   `server.py` wiring + integration tests. `features.use_decoupled_turn_runner=True`
   becomes safe for soak validation.
2. **Soak validation:** founder flips the flag for an instance; new
   path runs on real turns. Friction observer accumulates tickets;
   equivalence telemetry observed via the synthetic
   `reasoning.response` event's `turn_completed_via` field.
3. **PRESENCE-DECOUPLING-ACTIVATE:** flips flag default ON;
   deprecates legacy reasoning loop after benchmarked clearly and
   consistently better with no remaining tuning needs.

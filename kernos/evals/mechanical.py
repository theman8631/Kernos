"""Mechanical rubric primitives (EVAL-MECHANICAL-RUBRICS).

Pure functions against captured `ScenarioResult` data. No LLM call. Deterministic.

The eight primitives are grouped into four categories:

  Reply content:
    - reply_contains(turn, pattern)
    - reply_does_not_contain(turn, pattern)

  Observation structure:
    - observation_has(path, where)
    - observation_field_equals(path, field, value)
    - observation_absent(path) / observation_empty(path)   [same primitive, aliased]

  Trace/event:
    - trace_event_fired(event_name)

  Tool invocation:
    - tool_called(tool_name)
    - tool_not_called(tool_name)

Each returns `MechanicalVerdict(passed, reason)` — a one-line structured result the
evaluator loop turns into the final `RubricVerdict`. The reason line cites
exactly what was or wasn't found so failures are legible from the report without
re-running anything.

Governing principle: *LLMs for thinking. Python for state.* — if the check is a
function of captured data alone, it belongs here, not in an LLM call.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from kernos.evals.types import ScenarioResult


# ---------------------------------------------------------------------------
# Projector path registry (parse-time validation anchor).
#
# Lists every observation kind the runner can produce, plus the dict-field
# names mechanical rubrics may reference in `where:` / `field:`. Mechanical
# rubrics that reference an unknown kind or field fail LOUDLY at scenario
# load, not silently during evaluation. Projector refactors that rename a
# field must update this registry and the affected scenarios in the same
# batch — this is the "Observation Path Stability" seam from the spec.
# ---------------------------------------------------------------------------
PROJECTOR_SCHEMAS: dict[str, set[str] | None] = {
    "member_profile": {
        "display_name", "agent_name", "personality_notes", "timezone",
        "interaction_count", "hatched", "bootstrap_graduated",
        "relationship_to_owner", "member_id", "_missing", "looked_up",
    },
    "knowledge": {
        "id", "content", "subject", "category", "sensitivity", "archetype",
        "owner_member_id", "owner_display_name", "confidence",
    },
    "relational_messages": {
        "id", "origin", "origin_display_name", "addressee",
        "addressee_display_name", "intent", "urgency", "state",
        "conversation_id", "content", "target_space_hint", "resolution_reason",
        "reply_to_id", "created_at", "delivered_at", "surfaced_at",
        "resolved_at", "expired_at",
    },
    "relationships": {
        "declarer", "declarer_display_name", "other", "other_display_name",
        "permission",
    },
    "covenants": {"id", "rule_type", "description"},
    "outbound": {"channel", "message", "timestamp"},
    "conversation_log": None,  # list of strings, not dicts — no field schema
}


# ---------------------------------------------------------------------------
# Verdict / primitive dispatch
# ---------------------------------------------------------------------------


@dataclass
class MechanicalVerdict:
    """Result of running a mechanical primitive.

    Kept deliberately small — a passed flag and one-line reason citing the
    matched/missed data. The caller wraps this into the standard RubricVerdict.
    """
    passed: bool
    reason: str


# Set of check names recognised by the dispatcher. Keep in sync with the
# eight-function primitive set declared in the spec.
KNOWN_CHECKS = frozenset({
    "reply_contains",
    "reply_does_not_contain",
    "observation_has",
    "observation_field_equals",
    "observation_absent",
    "observation_empty",
    "trace_event_fired",
    "tool_called",
    "tool_not_called",
})


def evaluate_mechanical(
    check: str, params: dict[str, Any], result: ScenarioResult,
) -> MechanicalVerdict:
    """Dispatch a mechanical rubric to its primitive and return the verdict.

    Unknown checks fail loudly rather than falling through to semantic —
    the parser should have caught this at scenario load, so hitting it here
    is a harness bug.
    """
    if check == "reply_contains":
        return reply_contains(result, params.get("turn", "any"), params.get("pattern", ""))
    if check == "reply_does_not_contain":
        return reply_does_not_contain(result, params.get("turn", "any"), params.get("pattern", ""))
    if check == "observation_has":
        return observation_has(result, params.get("observation", ""), params.get("where", {}))
    if check == "observation_field_equals":
        return observation_field_equals(
            result,
            params.get("observation", ""),
            params.get("field", ""),
            params.get("value"),
        )
    if check in ("observation_absent", "observation_empty"):
        return observation_absent(result, params.get("observation", ""))
    if check == "trace_event_fired":
        return trace_event_fired(result, params.get("event_name", ""))
    if check == "tool_called":
        return tool_called(result, params.get("tool_name", ""))
    if check == "tool_not_called":
        return tool_not_called(result, params.get("tool_name", ""))
    return MechanicalVerdict(
        passed=False,
        reason=f"unknown mechanical check: {check!r}",
    )


# ---------------------------------------------------------------------------
# Reply content primitives
# ---------------------------------------------------------------------------


def _iter_replies(result: ScenarioResult, turn: Any) -> list[tuple[int, str]]:
    """Return (turn_index, reply_text) pairs matching the turn selector.

    `turn` is either `"any"` / `"all"` (every turn) or a 1-based integer / stringified
    integer selecting a single turn. Missing turns produce an empty list so the
    primitive can report it precisely.
    """
    turns = result.turn_results or []
    if turn in ("any", "all", "", None):
        return [(t.turn_index, t.reply or "") for t in turns]
    try:
        n = int(turn)
    except (TypeError, ValueError):
        return []
    return [(t.turn_index, t.reply or "") for t in turns if t.turn_index == n]


def reply_contains(
    result: ScenarioResult, turn: Any, pattern: str,
) -> MechanicalVerdict:
    """Regex match against the agent's reply text. Pass if any selected turn matches."""
    if not pattern:
        return MechanicalVerdict(False, "reply_contains: empty pattern")
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return MechanicalVerdict(False, f"reply_contains: invalid regex {pattern!r}: {exc}")
    replies = _iter_replies(result, turn)
    if not replies:
        return MechanicalVerdict(
            False, f"reply_contains: no replies for turn={turn!r}",
        )
    for idx, text in replies:
        m = rx.search(text)
        if m:
            return MechanicalVerdict(
                True,
                f"reply_contains: turn {idx} matched {pattern!r} at {m.group(0)!r}",
            )
    return MechanicalVerdict(
        False,
        f"reply_contains: no match for {pattern!r} across turn={turn!r}",
    )


def reply_does_not_contain(
    result: ScenarioResult, turn: Any, pattern: str,
) -> MechanicalVerdict:
    """Inverted match. Pass if NO selected turn contains the pattern."""
    if not pattern:
        return MechanicalVerdict(False, "reply_does_not_contain: empty pattern")
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        return MechanicalVerdict(
            False, f"reply_does_not_contain: invalid regex {pattern!r}: {exc}",
        )
    replies = _iter_replies(result, turn)
    if not replies:
        # No replies to examine = vacuously satisfied; cite it so the report is clear.
        return MechanicalVerdict(
            True, f"reply_does_not_contain: no replies for turn={turn!r} (vacuous pass)",
        )
    for idx, text in replies:
        m = rx.search(text)
        if m:
            return MechanicalVerdict(
                False,
                f"reply_does_not_contain: turn {idx} contains {m.group(0)!r} "
                f"(pattern {pattern!r})",
            )
    return MechanicalVerdict(
        True,
        f"reply_does_not_contain: {pattern!r} absent across turn={turn!r}",
    )


# ---------------------------------------------------------------------------
# Observation structure primitives
# ---------------------------------------------------------------------------


def _lookup_observation(result: ScenarioResult, path: str) -> tuple[bool, Any]:
    """Return (found, value) for an observation label.

    Observation labels are built as `{kind}:{arg}` (e.g., `relationships:emma`)
    or just `{kind}` when no arg. This matches how the runner stores them in
    `ScenarioResult.observations`.
    """
    if not path:
        return False, None
    if path in result.observations:
        return True, result.observations[path]
    return False, None


def observation_has(
    result: ScenarioResult, path: str, where: dict[str, Any],
) -> MechanicalVerdict:
    """Pass if the observation at `path` contains at least one entry matching `where`.

    The observation is expected to be a list of dicts (most observation kinds
    produce that shape — see PROJECTOR_SCHEMAS). Scalar observations fail with
    a clear reason.
    """
    found, value = _lookup_observation(result, path)
    if not found:
        return MechanicalVerdict(
            False, f"observation_has: no observation at {path!r}",
        )
    if not isinstance(value, list):
        return MechanicalVerdict(
            False,
            f"observation_has: observation at {path!r} is {type(value).__name__}, "
            "not a list",
        )
    where = where or {}
    for i, entry in enumerate(value):
        if not isinstance(entry, dict):
            continue
        if all(entry.get(k) == v for k, v in where.items()):
            return MechanicalVerdict(
                True, f"observation_has: {path!r} entry {i} matches {where!r}",
            )
    return MechanicalVerdict(
        False,
        f"observation_has: no entry in {path!r} matches {where!r} "
        f"(checked {len(value)} entries)",
    )


def observation_field_equals(
    result: ScenarioResult, path: str, field: str, value: Any,
) -> MechanicalVerdict:
    """Pass if the observation at `path` has `field == value`.

    For dict observations, checks the dict directly. For list observations
    (relationships, relational_messages, knowledge, outbound, …), passes if
    ANY entry has `field == value`. Earlier revisions checked only the first
    entry — Codex review (F1) flagged that a later entry could silently miss
    its match. Any-match is the intuitive reading of the primitive name and
    aligns with `observation_has`'s own semantics.
    """
    found, obs_value = _lookup_observation(result, path)
    if not found:
        return MechanicalVerdict(
            False, f"observation_field_equals: no observation at {path!r}",
        )
    # Dict observations: direct field lookup.
    if isinstance(obs_value, dict):
        actual = obs_value.get(field)
        if actual == value:
            return MechanicalVerdict(
                True,
                f"observation_field_equals: {path!r}.{field}={value!r}",
            )
        return MechanicalVerdict(
            False,
            f"observation_field_equals: {path!r}.{field}={actual!r}, "
            f"expected {value!r}",
        )
    # List observations: any entry with the field matching.
    if isinstance(obs_value, list):
        if not obs_value:
            return MechanicalVerdict(
                False,
                f"observation_field_equals: observation at {path!r} is empty list",
            )
        for i, entry in enumerate(obs_value):
            if isinstance(entry, dict) and entry.get(field) == value:
                return MechanicalVerdict(
                    True,
                    f"observation_field_equals: {path!r}[{i}].{field}={value!r}",
                )
        seen = [
            e.get(field) for e in obs_value if isinstance(e, dict)
        ]
        return MechanicalVerdict(
            False,
            f"observation_field_equals: no entry in {path!r} has "
            f"{field}={value!r}. Saw: {seen}",
        )
    return MechanicalVerdict(
        False,
        f"observation_field_equals: observation at {path!r} is "
        f"{type(obs_value).__name__}, not a dict or list",
    )


def observation_absent(result: ScenarioResult, path: str) -> MechanicalVerdict:
    """Pass if the observation is missing OR empty (list/dict/string with no content)."""
    found, value = _lookup_observation(result, path)
    if not found:
        return MechanicalVerdict(
            True, f"observation_absent: {path!r} not captured (absent)",
        )
    if value in (None, "", [], {}, ()):
        return MechanicalVerdict(
            True, f"observation_absent: {path!r} is empty ({type(value).__name__})",
        )
    if isinstance(value, dict) and value.get("_missing"):
        return MechanicalVerdict(
            True, f"observation_absent: {path!r} is _missing sentinel",
        )
    return MechanicalVerdict(
        False,
        f"observation_absent: {path!r} is populated "
        f"({type(value).__name__}, len={_safe_len(value)})",
    )


def _safe_len(value: Any) -> int | str:
    try:
        return len(value)
    except TypeError:
        return "n/a"


# ---------------------------------------------------------------------------
# Trace / event primitives
# ---------------------------------------------------------------------------


def trace_event_fired(
    result: ScenarioResult, event_name: str,
) -> MechanicalVerdict:
    """Pass if at least one captured trace event has the given name.

    Trace events are populated by the eval runner's log-capturing hook — it
    collects specific event patterns emitted during the scenario (e.g.,
    `SURFACE_LEAK_DETECTED`) into `result.trace_events`.
    """
    if not event_name:
        return MechanicalVerdict(False, "trace_event_fired: empty event_name")
    events = getattr(result, "trace_events", None) or []
    for evt in events:
        if not isinstance(evt, dict):
            continue
        if evt.get("event") == event_name or evt.get("name") == event_name:
            return MechanicalVerdict(
                True,
                f"trace_event_fired: {event_name!r} observed "
                f"(turn={evt.get('turn_index','?')})",
            )
    return MechanicalVerdict(
        False,
        f"trace_event_fired: {event_name!r} not observed "
        f"({len(events)} trace events captured)",
    )


# ---------------------------------------------------------------------------
# Tool invocation primitives
# ---------------------------------------------------------------------------


def _iter_tool_calls(result: ScenarioResult) -> list[dict[str, Any]]:
    """Flatten tool calls across all turns into a single list."""
    calls: list[dict[str, Any]] = []
    # ScenarioResult may populate tool_calls at the scenario level OR per-turn.
    scenario_level = getattr(result, "tool_calls", None) or []
    for c in scenario_level:
        if isinstance(c, dict):
            calls.append(c)
    for t in result.turn_results or []:
        for c in t.tool_calls or []:
            if isinstance(c, dict):
                enriched = dict(c)
                enriched.setdefault("turn_index", t.turn_index)
                calls.append(enriched)
    return calls


def tool_called(
    result: ScenarioResult, tool_name: str,
) -> MechanicalVerdict:
    """Pass if the named tool was invoked at least once during the scenario."""
    if not tool_name:
        return MechanicalVerdict(False, "tool_called: empty tool_name")
    calls = _iter_tool_calls(result)
    for c in calls:
        if c.get("name") == tool_name:
            return MechanicalVerdict(
                True,
                f"tool_called: {tool_name!r} invoked "
                f"(turn={c.get('turn_index', '?')})",
            )
    captured = sorted({c.get("name", "") for c in calls if c.get("name")})
    return MechanicalVerdict(
        False,
        f"tool_called: {tool_name!r} not invoked. Captured tools: {captured}",
    )


def tool_not_called(
    result: ScenarioResult, tool_name: str,
) -> MechanicalVerdict:
    """Pass if the named tool was NOT invoked during the scenario."""
    if not tool_name:
        return MechanicalVerdict(False, "tool_not_called: empty tool_name")
    calls = _iter_tool_calls(result)
    for c in calls:
        if c.get("name") == tool_name:
            return MechanicalVerdict(
                False,
                f"tool_not_called: {tool_name!r} was invoked "
                f"(turn={c.get('turn_index', '?')})",
            )
    return MechanicalVerdict(
        True, f"tool_not_called: {tool_name!r} absent",
    )


# ---------------------------------------------------------------------------
# Parse-time validation
# ---------------------------------------------------------------------------


def validate_mechanical_rubric(
    check: str, params: dict[str, Any],
) -> str:
    """Return an empty string if valid, otherwise a human-readable error.

    Called by the scenario parser at load time. Catches:
      - unknown check name
      - missing required parameter for a given check
      - `observation:` path referencing an unknown projector kind
      - `where:` / `field:` keys referencing unknown fields for that projector

    Fails loudly at parse time, not at evaluation time. This is the
    Observation Path Stability seam in action.
    """
    if check not in KNOWN_CHECKS:
        return (
            f"unknown check {check!r}. Known checks: "
            f"{sorted(KNOWN_CHECKS)}"
        )
    if check in ("reply_contains", "reply_does_not_contain"):
        if not params.get("pattern"):
            return f"{check}: missing required 'pattern'"
    if check in (
        "observation_has", "observation_field_equals",
        "observation_absent", "observation_empty",
    ):
        obs = params.get("observation", "")
        if not obs:
            return f"{check}: missing required 'observation'"
        kind = obs.split(":", 1)[0].strip()
        if kind not in PROJECTOR_SCHEMAS:
            return (
                f"{check}: observation kind {kind!r} (from path {obs!r}) "
                f"is not a known projector output. Known kinds: "
                f"{sorted(PROJECTOR_SCHEMAS)}"
            )
        schema = PROJECTOR_SCHEMAS[kind]
        if schema is not None:
            if check == "observation_has":
                where = params.get("where", {}) or {}
                bad = [k for k in where if k not in schema]
                if bad:
                    return (
                        f"{check}: where-keys {bad} not in "
                        f"{kind!r} schema {sorted(schema)}"
                    )
            if check == "observation_field_equals":
                f = params.get("field", "")
                if not f:
                    return f"{check}: missing required 'field'"
                if f not in schema:
                    return (
                        f"{check}: field {f!r} not in {kind!r} "
                        f"schema {sorted(schema)}"
                    )
    if check == "trace_event_fired":
        if not params.get("event_name"):
            return f"{check}: missing required 'event_name'"
    if check in ("tool_called", "tool_not_called"):
        if not params.get("tool_name"):
            return f"{check}: missing required 'tool_name'"
    return ""

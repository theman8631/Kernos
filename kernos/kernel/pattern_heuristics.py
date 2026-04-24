"""Pattern-declared heuristic parser + deterministic evaluator.

CANVAS-GARDENER-PATTERN-HEURISTICS. Pattern pages in the Workflow
Patterns canvas carry a fenced ``yaml`` block under their
``## Evolution heuristics`` section declaring machine-readable
versions of the prose-described heuristics. This module:

  1. Extracts the fenced block from a pattern page body.
  2. Parses declarations into ``HeuristicDecl`` dataclasses.
  3. Evaluates deterministic declarations against a canvas's state +
     the triggering event. Semantic declarations are returned as
     ``needs_llm`` decisions for the Gardener's existing consultation
     path to handle (this module does not invoke LLMs).

The declaration format is designed to accommodate prose heuristics
across the Workflow Pattern Library without invention — cross-checked
against Patterns 02 / 05 / 09 during the acceptance gate.

Discipline inherited from Batch B:
  - Only ``active`` declarations dispatch; ``disabled`` ones are
    visible in the declaration block for library-maintenance audit
    but do not fire.
  - Confidence is declared per-heuristic (``deterministic-high`` or
    ``llm-judgment``); the Gardener's existing confidence-floor +
    24h coalescer apply on top.
  - Only high-confidence matches surface as proposals.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schema (kept loose on purpose — dispatch rejects unknown trigger/check
# kinds at evaluation time rather than during parse, so pattern authors can
# add declarations without code changes. Format is versioned implicitly by
# the set of recognized trigger/check/action literals in this module.)
# ---------------------------------------------------------------------------

VALID_TRIGGERS = {
    "page-created",
    "page-changed",
    "page-state-changed",
    "page-reference-added",     # blocked on CANVAS-CROSS-PAGE-INDEX
    "periodic-daily",
    "periodic-weekly",
    "periodic-monthly",
    "periodic-per-phase-close",
    "date-relative",            # used by Pattern 05 (time-bounded-event)
}

VALID_DETERMINISTIC_CHECKS = {
    "page-count",                   # count of pages matching a glob exceeds threshold
    "page-size-lines",              # single page body exceeds line-count threshold
    "duration-since-write",         # time since a page's last_updated
    "missing-frontmatter-field",    # a field is absent / empty
    "date-relative-window",         # date math against a canvas-declared anchor
    "reference-count",              # blocked on CANVAS-CROSS-PAGE-INDEX
}

VALID_ACTION_KINDS = {
    "propose_subdivide",
    "propose_split",
    "propose_promote",
    "propose_merge",
    "propose_archive",
    "propose_transition",
    "flag",
    "whisper",
    "alarm",
    # Gardener-internal surface used by the upgrade-finding path.
    "flag_library_upgrade",
}

VALID_CONFIDENCE = {"deterministic-high", "llm-judgment"}
VALID_STATUS = {"active", "disabled"}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class HeuristicDecl:
    """One declared heuristic from a pattern page's YAML block."""
    id: str
    trigger: str
    signal: dict
    action: dict
    confidence: str
    status: str
    coalesce: dict = field(default_factory=dict)
    scope: dict = field(default_factory=dict)       # optional — narrows which events match
    #: CANVAS-GARDENER-PREFERENCE-CAPTURE: optional preference-awareness
    #: fields. When a canvas carries a matching confirmed preference, the
    #: heuristic either suppresses entirely (truthy preference value) or
    #: uses the preference value as its threshold override.
    suppressed_by_preference: str = ""
    threshold_preference: str = ""

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    @property
    def is_semantic(self) -> bool:
        return (self.signal or {}).get("type") == "semantic"


@dataclass
class HeuristicMatch:
    """Result of evaluating a declaration against a canvas event.

    ``evidence`` is structured so the Gardener's existing
    ``GardenerDecision`` can surface it without re-deriving the why.
    """
    decl_id: str
    fired: bool
    confidence: str
    rationale: str
    affected_pages: list[str] = field(default_factory=list)
    payload: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


#: Match a fenced ``yaml`` block anywhere in the page body. We take the FIRST
#: such block under the ``## Evolution heuristics`` section — additional
#: yaml code fences later in the page (e.g., frontmatter examples) don't
#: interfere because the parser short-circuits on the section header.
_EVOLUTION_SECTION_RE = re.compile(
    r"##\s+Evolution heuristics\s*\n(.*?)(?=\n##\s+|\Z)",
    re.DOTALL | re.IGNORECASE,
)
_FENCED_YAML_RE = re.compile(
    r"```ya?ml\s*\n(.*?)\n```",
    re.DOTALL,
)


def extract_declarations_block(page_body: str) -> str:
    """Return the raw YAML text from the ``## Evolution heuristics`` fence.

    Empty string when the section exists but carries no fenced block
    (pre-batch pattern) or when the section is absent entirely.
    """
    if not page_body:
        return ""
    section_match = _EVOLUTION_SECTION_RE.search(page_body)
    if not section_match:
        return ""
    fenced = _FENCED_YAML_RE.search(section_match.group(1))
    if not fenced:
        return ""
    return fenced.group(1)


def parse_heuristic_declarations(page_body: str) -> list[HeuristicDecl]:
    """Parse a pattern page's fenced YAML heuristics block.

    Defensive: malformed YAML, unknown trigger/check/action values, and
    missing required fields all produce a warning log and are skipped —
    one broken declaration never blocks the rest.
    """
    raw = extract_declarations_block(page_body)
    if not raw:
        return []
    try:
        parsed = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        logger.warning("PATTERN_HEURISTICS_YAML_PARSE_FAILED: %s", exc)
        return []
    if not isinstance(parsed, dict):
        return []
    entries = parsed.get("heuristics", [])
    if not isinstance(entries, list):
        return []

    out: list[HeuristicDecl] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        try:
            decl = _decl_from_dict(e)
        except ValueError as exc:
            logger.warning(
                "PATTERN_HEURISTICS_DECL_INVALID: id=%r reason=%s",
                e.get("id", "(no-id)"), exc,
            )
            continue
        out.append(decl)
    return out


def _decl_from_dict(d: dict) -> HeuristicDecl:
    hid = str(d.get("id") or "").strip()
    if not hid:
        raise ValueError("missing id")
    trigger = str(d.get("trigger") or "").strip()
    if trigger not in VALID_TRIGGERS:
        raise ValueError(f"unknown trigger: {trigger!r}")
    signal = d.get("signal")
    if not isinstance(signal, dict) or "type" not in signal:
        raise ValueError("signal must be an object with a type field")
    if signal["type"] == "deterministic":
        check = signal.get("check")
        if check not in VALID_DETERMINISTIC_CHECKS:
            raise ValueError(f"unknown deterministic check: {check!r}")
    elif signal["type"] == "semantic":
        if not signal.get("prompt_key"):
            raise ValueError("semantic signal requires prompt_key")
    else:
        raise ValueError(f"unknown signal.type: {signal.get('type')!r}")
    action = d.get("action")
    if not isinstance(action, dict) or "kind" not in action:
        raise ValueError("action must be an object with a kind field")
    if action["kind"] not in VALID_ACTION_KINDS:
        raise ValueError(f"unknown action kind: {action['kind']!r}")
    confidence = str(d.get("confidence") or "").strip()
    if confidence not in VALID_CONFIDENCE:
        raise ValueError(f"unknown confidence: {confidence!r}")
    status = str(d.get("status") or "").strip()
    if status not in VALID_STATUS:
        raise ValueError(f"unknown status: {status!r}")
    return HeuristicDecl(
        id=hid,
        trigger=trigger,
        signal=dict(signal),
        action=dict(action),
        confidence=confidence,
        status=status,
        coalesce=dict(d.get("coalesce") or {}),
        scope=dict(d.get("scope") or {}),
        suppressed_by_preference=str(d.get("suppressed_by_preference") or "").strip(),
        threshold_preference=str(d.get("threshold_preference") or "").strip(),
    )


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


@dataclass
class CanvasEvaluationState:
    """State the evaluator needs to answer deterministic checks.

    Populated by the Gardener before it calls ``evaluate_declaration``.
    Fields are best-effort: missing ones cause specific checks to skip
    rather than raise, so callers don't need to pre-compute everything
    up front.
    """
    canvas_id: str
    page_index: list[dict] = field(default_factory=list)    # [{path, title, type, state, last_updated}]
    page_path: str = ""
    page_body: str = ""
    page_frontmatter: dict = field(default_factory=dict)
    event_type: str = ""
    event_payload: dict = field(default_factory=dict)
    canvas_anchors: dict = field(default_factory=dict)       # e.g. {"event_date": "2026-06-01"}
    #: Confirmed member-captured preferences on the canvas
    #: (CANVAS-GARDENER-PREFERENCE-CAPTURE). Consulted by
    #: ``suppressed_by_preference`` + ``threshold_preference`` gates on
    #: active declarations. Empty dict = no overrides, default
    #: thresholds apply.
    preferences: dict = field(default_factory=dict)


def trigger_matches_event(decl: HeuristicDecl, event_type: str, event_payload: dict) -> bool:
    """True when this declaration subscribes to the given event.

    Periodic/date-relative triggers never match page-event types; the
    scheduler path invokes them separately (not shipped in this batch).
    """
    if not decl.trigger.startswith("page-"):
        return False
    if decl.trigger == "page-created":
        return event_type == "canvas.page.created"
    if decl.trigger == "page-changed":
        return event_type in ("canvas.page.changed", "canvas.page.created")
    if decl.trigger == "page-state-changed":
        if event_type != "canvas.page.state_changed":
            return False
        to_state = (decl.scope or {}).get("to_state")
        if to_state:
            return event_payload.get("new_state") == to_state
        return True
    if decl.trigger == "page-reference-added":
        # Deferred — blocked on CANVAS-CROSS-PAGE-INDEX.
        return False
    return False


def _scope_matches(decl: HeuristicDecl, page_path: str) -> bool:
    """Respect an optional ``scope.path_glob`` narrowing on page events."""
    glob = (decl.scope or {}).get("path_glob")
    if not glob:
        return True
    # Minimal glob support — directory prefix + extension. Use pathlib-style
    # matching without reaching for pathspec.
    from fnmatch import fnmatch
    return fnmatch(page_path, glob)


def evaluate_declaration(
    decl: HeuristicDecl, state: CanvasEvaluationState,
) -> HeuristicMatch | None:
    """Run a deterministic declaration against canvas state.

    Returns a HeuristicMatch when the signal fires, or None when it
    doesn't. Semantic declarations return a ``needs_llm`` marker on the
    match so the Gardener routes to its consultation path — this module
    never invokes LLMs.

    Reference-count and page-reference-added paths are blocked on a
    cross-page index that isn't built yet. They return None with a log
    so the dispatch layer can surface them as known-deferred.

    Preference-awareness (CANVAS-GARDENER-PREFERENCE-CAPTURE):
      - ``suppressed_by_preference``: if the named preference exists on
        the canvas with any truthy value, the heuristic doesn't fire.
      - ``threshold_preference``: if set AND the named preference exists,
        the preference value OVERRIDES the declaration's
        ``signal.params.threshold`` (or ``threshold_days`` for
        duration-since-write) at evaluation time.
    """
    if not decl.is_active:
        return None
    if not _scope_matches(decl, state.page_path):
        return None

    # Suppression gate: preference with truthy value skips evaluation.
    if decl.suppressed_by_preference:
        value = (state.preferences or {}).get(decl.suppressed_by_preference)
        if value:
            logger.debug(
                "PATTERN_HEURISTIC_SUPPRESSED_BY_PREF: id=%s pref=%s",
                decl.id, decl.suppressed_by_preference,
            )
            return None

    # Threshold override: preference value replaces the declared threshold.
    # Mutation applied to a COPY of the decl so we don't mutate the cached
    # declaration on the pattern body.
    if decl.threshold_preference:
        override_value = (state.preferences or {}).get(decl.threshold_preference)
        if override_value is not None:
            decl = _decl_with_threshold_override(decl, override_value)

    if decl.is_semantic:
        return HeuristicMatch(
            decl_id=decl.id,
            fired=True,
            confidence=decl.confidence,
            rationale="semantic heuristic — route to Gardener consultation",
            payload={
                "needs_llm": True,
                "prompt_key": decl.signal.get("prompt_key"),
                "inputs": decl.signal.get("inputs", {}),
                "action": decl.action,
            },
        )

    check = decl.signal.get("check")
    params = decl.signal.get("params") or {}
    handler = _DETERMINISTIC_HANDLERS.get(check)
    if handler is None:
        logger.debug("PATTERN_HEURISTICS_NO_HANDLER: check=%s", check)
        return None
    try:
        return handler(decl, state, params)
    except Exception as exc:  # noqa: BLE001 — never break canvas on a bad declaration
        logger.warning(
            "PATTERN_HEURISTICS_CHECK_FAILED: id=%s check=%s error=%s",
            decl.id, check, exc,
        )
        return None


# ---------------------------------------------------------------------------
# Deterministic check handlers
# ---------------------------------------------------------------------------


def _decl_with_threshold_override(
    decl: HeuristicDecl, override_value: Any,
) -> HeuristicDecl:
    """Return a shallow copy of ``decl`` with signal.params threshold replaced.

    Handles the two threshold keys used by the v1 deterministic handlers:
    ``threshold`` (page-count, page-size-lines) and ``threshold_days``
    (duration-since-write). Preferences override whichever the original
    declaration carried. Returns the original when neither key is present
    in params — the override is silently dropped rather than raising.
    """
    signal = dict(decl.signal or {})
    params = dict(signal.get("params") or {})
    changed = False
    if "threshold" in params:
        params["threshold"] = override_value
        changed = True
    if "threshold_days" in params:
        params["threshold_days"] = override_value
        changed = True
    if not changed:
        return decl
    signal["params"] = params
    return HeuristicDecl(
        id=decl.id,
        trigger=decl.trigger,
        signal=signal,
        action=decl.action,
        confidence=decl.confidence,
        status=decl.status,
        coalesce=decl.coalesce,
        scope=decl.scope,
        suppressed_by_preference=decl.suppressed_by_preference,
        threshold_preference=decl.threshold_preference,
    )


def _check_page_count(
    decl: HeuristicDecl, state: CanvasEvaluationState, params: dict,
) -> HeuristicMatch | None:
    """page-count: number of pages matching path_glob > threshold."""
    glob = params.get("path_glob", "")
    threshold = int(params.get("threshold", 0))
    if not glob or threshold <= 0:
        return None
    from fnmatch import fnmatch
    matching = [p for p in state.page_index if fnmatch(p.get("path", ""), glob)]
    if len(matching) <= threshold:
        return None
    return HeuristicMatch(
        decl_id=decl.id,
        fired=True,
        confidence=decl.confidence,
        rationale=(
            f"{len(matching)} pages match {glob!r}; "
            f"threshold {threshold}. Action: {decl.action.get('kind')}"
        ),
        affected_pages=[p["path"] for p in matching][:20],
        payload={"count": len(matching), "threshold": threshold, "glob": glob,
                 "action": decl.action},
    )


def _check_page_size_lines(
    decl: HeuristicDecl, state: CanvasEvaluationState, params: dict,
) -> HeuristicMatch | None:
    """page-size-lines: lines in the triggering page's body > threshold."""
    threshold = int(params.get("threshold", 0))
    if threshold <= 0 or not state.page_body:
        return None
    lines = state.page_body.count("\n")
    if lines <= threshold:
        return None
    return HeuristicMatch(
        decl_id=decl.id,
        fired=True,
        confidence=decl.confidence,
        rationale=(
            f"page {state.page_path!r} has {lines} lines; "
            f"threshold {threshold}. Action: {decl.action.get('kind')}"
        ),
        affected_pages=[state.page_path],
        payload={"lines": lines, "threshold": threshold, "action": decl.action},
    )


def _check_duration_since_write(
    decl: HeuristicDecl, state: CanvasEvaluationState, params: dict,
) -> HeuristicMatch | None:
    """duration-since-write: days since the target page's last_updated > threshold."""
    target_path = params.get("page_path") or state.page_path
    threshold_days = int(params.get("threshold_days", 0))
    if not target_path or threshold_days <= 0:
        return None
    # Resolve last_updated from the page index (or the triggering page).
    last_updated: str | None = None
    if target_path == state.page_path:
        last_updated = (state.page_frontmatter or {}).get("last_updated")
    else:
        for entry in state.page_index:
            if entry.get("path") == target_path:
                last_updated = entry.get("last_updated")
                break
    if not last_updated:
        return None
    try:
        when = datetime.fromisoformat(str(last_updated).replace("Z", "+00:00"))
    except ValueError:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - when).days
    if age_days < threshold_days:
        return None
    return HeuristicMatch(
        decl_id=decl.id,
        fired=True,
        confidence=decl.confidence,
        rationale=(
            f"{target_path} last updated {age_days} days ago; "
            f"threshold {threshold_days}. Action: {decl.action.get('kind')}"
        ),
        affected_pages=[target_path],
        payload={"age_days": age_days, "threshold_days": threshold_days,
                 "page_path": target_path, "action": decl.action},
    )


def _check_missing_frontmatter_field(
    decl: HeuristicDecl, state: CanvasEvaluationState, params: dict,
) -> HeuristicMatch | None:
    """missing-frontmatter-field: the triggering page's frontmatter lacks ``field``."""
    field_name = params.get("field", "")
    if not field_name:
        return None
    fm = state.page_frontmatter or {}
    value = fm.get(field_name)
    if value not in (None, "", [], {}):
        return None
    return HeuristicMatch(
        decl_id=decl.id,
        fired=True,
        confidence=decl.confidence,
        rationale=(
            f"page {state.page_path!r} missing frontmatter field "
            f"{field_name!r}. Action: {decl.action.get('kind')}"
        ),
        affected_pages=[state.page_path],
        payload={"missing_field": field_name, "action": decl.action},
    )


def _check_date_relative_window(
    decl: HeuristicDecl, state: CanvasEvaluationState, params: dict,
) -> HeuristicMatch | None:
    """date-relative-window: canvas anchor date is within a named window.

    Used by Pattern 05 — e.g., ``offset_days: -90`` means "fires when
    the canvas's event_date is <= 90 days away." The anchor field is
    looked up on ``state.canvas_anchors``; a canvas without the named
    anchor is simply not a match.
    """
    anchor_name = params.get("anchor", "event_date")
    offset_days = int(params.get("offset_days", 0))
    anchor_value = (state.canvas_anchors or {}).get(anchor_name)
    if not anchor_value:
        return None
    try:
        anchor_dt = datetime.fromisoformat(str(anchor_value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if anchor_dt.tzinfo is None:
        anchor_dt = anchor_dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    days_until = (anchor_dt - now).days
    # Fire when days_until is within the declared offset window.
    # offset_days < 0 means "fires at T minus N days" (classic event countdown).
    if offset_days >= 0:
        return None
    if days_until > abs(offset_days):
        return None
    return HeuristicMatch(
        decl_id=decl.id,
        fired=True,
        confidence=decl.confidence,
        rationale=(
            f"{anchor_name}={anchor_value}, {days_until} days out; "
            f"fires at T{offset_days} days. Action: {decl.action.get('kind')}"
        ),
        payload={"days_until": days_until, "anchor": anchor_name,
                 "offset_days": offset_days, "action": decl.action},
    )


def _check_reference_count(
    decl: HeuristicDecl, state: CanvasEvaluationState, params: dict,
) -> HeuristicMatch | None:
    """reference-count: deferred — blocked on CANVAS-CROSS-PAGE-INDEX.

    Declarations using this check should ship ``status: disabled``.
    This handler exists so the parser's signal.check validation passes
    for declarations present in the library; it always returns None.
    """
    return None


_DETERMINISTIC_HANDLERS = {
    "page-count": _check_page_count,
    "page-size-lines": _check_page_size_lines,
    "duration-since-write": _check_duration_since_write,
    "missing-frontmatter-field": _check_missing_frontmatter_field,
    "date-relative-window": _check_date_relative_window,
    "reference-count": _check_reference_count,
}

"""Trigger predicate AST + evaluator.

Canonical shape for trigger predicates. Three composite operators
(AND, OR, NOT) and a small set of leaf operators that match against
an Event's fields. Evaluator is deterministic — no LLM calls, no I/O.

AST shape:

  Leaf:
    {"op": "eq",                 "path": "...", "value": <any JSON>}
    {"op": "contains",           "path": "...", "value": "<str>"}
    {"op": "exists",             "path": "..."}
    {"op": "not_exists",         "path": "..."}
    {"op": "in_set",             "path": "...", "values": [<any>, ...]}
    {"op": "time_window",        "start": "<ISO>", "end": "<ISO>"}
    {"op": "actor_eq",           "value": "<member_id>"}
    {"op": "event_type_starts_with", "prefix": "<dotted>"}
    {"op": "correlation_eq",     "value": "<correlation_id>"}

  Composite:
    {"op": "AND", "operands": [<ast>, ...]}
    {"op": "OR",  "operands": [<ast>, ...]}
    {"op": "NOT", "operand":  <ast>}

Path resolution: a top-level field on the Event (event_type,
instance_id, member_id, space_id, correlation_id, timestamp,
event_id) OR a "payload.<dotted>" path that walks the event's
payload dict.
"""
from __future__ import annotations

from typing import Any

from kernos.kernel.event_stream import Event


LEAF_OPERATORS = frozenset({
    "eq", "contains", "exists", "not_exists", "in_set", "time_window",
    "actor_eq", "event_type_starts_with", "correlation_eq",
})
COMPOSITE_OPERATORS = frozenset({"AND", "OR", "NOT"})
ALL_OPERATORS = LEAF_OPERATORS | COMPOSITE_OPERATORS

_TOP_LEVEL_PATHS = {
    "event_id", "instance_id", "member_id", "space_id",
    "correlation_id", "timestamp", "event_type",
}


class PredicateError(ValueError):
    """Raised when a predicate AST is malformed."""


_NOT_FOUND = object()


def _resolve_path(event: Event, path: str) -> Any:
    """Resolve a dotted path against an Event. Returns ``_NOT_FOUND`` if
    the path doesn't resolve."""
    if not path:
        return _NOT_FOUND
    if path in _TOP_LEVEL_PATHS:
        return getattr(event, path, _NOT_FOUND)
    if path == "payload":
        return event.payload
    if path.startswith("payload."):
        cur: Any = event.payload
        for segment in path.split(".")[1:]:
            if isinstance(cur, dict) and segment in cur:
                cur = cur[segment]
            else:
                return _NOT_FOUND
        return cur
    return _NOT_FOUND


def validate(ast: Any) -> None:
    """Recursively validate an AST. Raises ``PredicateError`` on any
    malformed node — wrong type, unknown op, missing required arg.
    Use at trigger-registration time so bad predicates never reach
    the evaluator."""
    if not isinstance(ast, dict):
        raise PredicateError(f"predicate node must be dict, got {type(ast).__name__}")
    op = ast.get("op")
    if op not in ALL_OPERATORS:
        raise PredicateError(f"unknown predicate op: {op!r}")
    if op == "AND" or op == "OR":
        operands = ast.get("operands")
        if not isinstance(operands, list) or not operands:
            raise PredicateError(f"{op} requires non-empty 'operands' list")
        for child in operands:
            validate(child)
        return
    if op == "NOT":
        if "operand" not in ast:
            raise PredicateError("NOT requires 'operand'")
        validate(ast["operand"])
        return
    # Leaf operators
    if op in {"eq", "contains", "in_set", "exists", "not_exists"}:
        if not isinstance(ast.get("path"), str) or not ast["path"]:
            raise PredicateError(f"{op} requires non-empty 'path'")
    if op == "eq":
        if "value" not in ast:
            raise PredicateError("eq requires 'value'")
    elif op == "contains":
        if not isinstance(ast.get("value"), str):
            raise PredicateError("contains requires string 'value'")
    elif op == "in_set":
        if not isinstance(ast.get("values"), list):
            raise PredicateError("in_set requires list 'values'")
    elif op == "time_window":
        if not isinstance(ast.get("start"), str) or not isinstance(ast.get("end"), str):
            raise PredicateError("time_window requires string 'start' and 'end'")
    elif op == "actor_eq":
        if not isinstance(ast.get("value"), str):
            raise PredicateError("actor_eq requires string 'value'")
    elif op == "event_type_starts_with":
        if not isinstance(ast.get("prefix"), str) or not ast["prefix"]:
            raise PredicateError("event_type_starts_with requires non-empty 'prefix'")
    elif op == "correlation_eq":
        if not isinstance(ast.get("value"), str):
            raise PredicateError("correlation_eq requires string 'value'")


def evaluate(ast: dict, event: Event) -> bool:
    """Evaluate the AST against an event. Returns True if it matches.

    Caller must have validated the AST first (or accept that an
    invalid AST raises PredicateError mid-evaluation).
    """
    op = ast["op"]
    if op == "AND":
        return all(evaluate(child, event) for child in ast["operands"])
    if op == "OR":
        return any(evaluate(child, event) for child in ast["operands"])
    if op == "NOT":
        return not evaluate(ast["operand"], event)
    if op == "eq":
        return _resolve_path(event, ast["path"]) == ast["value"]
    if op == "contains":
        v = _resolve_path(event, ast["path"])
        return isinstance(v, str) and ast["value"] in v
    if op == "exists":
        return _resolve_path(event, ast["path"]) is not _NOT_FOUND
    if op == "not_exists":
        return _resolve_path(event, ast["path"]) is _NOT_FOUND
    if op == "in_set":
        v = _resolve_path(event, ast["path"])
        if v is _NOT_FOUND:
            return False
        return v in ast["values"]
    if op == "time_window":
        ts = event.timestamp
        return ast["start"] <= ts <= ast["end"]
    if op == "actor_eq":
        return event.member_id == ast["value"]
    if op == "event_type_starts_with":
        return event.event_type.startswith(ast["prefix"])
    if op == "correlation_eq":
        return event.correlation_id == ast["value"]
    raise PredicateError(f"unknown predicate op at evaluate time: {op!r}")


__all__ = [
    "ALL_OPERATORS",
    "COMPOSITE_OPERATORS",
    "LEAF_OPERATORS",
    "PredicateError",
    "evaluate",
    "validate",
]

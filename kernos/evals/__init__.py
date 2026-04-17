"""Kernos evaluation harness — run scenarios through the real pipeline and score them.

Two use cases, one mechanism:
1. Spec live tests — each spec's embedded live test becomes a scenario file.
2. Behavioral evals — canned scenarios that test ongoing behaviors and catch regressions.

See: specs/completed/SPEC-EVAL-HARNESS.md
"""
from kernos.evals.types import (
    MemberSpec, Observation, Rubric, RubricVerdict,
    Scenario, ScenarioResult, Setup, Turn, TurnResult,
)
from kernos.evals.scenario import parse_scenario, load_scenario

__all__ = [
    "MemberSpec", "Observation", "Rubric", "RubricVerdict",
    "Scenario", "ScenarioResult", "Setup", "Turn", "TurnResult",
    "parse_scenario", "load_scenario",
]

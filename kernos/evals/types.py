"""Eval harness data types.

Scenario is the input (parsed from markdown). ScenarioResult is the output
(what happened when the scenario ran). The runner produces ScenarioResult
from Scenario; the reporter renders it as markdown.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class MemberSpec:
    """A member that should exist before the scenario runs."""
    id: str                    # scenario-local identifier (e.g., "owner", "emma")
    display_name: str = ""
    role: str = "member"       # "owner" | "member"
    platform: str = "discord"
    channel_id: str = ""       # platform-specific channel (e.g., discord user id)


@dataclass
class Setup:
    """What state needs to be prepared before the scenario runs."""
    fresh_instance: bool = True
    members: list[MemberSpec] = field(default_factory=list)


@dataclass
class Turn:
    """A single turn in the scenario.

    For normal message turns: sender names a member in the setup, content is the text.
    For action turns: action is non-empty ("claim_code", "wipe_member", etc.) and
    action_args carries parameters.
    """
    sender: str                         # member id from setup, or "new_user"
    platform: str                       # discord | telegram | sms
    content: str
    action: str = ""                    # "" | "claim_code" | "wipe_member" | ...
    action_args: dict[str, Any] = field(default_factory=dict)


@dataclass
class Observation:
    """A declarative capture directive — what to pull from state for the report.

    kind determines what the runner captures:
      - "member_profile": full member profile dict for members[args['member']]
      - "knowledge": all active knowledge entries with content/sensitivity/owner
      - "covenants": all active covenants
      - "conversation_log": full log text for the member's active space
      - "outbound": any send_outbound calls recorded during the scenario
    """
    kind: str
    args: dict[str, Any] = field(default_factory=dict)
    label: str = ""                     # friendly label for the report section


@dataclass
class Rubric:
    """An LLM-judged question. verdict = pass/fail + reasoning."""
    question: str
    context: str = ""                   # optional hint about what "pass" means


@dataclass
class Scenario:
    """A parsed scenario file — the full input to a run."""
    name: str                           # from filename or "# Title"
    file_path: Path
    purpose: str = ""                   # from "## Purpose" section
    setup: Setup = field(default_factory=Setup)
    turns: list[Turn] = field(default_factory=list)
    observations: list[Observation] = field(default_factory=list)
    rubrics: list[Rubric] = field(default_factory=list)


# --- Results ---


@dataclass
class TurnResult:
    """What happened when one turn ran."""
    turn_index: int                     # 1-based
    sender_display: str                 # "Harold/discord" for the report
    content: str                        # the user message
    reply: str                          # the agent's reply (from process return)
    tool_calls: list[dict] = field(default_factory=list)  # {name, input, success}
    duration_ms: int = 0
    error: str = ""                     # non-empty if the turn crashed


@dataclass
class RubricVerdict:
    """Outcome of one rubric evaluation."""
    question: str
    passed: bool
    reasoning: str                      # LLM's explanation
    error: str = ""                     # non-empty if the evaluator itself failed


@dataclass
class ScenarioResult:
    """What the runner produced — serialized into the report."""
    scenario: Scenario
    started_at: str                     # ISO timestamp
    completed_at: str = ""
    commit_hash: str = ""
    turn_results: list[TurnResult] = field(default_factory=list)
    observations: dict[str, Any] = field(default_factory=dict)  # keyed by Observation.label
    rubric_verdicts: list[RubricVerdict] = field(default_factory=list)
    setup_summary: str = ""             # what state was prepared
    setup_error: str = ""               # non-empty if setup failed
    artifact_paths: list[str] = field(default_factory=list)  # extra files for the report

    @property
    def passed(self) -> bool:
        """Overall pass = setup succeeded, all turns ran, all rubrics passed."""
        if self.setup_error:
            return False
        if any(t.error for t in self.turn_results):
            return False
        if not self.rubric_verdicts:
            return False  # no rubrics graded == failure signal
        return all(v.passed for v in self.rubric_verdicts)

"""Generate readable markdown reports from ScenarioResult."""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from kernos.evals.types import ScenarioResult


def write_report(
    result: ScenarioResult,
    reports_dir: Path | str = "data/evals/reports",
) -> Path:
    """Render the result as markdown and write it to a timestamped file.

    Path: {reports_dir}/{slug(scenario_name)}__{timestamp}.md
    """
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)

    slug = _slug(result.scenario.name)
    ts = result.started_at.replace(":", "-").replace("+00:00", "Z")
    report_path = reports_dir / f"{slug}__{ts}.md"

    report_path.write_text(render_report(result), encoding="utf-8")
    return report_path


def render_report(result: ScenarioResult) -> str:
    """Render ScenarioResult as readable markdown."""
    s = result.scenario

    parts: list[str] = []
    parts.append(f"# Eval Report — {s.name}")
    parts.append("")

    # Header summary
    overall = "✅ PASS" if result.passed else "❌ FAIL"
    parts.append(f"**Overall:** {overall}")
    parts.append(f"**Scenario file:** `{s.file_path}`")
    parts.append(f"**Started:** {result.started_at}")
    if result.completed_at:
        parts.append(f"**Completed:** {result.completed_at}")
    if result.commit_hash:
        parts.append(f"**Commit:** `{result.commit_hash}`")
    parts.append("")

    # Rubric summary table
    if result.rubric_verdicts:
        parts.append("## Rubric Summary")
        parts.append("")
        for i, v in enumerate(result.rubric_verdicts, 1):
            mark = "✅" if v.passed else "❌"
            parts.append(f"- {mark} **R{i}.** {v.question}")
        parts.append("")

    # Setup
    parts.append("## Setup")
    parts.append("")
    if result.setup_error:
        parts.append("**SETUP FAILED**")
        parts.append("```")
        parts.append(result.setup_error)
        parts.append("```")
    else:
        parts.append("```")
        parts.append(result.setup_summary or "(default)")
        parts.append("```")
    parts.append("")

    # Purpose
    if s.purpose:
        parts.append("## Purpose")
        parts.append("")
        parts.append(s.purpose)
        parts.append("")

    # Transcript
    if result.turn_results:
        parts.append("## Transcript")
        parts.append("")
        for t in result.turn_results:
            parts.append(f"### Turn {t.turn_index} — {t.sender_display}")
            parts.append("")
            parts.append(f"**User:** {t.content}")
            parts.append("")
            if t.error:
                parts.append(f"**Error:** `{t.error}`")
            else:
                reply = t.reply or "(empty reply)"
                # Quote each line so long replies render cleanly
                for line in reply.splitlines() or [""]:
                    parts.append(f"> {line}")
            parts.append("")
            parts.append(f"*duration: {t.duration_ms}ms*")
            parts.append("")

    # Observations
    if result.observations:
        parts.append("## Observations")
        parts.append("")
        for label, value in result.observations.items():
            parts.append(f"### {label}")
            parts.append("")
            parts.append("```json")
            parts.append(_safe_json(value))
            parts.append("```")
            parts.append("")

    # Rubric verdicts with reasoning
    if result.rubric_verdicts:
        parts.append("## Rubric Verdicts")
        parts.append("")
        for i, v in enumerate(result.rubric_verdicts, 1):
            mark = "✅ PASS" if v.passed else "❌ FAIL"
            parts.append(f"### R{i} — {mark}")
            parts.append("")
            parts.append(f"**Question:** {v.question}")
            parts.append("")
            if v.error:
                parts.append(f"**Evaluator error:** `{v.error}`")
            else:
                parts.append(f"**Reasoning:** {v.reasoning}")
            parts.append("")

    # Artifacts
    if result.artifact_paths:
        parts.append("## Artifacts")
        parts.append("")
        for a in result.artifact_paths:
            parts.append(f"- `{a}`")
        parts.append("")

    return "\n".join(parts).rstrip() + "\n"


# --- Helpers ---


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", name).strip("_").lower()
    return slug or "scenario"


def _safe_json(value: Any) -> str:
    try:
        return json.dumps(value, indent=2, default=str)
    except Exception:
        return str(value)

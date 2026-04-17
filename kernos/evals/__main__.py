"""CLI entry point for the eval harness.

Usage:
  python -m kernos.evals                         # run all scenarios under evals/scenarios/
  python -m kernos.evals <path>                  # run a single scenario file
  python -m kernos.evals <dir>                   # run every *.md under <dir>

Reports are written to data/evals/reports/{slug}/{timestamp}.md.
Overall exit code is 0 if every scenario passed, 1 otherwise.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

from kernos.evals.report import write_report
from kernos.evals.runner import run_scenario
from kernos.evals.scenario import load_scenario

DEFAULT_SCENARIO_DIR = Path("evals/scenarios")
DEFAULT_REPORTS_DIR = Path("data/evals/reports")


def _collect_scenarios(target: Path) -> list[Path]:
    if target.is_file():
        return [target]
    if target.is_dir():
        return sorted(target.glob("*.md"))
    raise FileNotFoundError(f"scenario path not found: {target}")


def _current_commit() -> str:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL,
        )
        return out.decode().strip()
    except Exception:
        return ""


async def _run_many(
    paths: list[Path],
    reports_dir: Path,
    compaction_threshold: int | None,
) -> int:
    commit = _current_commit()
    all_passed = True
    for p in paths:
        print(f"\n=== running: {p} ===")
        scenario = load_scenario(p)
        result = await run_scenario(
            scenario, compaction_threshold=compaction_threshold,
        )
        result.commit_hash = commit
        report_path = write_report(result, reports_dir=reports_dir)
        status = "PASS" if result.passed else "FAIL"
        print(f"[{status}] {scenario.name} → {report_path}")
        for v in result.rubric_verdicts:
            mark = "  ok " if v.passed else "  FAIL"
            print(f"{mark} {v.question}")
        if not result.passed:
            all_passed = False
    return 0 if all_passed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m kernos.evals")
    parser.add_argument(
        "target", nargs="?", default=str(DEFAULT_SCENARIO_DIR),
        help="Scenario file or directory of scenarios (default: evals/scenarios/)",
    )
    parser.add_argument(
        "--reports-dir", default=str(DEFAULT_REPORTS_DIR),
        help="Where to write reports (default: data/evals/reports/)",
    )
    parser.add_argument(
        "--compaction-threshold", type=int, default=None,
        help="Override KERNOS_COMPACTION_THRESHOLD so compaction fires earlier.",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Enable DEBUG logging.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    target = Path(args.target)
    paths = _collect_scenarios(target)
    if not paths:
        print(f"no scenarios found at {target}")
        return 1

    reports_dir = Path(args.reports_dir)
    return asyncio.run(_run_many(paths, reports_dir, args.compaction_threshold))


if __name__ == "__main__":
    sys.exit(main())

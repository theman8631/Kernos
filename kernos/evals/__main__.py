"""CLI entry point for the eval harness.

Usage:
  python -m kernos.evals                         # run all scenarios under evals/scenarios/
  python -m kernos.evals <path>                  # run a single scenario file
  python -m kernos.evals <dir>                   # run every *.md under <dir>

Reports are written to data/evals/reports/{slug}/{timestamp}.md.
Exit code: 0 if every scenario passed, 1 otherwise.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import subprocess
import sys
import time
from pathlib import Path

from kernos.evals.report import write_report
from kernos.evals.runner import run_scenario
from kernos.evals.scenario import load_scenario

DEFAULT_SCENARIO_DIR = Path("evals/scenarios")
DEFAULT_REPORTS_DIR = Path("data/evals/reports")
DEFAULT_CONCURRENCY = 3

# Library/internal loggers that drown the console at INFO or DEBUG.
_NOISY_LOGGERS = (
    "aiosqlite", "httpcore", "httpx", "filelock", "anthropic", "openai",
    "kernos.providers.codex_provider",
    "kernos.providers.anthropic_provider",
    "kernos.messages.handler",
    "kernos.kernel.reasoning",
    "kernos.kernel.conversation_log",
    "kernos.kernel.runtime_trace",
    "kernos.kernel.files",
    "kernos.kernel.projectors",
    "kernos.kernel.compaction",
    "kernos.kernel.fact_harvest",
    "kernos.kernel.awareness",
)


def _configure_logging(verbose: bool) -> None:
    if verbose:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        return
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.ERROR)


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


def _make_progress_handler(label: str):
    """Return an on_event callback that prints compact progress to stdout."""
    def on_event(kind: str, payload: dict) -> None:
        if kind == "setup_done":
            print(f"[{label}] setup done, running turns...", flush=True)
        elif kind == "turn_done":
            ms = payload.get("duration_ms", 0)
            err = payload.get("error", "")
            idx = payload.get("index", "?")
            total = payload.get("total", "?")
            sender = payload.get("sender", "")
            suffix = f" ERROR: {err}" if err else ""
            print(
                f"[{label}] turn {idx}/{total} {sender} ({ms}ms){suffix}",
                flush=True,
            )
        elif kind == "observations_done":
            n = payload.get("count", 0)
            if n:
                print(f"[{label}] captured {n} observations", flush=True)
        elif kind == "rubrics_done":
            n = payload.get("count", 0)
            print(f"[{label}] judging {n} rubrics...", flush=True)
    return on_event


# --- In-process runner for a single scenario ---


async def _run_one_inproc(
    path: Path,
    reports_dir: Path,
    compaction_threshold: int | None,
    commit: str,
    label: str | None = None,
) -> tuple[bool, Path]:
    scenario = load_scenario(path)
    label = label or scenario.name
    t0 = time.monotonic()
    print(f"[{label}] starting ({len(scenario.turns)} turns, "
          f"{len(scenario.rubrics)} rubrics)", flush=True)
    result = await run_scenario(
        scenario,
        compaction_threshold=compaction_threshold,
        on_event=_make_progress_handler(label),
    )
    result.commit_hash = commit
    report_path = write_report(result, reports_dir=reports_dir)
    elapsed = time.monotonic() - t0
    status = "PASS" if result.passed else "FAIL"
    pass_count = sum(1 for v in result.rubric_verdicts if v.passed)
    total = len(result.rubric_verdicts)
    print(f"[{label}] {status} ({pass_count}/{total} rubrics, {elapsed:.1f}s)",
          flush=True)
    return result.passed, report_path


# --- Concurrent runner — subprocess per scenario ---


async def _run_one_subprocess(
    path: Path,
    reports_dir: Path,
    compaction_threshold: int | None,
    sem: asyncio.Semaphore,
) -> tuple[Path, bool, Path | None]:
    """Run a single scenario in its own subprocess. Returns (path, passed, report_path)."""
    async with sem:
        cmd = [
            sys.executable, "-m", "kernos.evals",
            "--single", str(path),
            "--reports-dir", str(reports_dir),
        ]
        if compaction_threshold is not None:
            cmd += ["--compaction-threshold", str(compaction_threshold)]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None
        report_path: Path | None = None
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").rstrip()
            if line:
                print(line, flush=True)
            # Child prints "REPORT_PATH: <path>" so we can collect it.
            if line.startswith("REPORT_PATH: "):
                report_path = Path(line[len("REPORT_PATH: "):].strip())
        rc = await proc.wait()
        return path, (rc == 0), report_path


async def _run_concurrent(
    paths: list[Path],
    reports_dir: Path,
    compaction_threshold: int | None,
    concurrency: int,
) -> tuple[int, list[tuple[Path, bool, Path | None]]]:
    sem = asyncio.Semaphore(max(1, concurrency))
    tasks = [
        _run_one_subprocess(p, reports_dir, compaction_threshold, sem)
        for p in paths
    ]
    results = await asyncio.gather(*tasks)
    any_fail = any(not passed for _, passed, _ in results)
    return (1 if any_fail else 0), list(results)


def _print_summary(
    elapsed_s: float,
    results: list[tuple[Path, bool, Path | None]],
) -> None:
    print()
    print("=" * 60)
    print(f"Eval suite finished in {elapsed_s:.1f}s")
    print("=" * 60)
    passed = sum(1 for _, ok, _ in results if ok)
    print(f"Scenarios: {passed}/{len(results)} passed")
    print()
    print("Reports:")
    for path, ok, report in results:
        mark = "PASS" if ok else "FAIL"
        if report is not None:
            abs_report = report.resolve()
            print(f"  [{mark}] {path.stem}")
            print(f"         file://{abs_report}")
        else:
            print(f"  [{mark}] {path.stem}  (no report)")


# --- Main ---


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m kernos.evals")
    parser.add_argument(
        "target", nargs="?", default=str(DEFAULT_SCENARIO_DIR),
        help="Scenario file or directory (default: evals/scenarios/)",
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
        "--concurrent", type=int, default=DEFAULT_CONCURRENCY,
        help=f"Parallel scenarios (default: {DEFAULT_CONCURRENCY}; set 1 for sequential).",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Enable DEBUG logging.",
    )
    parser.add_argument(
        "--single", type=str, default="",
        help=argparse.SUPPRESS,  # internal: run exactly one scenario in-process
    )
    args = parser.parse_args(argv)

    _configure_logging(args.verbose)
    commit = _current_commit()

    # --single mode: used by subprocess workers. Runs one scenario in-process
    # and prints "REPORT_PATH: <path>" so the parent can link it in summary.
    if args.single:
        path = Path(args.single)
        passed, report_path = asyncio.run(_run_one_inproc(
            path, Path(args.reports_dir), args.compaction_threshold, commit,
        ))
        print(f"REPORT_PATH: {report_path}", flush=True)
        return 0 if passed else 1

    target = Path(args.target)
    paths = _collect_scenarios(target)
    if not paths:
        print(f"no scenarios found at {target}")
        return 1

    reports_dir = Path(args.reports_dir)
    t0 = time.monotonic()

    if len(paths) == 1 or args.concurrent <= 1:
        # Sequential, in-process.
        results: list[tuple[Path, bool, Path | None]] = []
        for p in paths:
            passed, report = asyncio.run(_run_one_inproc(
                p, reports_dir, args.compaction_threshold, commit,
            ))
            results.append((p, passed, report))
        rc = 0 if all(ok for _, ok, _ in results) else 1
    else:
        rc, results = asyncio.run(_run_concurrent(
            paths, reports_dir, args.compaction_threshold, args.concurrent,
        ))

    _print_summary(time.monotonic() - t0, results)
    return rc


if __name__ == "__main__":
    sys.exit(main())

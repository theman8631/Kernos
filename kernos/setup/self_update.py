"""Startup-time self-update.

Kernos runs continuously on operator hardware and is under frequent
development. Without a self-update path, operators run stale Kernos
until they remember to SSH in and ``git pull``. This module closes that
gap: on startup, fetch ``origin/{branch}``, compare to local HEAD, pull
if behind, reinstall dependencies, and restart the process via
``os.execv``.

Graceful degradation is the whole point of the design. Every failure
mode — not a git checkout, dirty working tree, network error, diverged
history, reinstall failure — produces a log line and continues startup
with the current code. Auto-update never blocks startup and never
leaves the process in a limbo state.

Entry point: :func:`enforce_or_continue`. May ``os.execv`` and never
return; may return normally with no side effects.

Post-update whisper: when an update is applied, the commit range is
written to ``{data_dir}/.auto_update_log.md`` before ``execv``. On the
fresh process, :func:`queue_pending_whisper` — called from ``on_ready``
after state is ready — converts the file into a queued Whisper for the
owner member so the first turn after restart summarizes what changed.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


LOG_FILENAME = ".auto_update_log.md"
MARKER_FILENAME = ".auto_update_pending"

#: AUTO_UPDATE log line prefix family. Consistent so operators can grep
#: for a single token and see the full update trajectory.
_LOG_PREFIX = "AUTO_UPDATE"


def _kernos_source_dir() -> Path:
    """Return the Kernos source root (repo root).

    ``kernos/setup/self_update.py`` → repo root is two directories up.
    """
    return Path(__file__).resolve().parent.parent.parent


def _effective_branch() -> str:
    return (os.getenv("KERNOS_UPDATE_BRANCH", "") or "main").strip() or "main"


def _auto_update_enabled() -> bool:
    val = (os.getenv("KERNOS_AUTO_UPDATE", "") or "on").strip().lower()
    return val == "on"


def _run_git(
    args: list[str], *, cwd: Path, timeout: int = 60,
) -> subprocess.CompletedProcess:
    """Run a git subprocess with captured output."""
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _run_pip_install(cwd: Path, timeout: int = 300) -> subprocess.CompletedProcess:
    """Run ``pip install -e .`` for the dependency refresh step."""
    return subprocess.run(
        [sys.executable, "-m", "pip", "install", "-e", ".", "--quiet"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


@dataclass
class UpdateContext:
    """State threaded through the update sequence for logging + tests."""

    source_dir: Path
    branch: str
    enabled: bool
    #: The HEAD OID before any pull; captured so the commit-range log can
    #: render ``HEAD@{1}..HEAD`` reliably after the pull.
    pre_pull_head: str = ""


# ---------------------------------------------------------------------------
# Precondition checks
# ---------------------------------------------------------------------------


def _is_git_checkout(source_dir: Path) -> bool:
    return (source_dir / ".git").exists()


def _working_tree_clean(source_dir: Path) -> tuple[bool, str]:
    """Return (clean, status_output). Clean = no changes + no untracked files."""
    result = _run_git(["status", "--porcelain"], cwd=source_dir)
    if result.returncode != 0:
        return (False, result.stderr.strip() or "git status failed")
    return (not bool(result.stdout.strip()), result.stdout.strip())


# ---------------------------------------------------------------------------
# Sequence steps
# ---------------------------------------------------------------------------


def _fetch(source_dir: Path, branch: str) -> tuple[bool, str]:
    result = _run_git(["fetch", "origin", branch, "--quiet"], cwd=source_dir)
    if result.returncode != 0:
        return (False, result.stderr.strip() or "git fetch failed")
    return (True, "")


def _local_head(source_dir: Path) -> str:
    result = _run_git(["rev-parse", "HEAD"], cwd=source_dir)
    return result.stdout.strip() if result.returncode == 0 else ""


def _remote_head(source_dir: Path, branch: str) -> str:
    result = _run_git(
        ["rev-parse", f"origin/{branch}"], cwd=source_dir,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _is_ancestor(source_dir: Path, a: str, b: str) -> bool:
    """Return True if ``a`` is an ancestor of ``b`` (or equal).

    Used to detect "remote is strictly ahead of local" (local is ancestor
    of remote, and they're not equal).
    """
    result = _run_git(
        ["merge-base", "--is-ancestor", a, b], cwd=source_dir,
    )
    return result.returncode == 0


def _pull(source_dir: Path, branch: str) -> tuple[bool, str]:
    result = _run_git(
        ["pull", "--ff-only", "origin", branch], cwd=source_dir,
    )
    if result.returncode != 0:
        reason = (result.stderr + result.stdout).strip() or "git pull failed"
        return (False, reason)
    return (True, result.stdout.strip())


def _reinstall(source_dir: Path) -> tuple[bool, str]:
    result = _run_pip_install(source_dir)
    if result.returncode != 0:
        reason = (result.stderr + result.stdout).strip()[:500] or "pip install failed"
        return (False, reason)
    return (True, "")


def _commit_range_log(source_dir: Path, pre_pull_head: str) -> str:
    """Return the ``git log pre_pull_head..HEAD --oneline`` output."""
    if not pre_pull_head:
        return ""
    result = _run_git(
        ["log", f"{pre_pull_head}..HEAD", "--oneline"],
        cwd=source_dir,
    )
    return result.stdout.strip() if result.returncode == 0 else ""


def _write_update_log(
    data_dir: str, pre_pull_head: str, branch: str, commits: str,
) -> None:
    """Persist the commit-range summary so the fresh process can surface it."""
    data_path = Path(data_dir)
    data_path.mkdir(parents=True, exist_ok=True)
    log_path = data_path / LOG_FILENAME
    marker_path = data_path / MARKER_FILENAME
    from kernos.utils import utc_now

    lines = [
        f"# Auto-update applied at {utc_now()}",
        f"Branch: `{branch}`",
        f"Previous HEAD: `{pre_pull_head[:12]}`",
        "",
        "## Commits pulled",
        "",
        "```",
        commits or "(commit range empty)",
        "```",
    ]
    log_path.write_text("\n".join(lines), encoding="utf-8")
    marker_path.write_text(utc_now(), encoding="utf-8")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def enforce_or_continue(
    *,
    data_dir: str | None = None,
    _execv: callable | None = None,
    _argv: list[str] | None = None,
) -> None:
    """Run the startup update sequence. May not return (via ``os.execv``).

    :param data_dir: override for ``KERNOS_DATA_DIR``; tests pass tmp paths.
    :param _execv: test hook to replace ``os.execv`` with a mock.
    :param _argv: test hook to replace ``sys.argv`` with a fixed list.
    """
    source_dir = _kernos_source_dir()
    branch = _effective_branch()
    enabled = _auto_update_enabled()

    if not enabled:
        logger.info(
            "%s_DISABLED: KERNOS_AUTO_UPDATE=off — skipping update check",
            _LOG_PREFIX,
        )
        return

    if not _is_git_checkout(source_dir):
        logger.debug(
            "%s_NOT_GIT: %s is not a git checkout — skipping",
            _LOG_PREFIX, source_dir,
        )
        return

    clean, status = _working_tree_clean(source_dir)
    if not clean:
        logger.warning(
            "%s_DIRTY: working tree has uncommitted changes or untracked "
            "files — skipping update:\n%s",
            _LOG_PREFIX, status[:500],
        )
        return

    ok, reason = _fetch(source_dir, branch)
    if not ok:
        logger.warning(
            "%s_FETCH_FAILED: %s — proceeding with current code",
            _LOG_PREFIX, reason[:500],
        )
        return

    local = _local_head(source_dir)
    remote = _remote_head(source_dir, branch)
    if not local or not remote:
        logger.warning(
            "%s_REV_LOOKUP_FAILED: local=%r remote=%r — skipping update",
            _LOG_PREFIX, local, remote,
        )
        return

    if local == remote:
        logger.info(
            "%s_CURRENT: local and origin/%s both at %s — no update available",
            _LOG_PREFIX, branch, local[:12],
        )
        return

    if not _is_ancestor(source_dir, local, remote):
        logger.error(
            "%s_DIVERGED: local HEAD %s is not an ancestor of origin/%s %s "
            "— history has diverged, skipping update",
            _LOG_PREFIX, local[:12], branch, remote[:12],
        )
        return

    pre_pull_head = local
    logger.info(
        "%s_PULLING: local=%s → remote=%s on origin/%s",
        _LOG_PREFIX, local[:12], remote[:12], branch,
    )
    ok, reason = _pull(source_dir, branch)
    if not ok:
        logger.error(
            "%s_PULL_FAILED: %s — proceeding with current code",
            _LOG_PREFIX, reason[:500],
        )
        return

    logger.info("%s_REINSTALLING: pip install -e .", _LOG_PREFIX)
    ok, reason = _reinstall(source_dir)
    if not ok:
        # Loud: reinstall failure likely causes downstream breakage. We
        # still proceed to restart because the new code is already in
        # place; reinstall might succeed on the next startup after the
        # operator intervenes.
        logger.error(
            "%s_REINSTALL_FAILED: %s — continuing startup but dependency "
            "state may be inconsistent",
            _LOG_PREFIX, reason,
        )

    resolved_data_dir = data_dir or os.getenv("KERNOS_DATA_DIR", "./data")
    try:
        commits = _commit_range_log(source_dir, pre_pull_head)
        _write_update_log(resolved_data_dir, pre_pull_head, branch, commits)
    except Exception as exc:
        logger.warning(
            "%s_LOG_WRITE_FAILED: %s — update still applied",
            _LOG_PREFIX, exc,
        )

    logger.info(
        "%s_RESTARTING: execv(%s, %s)",
        _LOG_PREFIX, sys.executable, _argv or sys.argv,
    )
    execv = _execv or os.execv
    execv(sys.executable, [sys.executable, *(_argv or sys.argv)])
    # Unreachable in real execution; tests with a mock execv fall through.


# ---------------------------------------------------------------------------
# Post-restart whisper queueing
# ---------------------------------------------------------------------------


def _format_whisper_summary(log_text: str) -> str:
    """Extract a top-3-lines summary for the whisper text."""
    in_code = False
    commits: list[str] = []
    for line in log_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            continue
        if in_code and stripped:
            commits.append(stripped)
        if len(commits) >= 3:
            break
    top = "\n".join(f"- {c}" for c in commits[:3])
    if not top:
        top = "(commit range empty)"
    return (
        "I just auto-updated. Recent commits:\n"
        f"{top}\n"
        f"Full list at `{LOG_FILENAME}` in the data directory."
    )


async def queue_pending_whisper(
    *, state, instance_id: str, data_dir: str,
) -> bool:
    """If an auto-update completed on the previous startup, queue a Whisper.

    Called from ``server.on_ready`` after state + instance_db are ready
    but before the handler starts receiving turns. Returns True if a
    whisper was queued, False otherwise.

    The log file is left in place as a persistent record. Only the
    pending marker gets removed — the whisper is a one-time surface, the
    log is durable diagnostic artifact.
    """
    log_path = Path(data_dir) / LOG_FILENAME
    marker_path = Path(data_dir) / MARKER_FILENAME
    if not marker_path.exists() or not log_path.exists():
        return False

    try:
        log_text = log_path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("%s_LOG_READ_FAILED: %s", _LOG_PREFIX, exc)
        try:
            marker_path.unlink()
        except Exception:
            pass
        return False

    summary = _format_whisper_summary(log_text)
    from kernos.kernel.awareness import Whisper, generate_whisper_id
    from kernos.utils import utc_now

    whisper = Whisper(
        whisper_id=generate_whisper_id(),
        insight_text=summary,
        delivery_class="ambient",
        source_space_id="",
        target_space_id="",
        supporting_evidence=[],
        reasoning_trace=(
            "Auto-update applied on previous startup. "
            f"Commit range written to {LOG_FILENAME}."
        ),
        knowledge_entry_id="",
        foresight_signal="auto_update:applied",
        created_at=utc_now(),
        owner_member_id="",  # instance-wide; visible to whoever takes the next turn
    )
    try:
        await state.save_whisper(instance_id, whisper)
        logger.info(
            "%s_WHISPER_QUEUED: instance=%s whisper=%s",
            _LOG_PREFIX, instance_id, whisper.whisper_id,
        )
    except Exception as exc:
        logger.warning("%s_WHISPER_SAVE_FAILED: %s", _LOG_PREFIX, exc)
        return False
    finally:
        try:
            marker_path.unlink()
        except Exception:
            pass
    return True

"""GOVERNANCE-STATE-DOCUMENT-V1 — one atomically-replaced document.

Supersedes the three-artifact governance lifecycle (open item + archive file +
append-only manifest) shipped across b1db4b6..f38b31f. Nine review rounds found
nine reachable failure families there, and the reviewer's diagnosis was that the
source lifecycle raced the audit lifecycle while the lock protected only the
latter.

This does not add a tenth guard. It removes the artifacts. One document holds
both open items and retained closed history, so most of those families become
*unrepresentable* rather than *guarded*: there is no archive to validate, no
path derived from persisted state, no manifest to race, and no source lifecycle
separate from the audit lifecycle.

Design rules that are load-bearing (each traces to a specific prior failure):

* **Document-wide lock over the whole read-decide-write.** Per-signature locks
  are wrong for writers replacing one shared document. Lock acquisition failure
  **fails closed** — the race is worse than refusing to record, and there is no
  unlocked fallback.
* **Compare-and-close.** ``close(signature, expected_occurrence)`` — a
  signature-only close lets an ambiguous retry absorb a *new* recurrence, which
  is the central lost-recurrence family.
* **Occurrence identity is persisted**, never re-derived from a timestamp, and a
  recurrence always mints a new one.
* **Candidate validation preserves every prior closed entry AND every unrelated
  open entry** before the replace is allowed.
* **Shadow archive, never delete** becomes retained ``closed`` state.

Full contract: ``specs/GOVERNANCE-STATE-DOCUMENT-V1.md``; the failure families
it answers: ``docs/reference/governance-lifecycle-failure-state-enumeration.md``.
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: Bounded so migration notes cannot become an unbounded log inside state.
MAX_MIGRATION_NOTES = 50
#: Hard ceiling on the serialized document. Over-limit REFUSES the write; it
#: never truncates, because the only unbounded part is the shadow archive.
MAX_DOCUMENT_BYTES = 1_000_000
#: Warn from halfway, so the ceiling is approached visibly.
DOCUMENT_WARN_BYTES = MAX_DOCUMENT_BYTES // 2
#: Lock waits above this stall a caller on the chat path.
LOCK_WAIT_WARN_MS = 250.0


class CloseResult(str, Enum):
    """Outcome of a compare-and-close.

    A bool cannot distinguish "already done" from "refused because something
    newer is open", and the caller must not treat those alike.
    """

    CLOSED = "closed"
    ALREADY_CLOSED = "already_closed"
    OCCURRENCE_MISMATCH = "occurrence_mismatch"
    NOT_OPEN = "not_open"
    STATE_ERROR = "state_error"


@dataclass(frozen=True)
class GovernanceItem:
    """One open occurrence, as surfaced to readers."""

    occurrence: str
    signature: str
    title: str
    condition: str
    payload: tuple
    opened_iso: str
    last_seen_iso: str
    human_gated: bool = True


def state_path(data_dir: str) -> Path:
    return Path(data_dir) / "diagnostics" / "governance" / "state.json"


def _lock_path(data_dir: str) -> Path:
    return state_path(data_dir).with_name("state.json.lock")


def new_occurrence_id(signature: str, opened_iso: str, prior: str = "") -> str:
    """Identity for one OPEN occurrence.

    Mixing in ``prior`` guarantees a successor differs from its predecessor even
    when both are created within the same clock tick — a recurrence must never
    inherit the identity of the occurrence it follows.
    """
    basis = f"{signature}|{opened_iso}"
    if prior:
        basis = f"{basis}|{prior}"
    return hashlib.sha256(basis.encode()).hexdigest()[:16]


class StateError(RuntimeError):
    """Unreadable, corrupt, or unknown-version state. Always fails closed."""


@contextmanager
def _document_lock(data_dir: str):
    """Serialize the ENTIRE read-decide-write across processes.

    Fails closed. An unlocked fallback would let two writers replace the
    document from stale snapshots, each silently discarding the other's work —
    which is strictly worse than declining to record.
    """
    path = _lock_path(data_dir)
    fh = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = path.open("a+")
        try:
            import fcntl
        except ImportError as exc:  # pragma: no cover - platform guard
            raise StateError(
                "no cross-process lock available on this platform; refusing to "
                "mutate governance state unlocked") from exc
        started = time.monotonic()
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        except OSError as exc:
            raise StateError(f"could not acquire governance state lock: {exc}") from exc
        waited_ms = (time.monotonic() - started) * 1000.0
        if waited_ms >= LOCK_WAIT_WARN_MS:
            # Contention here stalls a caller on the chat path, so it must be
            # attributable rather than merely slow.
            logger.warning("GOVERNANCE_LOCK_WAIT ms=%.1f", waited_ms)
        else:
            logger.debug("GOVERNANCE_LOCK_WAIT ms=%.1f", waited_ms)
        yield
    finally:
        if fh is not None:
            try:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            try:
                fh.close()
            except Exception:
                pass


def _empty_document() -> dict:
    return {"schema_version": SCHEMA_VERSION, "migration_notes": [],
            "open": {}, "closed": []}


def read_document(data_dir: str) -> dict:
    """Load the state document, or raise ``StateError``.

    A missing document is an empty document. An unreadable, malformed, or
    unknown-version one is NOT — reporting those as empty would let a write
    silently discard committed history.
    """
    path = state_path(data_dir)
    if not path.is_file():
        return _empty_document()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StateError(f"governance state unreadable: {exc}") from exc
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise StateError(f"governance state is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise StateError("governance state is not an object")
    version = doc.get("schema_version")
    if version != SCHEMA_VERSION:
        raise StateError(
            f"unknown governance schema_version {version!r} "
            f"(this build understands {SCHEMA_VERSION})")
    for key, kind in (("open", dict), ("closed", list), ("migration_notes", list)):
        if not isinstance(doc.get(key), kind):
            raise StateError(f"governance state field {key!r} has the wrong shape")
    return doc


def _validate_candidate(previous: dict, candidate: dict, *, signature: str) -> None:
    """Refuse a write that would drop history or an unrelated open item.

    Validating only closed history would let a write silently discard another
    signature's open entry — the write path must not be able to lose work it was
    never asked to touch.
    """
    if candidate.get("schema_version") != SCHEMA_VERSION:
        raise StateError("candidate has the wrong schema_version")

    prior_closed = {c["occurrence"] for c in previous.get("closed", [])}
    new_closed = {c["occurrence"] for c in candidate.get("closed", [])}
    lost = prior_closed - new_closed
    if lost:
        raise StateError(f"candidate would drop closed history: {sorted(lost)}")

    prior_open = set(previous.get("open", {})) - {signature}
    new_open = set(candidate.get("open", {}))
    dropped = prior_open - new_open
    if dropped:
        raise StateError(
            f"candidate would drop unrelated open entries: {sorted(dropped)}")

    if len(candidate.get("migration_notes", [])) > MAX_MIGRATION_NOTES:
        raise StateError("migration_notes exceeded its bound")


def _write_document(data_dir: str, doc: dict) -> None:
    """Atomically replace the document. Durability is fsync'd, not assumed.

    Size and write latency are logged on every call so the ceiling below is
    approached visibly rather than hit as a surprise.
    """
    blob = json.dumps(doc, separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(blob) > MAX_DOCUMENT_BYTES:
        # Refuse, never truncate. `closed` is the shadow archive: dropping its
        # tail to fit would destroy exactly the history it exists to keep, and
        # would do it silently at the moment of a successful-looking close.
        raise StateError(
            f"governance document is {len(blob)} bytes, over the "
            f"{MAX_DOCUMENT_BYTES}-byte ceiling; refusing to write")
    if len(blob) > DOCUMENT_WARN_BYTES:
        logger.warning("GOVERNANCE_DOC_LARGE bytes=%d ceiling=%d",
                       len(blob), MAX_DOCUMENT_BYTES)

    started = time.monotonic()
    path = state_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".state-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.replace(path)
        # fsync the directory so the rename itself survives a crash.
        dir_fd = os.open(str(path.parent), os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
        logger.info("GOVERNANCE_WRITE bytes=%d write_ms=%.1f",
                    len(blob), (time.monotonic() - started) * 1000.0)
    except BaseException:
        try:
            tmp.unlink()
        except OSError as exc:
            if exc.errno != errno.ENOENT:
                logger.warning("GOVERNANCE_TMP_CLEANUP_FAILED path=%s", tmp)
        raise


def upsert_item(
    data_dir: str, *, signature: str, title: str, condition: str,
    payload: list, now_iso: str, human_gated: bool = True,
) -> str:
    """Open or update the occurrence for ``signature``; return its occurrence id.

    Re-detection updates payload and last-seen and PRESERVES the original
    ``opened_iso`` and occurrence — one item per condition, never a duplicate
    per scan. A signature whose previous occurrence is already closed starts a
    NEW occurrence, so a recurrence is never folded into closed history.
    """
    with _document_lock(data_dir):
        doc = read_document(data_dir)
        existing = doc["open"].get(signature)
        if existing:
            occurrence = existing["occurrence"]
            opened_iso = existing["opened_iso"]
        else:
            prior = ""
            for entry in reversed(doc["closed"]):
                if entry["signature"] == signature:
                    prior = entry["occurrence"]
                    break
            opened_iso = now_iso
            occurrence = new_occurrence_id(signature, opened_iso, prior)

        candidate = json.loads(json.dumps(doc))
        candidate["open"][signature] = {
            "occurrence": occurrence,
            "signature": signature,
            "title": title,
            "condition": condition,
            "payload": list(payload),
            "opened_iso": opened_iso,
            "last_seen_iso": now_iso,
            "human_gated": bool(human_gated),
        }
        _validate_candidate(doc, candidate, signature=signature)
        _write_document(data_dir, candidate)
        return occurrence


def close_item(
    data_dir: str, *, signature: str, expected_occurrence: str,
    now_iso: str, resolving_condition: str,
) -> CloseResult:
    """Compare-and-close: close ONLY the occurrence the caller observed.

    Precedence is part of the contract, not an implementation detail —
    ``ALREADY_CLOSED`` is checked before ``OCCURRENCE_MISMATCH`` so an ambiguous
    retry acknowledges the occurrence it meant while leaving a newer one
    untouched, and the next scan evaluates that newer one independently.
    """
    try:
        with _document_lock(data_dir):
            doc = read_document(data_dir)

            if any(c["occurrence"] == expected_occurrence for c in doc["closed"]):
                return CloseResult.ALREADY_CLOSED

            entry = doc["open"].get(signature)
            if entry is None:
                return CloseResult.NOT_OPEN
            if entry["occurrence"] != expected_occurrence:
                return CloseResult.OCCURRENCE_MISMATCH

            candidate = json.loads(json.dumps(doc))
            closing = candidate["open"].pop(signature)
            candidate["closed"].append({
                "occurrence": closing["occurrence"],
                "signature": closing["signature"],
                "title": closing["title"],
                "payload": closing["payload"],
                "opened_iso": closing["opened_iso"],
                "closed_iso": now_iso,
                "resolving_condition": resolving_condition,
                "human_gated": closing["human_gated"],
            })
            _validate_candidate(doc, candidate, signature=signature)
            _write_document(data_dir, candidate)
            return CloseResult.CLOSED
    except StateError as exc:
        logger.warning("GOVERNANCE_CLOSE_STATE_ERROR signature=%s: %s", signature, exc)
        return CloseResult.STATE_ERROR


def open_items(data_dir: str) -> list:
    """Every open occurrence. Read-only: enumeration is never surfacing."""
    try:
        doc = read_document(data_dir)
    except StateError as exc:
        logger.warning("GOVERNANCE_READ_FAILED: %s", exc)
        return []
    return [
        GovernanceItem(
            occurrence=e["occurrence"], signature=e["signature"], title=e["title"],
            condition=e.get("condition", ""), payload=tuple(e.get("payload", ())),
            opened_iso=e["opened_iso"], last_seen_iso=e["last_seen_iso"],
            human_gated=bool(e.get("human_gated", True)),
        )
        for e in doc["open"].values()
    ]


def health(data_dir: str) -> tuple:
    """``(status, detail)`` for operator surfaces. Never raises.

    ``open_items`` degrades a corrupt document to an empty list so a failed read
    can never break the chat path. On the RECOVERY surface that degradation is
    itself the defect: "(none open)" and "the queue is unreadable" must not
    render identically, or a missed whisper stays missed and looks resolved.

    ``unmigrated`` covers the same hazard for the other direction — a document
    that has never run migration while legacy artifacts still hold live items.

    Statuses: ``ok`` | ``unreadable`` | ``unmigrated``.
    """
    try:
        doc = read_document(data_dir)
    except StateError as exc:
        return "unreadable", str(exc)
    if not (doc["migration_notes"] or doc["open"] or doc["closed"]):
        fdir, rdir = _legacy_paths(data_dir)
        stale = sorted(
            [p.name for p in fdir.glob("GOVERNANCE_*.md")] if fdir.is_dir() else [])
        stale += sorted(
            [p.name for p in rdir.glob("GOVERNANCE_*.md")] if rdir.is_dir() else [])
        if stale:
            return "unmigrated", ", ".join(stale[:10])
    return "ok", ""


def closed_items(data_dir: str) -> list:
    """Retained closed history — the shadow archive. Never deleted."""
    try:
        return list(read_document(data_dir)["closed"])
    except StateError:
        return []


# ---------------------------------------------------------------------------
# Migration from the three-artifact lifecycle (b1db4b6..f38b31f)
#
# The prior design spread one occurrence across an open item (S), an archive
# (A) and a committed audit row (M). All EIGHT S/A/M combinations get a
# deterministic outcome, because two of them can otherwise lose the only
# surviving copy of a payload once state.json becomes authoritative.
#
# Two cases ABORT rather than guess, and the reason is epistemic: equality of S
# and A is consistent BOTH with "S is merely retirement-pending" and with "the
# condition was re-observed identically while close was in flight". The parent
# state does not contain evidence to prove no recurrence occurred, so equality
# is not proof. Aborting leaves legacy authoritative and loses nothing.
# ---------------------------------------------------------------------------

class MigrationOutcome(str, Enum):
    FRESH = "fresh"                  # case 1
    IMPORTED = "imported"            # cases 2/3/5/7/8-divergent
    ABORTED = "aborted"              # cases 4/6/8-identical, corrupt, unknown
    ALREADY_MIGRATED = "already"     # a valid document exists; legacy ignored


def _legacy_paths(data_dir: str) -> tuple:
    base = Path(data_dir) / "diagnostics"
    return (base / "friction", base / "friction_resolved")


def _is_recurrence(source: dict, archived: dict) -> bool:
    """Whether S is a DISTINCT occurrence from the archived one.

    Only occurrence identity can answer this, and comparing lifecycle FIELDS
    actively gets it wrong. The legacy upsert preserves the occurrence when it
    merely re-observes a live condition, so a differing payload or last-seen is
    equally explained by "the same occurrence was touched while its retirement
    was pending" — treating that as a recurrence would import a second entry
    carrying an occurrence id already recorded as closed.

    Field comparison is also unsound at the seam: S arrives through
    `friction_response` and A through `_parse_legacy_body`, and the two
    normalise `title` differently and neither recovers `condition` at all, so
    identical records compare unequal.

    An empty occurrence on either side proves nothing, so it is NOT a
    recurrence — which routes to the abort rather than to a guess.
    """
    s = str(source.get("occurrence") or "")
    a = str(archived.get("occurrence") or "")
    return bool(s and a and s != a)


def migrate(data_dir: str, *, now_iso: str) -> tuple:
    """Import legacy governance artifacts. Returns ``(outcome, notes)``.

    Idempotent and under the document lock. Once a valid ``state.json`` exists
    it is authoritative and legacy files are IGNORED — never re-imported, so a
    crash mid-migration retries safely and a later run cannot resurrect rows
    that were deliberately closed.
    """
    with _document_lock(data_dir):
        try:
            existing = read_document(data_dir)
        except StateError as exc:
            return MigrationOutcome.ABORTED, [f"existing state unreadable: {exc}"]

        if existing["open"] or existing["closed"] or existing["migration_notes"]:
            return MigrationOutcome.ALREADY_MIGRATED, []

        fdir, rdir = _legacy_paths(data_dir)
        try:
            from kernos.kernel import friction_response as fr
            sources = {i["signature"]: i for i in fr.open_governance_items(data_dir)}
        except Exception as exc:
            return MigrationOutcome.ABORTED, [f"legacy source read failed: {exc}"]

        rows, torn = _read_legacy_manifest(rdir)
        if rows is None:
            return MigrationOutcome.ABORTED, ["legacy manifest corrupt — refusing to write"]
        archives = _read_legacy_archives(rdir)

        signatures = set(sources) | {r.get("governance_signature", "") for r in rows} \
                                  | {a["signature"] for a in archives.values() if a.get("signature")}
        signatures.discard("")

        if not signatures:
            # A marker is REQUIRED even with nothing to import: an empty
            # document cannot distinguish "migrated, found nothing" from "never
            # migrated", so without it every run would re-scan legacy files and
            # could resurrect artifacts a later close had removed.
            fresh = _empty_document()
            fresh["migration_notes"] = [{
                "from_format": "none", "decision": "fresh", "occurrence": "",
                "note": f"no legacy governance artifacts at {now_iso}"}]
            _write_document(data_dir, fresh)
            return MigrationOutcome.FRESH, fresh["migration_notes"]

        doc = _empty_document()
        notes: list = []
        if torn:
            notes.append({"from_format": "manifest", "decision": "dropped_torn_tail",
                          "occurrence": "", "note": "incomplete append was never durable"})

        for sig in sorted(signatures):
            S = sources.get(sig)
            M = next((r for r in rows if r.get("governance_signature") == sig), None)
            A = next((a for a in archives.values() if a.get("signature") == sig), None)

            # case 4 / 6 — abort, no write at all
            if M is not None and A is None:
                return MigrationOutcome.ABORTED, [
                    f"{sig}: audit row without an archive cannot furnish a complete "
                    f"closed record, and equality cannot prove S is not a recurrence"]
            if M is not None and A is not None and S is not None \
                    and not _is_recurrence(S, A):
                return MigrationOutcome.ABORTED, [
                    f"{sig}: S carries the archived occurrence — that is equally "
                    f"consistent with a pending retirement AND with the condition "
                    f"being re-observed while the close ran; the parent state "
                    f"cannot distinguish them, so no field comparison may guess"]

            if M is not None and A is not None:            # cases 7 / 8-divergent
                doc["closed"].append(_closed_from(A, M))
                notes.append({"from_format": "A+M", "decision": "closed",
                              "occurrence": A["occurrence"], "note": ""})
                if S is not None:                          # case 8-divergent
                    doc["open"][sig] = _open_from(S)
                    notes.append({"from_format": "S", "decision": "open_recurrence",
                                  "occurrence": S["occurrence"],
                                  "note": "diverges from archived snapshot"})
            elif S is not None:                            # cases 2 / 5
                doc["open"][sig] = _open_from(S)
                notes.append({"from_format": "S", "decision": "open",
                              "occurrence": S["occurrence"],
                              "note": "uncommitted archive ignored" if A else ""})
            elif A is not None:                            # case 3
                if not _archive_is_complete(A):
                    return MigrationOutcome.ABORTED, [
                        f"{sig}: orphan archive is partial or unvalidatable"]
                doc["open"][sig] = _open_from(A)
                notes.append({"from_format": "A", "decision": "open_from_archive",
                              "occurrence": A["occurrence"],
                              "note": "closure never committed"})

        doc["migration_notes"] = notes[:MAX_MIGRATION_NOTES]
        _validate_candidate(_empty_document(), doc, signature="")
        _write_document(data_dir, doc)
        return MigrationOutcome.IMPORTED, notes


def _read_legacy_manifest(rdir: Path) -> tuple:
    """``(rows, torn_tail)``; ``(None, False)`` when committed history is corrupt.

    A malformed TRAILING row is a torn append — the write never completed, so
    the audit it described was never durable and dropping it is correct. A
    malformed INTERIOR row is corruption of committed history and must never be
    reinterpreted as "no audit exists".
    """
    path = rdir / "_governance_manifest.jsonl"
    if not path.is_file():
        return [], False
    try:
        lines = [ln for ln in path.read_text(errors="replace").splitlines() if ln.strip()]
    except OSError:
        return None, False
    rows: list = []
    for i, line in enumerate(lines):
        try:
            rows.append(json.loads(line))
        except ValueError:
            if i == len(lines) - 1:
                return rows, True
            return None, False
    return rows, False


def _read_legacy_archives(rdir: Path) -> dict:
    """Parse archived governance documents into field maps, keyed by filename."""
    out: dict = {}
    if not rdir.is_dir():
        return out
    for path in sorted(rdir.glob("GOVERNANCE_*.md")):
        try:
            body = path.read_text(errors="replace")
        except OSError:
            continue
        out[path.name] = _parse_legacy_body(body)
    return out


def _parse_legacy_body(body: str) -> dict:
    def field(name: str) -> str:
        want = name.lower() + ":"
        for line in body.splitlines()[:20]:
            s = line.strip()
            if s.lower().startswith(want):
                return s.split(":", 1)[1].strip()
        return ""

    lines = body.splitlines()
    return {
        "occurrence": field("Occurrence"),
        "signature": field("Signature"),
        "title": (lines[0].lstrip("# ").replace("GOVERNANCE: ", "").strip()
                  if lines else ""),
        "condition": "",
        "payload": [ln[2:].strip() for ln in lines if ln.startswith("- ")],
        "opened_iso": field("Opened"),
        "last_seen_iso": field("Last-seen"),
        "human_gated": field("Human-gated").lower() == "true",
    }


def _archive_is_complete(a: dict) -> bool:
    """Whether an orphan archive can be trusted as the sole surviving payload."""
    return bool(a.get("signature") and a.get("occurrence") and a.get("opened_iso"))


def _open_from(src: dict) -> dict:
    return {
        "occurrence": src["occurrence"], "signature": src["signature"],
        "title": src.get("title", ""), "condition": src.get("condition", ""),
        "payload": list(src.get("payload", ())),
        "opened_iso": src.get("opened_iso", ""),
        "last_seen_iso": src.get("last_seen_iso", src.get("opened_iso", "")),
        "human_gated": bool(src.get("human_gated", True)),
    }


def _closed_from(a: dict, m: dict) -> dict:
    return {
        "occurrence": a["occurrence"], "signature": a["signature"],
        "title": a.get("title", ""), "payload": list(a.get("payload", ())),
        "opened_iso": a.get("opened_iso", m.get("opened_iso", "")),
        "closed_iso": m.get("closed_iso", ""),
        "resolving_condition": m.get("resolving_condition", ""),
        "human_gated": bool(a.get("human_gated", True)),
    }

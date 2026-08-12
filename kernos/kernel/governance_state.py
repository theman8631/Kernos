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
    except (OSError, UnicodeDecodeError) as exc:
        # A decode failure is corruption like any other. Letting UnicodeDecodeError
        # escape means it is NOT a StateError, so health/open_items/close_item all
        # raise instead of degrading — every fail-closed surface bypassed at once.
        raise StateError(f"governance state unreadable: {exc}") from exc
    try:
        doc = json.loads(raw)
    except ValueError as exc:
        raise StateError(f"governance state is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise StateError("governance state is not an object")
    version = doc.get("schema_version")
    # `type(...) is int`, not `isinstance` and not bare equality: in Python
    # `True == 1` and `1.0 == 1`, so a JSON `true` or `1.0` would pass an
    # equality check and make a foreign or malformed version marker
    # authoritative instead of failing closed.
    if type(version) is not int or version != SCHEMA_VERSION:
        raise StateError(
            f"unknown governance schema_version {version!r} "
            f"(this build understands {SCHEMA_VERSION})")
    for key, kind in (("open", dict), ("closed", list), ("migration_notes", list)):
        if not isinstance(doc.get(key), kind):
            raise StateError(f"governance state field {key!r} has the wrong shape")
    _validate_entries(doc)
    return doc


#: Every field a reader dereferences without a default. `title` and `condition`
#: are here because `open_items` reads them positionally: defaulting them in the
#: validator and then requiring them at the read is how a "valid" document still
#: raised KeyError from outside the StateError path.
_OPEN_REQUIRED = ("occurrence", "signature", "opened_iso", "last_seen_iso",
                  "title", "condition")
_CLOSED_REQUIRED = ("occurrence", "signature", "opened_iso", "closed_iso",
                    "title", "resolving_condition")
def _usable(value: Any) -> bool:
    """A required string must carry content, not just satisfy a key lookup.

    Whitespace is semantically empty: `condition="   "` persists an open item
    whose recovery surface displays no condition at all, and an all-blank title
    or timestamp is no more usable than a missing one.
    """
    return isinstance(value, str) and bool(value.strip())


#: A migration note is the only evidence that migration ran. `[{}]` satisfies a
#: shape check, and its mere presence makes the next run report ALREADY_MIGRATED
#: — which suppresses a live legacy source forever. So notes are validated, not
#: merely counted.
_NOTE_REQUIRED = ("from_format", "decision", "occurrence", "note")


def _validate_entries(doc: dict) -> None:
    """Deep-validate every entry, not just the top-level container types.

    Container-only validation is not fail-closed: a document with
    ``open={"sig": {}}`` passes it, reports healthy, and then raises ``KeyError``
    from ``open_items`` — outside the ``StateError`` path every caller handles.
    That defeats both the fail-closed contract and `/dump`'s unreadable-vs-empty
    distinction, so corruption must be caught HERE, once, for every reader.
    """
    seen: set = set()
    for key, entry in doc["open"].items():
        if not isinstance(entry, dict):
            raise StateError(f"open entry {key!r} is not an object")
        for field in _OPEN_REQUIRED:
            if not _usable(entry.get(field)):
                # Usable, not merely present. An item with no condition has no
                # statement of what would resolve it, which is exactly what the
                # recovery surface exists to show.
                raise StateError(f"open entry {key!r} has no usable {field!r}")
        if entry["signature"] != key:
            raise StateError(
                f"open entry {key!r} disagrees with its signature "
                f"{entry['signature']!r}")
        _validate_common(entry, f"open entry {key!r}")
        if entry["occurrence"] in seen:
            raise StateError(f"duplicate occurrence {entry['occurrence']!r}")
        seen.add(entry["occurrence"])

    for i, entry in enumerate(doc["closed"]):
        if not isinstance(entry, dict):
            raise StateError(f"closed entry {i} is not an object")
        for field in _CLOSED_REQUIRED:
            if not _usable(entry.get(field)):
                # A closure with no resolving condition records that something
                # was closed without recording why — closed history with no
                # audit value.
                raise StateError(f"closed entry {i} has no usable {field!r}")
        _validate_common(entry, f"closed entry {i}")
        if entry["occurrence"] in seen:
            raise StateError(f"duplicate occurrence {entry['occurrence']!r}")
        seen.add(entry["occurrence"])

    if len(doc["migration_notes"]) > MAX_MIGRATION_NOTES:
        raise StateError("migration_notes exceeds its bound")
    for i, note in enumerate(doc["migration_notes"]):
        if not isinstance(note, dict):
            raise StateError(f"migration note {i} is not an object")
        for field in _NOTE_REQUIRED:
            if not isinstance(note.get(field), str):
                raise StateError(f"migration note {i} has no usable {field!r}")
        if not _usable(note["decision"]):
            raise StateError(f"migration note {i} records no decision")


def _validate_common(entry: dict, label: str) -> None:
    payload = entry.get("payload")
    if not isinstance(payload, list) or any(not isinstance(p, str) for p in payload):
        raise StateError(f"{label} has a malformed payload")
    if not isinstance(entry.get("human_gated"), bool):
        raise StateError(f"{label} has no usable human_gated marker")


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

    # The same deep validation the read path applies. A write must not be able
    # to introduce a document its own reader would reject.
    _validate_entries(candidate)


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

        # Strict readers, deliberately NOT the chat-path best-effort ones. A
        # reader that skips an unreadable file or parses a partial one into
        # empty fields turns missing evidence into "nothing to import" — which
        # then writes a document that makes the unimported artifact invisible
        # forever. Every discovered artifact parses and validates, or the whole
        # migration aborts.
        sources, err = _strict_read_sources(fdir)
        if err:
            return MigrationOutcome.ABORTED, [err]
        archives, err = _strict_read_archives(rdir)
        if err:
            return MigrationOutcome.ABORTED, [err]
        rows, torn, err = _strict_read_manifests(rdir)
        if err:
            return MigrationOutcome.ABORTED, [err]

        notes: list = []
        if torn:
            notes.append({"from_format": "manifest", "decision": "dropped_torn_tail",
                          "occurrence": "", "note": "incomplete append was never durable"})

        if not (sources or archives or rows):
            # A marker is REQUIRED even with nothing to import: an empty
            # document cannot distinguish "migrated, found nothing" from "never
            # migrated", so without it every run would re-scan legacy files and
            # could resurrect artifacts a later close had removed.
            #
            # The torn note comes FIRST and is never dropped here: a document
            # that says only "no legacy governance artifacts" when a torn append
            # was in fact discarded is a false statement about what migration
            # saw, permanently, on the one surface that records it.
            fresh = _empty_document()
            fresh["migration_notes"] = notes + [{
                "from_format": "none", "decision": "fresh", "occurrence": "",
                "note": f"nothing importable at {now_iso}"}]
            _write_document(data_dir, fresh)
            return MigrationOutcome.FRESH, fresh["migration_notes"]

        doc = _empty_document()

        # Reconcile by OCCURRENCE, never by signature. The parent format allows
        # repeated open/close cycles per signature, each with its own archive and
        # audit row; selecting "the" A and "the" M per signature imports one
        # cycle and silently discards every earlier closure the moment the
        # document becomes authoritative.
        paired, err = _pair_closures(archives, rows)
        if err:
            return MigrationOutcome.ABORTED, [err]

        closed_by_signature: dict = {}
        for A, M in paired:
            doc["closed"].append(_closed_from(A, M))
            closed_by_signature.setdefault(A["signature"], set()).add(A["occurrence"])
            notes.append({"from_format": "A+M", "decision": "closed",
                          "occurrence": A["occurrence"], "note": ""})

        committed = {A["occurrence"] for A, _ in paired}
        orphans: dict = {}
        for A in archives.values():
            if A["occurrence"] not in committed:
                orphans.setdefault(A["signature"], []).append(A)

        for sig in sorted(set(sources) | set(orphans)):
            S = sources.get(sig)
            spare = orphans.get(sig, [])
            if len(spare) > 1:
                return MigrationOutcome.ABORTED, [
                    f"{sig}: {len(spare)} uncommitted archives share this signature; "
                    f"nothing in the parent state says which one is live"]

            if S is not None:
                if S["occurrence"] in closed_by_signature.get(sig, set()):
                    # The central epistemic case. S carrying an occurrence that
                    # is already committed as closed is equally explained by "the
                    # retirement never completed" and by "the condition was
                    # re-observed while the close was in flight".
                    return MigrationOutcome.ABORTED, [
                        f"{sig}: the open item carries occurrence "
                        f"{S['occurrence']!r}, which is already committed as "
                        f"closed — equally consistent with a pending retirement "
                        f"AND with a re-observation during close; the parent "
                        f"state cannot distinguish them"]
                doc["open"][sig] = _open_from(S)
                notes.append({"from_format": "S", "decision": "open",
                              "occurrence": S["occurrence"],
                              "note": "uncommitted archive ignored" if spare else ""})
            elif spare:
                # No audit row committed, so the closure never happened and this
                # archive is the sole surviving copy of a live item.
                doc["open"][sig] = _open_from(spare[0])
                notes.append({"from_format": "A", "decision": "open_from_archive",
                              "occurrence": spare[0]["occurrence"],
                              "note": "closure never committed"})

        doc["migration_notes"] = notes[:MAX_MIGRATION_NOTES]
        try:
            _validate_candidate(_empty_document(), doc, signature="")
        except StateError as exc:
            return MigrationOutcome.ABORTED, [f"migrated document rejected: {exc}"]
        _write_document(data_dir, doc)
        return MigrationOutcome.IMPORTED, notes


def _pair_closures(archives: dict, rows: list) -> tuple:
    """Match every audit row to the archive it committed. ``(pairs, error)``.

    Every relation is proved, never assumed by list order: an audit row must
    have an archive with the SAME occurrence and the SAME signature. An
    unmatched row is a closure with no recoverable payload, and a mismatched
    pair would fabricate a closure association between unrelated records.
    """
    pairs: list = []
    for m in rows:
        occ = str(m.get("governance_txn") or "")
        sig = str(m.get("governance_signature") or "")
        A = archives.get(occ)
        if A is None:
            return [], (
                f"{sig or '<unsigned>'}: audit row for occurrence {occ!r} has no "
                f"archive; a row alone has no recoverable payload and must never "
                f"be synthesised into a closed entry")
        if A["signature"] != sig:
            return [], (
                f"audit row for occurrence {occ!r} claims signature {sig!r} but "
                f"its archive carries {A['signature']!r}; refusing to fabricate a "
                f"closure association between unrelated records")

        for field in ("opened_iso", "closed_iso", "resolving_condition"):
            if not isinstance(m.get(field), str) or not m[field]:
                return [], (
                    f"audit row for occurrence {occ!r} has no usable {field!r}; "
                    f"an incomplete row cannot furnish a complete closed record")
        payload = m.get("final_payload")
        if not isinstance(payload, list) or any(not isinstance(x, str) for x in payload):
            return [], (
                f"audit row for occurrence {occ!r} has no usable final_payload")

        # CONTENT agreement, not merely identity agreement. The parent read the
        # payload, then copied the source, then wrote the row — with no lock on
        # the source across those steps. So a re-detection landing mid-close
        # produces an archive holding the NEW payload and a row holding the
        # pre-close one. Matching occurrence and signature cannot see that: the
        # pair is internally inconsistent, and committing it would record the
        # re-detected finding as closed and leave nothing open. That is the
        # lost-recurrence race, encoded in the artifacts themselves.
        if m["opened_iso"] != A["opened_iso"]:
            return [], (
                f"occurrence {occ!r}: audit row opened {m['opened_iso']!r} but its "
                f"archive opened {A['opened_iso']!r}; the pair is inconsistent")
        if list(payload) != list(A["payload"]):
            return [], (
                f"occurrence {occ!r}: audit row committed payload {payload!r} but "
                f"its archive holds {A['payload']!r} — the archive was rewritten "
                f"between the read and the commit, so this closure cannot be "
                f"proved to describe the item it closed")
        pairs.append((A, m))
    return pairs, ""


#: Both audit-manifest eras. The older file is SHARED with friction
#: resolutions, so rows there are accepted only when they carry both governance
#: fields — a friction row must never be imported as a governance closure.
_MANIFEST_NAMES = ("_governance_manifest.jsonl", "_manifest.jsonl")


def _strict_read_manifests(rdir: Path) -> tuple:
    """``(rows, torn_tail, error)`` across both manifest eras.

    A malformed TRAILING row is a torn append **only if its terminating newline
    is also missing.** The parent always wrote each row followed by a newline,
    so a malformed final record that IS newline-terminated was fully written —
    it is corrupt committed history, not an incomplete append. Position alone
    cannot tell those apart, and getting it wrong converts corrupt closure
    evidence into a false claim that the closure never committed, which reopens
    a resolved finding as a live one.

    A malformed INTERIOR row is always corruption of committed history and must
    never be reinterpreted as "no audit exists".
    """
    rows: list = []
    torn = False
    seen_txns: set = set()
    for name in _MANIFEST_NAMES:
        path = rdir / name
        if not path.is_file():
            continue
        try:
            # STRICT. `errors="replace"` turns an invalid byte inside an
            # otherwise-valid JSON string into U+FFFD, so a corrupt audit
            # parses cleanly and its damage is persisted into the document as
            # if it were the committed text.
            raw = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as exc:
            return [], False, f"{name} is unreadable: {exc}"
        # Keep each record's OWN terminator. Testing `raw.endswith("\n")` is
        # not the same question: blank records are filtered out afterwards, so
        # `not-json\n   ` has a fully-written malformed row followed by
        # whitespace with no final newline — the file looks incomplete while
        # the last real record is complete, and the row gets dropped as torn.
        records = []
        for chunk in raw.splitlines(keepends=True):
            text = chunk.strip()
            if text:
                records.append((text, chunk.endswith(("\n", "\r"))))

        for i, (line, terminated) in enumerate(records):
            try:
                row = json.loads(line)
            except ValueError:
                if i == len(records) - 1 and not terminated:
                    torn = True
                    break
                return [], False, f"{name} row {i} is corrupt committed history"
            if not isinstance(row, dict):
                return [], False, f"{name} row {i} is not an object"
            txn, sig = row.get("governance_txn"), row.get("governance_signature")
            if not (isinstance(txn, str) and txn
                    and isinstance(sig, str) and sig):
                # In the shared file this is an ordinary friction row.
                if name == "_manifest.jsonl":
                    continue
                return [], False, (
                    f"{name} row {i} is a governance audit row without a usable "
                    f"occurrence/signature pair")
            if txn in seen_txns:
                return [], False, (
                    f"occurrence {txn!r} is committed twice across the audit "
                    f"manifests; nothing says which closure is authoritative")
            seen_txns.add(txn)
            rows.append(row)
    return rows, torn, ""


def _strict_read_archives(rdir: Path) -> tuple:
    """``(by_occurrence, error)``. Every archive parses and validates, or abort.

    Skipping an unreadable archive, or parsing a partial one into empty fields
    that later get discarded, converts missing evidence into "nothing to
    import" — and the document written on that basis makes the artifact
    invisible forever.
    """
    out: dict = {}
    if not rdir.is_dir():
        return out, ""
    for path in sorted(rdir.glob("GOVERNANCE_*.md")):
        try:
            body = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as exc:
            return {}, f"archive {path.name} is unreadable: {exc}"
        rec, err = _validated_legacy_record(body, path.name)
        if err:
            return {}, err
        if rec["occurrence"] in out:
            return {}, (
                f"archives {out[rec['occurrence']]['_file']} and {path.name} "
                f"share occurrence {rec['occurrence']!r}")
        rec["_file"] = path.name
        out[rec["occurrence"]] = rec
    return out, ""


def _strict_read_sources(fdir: Path) -> tuple:
    """``(by_signature, error)`` for open legacy items. Same strictness.

    The parent's own reader was best-effort by design, because it sat on the
    chat path and a failed read must never break a conversation. Migration
    inherits the opposite obligation: an unreadable item is missing evidence,
    not an absent one, so it aborts instead of quietly dropping it.
    """
    out: dict = {}
    if not fdir.is_dir():
        return out, ""
    for path in sorted(fdir.glob("GOVERNANCE_*.md")):
        try:
            body = path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError) as exc:
            return {}, f"open item {path.name} is unreadable: {exc}"
        rec, err = _validated_legacy_record(body, path.name)
        if err:
            return {}, err
        if rec["signature"] in out:
            return {}, f"two open items share signature {rec['signature']!r}"
        rec["_file"] = path.name
        out[rec["signature"]] = rec
    return out, ""


def _validated_legacy_record(body: str, name: str) -> tuple:
    """Parse one legacy document and prove it COMPLETE. ``(record, error)``.

    "Has a signature and an opened stamp" is not completeness. A truncated
    document that happens to retain those two imports with an empty payload, an
    empty condition, and — worse — ``human_gated=False``, because the absent
    marker parses as not-true. That makes a partial artifact authoritative AND
    quietly drops the human gate, so every field the parent always writes is
    required here: the class line, the title, the gate marker, both timestamps,
    and both section headings.

    Occurrence ids were not always persisted. For an otherwise complete pre-id
    record the id is DERIVED with the parent's own derivation —
    ``sha256(signature|opened_iso)[:16]``. An empty occurrence is never
    committed: compare-and-close and the closed relation are both keyed on it.
    """
    rec = _parse_legacy_body(body)

    if rec["class"] != "governance":
        return {}, (
            f"{name} declares class {rec['class']!r}, not 'governance'; only a "
            f"governance document may become a governance item")
    for field in ("signature", "opened_iso", "last_seen_iso", "title"):
        if not _usable(rec.get(field)):
            return {}, (
                f"{name} has no {field}; a partial legacy document cannot be "
                f"proved to be a complete record and must not be imported")
    if any(not _usable(p) for p in rec["payload"]):
        return {}, f"{name} has a blank entry in its '## Payload' section"
    if rec["human_gated"] is not True:
        # Absent parses the same as "false", and silently importing that would
        # strip the gate from a finding whose whole point is the gate.
        return {}, (
            f"{name} does not carry an affirmative 'Human-gated: true' marker; "
            f"refusing to import a governance item with no provable gate")
    for section in ("condition", "payload"):
        if not rec[f"_has_{section}_section"]:
            return {}, (
                f"{name} has no '## {section.title()}' section; the parent "
                f"always writes both, so this document is truncated")
    if not rec["payload"]:
        # A truncation can land just after the heading, leaving the section
        # present and empty. For the one producer the parent had, an empty
        # payload means the condition CLEARED — which contradicts the item
        # being open at all, so this is a torn document, not a real state.
        return {}, (
            f"{name} has an empty '## Payload' section; an open governance item "
            f"with nothing in it is a truncated document, not a live finding")
    if not rec["condition"]:
        return {}, f"{name} has an empty '## Condition' section"

    if not rec["occurrence"]:
        rec["occurrence"] = new_occurrence_id(rec["signature"], rec["opened_iso"])
        rec["_derived_occurrence"] = True
    return rec, ""


def _parse_legacy_body(body: str) -> dict:
    """Parse the parent's governance document format, by SECTION.

    The previous version collected every ``- `` line in the file as payload and
    hard-coded ``condition`` to empty, so a complete parent source always lost
    its condition on import and any bulleted prose elsewhere in the document
    would have been read as payload.
    """
    lines = body.splitlines()

    def field(name: str) -> str:
        want = name.lower() + ":"
        for line in lines[:20]:
            s = line.strip()
            if s.lower().startswith(want):
                return s.split(":", 1)[1].strip()
        return ""

    sections: dict = {}
    current = None
    for line in lines:
        if line.startswith("## "):
            current = line[3:].strip().lower()
            sections[current] = []
        elif current is not None:
            sections[current].append(line)

    condition_lines = sections.get("condition", [])
    payload_lines = sections.get("payload", [])
    gate = field("Human-gated").lower()

    return {
        "class": field("Class").lower(),
        "occurrence": field("Occurrence"),
        "signature": field("Signature"),
        "title": (lines[0].lstrip("# ").replace("GOVERNANCE: ", "").strip()
                  if lines else ""),
        "condition": "\n".join(condition_lines).strip(),
        "payload": [ln[2:].strip() for ln in payload_lines if ln.startswith("- ")],
        "opened_iso": field("Opened"),
        "last_seen_iso": field("Last-seen"),
        "human_gated": True if gate == "true" else (False if gate == "false" else None),
        "_has_condition_section": "condition" in sections,
        "_has_payload_section": "payload" in sections,
    }



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

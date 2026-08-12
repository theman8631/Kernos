"""GOVERNANCE-STATE-DOCUMENT-V1 — the single atomically-replaced document.

The prior three-artifact design (open item + archive + append-only manifest)
took nine review rounds and still had reachable loss paths, because one logical
occurrence was spread across artifacts that no transaction spanned. These
assert the properties that replaced it, and — for migration — that every
direct-parent state has ONE expected outcome rather than "either".
"""
from __future__ import annotations

import json

import pytest

from kernos.kernel import friction_response as fr
from kernos.kernel import governance_state as gs

SIG = "self-review:coverage-gap"


def _open(d, payload, now="2026-08-01T00:00:00+00:00"):
    return gs.upsert_item(d, signature=SIG, title="Coverage gap",
                          condition="modules unowned", payload=payload,
                          now_iso=now)


# --- compare-and-close ------------------------------------------------------

def test_ambiguous_retry_acknowledges_A_and_leaves_recurrence_B(tmp_path):
    """The central lost-recurrence family, constructed.

    close A -> replace succeeds -> acknowledgement fails -> upsert opens
    recurrence B -> retry close(expected=A). A signature-only close would shut
    B; compare-and-close must not.
    """
    d = str(tmp_path)
    a = _open(d, ["a.py"])
    assert gs.close_item(d, signature=SIG, expected_occurrence=a,
                         now_iso="2026-08-02T00:00:00+00:00",
                         resolving_condition="cleared") is gs.CloseResult.CLOSED

    b = _open(d, ["b.py"], "2026-08-03T00:00:00+00:00")
    assert b != a, "a recurrence must mint a new occurrence"

    assert gs.close_item(d, signature=SIG, expected_occurrence=a,
                         now_iso="2026-08-04T00:00:00+00:00",
                         resolving_condition="cleared") \
        is gs.CloseResult.ALREADY_CLOSED
    still = gs.open_items(d)
    assert [i.occurrence for i in still] == [b], "B must remain open"
    assert len(gs.closed_items(d)) == 1, "exactly one closure"


def test_close_result_precedence(tmp_path):
    """ALREADY_CLOSED is checked before OCCURRENCE_MISMATCH — both can be true
    after the schedule above, and the order is part of the contract."""
    d = str(tmp_path)
    a = _open(d, ["a.py"])
    gs.close_item(d, signature=SIG, expected_occurrence=a,
                  now_iso="2026-08-02T00:00:00+00:00", resolving_condition="x")
    b = _open(d, ["b.py"], "2026-08-03T00:00:00+00:00")

    # expected=A: already closed, even though B is open (rule 2 before rule 4)
    assert gs.close_item(d, signature=SIG, expected_occurrence=a,
                         now_iso="2026-08-04T00:00:00+00:00",
                         resolving_condition="x") is gs.CloseResult.ALREADY_CLOSED
    # a never-seen occurrence with B open: mismatch, not "not open"
    assert gs.close_item(d, signature=SIG, expected_occurrence="deadbeefdeadbeef",
                         now_iso="2026-08-04T00:00:00+00:00",
                         resolving_condition="x") \
        is gs.CloseResult.OCCURRENCE_MISMATCH
    assert gs.close_item(d, signature="other:signature",
                         expected_occurrence="zz", now_iso="2026-08-04T00:00:00+00:00",
                         resolving_condition="x") is gs.CloseResult.NOT_OPEN
    assert [i.occurrence for i in gs.open_items(d)] == [b], "nothing touched B"


def test_upsert_preserves_occurrence_and_opened_stamp(tmp_path):
    d = str(tmp_path)
    a = _open(d, ["a.py", "b.py"], "2026-08-01T00:00:00+00:00")
    again = _open(d, ["a.py"], "2026-08-05T00:00:00+00:00")   # partial repair
    assert again == a, "a partial repair updates; it does not reopen"
    item = gs.open_items(d)[0]
    assert item.opened_iso == "2026-08-01T00:00:00+00:00"
    assert item.last_seen_iso == "2026-08-05T00:00:00+00:00"
    assert list(item.payload) == ["a.py"]


# --- fail-closed reads ------------------------------------------------------

@pytest.mark.parametrize("body,label", [
    ("{not json", "malformed JSON"),
    (json.dumps({"schema_version": 99, "open": {}, "closed": [],
                 "migration_notes": []}), "unknown schema_version"),
    (json.dumps({"schema_version": 1, "open": [], "closed": [],
                 "migration_notes": []}), "wrong field shape"),
])
def test_corrupt_state_fails_closed(tmp_path, body, label):
    """Reporting corrupt state as empty would let the next write silently
    discard committed history."""
    d = str(tmp_path)
    p = gs.state_path(d)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    with pytest.raises(gs.StateError):
        gs.read_document(d)
    assert gs.close_item(d, signature=SIG, expected_occurrence="x",
                         now_iso="2026-08-02T00:00:00+00:00",
                         resolving_condition="x") is gs.CloseResult.STATE_ERROR


def test_candidate_validation_refuses_to_drop_unrelated_open(tmp_path):
    """A write must not lose work it was never asked to touch."""
    d = str(tmp_path)
    _open(d, ["a.py"])
    prev = gs.read_document(d)
    candidate = {"schema_version": 1, "migration_notes": [], "open": {}, "closed": []}
    with pytest.raises(gs.StateError, match="unrelated open"):
        gs._validate_candidate(prev, candidate, signature="something:else")


def test_candidate_validation_refuses_to_drop_closed_history(tmp_path):
    d = str(tmp_path)
    a = _open(d, ["a.py"])
    gs.close_item(d, signature=SIG, expected_occurrence=a,
                  now_iso="2026-08-02T00:00:00+00:00", resolving_condition="x")
    prev = gs.read_document(d)
    candidate = {"schema_version": 1, "migration_notes": [], "open": {}, "closed": []}
    with pytest.raises(gs.StateError, match="closed history"):
        gs._validate_candidate(prev, candidate, signature=SIG)


# --- AC 20: the artifact classes the families lived in are gone -------------

def test_no_governance_operation_creates_a_second_artifact(tmp_path):
    """Families 1, 2, 3, 5 and 9 are all reachable only because closure spanned
    a source file, a derived archive path and an append-only audit log. The
    claim that they are UNREPRESENTABLE rests on those artifacts not existing —
    so assert that, rather than asserting it in prose.
    """
    d = str(tmp_path)
    a = _open(d, ["a.py"])
    _open(d, ["a.py", "b.py"], "2026-08-02T00:00:00+00:00")
    gs.close_item(d, signature=SIG, expected_occurrence=a,
                  now_iso="2026-08-03T00:00:00+00:00", resolving_condition="cleared")
    b = _open(d, ["c.py"], "2026-08-04T00:00:00+00:00")
    gs.close_item(d, signature=SIG, expected_occurrence=b,
                  now_iso="2026-08-05T00:00:00+00:00", resolving_condition="again")

    written = {p.name for p in tmp_path.rglob("*") if p.is_file()}
    assert written == {"state.json", "state.json.lock"}, \
        f"closure must touch nothing but the document and its lock: {written}"
    assert len(gs.closed_items(d)) == 2, "both closures retained in-document"


# --- AC 26: size and latency are observable; over-limit refuses -------------

def test_over_limit_write_refuses_and_keeps_prior_state(tmp_path, monkeypatch):
    """`closed` is the shadow archive. Truncating to fit would destroy exactly
    the history it exists to keep, silently, during a successful-looking close.
    """
    d = str(tmp_path)
    a = _open(d, ["a.py"])
    before = gs.state_path(d).read_bytes()

    monkeypatch.setattr(gs, "MAX_DOCUMENT_BYTES", 10)
    assert gs.close_item(d, signature=SIG, expected_occurrence=a,
                         now_iso="2026-08-02T00:00:00+00:00",
                         resolving_condition="cleared") is gs.CloseResult.STATE_ERROR
    assert gs.state_path(d).read_bytes() == before, "nothing may be replaced"

    monkeypatch.undo()
    assert gs.close_item(d, signature=SIG, expected_occurrence=a,
                         now_iso="2026-08-03T00:00:00+00:00",
                         resolving_condition="cleared") is gs.CloseResult.CLOSED


def test_write_size_and_latency_are_logged(tmp_path, caplog):
    import logging as _logging
    with caplog.at_level(_logging.INFO, logger="kernos.kernel.governance_state"):
        _open(str(tmp_path), ["a.py"])
    assert any("GOVERNANCE_WRITE" in r.message and "bytes=" in r.message
               and "write_ms=" in r.message for r in caplog.records)


def test_approaching_the_ceiling_warns_before_it_refuses(tmp_path, monkeypatch, caplog):
    import logging as _logging
    monkeypatch.setattr(gs, "MAX_DOCUMENT_BYTES", 4000)
    monkeypatch.setattr(gs, "DOCUMENT_WARN_BYTES", 100)
    with caplog.at_level(_logging.WARNING, logger="kernos.kernel.governance_state"):
        _open(str(tmp_path), [f"kernos/module_{i}.py" for i in range(20)])
    assert any("GOVERNANCE_DOC_LARGE" in r.message for r in caplog.records)


# --- migration: eight cases, ONE outcome each -------------------------------

def _legacy_source(tmp_path, payload, occ="occ-A", opened="2026-07-01T00:00:00+00:00",
                   title="Coverage gap", last_seen=None):
    fdir = tmp_path / "diagnostics" / "friction"
    fdir.mkdir(parents=True, exist_ok=True)
    lines = [f"# GOVERNANCE: {title}", "", "Class: governance",
             f"Signature: {SIG}", f"Occurrence: {occ}", "Human-gated: true",
             f"Opened: {opened}", f"Last-seen: {last_seen or opened}", "",
             "## Condition", "modules unowned", "", "## Payload"]
    lines += [f"- {x}" for x in payload]
    (fdir / fr.governance_filename(SIG)).write_text("\n".join(lines) + "\n")


def _legacy_archive(tmp_path, payload, occ="occ-A", opened="2026-07-01T00:00:00+00:00",
                    title="Coverage gap"):
    rdir = tmp_path / "diagnostics" / "friction_resolved"
    rdir.mkdir(parents=True, exist_ok=True)
    lines = [f"# GOVERNANCE: {title}", "", "Class: governance",
             f"Signature: {SIG}", f"Occurrence: {occ}", "Human-gated: true",
             f"Opened: {opened}", f"Last-seen: {opened}", "", "## Payload"]
    lines += [f"- {x}" for x in payload]
    (rdir / f"GOVERNANCE_x_closed_{occ}.md").write_text("\n".join(lines) + "\n")


def _legacy_manifest(tmp_path, occ="occ-A", closed="2026-07-05T00:00:00+00:00"):
    rdir = tmp_path / "diagnostics" / "friction_resolved"
    rdir.mkdir(parents=True, exist_ok=True)
    (rdir / "_governance_manifest.jsonl").write_text(json.dumps({
        "governance_txn": occ, "governance_signature": SIG,
        "opened_iso": "2026-07-01T00:00:00+00:00", "closed_iso": closed,
        "resolving_condition": "cleared", "final_payload": ["a.py"]}) + "\n")


NOW = "2026-08-12T00:00:00+00:00"


def test_case1_nothing(tmp_path):
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.FRESH
    assert gs.open_items(str(tmp_path)) == []


def test_case2_source_only_becomes_open(tmp_path):
    """The case rev 1 would have DISCARDED — losing a live finding."""
    _legacy_source(tmp_path, ["a.py"])
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.IMPORTED
    items = gs.open_items(str(tmp_path))
    assert len(items) == 1 and list(items[0].payload) == ["a.py"]
    assert items[0].opened_iso == "2026-07-01T00:00:00+00:00"


def test_case3_orphan_archive_recovers_as_open(tmp_path):
    """No audit committed => closure did not happen, and A is the sole copy."""
    _legacy_archive(tmp_path, ["a.py"])
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.IMPORTED
    assert len(gs.open_items(str(tmp_path))) == 1
    assert gs.closed_items(str(tmp_path)) == []


def test_case4_manifest_only_aborts(tmp_path):
    """A row alone has no recoverable payload; never synthesise a closed entry."""
    _legacy_manifest(tmp_path)
    out, notes = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED
    assert not gs.state_path(str(tmp_path)).exists(), "abort must write nothing"


def test_case5_source_and_uncommitted_archive_prefers_source(tmp_path):
    _legacy_source(tmp_path, ["fresh.py"])
    _legacy_archive(tmp_path, ["stale.py"])
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.IMPORTED
    items = gs.open_items(str(tmp_path))
    assert list(items[0].payload) == ["fresh.py"], "S governs; A was an attempt"


def test_case6_source_and_manifest_without_archive_aborts(tmp_path):
    """M cannot furnish a complete closed record without an archive, and
    equality cannot prove S is not a raced recurrence."""
    _legacy_source(tmp_path, ["a.py"])
    _legacy_manifest(tmp_path)
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED
    assert not gs.state_path(str(tmp_path)).exists()


def test_case7_archive_and_manifest_becomes_closed(tmp_path):
    _legacy_archive(tmp_path, ["a.py"])
    _legacy_manifest(tmp_path)
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.IMPORTED
    assert gs.open_items(str(tmp_path)) == []
    closed = gs.closed_items(str(tmp_path))
    assert len(closed) == 1 and closed[0]["payload"] == ["a.py"]


def test_case8_divergent_keeps_closure_AND_carries_recurrence(tmp_path):
    """A/M describe occurrence A while S holds recurrence B: unconditionally
    dropping S would repeat the lost-recurrence family inside the migration."""
    _legacy_archive(tmp_path, ["a.py"], occ="occ-A")
    _legacy_manifest(tmp_path, occ="occ-A")
    _legacy_source(tmp_path, ["b.py"], occ="occ-B", opened="2026-07-06T00:00:00+00:00")
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.IMPORTED
    assert len(gs.closed_items(str(tmp_path))) == 1
    items = gs.open_items(str(tmp_path))
    assert len(items) == 1 and list(items[0].payload) == ["b.py"], \
        "the recurrence must survive migration"


def test_case8_identical_aborts_because_sameness_is_not_proof(tmp_path):
    """S sharing A's occurrence is EQUALLY explained by 'retirement pending' and
    by 'the condition was re-observed during close'. The parent state cannot
    distinguish them, so migration refuses rather than guessing. AC 23b's
    same-clock-tick variant, where no field differs at all."""
    _legacy_archive(tmp_path, ["a.py"], occ="occ-A")
    _legacy_manifest(tmp_path, occ="occ-A")
    _legacy_source(tmp_path, ["a.py"], occ="occ-A")
    out, notes = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED
    assert "occurrence" in " ".join(notes).lower()
    assert not gs.state_path(str(tmp_path)).exists()


def test_recurrence_is_decided_by_occurrence_not_by_field_comparison(tmp_path):
    """AC 23b's schedule, at the predicate.

    Legacy upsert PRESERVES the occurrence when it re-observes a live
    condition, so "payload differs" is equally explained by a recurrence and by
    the same occurrence being touched while its retirement was pending. Only a
    distinct occurrence proves a recurrence.

    Field comparison is also unsound at this seam: S and A arrive through two
    different parsers that normalise `title` differently and neither of which
    recovers `condition`, so identical records compare unequal.
    """
    _legacy_source(tmp_path, ["a.py"], occ="occ-A")
    _legacy_archive(tmp_path, ["a.py"], occ="occ-A")
    rdir = tmp_path / "diagnostics" / "friction_resolved"
    S = fr.open_governance_items(str(tmp_path))[0]
    A = list(gs._read_legacy_archives(rdir).values())[0]
    assert S["title"] != A["title"], "the raw parser outputs really do differ"

    assert not gs._is_recurrence(S, A)
    # same occurrence, later re-detection with a changed payload: still not a
    # recurrence — importing it would duplicate an occurrence already closed
    assert not gs._is_recurrence(
        {**S, "payload": ["b.py"], "last_seen_iso": "2026-07-09T00:00:00+00:00"}, A)
    # a missing id on either side proves nothing, so it routes to the abort
    assert not gs._is_recurrence({**S, "occurrence": ""}, A)
    assert not gs._is_recurrence(S, {**A, "occurrence": ""})
    # and the predicate still discriminates — it is not vacuously False
    assert gs._is_recurrence({**S, "occurrence": "occ-B"}, A)


def test_case8_same_occurrence_aborts_even_when_payload_differs(tmp_path):
    """AC 23b end-to-end: copy at t1, re-detection at t2, M committed for the
    t1 snapshot, retirement failed. Must abort, never merge."""
    _legacy_archive(tmp_path, ["a.py"], occ="occ-A")
    _legacy_manifest(tmp_path, occ="occ-A")
    _legacy_source(tmp_path, ["a.py", "b.py"], occ="occ-A",
                   last_seen="2026-07-04T00:00:00+00:00")
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED
    assert not gs.state_path(str(tmp_path)).exists()


# --- AC 23a: case 6 aborts for structural reasons, not comparison ones ------

@pytest.mark.parametrize("variant", [
    "title_only", "human_gate_only", "manifest_lacks_payload", "identical"])
def test_case6_aborts_for_every_variant(tmp_path, variant):
    """S+M with no archive: M alone cannot furnish a complete closed record,
    and no comparison of S against M can prove S is not a live recurrence. The
    abort must therefore be structural — one expected outcome, never "either".
    """
    kw = {}
    if variant == "title_only":
        kw["title"] = "Coverage gap (reworded)"
    _legacy_source(tmp_path, ["a.py"], occ="occ-A", **kw)
    if variant == "human_gate_only":
        p = tmp_path / "diagnostics" / "friction" / fr.governance_filename(SIG)
        p.write_text(p.read_text().replace("Human-gated: true", "Human-gated: false"))

    rdir = tmp_path / "diagnostics" / "friction_resolved"
    rdir.mkdir(parents=True, exist_ok=True)
    row = {"governance_txn": "occ-A", "governance_signature": SIG,
           "opened_iso": "2026-07-01T00:00:00+00:00",
           "closed_iso": "2026-07-05T00:00:00+00:00",
           "resolving_condition": "cleared", "final_payload": ["a.py"]}
    if variant == "manifest_lacks_payload":
        row.pop("final_payload")
    (rdir / "_governance_manifest.jsonl").write_text(json.dumps(row) + "\n")

    out, notes = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED, variant
    assert not gs.state_path(str(tmp_path)).exists(), "abort writes nothing"
    # legacy is left authoritative and untouched
    assert (tmp_path / "diagnostics" / "friction"
            / fr.governance_filename(SIG)).is_file()
    assert (rdir / "_governance_manifest.jsonl").is_file()


def test_corrupt_interior_manifest_row_aborts(tmp_path):
    """Committed history is never silently discarded."""
    _legacy_archive(tmp_path, ["a.py"])
    rdir = tmp_path / "diagnostics" / "friction_resolved"
    (rdir / "_governance_manifest.jsonl").write_text(
        'not-json\n{"governance_txn":"occ-A","governance_signature":"%s"}\n' % SIG)
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED


def test_migration_is_idempotent_and_never_reimports(tmp_path):
    """Once a valid document exists it is authoritative; legacy is ignored, so a
    later run cannot resurrect rows a close had already removed."""
    d = str(tmp_path)
    _legacy_source(tmp_path, ["a.py"])
    assert gs.migrate(d, now_iso=NOW)[0] is gs.MigrationOutcome.IMPORTED
    assert gs.migrate(d, now_iso=NOW)[0] is gs.MigrationOutcome.ALREADY_MIGRATED

    item = gs.open_items(d)[0]
    gs.close_item(d, signature=SIG, expected_occurrence=item.occurrence,
                  now_iso=NOW, resolving_condition="cleared")
    assert gs.migrate(d, now_iso=NOW)[0] is gs.MigrationOutcome.ALREADY_MIGRATED
    assert gs.open_items(d) == [], "legacy source must not be re-imported"


def test_fresh_migration_records_that_it_ran(tmp_path):
    """An empty document cannot distinguish 'migrated, found nothing' from
    'never migrated' — without a marker every run would re-scan legacy files."""
    d = str(tmp_path)
    gs.migrate(d, now_iso=NOW)
    assert gs.read_document(d)["migration_notes"], "fresh run must leave a marker"
    assert gs.migrate(d, now_iso=NOW)[0] is gs.MigrationOutcome.ALREADY_MIGRATED

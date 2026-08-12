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

from kernos.kernel import governance_state as gs

SIG = "self-review:coverage-gap"

# FROZEN PARENT-FORMAT CONSTANTS.
#
# The writer that produced these was deleted once nothing in production called
# it (kreview round 2). Deriving fixtures from a live writer would have been
# wrong anyway: it makes the tests agree with whatever the writer currently
# does, when what migration must handle is what the writer did AT f38b31f.
# These are captured from that writer and are now literals on purpose.
LEGACY_FILENAME = "GOVERNANCE_self_review_coverage_gap_04ade3df6e6b.md"
LEGACY_MANIFEST = "_governance_manifest.jsonl"
#: sha256("self-review:coverage-gap|2026-07-01T00:00:00+00:00")[:16] — the id the
#: parent itself derived before occurrence ids were persisted.
LEGACY_DERIVED_OCC = "c380addc84ef8f52"


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
    (fdir / LEGACY_FILENAME).write_text("\n".join(lines) + "\n")


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
    (rdir / LEGACY_MANIFEST).write_text(json.dumps({
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
    """AC 23b, at the decision.

    An earlier revision compared lifecycle FIELDS between the legacy source (S)
    and the archived snapshot (A). Legacy upsert PRESERVES the occurrence when
    it re-observes a live condition, so "payload differs" is equally explained
    by a recurrence and by the same occurrence being touched while its
    retirement was pending — importing that as a recurrence would open an entry
    carrying an occurrence already committed as closed.

    Field comparison was also unsound at the seam: S and A were parsed by two
    different readers that normalise `title` differently and neither of which
    recovers `condition`, so identical records compared unequal and this abort
    was unreachable. The decision is occurrence identity, and nothing else.
    """
    _legacy_archive(tmp_path, ["a.py"], occ="occ-A")
    _legacy_manifest(tmp_path, occ="occ-A")
    # S differs from A in payload and last-seen but carries the SAME occurrence
    _legacy_source(tmp_path, ["a.py", "b.py"], occ="occ-A",
                   last_seen="2026-07-04T00:00:00+00:00")
    out, notes = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED, \
        "differing fields must not be read as a recurrence"
    assert "occurrence" in " ".join(notes).lower()

    # and a genuinely distinct occurrence still imports as a recurrence
    _legacy_source(tmp_path, ["b.py"], occ="occ-B",
                   opened="2026-07-06T00:00:00+00:00")
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.IMPORTED
    assert [i.occurrence for i in gs.open_items(str(tmp_path))] == ["occ-B"]


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
        p = tmp_path / "diagnostics" / "friction" / LEGACY_FILENAME
        p.write_text(p.read_text().replace("Human-gated: true", "Human-gated: false"))

    rdir = tmp_path / "diagnostics" / "friction_resolved"
    rdir.mkdir(parents=True, exist_ok=True)
    row = {"governance_txn": "occ-A", "governance_signature": SIG,
           "opened_iso": "2026-07-01T00:00:00+00:00",
           "closed_iso": "2026-07-05T00:00:00+00:00",
           "resolving_condition": "cleared", "final_payload": ["a.py"]}
    if variant == "manifest_lacks_payload":
        row.pop("final_payload")
    (rdir / LEGACY_MANIFEST).write_text(json.dumps(row) + "\n")

    out, notes = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED, variant
    assert not gs.state_path(str(tmp_path)).exists(), "abort writes nothing"
    # legacy is left authoritative and untouched
    assert (tmp_path / "diagnostics" / "friction"
            / LEGACY_FILENAME).is_file()
    assert (rdir / LEGACY_MANIFEST).is_file()


def test_corrupt_interior_manifest_row_aborts(tmp_path):
    """Committed history is never silently discarded."""
    _legacy_archive(tmp_path, ["a.py"])
    rdir = tmp_path / "diagnostics" / "friction_resolved"
    (rdir / LEGACY_MANIFEST).write_text(
        'not-json\n{"governance_txn":"occ-A","governance_signature":"%s"}\n' % SIG)
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED


# --- kreview round 2: states the signature-keyed reconciler could not see ----

def _pair(tmp_path, occ, payload, opened, closed, name=None):
    """One committed closure: an archive plus the audit row that committed it."""
    rdir = tmp_path / "diagnostics" / "friction_resolved"
    rdir.mkdir(parents=True, exist_ok=True)
    lines = [f"# GOVERNANCE: Coverage gap", "", "Class: governance",
             f"Signature: {SIG}", f"Occurrence: {occ}", "Human-gated: true",
             f"Opened: {opened}", f"Last-seen: {opened}", "", "## Payload"]
    lines += [f"- {x}" for x in payload]
    (rdir / (name or f"GOVERNANCE_x_closed_{occ}.md")).write_text("\n".join(lines) + "\n")
    with (rdir / LEGACY_MANIFEST).open("a") as fh:
        fh.write(json.dumps({
            "governance_txn": occ, "governance_signature": SIG,
            "opened_iso": opened, "closed_iso": closed,
            "resolving_condition": "cleared", "final_payload": payload}) + "\n")


def test_every_historical_occurrence_survives_migration(tmp_path):
    """The parent format allows repeated open/close cycles per signature.

    Reconciling by signature picks ONE archive and ONE row, imports that cycle,
    and destroys every earlier closure the instant the document becomes
    authoritative. Reconciliation is by occurrence for exactly this reason.
    """
    _pair(tmp_path, "occ-A", ["a.py"], "2026-07-01T00:00:00+00:00",
          "2026-07-02T00:00:00+00:00")
    _pair(tmp_path, "occ-B", ["b.py"], "2026-07-03T00:00:00+00:00",
          "2026-07-04T00:00:00+00:00")
    _pair(tmp_path, "occ-C", ["c.py"], "2026-07-05T00:00:00+00:00",
          "2026-07-06T00:00:00+00:00")

    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.IMPORTED
    got = sorted(c["occurrence"] for c in gs.closed_items(str(tmp_path)))
    assert got == ["occ-A", "occ-B", "occ-C"], \
        "every committed closure must survive; none may be selected away"
    assert gs.open_items(str(tmp_path)) == []


def test_history_plus_a_live_recurrence(tmp_path):
    """Two committed closures AND a distinct open occurrence, together."""
    _pair(tmp_path, "occ-A", ["a.py"], "2026-07-01T00:00:00+00:00",
          "2026-07-02T00:00:00+00:00")
    _pair(tmp_path, "occ-B", ["b.py"], "2026-07-03T00:00:00+00:00",
          "2026-07-04T00:00:00+00:00")
    _legacy_source(tmp_path, ["c.py"], occ="occ-C", opened="2026-07-05T00:00:00+00:00")

    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.IMPORTED
    assert len(gs.closed_items(str(tmp_path))) == 2
    assert [i.occurrence for i in gs.open_items(str(tmp_path))] == ["occ-C"]


def test_audit_row_matched_to_an_unrelated_archive_aborts(tmp_path):
    """The A+M branch must PROVE the relation, not assume it.

    An unvalidated pair fabricates a closure association between records that
    have nothing to do with each other, and records it as committed history.
    """
    _legacy_archive(tmp_path, ["a.py"], occ="occ-A")
    _legacy_manifest(tmp_path, occ="occ-X")            # row for a different occurrence
    out, notes = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED
    assert "no archive" in " ".join(notes)
    assert not gs.state_path(str(tmp_path)).exists()


def test_audit_row_whose_archive_carries_another_signature_aborts(tmp_path):
    rdir = tmp_path / "diagnostics" / "friction_resolved"
    _legacy_archive(tmp_path, ["a.py"], occ="occ-A")
    (rdir / LEGACY_MANIFEST).write_text(json.dumps({
        "governance_txn": "occ-A", "governance_signature": "some:other-condition",
        "opened_iso": "2026-07-01T00:00:00+00:00",
        "closed_iso": "2026-07-05T00:00:00+00:00",
        "resolving_condition": "cleared", "final_payload": ["a.py"]}) + "\n")
    out, notes = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED
    assert "unrelated records" in " ".join(notes)
    assert not gs.state_path(str(tmp_path)).exists()


def test_two_uncommitted_archives_for_one_signature_abort(tmp_path):
    """Nothing in the parent state says which orphan is the live item."""
    _legacy_archive(tmp_path, ["a.py"], occ="occ-A")
    _legacy_archive(tmp_path, ["b.py"], occ="occ-B")
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED
    assert not gs.state_path(str(tmp_path)).exists()


@pytest.mark.parametrize("where", ["friction", "friction_resolved"])
def test_a_partial_legacy_document_aborts_rather_than_reading_as_fresh(tmp_path, where):
    """A best-effort reader parses this into empty fields, they get discarded as
    "no signatures", and FRESH writes a marker that makes the artifact invisible
    forever. Missing evidence is not an empty world."""
    d = tmp_path / "diagnostics" / where
    d.mkdir(parents=True)
    (d / "GOVERNANCE_partial.md").write_text("# GOVERNANCE: truncated mid-write\n")
    out, notes = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED, "must not read as FRESH"
    assert not gs.state_path(str(tmp_path)).exists()


def test_unreadable_legacy_document_aborts(tmp_path):
    d = tmp_path / "diagnostics" / "friction_resolved"
    d.mkdir(parents=True)
    (d / "GOVERNANCE_binary.md").write_bytes(b"\xff\xfe\x00 not utf-8")
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED
    assert not gs.state_path(str(tmp_path)).exists()


def test_pre_id_source_gets_the_parents_own_derived_occurrence(tmp_path):
    """Occurrence ids were not always persisted. An empty occurrence must never
    be committed — compare-and-close and the closed relation are both keyed on
    it — so a complete pre-id record derives the id the parent would have minted.
    """
    fdir = tmp_path / "diagnostics" / "friction"
    fdir.mkdir(parents=True)
    (fdir / LEGACY_FILENAME).write_text(
        "# GOVERNANCE: Coverage gap\n\nClass: governance\n"
        f"Signature: {SIG}\nHuman-gated: true\n"
        "Opened: 2026-07-01T00:00:00+00:00\n"
        "Last-seen: 2026-07-01T00:00:00+00:00\n\n## Payload\n- a.py\n")

    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.IMPORTED
    item = gs.open_items(str(tmp_path))[0]
    assert item.occurrence, "an empty occurrence must never be committed"
    assert item.occurrence == LEGACY_DERIVED_OCC, \
        "must match what the parent itself derived, not merely be non-empty"


def test_pre_id_document_without_an_opened_stamp_aborts(tmp_path):
    """Nothing can prove the identity of a record with neither an id nor the
    fields the derivation needs."""
    fdir = tmp_path / "diagnostics" / "friction"
    fdir.mkdir(parents=True)
    (fdir / LEGACY_FILENAME).write_text(
        f"# GOVERNANCE: Coverage gap\n\nSignature: {SIG}\nHuman-gated: true\n"
        "\n## Payload\n- a.py\n")
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED
    assert not gs.state_path(str(tmp_path)).exists()


# --- the older SHARED manifest era -------------------------------------------

def test_shared_manifest_era_closure_is_retained(tmp_path):
    """The spec requires importing the pre-`f38b31f` shared `_manifest.jsonl`.
    Reading only the newer file treats a committed closure as uncommitted and
    REOPENS it — resurrecting a finding that was resolved."""
    _legacy_archive(tmp_path, ["a.py"], occ="occ-A")
    rdir = tmp_path / "diagnostics" / "friction_resolved"
    (rdir / "_manifest.jsonl").write_text("\n".join([
        json.dumps({"signature": "friction:some-error", "archived_iso": "x"}),
        json.dumps({"governance_txn": "occ-A", "governance_signature": SIG,
                    "opened_iso": "2026-07-01T00:00:00+00:00",
                    "closed_iso": "2026-07-05T00:00:00+00:00",
                    "resolving_condition": "cleared", "final_payload": ["a.py"]}),
    ]) + "\n")

    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.IMPORTED
    assert gs.open_items(str(tmp_path)) == [], "a committed closure must not reopen"
    assert [c["occurrence"] for c in gs.closed_items(str(tmp_path))] == ["occ-A"]


def test_friction_rows_in_the_shared_manifest_are_never_governance_closures(tmp_path):
    """Strict row filtering: a friction-resolution row lacks both governance
    fields and must never be imported as a governance closure."""
    rdir = tmp_path / "diagnostics" / "friction_resolved"
    rdir.mkdir(parents=True)
    (rdir / "_manifest.jsonl").write_text("\n".join([
        json.dumps({"signature": "friction:a", "archived_iso": "x"}),
        json.dumps({"governance_txn": "occ-Z"}),          # half a pair: not usable
    ]) + "\n")
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.FRESH
    assert gs.closed_items(str(tmp_path)) == []


def test_one_occurrence_committed_in_both_manifest_eras_aborts(tmp_path):
    _legacy_archive(tmp_path, ["a.py"], occ="occ-A")
    _legacy_manifest(tmp_path, occ="occ-A")
    rdir = tmp_path / "diagnostics" / "friction_resolved"
    (rdir / "_manifest.jsonl").write_text(json.dumps({
        "governance_txn": "occ-A", "governance_signature": SIG,
        "opened_iso": "2026-07-01T00:00:00+00:00",
        "closed_iso": "2026-07-09T00:00:00+00:00",
        "resolving_condition": "different", "final_payload": ["a.py"]}) + "\n")
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED
    assert not gs.state_path(str(tmp_path)).exists()


# --- deep document validation ------------------------------------------------

@pytest.mark.parametrize("doc,label", [
    ({"open": {SIG: {}}, "closed": []}, "open entry with no fields"),
    ({"open": {SIG: {"occurrence": "o", "signature": "other:sig",
                     "opened_iso": "t", "last_seen_iso": "t", "payload": []}},
      "closed": []}, "key disagrees with signature"),
    ({"open": {SIG: {"occurrence": "", "signature": SIG, "opened_iso": "t",
                     "last_seen_iso": "t", "payload": []}},
      "closed": []}, "empty occurrence"),
    ({"open": {SIG: {"occurrence": "o", "signature": SIG, "opened_iso": "t",
                     "last_seen_iso": "t", "payload": "a.py"}},
      "closed": []}, "payload is not a list"),
    ({"open": {}, "closed": [{"occurrence": "o", "signature": SIG}]},
     "closed entry missing required fields"),
    ({"open": {SIG: {"occurrence": "dup", "signature": SIG, "opened_iso": "t",
                     "last_seen_iso": "t", "payload": []}},
      "closed": [{"occurrence": "dup", "signature": SIG, "opened_iso": "t",
                  "closed_iso": "t", "payload": []}]}, "occurrence open AND closed"),
])
def test_corrupt_nested_entries_fail_closed_everywhere(tmp_path, doc, label):
    """Container-only validation lets `open={"sig": {}}` report healthy and then
    raise KeyError from `open_items` — outside the StateError path every caller
    handles. Corruption is caught once, in the reader, for every surface.
    """
    d = str(tmp_path)
    p = gs.state_path(d)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema_version": 1, "migration_notes": [], **doc}))

    with pytest.raises(gs.StateError):
        gs.read_document(d)
    assert gs.health(d)[0] == "unreadable", label
    assert gs.open_items(d) == [], label            # degrades, never raises
    assert gs.closed_items(d) == [], label
    assert gs.close_item(d, signature=SIG, expected_occurrence="o",
                         now_iso=NOW, resolving_condition="x") \
        is gs.CloseResult.STATE_ERROR
    with pytest.raises(gs.StateError):
        gs.upsert_item(d, signature=SIG, title="t", condition="c",
                       payload=["a.py"], now_iso=NOW)


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


def test_enumeration_is_not_surfacing(tmp_path):
    """Reading the recovery queue must not mutate it — `/dump` is a recovery
    surface, and a read that re-surfaced or re-stamped would make inspecting the
    queue indistinguishable from acting on it."""
    d = str(tmp_path)
    _open(d, ["a.py"])
    before = gs.state_path(d).read_bytes()
    for _ in range(3):
        gs.open_items(d)
        gs.closed_items(d)
        gs.health(d)
    assert gs.state_path(d).read_bytes() == before

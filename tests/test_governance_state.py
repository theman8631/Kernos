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

from tests import _legacy_governance_fixtures as F

SIG = F.SIGNATURE


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




# ---------------------------------------------------------------------------
# Migration from the three-artifact parent lifecycle
#
# Every artifact here is derived from a BYTE-EXACT golden the parent itself
# wrote (tests/fixtures/legacy_governance/). The hand-rolled fixtures these
# replaced were each missing something real artifacts always carry — the
# `## Condition` section, the human-gate marker, the manifest's `final_payload`
# agreement — which is exactly how migration defects survived two review rounds.
# ---------------------------------------------------------------------------

NOW = "2026-08-12T00:00:00+00:00"
OCC_A, OCC_B, OCC_C = "occ-aaaa00000000", "occ-bbbb00000000", "occ-cccc00000000"
T1, T2, T3 = ("2026-07-01T00:00:00+00:00", "2026-07-03T00:00:00+00:00",
              "2026-07-05T00:00:00+00:00")


def test_the_golden_artifacts_are_accepted_unmodified(tmp_path):
    """The base case every abort assertion is measured against.

    If the goldens themselves were rejected, every "this state aborts" test
    below would pass for the wrong reason and prove nothing.
    """
    F.write_open_item(tmp_path)
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.IMPORTED
    item = gs.open_items(str(tmp_path))[0]
    assert item.occurrence == F.GOLDEN_OCCURRENCE
    assert list(item.payload) == F.GOLDEN_PAYLOAD
    assert item.human_gated is True
    assert item.title and item.condition, \
        "the parent writes both; losing either silently degrades the item"
    assert "unassigned_modules" in item.condition, \
        "the condition must be recovered from its section, not dropped"


# --- the eight S/A/M relations, ONE outcome each -----------------------------

def test_case1_nothing(tmp_path):
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.FRESH
    assert gs.open_items(str(tmp_path)) == []


def test_case2_source_only_becomes_open(tmp_path):
    """The case rev 1 would have DISCARDED — losing a live finding."""
    F.write_open_item(tmp_path, payload=["a.py"])
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.IMPORTED
    items = gs.open_items(str(tmp_path))
    assert len(items) == 1 and list(items[0].payload) == ["a.py"]
    assert items[0].opened_iso == F.GOLDEN_OPENED


def test_case3_orphan_archive_recovers_as_open(tmp_path):
    """No audit committed => the closure did not happen, and A is the sole copy."""
    F.write_archive(tmp_path, payload=["a.py"])
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.IMPORTED
    assert len(gs.open_items(str(tmp_path))) == 1
    assert gs.closed_items(str(tmp_path)) == []


def test_case4_manifest_only_aborts(tmp_path):
    """A row alone has no recoverable payload; never synthesise a closed entry."""
    F.append_manifest(tmp_path, F.manifest_row())
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED
    assert not gs.state_path(str(tmp_path)).exists(), "abort must write nothing"


def test_case5_source_and_uncommitted_archive_prefers_source(tmp_path):
    F.write_open_item(tmp_path, payload=["fresh.py"])
    F.write_archive(tmp_path, payload=["stale.py"])
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.IMPORTED
    assert list(gs.open_items(str(tmp_path))[0].payload) == ["fresh.py"], \
        "S governs; A was an uncommitted attempt"


def test_case6_source_and_manifest_without_archive_aborts(tmp_path):
    """M cannot furnish a complete closed record without an archive, and
    sameness cannot prove S is not a raced recurrence."""
    F.write_open_item(tmp_path)
    F.append_manifest(tmp_path, F.manifest_row())
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED
    assert not gs.state_path(str(tmp_path)).exists()


def test_case7_archive_and_manifest_becomes_closed(tmp_path):
    F.write_closure(tmp_path, payload=["a.py"])
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.IMPORTED
    assert gs.open_items(str(tmp_path)) == []
    closed = gs.closed_items(str(tmp_path))
    assert len(closed) == 1 and closed[0]["payload"] == ["a.py"]
    assert closed[0]["resolving_condition"], "the closure audit must survive"


def test_case8_divergent_keeps_closure_AND_carries_recurrence(tmp_path):
    """A/M describe occurrence A while S holds recurrence B: unconditionally
    dropping S would repeat the lost-recurrence family inside the migration."""
    F.write_closure(tmp_path, occurrence=OCC_A, payload=["a.py"], opened=T1)
    F.write_open_item(tmp_path, occurrence=OCC_B, payload=["b.py"], opened=T2)
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.IMPORTED
    assert len(gs.closed_items(str(tmp_path))) == 1
    items = gs.open_items(str(tmp_path))
    assert len(items) == 1 and list(items[0].payload) == ["b.py"], \
        "the recurrence must survive migration"


def test_case8_identical_aborts_because_sameness_is_not_proof(tmp_path):
    """S sharing A's occurrence is EQUALLY explained by 'retirement pending' and
    by 'the condition was re-observed during close'. The same-clock-tick variant,
    where no field differs at all."""
    F.write_closure(tmp_path, occurrence=OCC_A, payload=["a.py"], opened=T1)
    F.write_open_item(tmp_path, occurrence=OCC_A, payload=["a.py"], opened=T1)
    out, notes = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED
    assert "occurrence" in " ".join(notes).lower()
    assert not gs.state_path(str(tmp_path)).exists()


def test_recurrence_is_decided_by_occurrence_not_by_field_comparison(tmp_path):
    """AC 23b, at the decision.

    An earlier revision compared lifecycle FIELDS between S and A. The parent's
    upsert PRESERVES the occurrence when it re-observes a live condition, so
    "payload differs" is equally explained by a recurrence and by the same
    occurrence being touched while its retirement was pending — importing that
    as a recurrence opens an entry carrying an occurrence already committed as
    closed. Field comparison was also unsound at the seam: S and A were parsed
    by two different readers that normalise `title` differently and neither of
    which recovered `condition`, so identical records compared unequal and this
    abort was unreachable.
    """
    F.write_closure(tmp_path, occurrence=OCC_A, payload=["a.py"], opened=T1)
    # S differs in payload and last-seen but carries the SAME occurrence
    F.write_open_item(tmp_path, occurrence=OCC_A, payload=["a.py", "b.py"],
                      opened=T1, last_seen=T2)
    out, notes = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED, \
        "differing fields must not be read as a recurrence"
    assert "occurrence" in " ".join(notes).lower()

    # a genuinely distinct occurrence still imports as a recurrence
    F.write_open_item(tmp_path, occurrence=OCC_B, payload=["b.py"], opened=T2)
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.IMPORTED
    assert [i.occurrence for i in gs.open_items(str(tmp_path))] == [OCC_B]


# --- AC 23a: case 6 aborts structurally, never by comparison -----------------

@pytest.mark.parametrize("variant", ["title_only", "manifest_lacks_payload",
                                     "identical", "last_seen_only"])
def test_case6_aborts_for_every_variant(tmp_path, variant):
    """S+M with no archive: M alone cannot furnish a complete closed record, and
    no comparison of S against M can prove S is not a live recurrence. The abort
    must therefore be structural — one expected outcome, never "either"."""
    kw = {}
    if variant == "title_only":
        kw["title"] = "Coverage gap (reworded)"
    if variant == "last_seen_only":
        kw["last_seen"] = T2
    F.write_open_item(tmp_path, payload=["a.py"], **kw)

    row = F.manifest_row(payload=["a.py"])
    if variant == "manifest_lacks_payload":
        row.pop("final_payload")
    F.append_manifest(tmp_path, row)

    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED, variant
    assert not gs.state_path(str(tmp_path)).exists(), "abort writes nothing"
    # legacy is left authoritative and untouched
    assert (F.friction_dir(tmp_path) / F.SOURCE_FILENAME).is_file()
    assert (F.resolved_dir(tmp_path) / F.MANIFEST_FILENAME).is_file()


def test_a_non_affirmative_human_gate_is_refused_at_the_reader(tmp_path):
    """The gate is the entire point of a governance item. An absent or negative
    marker parses the same as "not gated", so importing it would strip the gate
    from the finding it protects."""
    for gate in ("false", None):
        root = tmp_path / f"gate_{gate}"
        body = F.OPEN_ITEM if gate is None else None
        if gate is None:
            body = "\n".join(ln for ln in F.OPEN_ITEM.splitlines()
                             if not ln.startswith("Human-gated:")) + "\n"
        F.write_open_item(root, human_gated=gate, body=body if gate is None else None)
        out, notes = gs.migrate(str(root), now_iso=NOW)
        assert out is gs.MigrationOutcome.ABORTED, gate
        assert "gate" in " ".join(notes).lower()
        assert not gs.state_path(str(root)).exists()


# --- occurrence-keyed reconciliation -----------------------------------------

def test_every_historical_occurrence_survives_migration(tmp_path):
    """The parent format allows repeated open/close cycles per signature.

    Reconciling by signature picks ONE archive and ONE row, imports that cycle,
    and destroys every earlier closure the instant the document becomes
    authoritative."""
    F.write_closure(tmp_path, occurrence=OCC_A, payload=["a.py"], opened=T1)
    F.write_closure(tmp_path, occurrence=OCC_B, payload=["b.py"], opened=T2)
    F.write_closure(tmp_path, occurrence=OCC_C, payload=["c.py"], opened=T3)

    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.IMPORTED
    got = sorted(c["occurrence"] for c in gs.closed_items(str(tmp_path)))
    assert got == sorted([OCC_A, OCC_B, OCC_C]), \
        "every committed closure must survive; none may be selected away"
    assert gs.open_items(str(tmp_path)) == []


def test_history_plus_a_live_recurrence(tmp_path):
    """Two committed closures AND a distinct open occurrence, together."""
    F.write_closure(tmp_path, occurrence=OCC_A, payload=["a.py"], opened=T1)
    F.write_closure(tmp_path, occurrence=OCC_B, payload=["b.py"], opened=T2)
    F.write_open_item(tmp_path, occurrence=OCC_C, payload=["c.py"], opened=T3)

    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.IMPORTED
    assert len(gs.closed_items(str(tmp_path))) == 2
    assert [i.occurrence for i in gs.open_items(str(tmp_path))] == [OCC_C]


# --- every A/M relation is PROVED, in identity and in content ----------------

def test_audit_row_matched_to_an_unrelated_archive_aborts(tmp_path):
    F.write_archive(tmp_path, occurrence=OCC_A, payload=["a.py"])
    F.append_manifest(tmp_path, F.manifest_row(occurrence="occ-xxxx00000000",
                                               payload=["a.py"]))
    out, notes = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED
    assert "no archive" in " ".join(notes)
    assert not gs.state_path(str(tmp_path)).exists()


def test_audit_row_whose_archive_carries_another_signature_aborts(tmp_path):
    F.write_archive(tmp_path, occurrence=OCC_A, payload=["a.py"])
    F.append_manifest(tmp_path, F.manifest_row(
        occurrence=OCC_A, payload=["a.py"], signature="some:other-condition"))
    out, notes = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED
    assert "unrelated records" in " ".join(notes)
    assert not gs.state_path(str(tmp_path)).exists()


def test_archive_and_row_that_disagree_on_payload_abort(tmp_path):
    """The lost-recurrence race, encoded in the artifacts themselves.

    The parent read the payload, then copied the source, then wrote the row —
    with no lock on the source across those steps. A re-detection landing
    mid-close therefore yields an archive holding the NEW payload and a row
    holding the pre-close one. Identity agreement cannot see that; committing
    the pair would record the re-detected finding as closed and leave nothing
    open.
    """
    F.write_archive(tmp_path, occurrence=OCC_A, payload=["redetected.py"])
    F.append_manifest(tmp_path, F.manifest_row(occurrence=OCC_A,
                                               payload=["pre-close.py"]))
    out, notes = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED
    assert "between the read and the commit" in " ".join(notes)
    assert not gs.state_path(str(tmp_path)).exists()


def test_archive_and_row_that_disagree_on_the_opened_stamp_abort(tmp_path):
    F.write_archive(tmp_path, occurrence=OCC_A, payload=["a.py"], opened=T1)
    F.append_manifest(tmp_path, F.manifest_row(occurrence=OCC_A,
                                               payload=["a.py"], opened=T2))
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED
    assert not gs.state_path(str(tmp_path)).exists()


@pytest.mark.parametrize("missing", ["closed_iso", "resolving_condition",
                                     "final_payload"])
def test_an_incomplete_audit_row_aborts(tmp_path, missing):
    """A row missing any part of the closure it claims to record cannot furnish
    a complete closed entry."""
    F.write_archive(tmp_path, occurrence=OCC_A, payload=["a.py"])
    row = F.manifest_row(occurrence=OCC_A, payload=["a.py"])
    row.pop(missing)
    F.append_manifest(tmp_path, row)
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED, missing
    assert not gs.state_path(str(tmp_path)).exists()


def test_two_uncommitted_archives_for_one_signature_abort(tmp_path):
    """Nothing in the parent state says which orphan is the live item."""
    F.write_archive(tmp_path, occurrence=OCC_A, payload=["a.py"], opened=T1)
    F.write_archive(tmp_path, occurrence=OCC_B, payload=["b.py"], opened=T2)
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED
    assert not gs.state_path(str(tmp_path)).exists()


# --- strict readers: missing evidence is not an empty world ------------------

@pytest.mark.parametrize("where", ["open", "archive"])
@pytest.mark.parametrize("marker", ["Last-seen:", "## Condition", "## Payload"])
def test_a_truncated_legacy_document_aborts(tmp_path, where, marker):
    """A record that RETAINS signature and opened stamp but lost its sections
    still imports under a completeness check that only looks at those two: the
    result is an authoritative item with an empty payload and no gate.
    """
    golden = F.OPEN_ITEM if where == "open" else F.ARCHIVE
    body = F.truncate_after(golden, marker)
    if where == "open":
        F.write_open_item(tmp_path, body=body)
    else:
        F.write_archive(tmp_path, body=body)

    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED, f"{where}/{marker}"
    assert not gs.state_path(str(tmp_path)).exists()


@pytest.mark.parametrize("where", ["open", "archive"])
def test_a_document_with_no_identity_aborts_rather_than_reading_as_fresh(tmp_path, where):
    """A best-effort reader parses this into empty fields, they get discarded as
    "no signatures", and FRESH writes a marker that makes the artifact invisible
    forever. Missing evidence is not an empty world."""
    body = "# GOVERNANCE: truncated mid-write\n"
    if where == "open":
        F.write_open_item(tmp_path, body=body)
    else:
        F.write_archive(tmp_path, body=body)
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED, "must not read as FRESH"
    assert not gs.state_path(str(tmp_path)).exists()


def test_a_non_governance_document_is_never_imported_as_one(tmp_path):
    body = F.OPEN_ITEM.replace("Class: governance", "Class: error")
    F.write_open_item(tmp_path, body=body)
    out, notes = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED
    assert "governance" in " ".join(notes)


@pytest.mark.parametrize("where", ["friction", "friction_resolved"])
def test_a_non_utf8_legacy_document_aborts(tmp_path, where):
    d = tmp_path / "diagnostics" / where
    d.mkdir(parents=True)
    (d / "GOVERNANCE_binary.md").write_bytes(b"\xff\xfe\x00 not utf-8")
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED
    assert not gs.state_path(str(tmp_path)).exists()


def test_a_non_utf8_manifest_aborts_rather_than_repairing_the_bytes(tmp_path):
    """`errors="replace"` turns an invalid byte inside an otherwise-valid JSON
    string into U+FFFD, so a corrupt audit parses cleanly and its damage is
    persisted into the document as if it were the committed text."""
    F.write_archive(tmp_path, occurrence=OCC_A, payload=["a.py"])
    row = json.dumps(F.manifest_row(occurrence=OCC_A, payload=["a.py"],
                                    resolving_condition="cleared")).encode()
    corrupt = row.replace(b"cleared", b"clea\xffed")
    (F.resolved_dir(tmp_path) / F.MANIFEST_FILENAME).write_bytes(corrupt + b"\n")

    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED
    assert not gs.state_path(str(tmp_path)).exists()


def test_corrupt_interior_manifest_row_aborts(tmp_path):
    """Committed history is never silently discarded."""
    F.write_archive(tmp_path, occurrence=OCC_A, payload=["a.py"])
    (F.resolved_dir(tmp_path) / F.MANIFEST_FILENAME).write_text(
        "not-json\n" + json.dumps(F.manifest_row(occurrence=OCC_A,
                                                 payload=["a.py"])) + "\n")
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED


def test_torn_manifest_tail_is_dropped_not_treated_as_corruption(tmp_path):
    """A malformed TRAILING row is an append that never completed, so the audit
    it described was never durable."""
    F.write_archive(tmp_path, occurrence=OCC_A, payload=["a.py"])
    F.append_manifest(tmp_path, F.manifest_row(occurrence=OCC_A, payload=["a.py"]))
    with (F.resolved_dir(tmp_path) / F.MANIFEST_FILENAME).open("a") as fh:
        fh.write('{"governance_txn": "occ-torn", "arch')
    out, notes = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.IMPORTED
    assert any(n.get("decision") == "dropped_torn_tail" for n in notes)
    assert len(gs.closed_items(str(tmp_path))) == 1


# --- pre-identity records ----------------------------------------------------

def test_pre_id_source_gets_the_parents_own_derived_occurrence(tmp_path):
    """Occurrence ids were not always persisted. An empty occurrence must never
    be committed — compare-and-close and the closed relation are both keyed on
    it — so a complete pre-id record derives the id the parent would have minted.
    """
    body = "\n".join(ln for ln in F.OPEN_ITEM.splitlines()
                     if not ln.startswith("Occurrence:")) + "\n"
    F.write_open_item(tmp_path, body=body)

    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.IMPORTED
    item = gs.open_items(str(tmp_path))[0]
    assert item.occurrence == F.GOLDEN_OCCURRENCE, \
        "must match what the parent itself derived, not merely be non-empty"


def test_pre_id_document_without_an_opened_stamp_aborts(tmp_path):
    """Nothing can prove the identity of a record with neither an id nor the
    fields the derivation needs."""
    body = "\n".join(ln for ln in F.OPEN_ITEM.splitlines()
                     if not ln.startswith(("Occurrence:", "Opened:"))) + "\n"
    F.write_open_item(tmp_path, body=body)
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED
    assert not gs.state_path(str(tmp_path)).exists()


# --- the older SHARED manifest era -------------------------------------------

def test_shared_manifest_era_closure_is_retained(tmp_path):
    """Reading only the newer file treats a committed closure as uncommitted and
    REOPENS it — resurrecting a finding that was resolved."""
    F.write_archive(tmp_path, occurrence=OCC_A, payload=["a.py"])
    F.append_manifest(
        tmp_path,
        {"signature": "friction:some-error", "archived_iso": "x"},
        F.manifest_row(occurrence=OCC_A, payload=["a.py"]),
        filename=F.SHARED_MANIFEST_FILENAME)

    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.IMPORTED
    assert gs.open_items(str(tmp_path)) == [], "a committed closure must not reopen"
    assert [c["occurrence"] for c in gs.closed_items(str(tmp_path))] == [OCC_A]


def test_friction_rows_in_the_shared_manifest_are_never_governance_closures(tmp_path):
    """Strict row filtering: a friction-resolution row lacks both governance
    fields and must never be imported as a governance closure."""
    F.append_manifest(tmp_path,
                      {"signature": "friction:a", "archived_iso": "x"},
                      {"governance_txn": OCC_A},          # half a pair: not usable
                      filename=F.SHARED_MANIFEST_FILENAME)
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.FRESH
    assert gs.closed_items(str(tmp_path)) == []


def test_one_occurrence_committed_in_both_manifest_eras_aborts(tmp_path):
    F.write_archive(tmp_path, occurrence=OCC_A, payload=["a.py"])
    F.append_manifest(tmp_path, F.manifest_row(occurrence=OCC_A, payload=["a.py"]))
    F.append_manifest(tmp_path,
                      F.manifest_row(occurrence=OCC_A, payload=["a.py"],
                                     closed="2026-07-09T00:00:00+00:00"),
                      filename=F.SHARED_MANIFEST_FILENAME)
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED
    assert not gs.state_path(str(tmp_path)).exists()


# --- deep document validation ------------------------------------------------

def _entry(**over):
    base = {"occurrence": "occ-1", "signature": SIG, "title": "t",
            "condition": "c", "payload": [], "opened_iso": "t1",
            "last_seen_iso": "t1", "human_gated": True}
    base.update(over)
    return base


def _closed(**over):
    base = {"occurrence": "occ-2", "signature": SIG, "title": "t",
            "payload": [], "opened_iso": "t1", "closed_iso": "t2",
            "resolving_condition": "r", "human_gated": True}
    base.update(over)
    return base


@pytest.mark.parametrize("doc,label", [
    ({"open": {SIG: {}}, "closed": []}, "open entry with no fields"),
    ({"open": {SIG: _entry(signature="other:sig")}, "closed": []},
     "key disagrees with signature"),
    ({"open": {SIG: _entry(occurrence="")}, "closed": []}, "empty occurrence"),
    ({"open": {SIG: _entry(payload="a.py")}, "closed": []}, "payload not a list"),
    ({"open": {SIG: _entry(title="")}, "closed": []}, "empty title"),
    ({"open": {SIG: {k: v for k, v in _entry().items() if k != "title"}},
      "closed": []}, "title absent"),
    ({"open": {SIG: {k: v for k, v in _entry().items() if k != "condition"}},
      "closed": []}, "condition absent"),
    ({"open": {SIG: {k: v for k, v in _entry().items() if k != "human_gated"}},
      "closed": []}, "human gate absent"),
    ({"open": {}, "closed": [{"occurrence": "o", "signature": SIG}]},
     "closed entry missing required fields"),
    ({"open": {}, "closed": [{k: v for k, v in _closed().items()
                              if k != "resolving_condition"}]},
     "closed entry has no resolving condition"),
    ({"open": {SIG: _entry(occurrence="dup")},
      "closed": [_closed(occurrence="dup")]}, "occurrence open AND closed"),
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


@pytest.mark.parametrize("notes,label", [
    ([{}], "a note with no fields"),
    ([{"from_format": "S", "decision": "", "occurrence": "", "note": ""}],
     "a note recording no decision"),
    ([{"from_format": "S", "decision": 1, "occurrence": "", "note": ""}],
     "a non-string decision"),
    ([{"from_format": "S", "occurrence": "", "note": ""}], "a note with no decision key"),
    ([{"from_format": "S", "decision": "open", "occurrence": "", "note": ""}]
     * (gs.MAX_MIGRATION_NOTES + 1), "more notes than the bound allows"),
])
def test_corrupt_migration_notes_fail_closed(tmp_path, notes, label):
    """A migration note is the only evidence that migration ran, and its mere
    presence makes the next run report ALREADY_MIGRATED. `[{}]` passing a shape
    check therefore suppresses a live legacy source forever."""
    d = str(tmp_path)
    p = gs.state_path(d)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema_version": 1, "migration_notes": notes,
                             "open": {}, "closed": []}))
    with pytest.raises(gs.StateError):
        gs.read_document(d)
    assert gs.health(d)[0] == "unreadable", label

    # and the legacy source it would otherwise have suppressed still aborts
    # loudly rather than being silently declared already-migrated
    F.write_open_item(tmp_path)
    out, _ = gs.migrate(d, now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED, label


def test_a_non_utf8_state_document_fails_closed_on_every_surface(tmp_path):
    """A decode failure is corruption like any other. Letting UnicodeDecodeError
    escape means it is not a StateError, so health/open_items/close_item all
    raise instead of degrading — every fail-closed surface bypassed at once."""
    d = str(tmp_path)
    p = gs.state_path(d)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b'{"schema_version": 1, "open": {}, "closed": [], '
                  b'"migration_notes": [], "x": "\xff"}')
    with pytest.raises(gs.StateError):
        gs.read_document(d)
    assert gs.health(d)[0] == "unreadable"
    assert gs.open_items(d) == []
    assert gs.closed_items(d) == []
    assert gs.close_item(d, signature=SIG, expected_occurrence="o",
                         now_iso=NOW, resolving_condition="x") \
        is gs.CloseResult.STATE_ERROR


# --- idempotence -------------------------------------------------------------

def test_migration_is_idempotent_and_never_reimports(tmp_path):
    """Once a valid document exists it is authoritative; legacy is ignored, so a
    later run cannot resurrect rows a close had already removed."""
    d = str(tmp_path)
    F.write_open_item(tmp_path, payload=["a.py"])
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


# --- kreview round 4 ---------------------------------------------------------

def test_a_newline_terminated_corrupt_row_is_corruption_not_a_torn_append(tmp_path):
    """Position alone cannot tell a torn append from a complete corrupt record.

    The parent always wrote each row followed by a newline, so a malformed FINAL
    record that IS newline-terminated was fully written. Dropping it converts
    corrupt closure evidence into a false claim that the closure never
    committed — which reopens a resolved finding as a live one.
    """
    F.write_archive(tmp_path, occurrence=OCC_A, payload=["a.py"])
    (F.resolved_dir(tmp_path) / F.MANIFEST_FILENAME).write_text("not-json\n")

    out, notes = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED
    assert "corrupt committed history" in " ".join(notes)
    assert not gs.state_path(str(tmp_path)).exists()
    assert gs.open_items(str(tmp_path)) == [], "the archive must not be reopened"


def test_a_newline_terminated_corrupt_row_after_a_valid_one_aborts(tmp_path):
    F.write_archive(tmp_path, occurrence=OCC_A, payload=["a.py"])
    F.append_manifest(tmp_path, F.manifest_row(occurrence=OCC_A, payload=["a.py"]))
    with (F.resolved_dir(tmp_path) / F.MANIFEST_FILENAME).open("a") as fh:
        fh.write('{"governance_txn": "occ-torn", "arch\n')      # NEWLINE-terminated
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED
    assert not gs.state_path(str(tmp_path)).exists()


def test_an_unterminated_corrupt_row_is_still_a_torn_append(tmp_path):
    """The genuine torn-append case must keep working: no terminating newline
    means the append never completed, so the audit was never durable."""
    F.write_archive(tmp_path, occurrence=OCC_A, payload=["a.py"])
    F.append_manifest(tmp_path, F.manifest_row(occurrence=OCC_A, payload=["a.py"]))
    with (F.resolved_dir(tmp_path) / F.MANIFEST_FILENAME).open("a") as fh:
        fh.write('{"governance_txn": "occ-torn", "arch')        # no newline
    out, notes = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.IMPORTED
    assert any(n.get("decision") == "dropped_torn_tail" for n in notes)
    assert len(gs.closed_items(str(tmp_path))) == 1


@pytest.mark.parametrize("version,label", [
    (True, "JSON true equals 1 in Python"),
    (1.0, "1.0 equals 1 in Python"),
    ("1", "a string version"),
    (2, "a future version"),
])
def test_only_an_exact_integer_schema_version_is_accepted(tmp_path, version, label):
    """`True == 1` and `1.0 == 1`, so an equality check alone makes a foreign or
    malformed version marker authoritative instead of failing closed."""
    d = str(tmp_path)
    p = gs.state_path(d)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema_version": version, "migration_notes": [],
                             "open": {}, "closed": []}))
    with pytest.raises(gs.StateError):
        gs.read_document(d)
    assert gs.health(d)[0] == "unreadable", label
    assert gs.open_items(d) == []


def test_an_empty_condition_is_refused_at_the_write_surface(tmp_path):
    """Presence avoids KeyError; it does not satisfy the schema. An item with no
    condition has no statement of what would resolve it, which is exactly what
    the recovery surface exists to show."""
    d = str(tmp_path)
    with pytest.raises(gs.StateError, match="condition"):
        gs.upsert_item(d, signature=SIG, title="Coverage gap", condition="",
                       payload=["a.py"], now_iso=NOW)
    assert not gs.state_path(d).exists(), "nothing may be written"


def test_an_empty_resolving_condition_is_refused_at_the_write_surface(tmp_path):
    """A closure with no resolving condition records that something was closed
    without recording why — closed history with no audit value."""
    d = str(tmp_path)
    occ = _open(d, ["a.py"])
    before = gs.state_path(d).read_bytes()
    assert gs.close_item(d, signature=SIG, expected_occurrence=occ, now_iso=NOW,
                         resolving_condition="") is gs.CloseResult.STATE_ERROR
    assert gs.state_path(d).read_bytes() == before, "nothing may be replaced"
    assert [i.occurrence for i in gs.open_items(d)] == [occ], "still open"


@pytest.mark.parametrize("doc,label", [
    ({"open": {SIG: _entry(condition="")}, "closed": []}, "empty condition"),
    ({"open": {}, "closed": [_closed(resolving_condition="")]},
     "empty resolving condition"),
])
def test_empty_lifecycle_conditions_fail_closed_on_read(tmp_path, doc, label):
    d = str(tmp_path)
    p = gs.state_path(d)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema_version": 1, "migration_notes": [], **doc}))
    with pytest.raises(gs.StateError):
        gs.read_document(d)
    assert gs.health(d)[0] == "unreadable", label
    assert gs.open_items(d) == []
    assert gs.closed_items(d) == []


# --- kreview round 5 ---------------------------------------------------------

@pytest.mark.parametrize("tail,outcome,label", [
    ("not-json\n", "abort", "terminated, then nothing"),
    ("not-json\n   ", "abort", "terminated, then blank whitespace with no newline"),
    ("not-json\n\n\n", "abort", "terminated, then blank lines"),
    ("not-json", "torn", "no terminator at all"),
    ("not-json\n   \nstill-not-json", "abort",
     "an INTERIOR malformed row aborts regardless of the tail"),
])
def test_torn_classification_uses_the_last_RECORDs_terminator(tmp_path, tail,
                                                              outcome, label):
    """Completion is a property of the last non-blank RECORD, not of the file.

    Testing `raw.endswith("\\n")` asks a different question: blank records are
    filtered out afterwards, so a fully-written malformed row followed by
    whitespace looks incomplete and gets dropped as torn — resurrecting the
    archive beside it as a live item, which is the resolved-item resurrection
    the framing rule exists to prevent.
    """
    F.write_archive(tmp_path, occurrence=OCC_A, payload=["a.py"])
    (F.resolved_dir(tmp_path) / F.MANIFEST_FILENAME).write_text(tail)

    out, notes = gs.migrate(str(tmp_path), now_iso=NOW)
    if outcome == "abort":
        assert out is gs.MigrationOutcome.ABORTED, label
        assert not gs.state_path(str(tmp_path)).exists(), label
        assert gs.open_items(str(tmp_path)) == [], "the archive must not be reopened"
    else:
        assert out is gs.MigrationOutcome.IMPORTED, label
        assert any(n.get("decision") == "dropped_torn_tail" for n in notes), label
        # no committed audit, so the orphan archive is the sole surviving copy
        assert len(gs.open_items(str(tmp_path))) == 1, label


def test_a_torn_tail_is_recorded_even_when_nothing_is_importable(tmp_path):
    """The fresh marker must not claim there were no legacy artifacts when a
    torn append was in fact discarded. That is a false statement about what
    migration saw, made permanent on the one surface that records it."""
    F.resolved_dir(tmp_path)
    (F.resolved_dir(tmp_path) / F.MANIFEST_FILENAME).write_text('{"governance_txn": "occ')

    out, notes = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.FRESH
    decisions = [n["decision"] for n in notes]
    assert "dropped_torn_tail" in decisions
    assert "no legacy governance artifacts" not in " ".join(n["note"] for n in notes)
    # and it is durable, not just returned
    assert "dropped_torn_tail" in [
        n["decision"] for n in gs.read_document(str(tmp_path))["migration_notes"]]


@pytest.mark.parametrize("doc,label", [
    ({"open": {SIG: _entry(condition="   ")}, "closed": []}, "blank condition"),
    ({"open": {SIG: _entry(title="  \t ")}, "closed": []}, "blank title"),
    ({"open": {SIG: _entry(opened_iso=" ")}, "closed": []}, "blank timestamp"),
    ({"open": {}, "closed": [_closed(resolving_condition="  ")]},
     "blank resolving condition"),
])
def test_whitespace_only_required_strings_are_not_usable(tmp_path, doc, label):
    """Whitespace is semantically empty: `condition="   "` persists an item
    whose recovery surface displays no condition at all."""
    d = str(tmp_path)
    p = gs.state_path(d)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema_version": 1, "migration_notes": [], **doc}))
    with pytest.raises(gs.StateError):
        gs.read_document(d)
    assert gs.health(d)[0] == "unreadable", label
    assert gs.open_items(d) == []
    assert gs.closed_items(d) == []


def test_whitespace_only_conditions_are_refused_at_the_write_surface(tmp_path):
    d = str(tmp_path)
    with pytest.raises(gs.StateError, match="condition"):
        gs.upsert_item(d, signature=SIG, title="Coverage gap", condition="   ",
                       payload=["a.py"], now_iso=NOW)
    assert not gs.state_path(d).exists()

    occ = _open(d, ["a.py"])
    before = gs.state_path(d).read_bytes()
    assert gs.close_item(d, signature=SIG, expected_occurrence=occ, now_iso=NOW,
                         resolving_condition=" \t ") is gs.CloseResult.STATE_ERROR
    assert gs.state_path(d).read_bytes() == before


def test_a_legacy_document_with_a_blank_payload_entry_aborts(tmp_path):
    body = F.OPEN_ITEM.replace("- kernos/a.py", "- ")
    F.write_open_item(tmp_path, body=body)
    out, _ = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED
    assert not gs.state_path(str(tmp_path)).exists()


def test_blank_padding_before_an_unterminated_tear_is_still_torn(tmp_path):
    """The torn case must survive the stricter framing rule, including when the
    incomplete append is separated from the committed rows by blank lines."""
    F.write_closure(tmp_path, occurrence=OCC_A, payload=["a.py"])
    with (F.resolved_dir(tmp_path) / F.MANIFEST_FILENAME).open("a") as fh:
        fh.write('   \n\n{"governance_txn": "occ-torn", "arch')

    out, notes = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.IMPORTED
    assert any(n.get("decision") == "dropped_torn_tail" for n in notes)
    assert len(gs.closed_items(str(tmp_path))) == 1
    assert gs.open_items(str(tmp_path)) == []


# --- kreview round 6: one rule, applied on every surface ---------------------

@pytest.mark.parametrize("note,label", [
    ({"from_format": "   ", "decision": "fresh", "occurrence": "", "note": ""},
     "blank provenance"),
    ({"from_format": "", "decision": "fresh", "occurrence": "", "note": ""},
     "empty provenance"),
    ({"from_format": "invented", "decision": "fresh", "occurrence": "", "note": ""},
     "unknown provenance"),
    ({"from_format": "S", "decision": "invented", "occurrence": "", "note": ""},
     "unknown decision"),
])
def test_a_corrupt_note_provenance_cannot_suppress_a_live_legacy_source(tmp_path,
                                                                        note, label):
    """A migration note is the authority marker that makes the next run report
    ALREADY_MIGRATED. A blank or invented provenance is the same
    corrupt-authority failure as `[{}]`, one layer deeper: the live legacy item
    disappears from the recovery surface with no error anywhere.
    """
    d = str(tmp_path)
    F.write_open_item(tmp_path, payload=["kernos/live.py"])
    p = gs.state_path(d)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema_version": 1, "migration_notes": [note],
                             "open": {}, "closed": []}))

    with pytest.raises(gs.StateError):
        gs.read_document(d)
    assert gs.health(d)[0] == "unreadable", label
    assert gs.migrate(d, now_iso=NOW)[0] is gs.MigrationOutcome.ABORTED, label


def test_the_notes_migration_actually_writes_all_validate(tmp_path):
    """The closed vocabulary must admit every note the producer emits, or a
    document this build wrote would be rejected by this build's reader."""
    seen = set()
    # fresh
    d1 = tmp_path / "fresh"
    gs.migrate(str(d1), now_iso=NOW)
    seen |= {n["decision"] for n in gs.read_document(str(d1))["migration_notes"]}
    # torn tail + committed closure + a live recurrence + an orphan archive
    d2 = tmp_path / "mixed"
    F.write_closure(d2, occurrence=OCC_A, payload=["a.py"], opened=T1)
    F.write_open_item(d2, occurrence=OCC_B, payload=["b.py"], opened=T2)
    with (F.resolved_dir(d2) / F.MANIFEST_FILENAME).open("a") as fh:
        fh.write('{"governance_txn": "torn')
    assert gs.migrate(str(d2), now_iso=NOW)[0] is gs.MigrationOutcome.IMPORTED
    seen |= {n["decision"] for n in gs.read_document(str(d2))["migration_notes"]}

    d3 = tmp_path / "orphan"
    F.write_archive(d3, occurrence=OCC_C, payload=["c.py"], opened=T3)
    assert gs.migrate(str(d3), now_iso=NOW)[0] is gs.MigrationOutcome.IMPORTED
    seen |= {n["decision"] for n in gs.read_document(str(d3))["migration_notes"]}

    assert seen <= gs._NOTE_DECISIONS, f"producer emits decisions the reader rejects: {seen}"
    assert {"fresh", "dropped_torn_tail", "closed", "open_from_archive"} <= seen, \
        "this test must actually exercise the vocabulary it claims to check"


@pytest.mark.parametrize("version", [True, 1.0, "1", 2, None])
def test_candidate_validation_and_the_reader_agree_on_the_version(tmp_path, version):
    """A validated candidate must never produce a document its own reader
    rejects. Bare equality in the write validator accepts True and 1.0, which
    read_document then refuses — the trust boundary disagreeing with itself.
    """
    prev = gs._empty_document()
    candidate = {"schema_version": version, "migration_notes": [],
                 "open": {}, "closed": []}
    with pytest.raises(gs.StateError, match="schema_version"):
        gs._validate_candidate(prev, candidate, signature=SIG)


def test_every_accepted_candidate_round_trips_through_the_reader(tmp_path):
    """The invariant behind the case above, stated directly."""
    d = str(tmp_path)
    occ = _open(d, ["a.py"])
    gs.close_item(d, signature=SIG, expected_occurrence=occ, now_iso=NOW,
                  resolving_condition="cleared")
    _open(d, ["b.py"], "2026-08-06T00:00:00+00:00")
    doc = gs.read_document(d)
    gs._validate_candidate(doc, doc, signature=SIG)      # accepted...
    gs._write_document(d, doc)
    assert gs.read_document(d) == doc                    # ...and readable back


@pytest.mark.parametrize("payload,label", [
    (["   "], "whitespace-only entry"),
    (["kernos/a.py", ""], "an empty entry beside a real one"),
    (["kernos/a.py", "\t\n"], "a blank entry beside a real one"),
])
def test_blank_payload_entries_are_refused_on_both_surfaces(tmp_path, payload, label):
    """The legacy validator already rejects this; the shared validator must
    agree, or the same semantic emptiness is legal depending on how it arrived.
    """
    d = str(tmp_path)
    with pytest.raises(gs.StateError, match="payload"):
        gs.upsert_item(d, signature=SIG, title="Coverage gap",
                       condition="modules unowned", payload=payload, now_iso=NOW)
    assert not gs.state_path(d).exists(), label

    p = gs.state_path(d)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"schema_version": 1, "migration_notes": [],
                             "open": {SIG: _entry(payload=payload)}, "closed": []}))
    with pytest.raises(gs.StateError):
        gs.read_document(d)
    assert gs.health(d)[0] == "unreadable", label


# --- kreview round 7: corruption must not collapse before validation ---------

def test_a_duplicate_closed_member_cannot_hide_retained_history(tmp_path):
    """`json.loads` silently keeps the LAST member, so two `closed` arrays
    collapse to whichever came second and the conflicting history is gone
    before a single validator runs — then the next ordinary write persists the
    collapsed document and the loss is permanent.
    """
    d = str(tmp_path)
    p = gs.state_path(d)
    p.parent.mkdir(parents=True, exist_ok=True)
    history = [_closed(occurrence="occ-history")]
    p.write_text(
        '{"schema_version": 1, "migration_notes": [], "open": {}, '
        f'"closed": {json.dumps(history)}, "closed": []' + "}")

    with pytest.raises(gs.StateError):
        gs.read_document(d)
    assert gs.health(d)[0] == "unreadable"
    assert gs.closed_items(d) == []
    # and the write path must not be able to overwrite what it could not read
    with pytest.raises(gs.StateError):
        gs.upsert_item(d, signature=SIG, title="Coverage gap",
                       condition="modules unowned", payload=["a.py"], now_iso=NOW)
    assert "occ-history" in p.read_text(), "retained history must survive on disk"


@pytest.mark.parametrize("body,label", [
    ('{"schema_version": 1, "migration_notes": [], "open": {}, "closed": [], '
     '"open": {}}', "duplicate top-level open"),
    ('{"schema_version": 1, "schema_version": 1, "migration_notes": [], '
     '"open": {}, "closed": []}', "duplicate schema_version"),
    ('{"schema_version": 1, "migration_notes": [], "closed": [], "open": '
     '{"self-review:coverage-gap": {"occurrence": "o", "occurrence": "o2", '
     '"signature": "self-review:coverage-gap", "title": "t", "condition": "c", '
     '"payload": [], "opened_iso": "t1", "last_seen_iso": "t1", '
     '"human_gated": true}}}', "duplicate field inside a nested open entry"),
])
def test_duplicate_keys_fail_closed_at_any_depth(tmp_path, body, label):
    d = str(tmp_path)
    p = gs.state_path(d)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    with pytest.raises(gs.StateError, match="duplicate"):
        gs.read_document(d)
    assert gs.health(d)[0] == "unreadable", label
    assert gs.open_items(d) == []
    assert gs.closed_items(d) == []


@pytest.mark.parametrize("dup,label", [
    ('"governance_txn": "occ-other", ', "conflicting occurrence identity"),
    ('"final_payload": ["something-else.py"], ', "conflicting payload evidence"),
    ('"governance_signature": "some:other-condition", ', "conflicting signature"),
])
def test_a_duplicate_key_audit_row_aborts_migration(tmp_path, dup, label):
    """The last value happens to match the archive, so every downstream check
    passes — while the row on disk carries conflicting evidence that the A/M
    content proof never gets to see."""
    F.write_archive(tmp_path, occurrence=OCC_A, payload=["a.py"])
    row = json.dumps(F.manifest_row(occurrence=OCC_A, payload=["a.py"]))
    assert row.startswith("{")
    (F.resolved_dir(tmp_path) / F.MANIFEST_FILENAME).write_text(
        "{" + dup + row[1:] + "\n")

    out, notes = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.ABORTED, label
    assert "corrupt committed history" in " ".join(notes), label
    assert not gs.state_path(str(tmp_path)).exists(), label
    assert gs.open_items(str(tmp_path)) == [], "the archive must not be reopened"


def test_an_unterminated_duplicate_key_row_is_still_a_torn_append(tmp_path):
    """The framing rule outranks the content of a malformed row: an incomplete
    append is incomplete whatever it would have said."""
    F.write_closure(tmp_path, occurrence=OCC_A, payload=["a.py"])
    row = json.dumps(F.manifest_row(occurrence="occ-torn", payload=["b.py"]))
    with (F.resolved_dir(tmp_path) / F.MANIFEST_FILENAME).open("a") as fh:
        fh.write('{"governance_txn": "dup", ' + row[1:])       # no terminator
    out, notes = gs.migrate(str(tmp_path), now_iso=NOW)
    assert out is gs.MigrationOutcome.IMPORTED
    assert any(n.get("decision") == "dropped_torn_tail" for n in notes)
    assert len(gs.closed_items(str(tmp_path))) == 1


def test_strict_json_rejects_duplicates_but_accepts_ordinary_documents(tmp_path):
    """The decoder must not become vacuously strict."""
    assert gs.strict_json('{"a": 1, "b": {"c": [1, 2, {"d": 3}]}}') == \
        {"a": 1, "b": {"c": [1, 2, {"d": 3}]}}
    for bad in ('{"a": 1, "a": 2}', '{"b": {"c": 1, "c": 2}}',
                '[{"x": 1, "x": 2}]'):
        with pytest.raises(ValueError, match="duplicate"):
            gs.strict_json(bad)

"""Governance classification, and the one live producer behind the human gate.

The invariant: a human-gated finding must not be routed to a one-shot ephemeral
whisper, because that is indistinguishable from a finding that was never raised.

The durable queue itself moved to `governance_state` (GOVERNANCE-STATE-DOCUMENT-V1)
and is asserted in `test_governance_state.py`. What remains here is the part
`friction_response` still owns — the fail-closed CLASSIFICATION boundary that
keeps a governance finding out of the reactive lane — plus the coverage-gap
producer end-to-end through `maybe_run_daily`.

The ~40 tests that exercised the old three-artifact writer were removed with the
writer itself (kreview round 2): nothing in production called it, and tests for a
dead writer assert the behaviour of code that can no longer run. The parent
formats migration must still consume are frozen as literal fixtures in
`test_governance_state.py`, not derived from a live writer.
"""
import json

import pytest

from kernos.kernel import friction_response as fr
from kernos.kernel import governance_state as gs
from kernos.kernel import self_maintenance_review as smr
from tests import _legacy_governance_fixtures as F


# --- classification boundary: fails CLOSED -----------------------------------

def test_report_class_matrix():
    assert fr.report_class("no header here") == "error"          # legacy
    assert fr.report_class("Class: error") == "error"
    assert fr.report_class("Class: opportunity") == "opportunity"
    assert fr.report_class("Class: governance") == "governance"


def test_typod_governance_class_is_quarantined_not_escalated():
    """A typo must never BROADEN a human-gated item into an automated lane.

    `error` is not a neutral bucket — it enters reactive Shape B and can reach
    gated automation. So an explicit-but-unrecognized class quarantines.
    """
    assert fr.report_class("Class: governnace") == "unknown"
    assert fr.report_class("Class: whatever") == "unknown"
    # and quarantine means excluded from Shape B, like governance
    assert "unknown" in fr.SHAPE_B_EXCLUDED_CLASSES
    assert "governance" in fr.SHAPE_B_EXCLUDED_CLASSES
    # legacy class-less reports are still ordinary Shape B work
    assert "error" not in fr.SHAPE_B_EXCLUDED_CLASSES


def test_governance_and_unknown_skipped_by_shape_b_inventory(tmp_path):
    d = str(tmp_path)
    fdir = tmp_path / "diagnostics" / "friction"
    fdir.mkdir(parents=True)
    (fdir / "FRICTION_20260804_120000_REAL_ERROR_aaaaaaaa.md").write_text("boom")
    (fdir / "FRICTION_20260804_120001_GOV_bbbbbbbb.md").write_text("Class: governance\n")
    (fdir / "FRICTION_20260804_120002_TYPO_cccccccc.md").write_text("Class: governnace\n")
    sigs = fr.list_open_signatures(d)
    bodies = " ".join(g["sample_body"] for g in sigs)
    assert "boom" in bodies              # the real error is Shape B work
    assert "governance" not in bodies    # human-gated: never reactive
    assert "governnace" not in bodies    # quarantined: never reactive


# --- the live producer, end to end -------------------------------------------

@pytest.mark.asyncio
async def test_map_repair_closes_the_item_despite_unchanged_shape(
        tmp_path, monkeypatch):
    """AC6 — the load-bearing test.

    `shape_fingerprint()` hashes the set of module PATHS. Repairing the map
    edits REVIEW_SLICES ownership and changes NO path, so the fingerprint is
    unchanged by exactly the repair that resolves the gap. If lifecycle
    evaluation sat behind that fingerprint the item could never close.
    """
    monkeypatch.setenv("KERNOS_SELF_MAINTENANCE_REVIEW", "1")
    d = str(tmp_path)
    payload = ('```json\n{"overall_health":"healthy","corrective_findings":[],'
               '"evolution_idea":null,"serves_the_whole":true}\n```')

    async def _consult(_p, _s=None): return payload
    async def _ok(_t, _r): pass

    # 1. a gap exists → item opens
    monkeypatch.setattr(smr, "unassigned_modules", lambda *a, **k: ["kernos/x.py"])
    await smr.maybe_run_daily(data_dir=d, now_iso="2026-08-01T00:00:00+00:00",
                              consult_fn=_consult, whisper_fn=_ok)
    items = gs.open_items(d)
    assert len(items) == 1 and list(items[0].payload) == ["kernos/x.py"]
    fp_before = smr.load_state(d)["shape_fingerprint"]

    # 2. the map is repaired — ownership changes, module paths do NOT
    monkeypatch.setattr(smr, "unassigned_modules", lambda *a, **k: [])
    st = smr.load_state(d); st["last_run_iso"] = ""; smr.save_state(d, st)
    await smr.maybe_run_daily(data_dir=d, now_iso="2026-08-02T00:00:00+00:00",
                              consult_fn=_consult, whisper_fn=_ok)

    assert smr.load_state(d)["shape_fingerprint"] == fp_before, \
        "precondition: the repair must NOT change the shape fingerprint"
    assert gs.open_items(d) == [], \
        "item must close on the live condition, not on the shape fingerprint"


@pytest.mark.asyncio
async def test_failed_durable_write_stays_retryable(tmp_path, monkeypatch):
    """AC9 — a landed whisper must never imply the finding was recorded."""
    monkeypatch.setenv("KERNOS_SELF_MAINTENANCE_REVIEW", "1")
    d = str(tmp_path)
    payload = ('```json\n{"overall_health":"healthy","corrective_findings":[],'
               '"evolution_idea":null,"serves_the_whole":true}\n```')

    async def _consult(_p, _s=None): return payload
    async def _ok(_t, _r): pass

    monkeypatch.setattr(smr, "unassigned_modules", lambda *a, **k: ["kernos/x.py"])
    def _boom(*a, **k):
        raise gs.StateError("injected durable-write failure")
    monkeypatch.setattr(gs, "upsert_item", _boom)
    await smr.maybe_run_daily(data_dir=d, now_iso="2026-08-01T00:00:00+00:00",
                              consult_fn=_consult, whisper_fn=_ok)
    # the whisper succeeded, but persistence did NOT — so persistence state is
    # empty and the item remains eligible for retry on the next scan.
    assert smr.load_state(d)["governance_persisted_fingerprint"] == ""


# --- an aborted migration binds the CALLER, not just migrate() ---------------

@pytest.mark.asyncio
async def test_aborted_migration_leaves_governance_state_untouched(tmp_path, monkeypatch):
    """kreview round 2, P0-1 — asserted through the production caller.

    ABORTED means the legacy artifacts are still authoritative and their
    ambiguity is unresolved. A caller that discards the outcome and upserts
    anyway CREATES state.json, which is then authoritative and makes the
    un-migrated legacy occurrence invisible forever — the exact loss the abort
    exists to prevent.
    """
    monkeypatch.setenv("KERNOS_SELF_MAINTENANCE_REVIEW", "1")
    d = str(tmp_path)

    # case 6: an open item plus an audit row with no archive — always aborts.
    # Byte-exact parent artifacts, not approximations of them.
    F.write_open_item(tmp_path, payload=["legacy.py"])
    F.append_manifest(tmp_path, F.manifest_row(payload=["legacy.py"]))

    assert gs.migrate(d, now_iso="2026-08-01T00:00:00+00:00")[0] \
        is gs.MigrationOutcome.ABORTED, "precondition: this state aborts"

    payload = ('```json\n{"overall_health":"healthy","corrective_findings":[],'
               '"evolution_idea":null,"serves_the_whole":true}\n```')

    async def _consult(_p, _s=None): return payload
    async def _ok(_t, _r): pass

    monkeypatch.setattr(smr, "unassigned_modules", lambda *a, **k: ["new.py"])
    await smr.maybe_run_daily(data_dir=d, now_iso="2026-08-01T00:00:00+00:00",
                              consult_fn=_consult, whisper_fn=_ok)

    assert not gs.state_path(d).exists(), \
        "no governance document may be created while migration is unresolved"
    assert smr.load_state(d)["governance_persisted_fingerprint"] == "", \
        "nothing was persisted, so nothing may be acknowledged"
    assert (F.friction_dir(tmp_path) / F.SOURCE_FILENAME).is_file(), \
        "legacy stays authoritative"

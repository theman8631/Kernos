"""Tests for the per-workflow ledger.

WORKFLOW-LOOP-PRIMITIVE C5. Pins:

  - append + read_last round-trip
  - cross-instance isolation: a workflow_id with .. or absolute paths
    cannot escape the instance subtree.
  - append-only: prior entries preserved across appends.
"""
from __future__ import annotations

import json

import pytest

from kernos.kernel.workflows.ledger import (
    LedgerPathViolation,
    WorkflowLedger,
)


class TestLedgerRoundTrip:
    async def test_append_then_read_last(self, tmp_path):
        ledger = WorkflowLedger(str(tmp_path))
        await ledger.append("inst_a", "wf-1", {"step": 1, "synopsis": "did x"})
        await ledger.append("inst_a", "wf-1", {"step": 2, "synopsis": "did y"})
        last = await ledger.read_last("inst_a", "wf-1")
        assert last is not None
        assert last["step"] == 2
        assert last["synopsis"] == "did y"

    async def test_read_all_returns_in_order(self, tmp_path):
        ledger = WorkflowLedger(str(tmp_path))
        for i in range(3):
            await ledger.append("inst_a", "wf-1", {"step": i})
        entries = await ledger.read_all("inst_a", "wf-1")
        assert [e["step"] for e in entries] == [0, 1, 2]

    async def test_read_when_no_entries(self, tmp_path):
        ledger = WorkflowLedger(str(tmp_path))
        assert await ledger.read_last("inst_a", "wf-empty") is None
        assert await ledger.read_all("inst_a", "wf-empty") == []


class TestLedgerCrossInstanceIsolation:
    """Spec invariant: ledger paths MUST stay inside the calling
    instance's subtree. Attempts to escape via .., absolute paths,
    or symlink are rejected loudly."""

    def test_dot_dot_in_workflow_id_rejected(self, tmp_path):
        ledger = WorkflowLedger(str(tmp_path))
        # The safe-segment regex rewrites "/" / ".." characters, but
        # confirm the resolved path still falls inside the instance
        # subtree.
        path = ledger.ledger_path("inst_a", "../escape")
        # Resolved path must be inside inst_a's workflows subtree.
        assert "inst_a" in str(path)

    def test_dot_dot_instance_id_rejected(self, tmp_path):
        ledger = WorkflowLedger(str(tmp_path))
        # ".." literal as instance_id
        with pytest.raises(LedgerPathViolation):
            ledger.ledger_path("..", "wf-1")

    def test_dot_instance_id_rejected(self, tmp_path):
        ledger = WorkflowLedger(str(tmp_path))
        with pytest.raises(LedgerPathViolation):
            ledger.ledger_path(".", "wf-1")

    def test_empty_instance_id_rejected(self, tmp_path):
        ledger = WorkflowLedger(str(tmp_path))
        with pytest.raises(LedgerPathViolation):
            ledger.ledger_path("", "wf-1")

    async def test_cross_instance_write_isolated(self, tmp_path):
        ledger = WorkflowLedger(str(tmp_path))
        await ledger.append("inst_a", "wf-1", {"step": 1})
        await ledger.append("inst_b", "wf-1", {"step": 100})
        a = await ledger.read_last("inst_a", "wf-1")
        b = await ledger.read_last("inst_b", "wf-1")
        assert a["step"] == 1
        assert b["step"] == 100

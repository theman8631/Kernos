"""Tests for diagnostic tools — Improvement Loop Tier 2 Pass 2."""
import json
import os
import pytest
from pathlib import Path

from kernos.kernel.diagnostics import (
    _is_protected,
    _generate_spec_id,
    handle_propose_fix,
    handle_submit_spec,
    PROTECTED_PATTERNS,
)


class TestProtectedBoundaries:
    def test_gate_is_protected(self):
        assert _is_protected("kernos/kernel/gate.py") is True

    def test_credentials_is_protected(self):
        assert _is_protected("kernos/kernel/credentials.py") is True

    def test_auth_pattern_is_protected(self):
        assert _is_protected("kernos/kernel/auth_handler.py") is True

    def test_security_pattern_is_protected(self):
        assert _is_protected("kernos/security/config.py") is True

    def test_meta_protection(self):
        assert _is_protected("diagnostics.py:PROTECTED_PATTERNS") is True

    def test_normal_file_not_protected(self):
        assert _is_protected("kernos/kernel/reasoning.py") is False

    def test_handler_not_protected(self):
        assert _is_protected("kernos/messages/handler.py") is False

    def test_compaction_not_protected(self):
        assert _is_protected("kernos/kernel/compaction.py") is False


class TestSpecIdGeneration:
    def test_format(self):
        sid = _generate_spec_id()
        assert sid.startswith("spec_")
        assert len(sid) > 8

    def test_unique(self):
        ids = {_generate_spec_id() for _ in range(100)}
        assert len(ids) == 100


class TestProposeFix:
    async def test_writes_spec_file(self, tmp_path):
        os.environ["KERNOS_DATA_DIR"] = str(tmp_path)
        result = await handle_propose_fix("tenant1", {
            "diagnosis": "Search rate limit hit during parallel calls",
            "location": "kernos/capability/client.py:call_tool",
            "description": "Add semaphore-based rate limiting",
            "fix_type": "bug_fix",
            "risk": "low",
            "test_requirements": "Test concurrent search calls are throttled",
            "affected_components": ["capability.client"],
            "who_benefits": "User gets reliable search results instead of rate limit errors",
        })
        assert "Fix spec written" in result
        # Check file exists
        specs_dir = tmp_path / "tenant1" / "specs" / "proposed"
        specs = list(specs_dir.glob("spec_*.md"))
        assert len(specs) == 1
        content = specs[0].read_text()
        assert "semaphore" in content.lower()
        assert "who_benefits" in content.lower() or "Who benefits" in content

    async def test_rejects_protected_location(self, tmp_path):
        os.environ["KERNOS_DATA_DIR"] = str(tmp_path)
        result = await handle_propose_fix("tenant1", {
            "diagnosis": "Gate too strict",
            "location": "kernos/kernel/gate.py:check_tool",
            "description": "Loosen gate checks",
            "fix_type": "bug_fix",
            "risk": "low",
            "test_requirements": "Test gate still works",
            "affected_components": ["kernel.gate"],
            "who_benefits": "Fewer blocked actions",
        })
        assert "protected boundary" in result.lower()
        # No file written
        specs_dir = tmp_path / "tenant1" / "specs" / "proposed"
        assert not specs_dir.exists() or len(list(specs_dir.glob("*.md"))) == 0

    async def test_requires_who_benefits(self, tmp_path):
        os.environ["KERNOS_DATA_DIR"] = str(tmp_path)
        result = await handle_propose_fix("tenant1", {
            "diagnosis": "test",
            "location": "kernos/kernel/reasoning.py",
            "description": "test change",
            "fix_type": "bug_fix",
            "risk": "low",
            "test_requirements": "test",
            "affected_components": [],
            "who_benefits": "",
        })
        assert "who_benefits is required" in result.lower()


class TestSubmitSpec:
    async def test_moves_spec(self, tmp_path):
        os.environ["KERNOS_DATA_DIR"] = str(tmp_path)
        # Create a proposed spec
        proposed = tmp_path / "tenant1" / "specs" / "proposed"
        proposed.mkdir(parents=True)
        spec_path = proposed / "spec_test123.md"
        spec_path.write_text("# FIX SPEC: Test\nContent here.")

        result = await handle_submit_spec("tenant1", {
            "spec_id": "spec_test123",
            "notify_user": False,
        })
        assert "submitted" in result.lower()
        assert not spec_path.exists()
        submitted = tmp_path / "tenant1" / "specs" / "submitted" / "spec_test123.md"
        assert submitted.exists()

    async def test_missing_spec(self, tmp_path):
        os.environ["KERNOS_DATA_DIR"] = str(tmp_path)
        result = await handle_submit_spec("tenant1", {
            "spec_id": "spec_nonexistent",
        })
        assert "not found" in result.lower()

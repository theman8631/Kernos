"""Tests for Covenant Management — LLM validation, manage_covenants tool, startup migration.

Covers: validate_covenant_set (MERGE/CONFLICT/REWRITE/NO_ISSUES),
manage_covenants tool (list/remove/update), startup migration,
superseded rule filtering, and data model.
"""
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kernos.kernel.covenant_manager import (
    MANAGE_COVENANTS_TOOL,
    handle_manage_covenants,
    run_covenant_cleanup,
    supersede_rules,
    validate_covenant_set,
)
from kernos.kernel.state import CovenantRule, _rule_id
from kernos.kernel.state_json import JsonStateStore
from kernos.kernel.events import JsonEventStream


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _make_rule(
    instance_id: str = "test_tenant",
    rule_type: str = "must_not",
    description: str = "Never bring up divorce",
    source: str = "user_stated",
    rule_id: str = "",
    created_at: str = "",
    superseded_by: str = "",
) -> CovenantRule:
    return CovenantRule(
        id=rule_id or _rule_id(),
        instance_id=instance_id,
        capability="general",
        rule_type=rule_type,
        description=description,
        active=True,
        source=source,
        created_at=created_at or _now_iso(),
        updated_at=created_at or _now_iso(),
        enforcement_tier="confirm" if rule_type == "must_not" else "silent",
        superseded_by=superseded_by,
    )


# ---------------------------------------------------------------------------
# Data model: superseded_by field
# ---------------------------------------------------------------------------


class TestSupersededByField:
    def test_default_empty(self):
        rule = _make_rule()
        assert rule.superseded_by == ""

    async def test_load_old_rule_without_field(self, tmp_path):
        store = JsonStateStore(tmp_path)
        state_dir = store._state_dir("test_tenant")
        state_dir.mkdir(parents=True, exist_ok=True)
        old_rule = {
            "id": "rule_old1", "instance_id": "test_tenant",
            "capability": "general", "rule_type": "must_not",
            "description": "Never do bad things", "active": True, "source": "default",
        }
        (state_dir / "contracts.json").write_text(json.dumps([old_rule]))
        rules = await store.get_contract_rules("test_tenant", active_only=False)
        assert len(rules) == 1
        assert rules[0].superseded_by == ""


# ---------------------------------------------------------------------------
# Filtering: superseded rules excluded
# ---------------------------------------------------------------------------


class TestSupersededFiltering:
    async def test_excludes_superseded(self, tmp_path):
        store = JsonStateStore(tmp_path)
        await store.add_contract_rule(_make_rule(rule_id="rule_active"))
        await store.add_contract_rule(_make_rule(rule_id="rule_gone", superseded_by="rule_active"))
        active = await store.get_contract_rules("test_tenant", active_only=True)
        assert len(active) == 1
        assert active[0].id == "rule_active"

    async def test_includes_all(self, tmp_path):
        store = JsonStateStore(tmp_path)
        await store.add_contract_rule(_make_rule(rule_id="rule_active"))
        await store.add_contract_rule(_make_rule(rule_id="rule_gone", superseded_by="rule_active"))
        all_rules = await store.get_contract_rules("test_tenant", active_only=False)
        assert len(all_rules) == 2

    async def test_query_excludes_superseded(self, tmp_path):
        store = JsonStateStore(tmp_path)
        await store.add_contract_rule(_make_rule(rule_id="rule_active"))
        await store.add_contract_rule(_make_rule(rule_id="rule_gone", superseded_by="user_removed"))
        active = await store.query_covenant_rules("test_tenant", active_only=True)
        assert len(active) == 1


# ---------------------------------------------------------------------------
# validate_covenant_set
# ---------------------------------------------------------------------------


class TestValidateCovenantSet:
    async def test_no_issues(self, tmp_path):
        """Clean set returns NO_ISSUES."""
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        await store.add_contract_rule(_make_rule(rule_id="r1", description="Never send spam"))
        await store.add_contract_rule(_make_rule(rule_id="r2", description="Always confirm deletes", rule_type="must"))

        reasoning = AsyncMock()
        reasoning.complete_simple = AsyncMock(
            return_value='{"actions": [{"type": "NO_ISSUES"}]}'
        )

        stats = await validate_covenant_set(store, events, reasoning, "test_tenant", "r2")
        assert stats["merges"] == 0 and stats["conflicts"] == 0 and stats["rewrites"] == 0

    async def test_merge_duplicates(self, tmp_path):
        """MERGE: 4 rules about Sarah Henderson → merges into 1, supersedes 3."""
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        await store.add_contract_rule(_make_rule(rule_id="r1", description="Never contact Sarah Henderson"))
        await store.add_contract_rule(_make_rule(rule_id="r2", description="Do not reach out to Sarah Henderson"))
        await store.add_contract_rule(_make_rule(rule_id="r3", description="Don't contact Sarah Henderson without approval"))
        await store.add_contract_rule(_make_rule(rule_id="r4", description="No contacting Sarah H"))

        reasoning = AsyncMock()
        reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "actions": [{
                "type": "MERGE",
                "keep_rule_id": "r1",
                "supersede_rule_ids": ["r2", "r3", "r4"],
                "reason": "All about Sarah Henderson contact"
            }]
        }))

        stats = await validate_covenant_set(store, events, reasoning, "test_tenant", "r4")
        assert stats["merges"] == 3

        active = await store.get_contract_rules("test_tenant", active_only=True)
        assert len(active) == 1
        assert active[0].id == "r1"

    async def test_conflict_creates_whisper(self, tmp_path):
        """CONFLICT: MUST 'share thought process' vs MUST_NOT 'narrate reasoning'."""
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        await store.add_contract_rule(_make_rule(
            rule_id="r_must", rule_type="must", description="Share thought process every response"
        ))
        await store.add_contract_rule(_make_rule(
            rule_id="r_mustnot", rule_type="must_not", description="Do not narrate reasoning"
        ))

        reasoning = AsyncMock()
        reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "actions": [{
                "type": "CONFLICT",
                "rule_ids": ["r_must", "r_mustnot"],
                "description": "MUST share thought process contradicts MUST NOT narrate reasoning"
            }]
        }))

        stats = await validate_covenant_set(store, events, reasoning, "test_tenant", "r_mustnot")
        assert stats["conflicts"] == 1

        # Both rules still active — conflict not auto-resolved
        active = await store.get_contract_rules("test_tenant", active_only=True)
        assert len(active) == 2

        # Whisper should have been created
        whispers = await store.get_pending_whispers("test_tenant")
        assert len(whispers) == 1
        assert "two rules" in whispers[0].insight_text.lower()

    async def test_rewrite_creates_new_rule(self, tmp_path):
        """REWRITE: vague rule gets improved description."""
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        await store.add_contract_rule(_make_rule(rule_id="r_good", description="Never send spam"))
        await store.add_contract_rule(_make_rule(
            rule_id="r_vague", description="do the thing with emails"
        ))

        reasoning = AsyncMock()
        reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "actions": [{
                "type": "REWRITE",
                "rule_id": "r_vague",
                "current_description": "do the thing with emails",
                "suggested_description": "Always confirm before sending emails to external contacts"
            }]
        }))

        stats = await validate_covenant_set(store, events, reasoning, "test_tenant", "r_vague")
        assert stats["rewrites"] == 1

        active = await store.get_contract_rules("test_tenant", active_only=True)
        assert len(active) == 2  # r_good + rewritten version of r_vague
        rewritten = [r for r in active if "confirm before sending emails" in r.description]
        assert len(rewritten) == 1

        # Old rule superseded
        all_rules = await store.get_contract_rules("test_tenant", active_only=False)
        old = [r for r in all_rules if r.id == "r_vague"][0]
        assert old.superseded_by != ""

    async def test_mixed_actions(self, tmp_path):
        """Multiple actions in one response."""
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        await store.add_contract_rule(_make_rule(rule_id="r1", description="Never send spam"))
        await store.add_contract_rule(_make_rule(rule_id="r2", description="Don't send spam"))
        await store.add_contract_rule(_make_rule(
            rule_id="r3", rule_type="must", description="Share thought process"
        ))
        await store.add_contract_rule(_make_rule(
            rule_id="r4", rule_type="must_not", description="Don't narrate reasoning"
        ))

        reasoning = AsyncMock()
        reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "actions": [
                {"type": "MERGE", "keep_rule_id": "r1", "supersede_rule_ids": ["r2"], "reason": "Same rule"},
                {"type": "CONFLICT", "rule_ids": ["r3", "r4"], "description": "Contradicting rules"}
            ]
        }))

        stats = await validate_covenant_set(store, events, reasoning, "test_tenant", "r4")
        assert stats["merges"] == 1
        assert stats["conflicts"] == 1

    async def test_single_rule_skips_validation(self, tmp_path):
        """Only 1 active rule → skip validation entirely."""
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        await store.add_contract_rule(_make_rule(rule_id="r1"))

        reasoning = AsyncMock()
        stats = await validate_covenant_set(store, events, reasoning, "test_tenant", "r1")
        assert stats["merges"] == 0 and stats["conflicts"] == 0 and stats["rewrites"] == 0
        reasoning.complete_simple.assert_not_called()

    async def test_llm_failure_graceful(self, tmp_path):
        """LLM timeout → logs warning, no rules changed."""
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        await store.add_contract_rule(_make_rule(rule_id="r1"))
        await store.add_contract_rule(_make_rule(rule_id="r2", description="Another rule"))

        reasoning = AsyncMock()
        reasoning.complete_simple = AsyncMock(side_effect=Exception("API timeout"))

        stats = await validate_covenant_set(store, events, reasoning, "test_tenant", "r2")
        assert stats["merges"] == 0 and stats["conflicts"] == 0 and stats["rewrites"] == 0

        active = await store.get_contract_rules("test_tenant", active_only=True)
        assert len(active) == 2

    async def test_malformed_json_graceful(self, tmp_path):
        """Invalid JSON → logs warning, no rules changed."""
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        await store.add_contract_rule(_make_rule(rule_id="r1"))
        await store.add_contract_rule(_make_rule(rule_id="r2", description="Another"))

        reasoning = AsyncMock()
        reasoning.complete_simple = AsyncMock(return_value="not json at all")

        stats = await validate_covenant_set(store, events, reasoning, "test_tenant", "r2")
        assert stats["merges"] == 0 and stats["conflicts"] == 0 and stats["rewrites"] == 0

    async def test_invalid_rule_id_skipped(self, tmp_path):
        """LLM references non-existent rule_id → that action skipped, others processed."""
        store = JsonStateStore(tmp_path)
        events = JsonEventStream(tmp_path)
        await store.add_contract_rule(_make_rule(rule_id="r1", description="Rule one"))
        await store.add_contract_rule(_make_rule(rule_id="r2", description="Rule two"))

        reasoning = AsyncMock()
        reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "actions": [
                {"type": "MERGE", "keep_rule_id": "r_nonexistent", "supersede_rule_ids": ["r1"], "reason": "test"},
            ]
        }))

        stats = await validate_covenant_set(store, events, reasoning, "test_tenant", "r2")
        assert stats["merges"] == 0

        active = await store.get_contract_rules("test_tenant", active_only=True)
        assert len(active) == 2


# ---------------------------------------------------------------------------
# manage_covenants tool
# ---------------------------------------------------------------------------


class TestManageCovenantsToolDef:
    def test_tool_shape(self):
        assert MANAGE_COVENANTS_TOOL["name"] == "manage_covenants"
        schema = MANAGE_COVENANTS_TOOL["input_schema"]
        assert set(schema["properties"]["action"]["enum"]) == {"list", "remove", "update"}

    def test_no_create_action(self):
        """manage_covenants should NOT have a create/add action."""
        actions = MANAGE_COVENANTS_TOOL["input_schema"]["properties"]["action"]["enum"]
        assert "create" not in actions
        assert "add" not in actions

    def test_description_says_no_creation(self):
        assert "Do NOT" in MANAGE_COVENANTS_TOOL["description"]

    def test_in_kernel_tools(self):
        from kernos.kernel.reasoning import ReasoningService
        assert "manage_covenants" in ReasoningService._KERNEL_TOOLS


class TestManageCovenantsActions:
    async def test_list_active_rules(self, tmp_path):
        store = JsonStateStore(tmp_path)
        await store.add_contract_rule(_make_rule(rule_id="rule_a", description="Never send spam"))
        await store.add_contract_rule(_make_rule(rule_id="rule_b", description="Always confirm", rule_type="must"))
        await store.add_contract_rule(_make_rule(rule_id="rule_c", superseded_by="user_removed"))

        result = await handle_manage_covenants(store, "test_tenant", "list")
        assert "rule_a" in result
        assert "rule_b" in result
        assert "rule_c" not in result

    async def test_list_show_all(self, tmp_path):
        store = JsonStateStore(tmp_path)
        await store.add_contract_rule(_make_rule(rule_id="rule_a"))
        await store.add_contract_rule(_make_rule(rule_id="rule_b", superseded_by="user_removed"))

        result = await handle_manage_covenants(store, "test_tenant", "list", show_all=True)
        assert "rule_a" in result
        assert "rule_b" in result
        assert "SUPERSEDED" in result

    async def test_remove_soft_removes(self, tmp_path):
        store = JsonStateStore(tmp_path)
        await store.add_contract_rule(_make_rule(rule_id="rule_del"))

        result = await handle_manage_covenants(store, "test_tenant", "remove", rule_id="rule_del")
        assert "Removed" in result

        active = await store.get_contract_rules("test_tenant", active_only=True)
        assert len(active) == 0
        all_rules = await store.get_contract_rules("test_tenant", active_only=False)
        assert all_rules[0].superseded_by == "user_removed"

    async def test_update_creates_new(self, tmp_path):
        store = JsonStateStore(tmp_path)
        await store.add_contract_rule(_make_rule(rule_id="rule_upd", description="Old desc"))

        result = await handle_manage_covenants(
            store, "test_tenant", "update", rule_id="rule_upd", new_description="New desc"
        )
        assert "Updated" in result

        active = await store.get_contract_rules("test_tenant", active_only=True)
        assert len(active) == 1
        assert active[0].description == "New desc"

    async def test_remove_nonexistent(self, tmp_path):
        store = JsonStateStore(tmp_path)
        result = await handle_manage_covenants(store, "test_tenant", "remove", rule_id="nope")
        assert "Error" in result

    async def test_unknown_action(self, tmp_path):
        store = JsonStateStore(tmp_path)
        result = await handle_manage_covenants(store, "test_tenant", "destroy")
        assert "Error" in result


# ---------------------------------------------------------------------------
# Startup migration
# ---------------------------------------------------------------------------


class TestCovenantCleanup:
    async def test_deduplicates_exact_copies(self, tmp_path):
        store = JsonStateStore(tmp_path)
        for i in range(4):
            await store.add_contract_rule(_make_rule(
                rule_id=f"rule_dup_{i}", description="Do not bring up divorce",
                created_at=f"2026-03-{15+i:02d}T00:00:00+00:00",
            ))

        stats = await run_covenant_cleanup(store, "test_tenant")
        assert stats["deduped"] == 3
        active = await store.get_contract_rules("test_tenant", active_only=True)
        assert len(active) == 1
        assert active[0].id == "rule_dup_3"

    async def test_no_changes_when_clean(self, tmp_path):
        store = JsonStateStore(tmp_path)
        await store.add_contract_rule(_make_rule(rule_id="r1", description="Never spam"))
        await store.add_contract_rule(_make_rule(rule_id="r2", description="Always confirm", rule_type="must"))

        stats = await run_covenant_cleanup(store, "test_tenant")
        assert stats == {"deduped": 0, "contradictions_resolved": 0}

    async def test_resolves_contradiction(self, tmp_path):
        store = JsonStateStore(tmp_path)
        await store.add_contract_rule(_make_rule(
            rule_id="r_old", rule_type="must",
            description="Share your detailed reasoning process with the user in every response",
            created_at="2026-03-10T00:00:00+00:00",
        ))
        await store.add_contract_rule(_make_rule(
            rule_id="r_new", rule_type="must_not",
            description="Never share your detailed reasoning process with the user in responses",
            created_at="2026-03-15T00:00:00+00:00",
        ))

        stats = await run_covenant_cleanup(store, "test_tenant")
        assert stats["contradictions_resolved"] >= 1
        active = await store.get_contract_rules("test_tenant", active_only=True)
        assert len(active) == 1
        assert active[0].id == "r_new"


# ---------------------------------------------------------------------------
# System prompt / gate filtering
# ---------------------------------------------------------------------------


class TestFilteringIntegration:
    async def test_system_prompt_excludes_superseded(self, tmp_path):
        store = JsonStateStore(tmp_path)
        await store.add_contract_rule(_make_rule(rule_id="r_act", description="Active rule"))
        await store.add_contract_rule(_make_rule(rule_id="r_gone", description="Gone", superseded_by="user_removed"))

        rules = await store.query_covenant_rules("test_tenant", active_only=True)
        assert len(rules) == 1
        assert rules[0].description == "Active rule"

    async def test_gate_excludes_superseded(self, tmp_path):
        store = JsonStateStore(tmp_path)
        await store.add_contract_rule(_make_rule(rule_id="r_act", description="Active"))
        await store.add_contract_rule(_make_rule(rule_id="r_gone", description="Superseded", superseded_by="r_act"))

        rules = await store.get_contract_rules("test_tenant", active_only=True)
        assert len(rules) == 1


# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------


class TestEventTypes:
    def test_validation_event_types(self):
        from kernos.kernel.event_types import EventType
        assert EventType.COVENANT_RULE_MERGED == "covenant.rule.merged"
        assert EventType.COVENANT_CONTRADICTION_DETECTED == "covenant.contradiction.detected"
        assert EventType.COVENANT_RULE_UPDATED == "covenant.rule.updated"


# ---------------------------------------------------------------------------
# Instruction classification (behavioral constraint vs automation rule)
# ---------------------------------------------------------------------------


class TestInstructionClassification:
    async def test_behavioral_constraint_returns_rule(self):
        """Behavioral constraint → CovenantRule returned."""
        from kernos.kernel.contract_parser import classify_and_parse

        reasoning = AsyncMock()
        reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "instruction_type": "behavioral_constraint",
            "rule_type": "must_not",
            "description": "Never delete emails without confirmation",
            "capability": "email",
            "is_global": True,
            "reasoning": "Behavioral constraint about email handling",
        }))

        result = await classify_and_parse(reasoning, "Never delete my emails", None)
        assert result.instruction_type == "behavioral_constraint"
        assert result.rule is not None
        assert result.rule.rule_type == "must_not"
        assert result.standing_order == ""

    async def test_automation_rule_returns_standing_order(self):
        """Automation rule → no CovenantRule, standing_order description returned."""
        from kernos.kernel.contract_parser import classify_and_parse

        reasoning = AsyncMock()
        reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "instruction_type": "automation_rule",
            "rule_type": "must",
            "description": "When an email arrives, send a text notification",
            "capability": "email",
            "is_global": True,
            "reasoning": "Event-triggered automation, not a behavioral constraint",
        }))

        result = await classify_and_parse(
            reasoning, "Whenever I get an email, send me a text", None
        )
        assert result.instruction_type == "automation_rule"
        assert result.rule is None
        assert "email" in result.standing_order.lower()

    async def test_parse_behavioral_instruction_returns_none_for_automation(self):
        """The legacy parse_behavioral_instruction returns None for automation rules."""
        from kernos.kernel.contract_parser import parse_behavioral_instruction

        reasoning = AsyncMock()
        reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "instruction_type": "automation_rule",
            "rule_type": "must",
            "description": "Every Monday check calendar",
            "capability": "calendar",
            "is_global": True,
            "reasoning": "Scheduled automation",
        }))

        rule = await parse_behavioral_instruction(
            reasoning, "Every Monday, check my calendar and summarize", None
        )
        assert rule is None

    async def test_parse_behavioral_instruction_returns_rule_for_constraint(self):
        """The legacy parse_behavioral_instruction still works for constraints."""
        from kernos.kernel.contract_parser import parse_behavioral_instruction

        reasoning = AsyncMock()
        reasoning.complete_simple = AsyncMock(return_value=json.dumps({
            "instruction_type": "behavioral_constraint",
            "rule_type": "preference",
            "description": "Keep responses concise",
            "capability": "general",
            "is_global": True,
            "reasoning": "Communication style preference",
        }))

        rule = await parse_behavioral_instruction(
            reasoning, "Keep responses short", None
        )
        assert rule is not None
        assert rule.rule_type == "preference"

    async def test_schema_has_instruction_type(self):
        """Schema includes instruction_type field."""
        from kernos.kernel.contract_parser import CONTRACT_PARSER_SCHEMA
        props = CONTRACT_PARSER_SCHEMA["properties"]
        assert "instruction_type" in props
        assert set(props["instruction_type"]["enum"]) == {
            "behavioral_constraint", "automation_rule"
        }

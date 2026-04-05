"""JSON-on-disk implementation of StateStore.

Files per tenant under {data_dir}/{tenant_id}/state/:
  profile.json         — TenantProfile (single object)
  knowledge.json       — list of KnowledgeEntry
  contracts.json       — list of CovenantRule
  conversations.json   — list of ConversationSummary
  soul.json            — Soul (single object)
  entities.json        — list of EntityNode
  identity_edges.json  — list of IdentityEdge (per-tenant, Phase 2A)
  pending_actions.json — list of PendingAction
  preferences.json     — list of Preference (Phase 6A)

The interface abstracts the backend — a future MemOS or database integration
changes only this file.
"""
import json
import logging
from dataclasses import asdict
from pathlib import Path

from filelock import FileLock

from kernos.kernel.entities import EntityNode, IdentityEdge
from kernos.kernel.soul import Soul
from kernos.kernel.spaces import ContextSpace
from kernos.kernel.state import (
    ConversationSummary,
    CovenantRule,
    KnowledgeEntry,
    PendingAction,
    Preference,
    StateStore,
    TenantProfile,
)

logger = logging.getLogger(__name__)


from kernos.utils import _safe_name


# ---------------------------------------------------------------------------
# Migration helpers
# ---------------------------------------------------------------------------


def _durability_to_archetype(durability: str) -> str:
    """Map legacy durability string to lifecycle_archetype."""
    if not durability or durability == "permanent":
        return "structural"
    if durability == "session":
        return "ephemeral"
    if durability.startswith("expires_at:"):
        return "contextual"
    return "structural"


_RULE_TYPE_TO_ENFORCEMENT_TIER = {
    "must_not": "confirm",
    "must": "confirm",
    "preference": "silent",
    "escalation": "confirm",
}


def _load_covenant_rule(d: dict) -> CovenantRule:
    """Load CovenantRule from dict, migrating fields from older formats.

    Old contracts.json files may lack enforcement_tier or superseded_by.
    """
    data = dict(d)
    if "enforcement_tier" not in data:
        data["enforcement_tier"] = _RULE_TYPE_TO_ENFORCEMENT_TIER.get(
            data.get("rule_type", ""), "confirm"
        )
    if "superseded_by" not in data:
        data["superseded_by"] = ""
    return CovenantRule(**data)


_CONTEXT_SPACE_FIELDS = {
    "id", "tenant_id", "name", "description", "space_type", "status",
    "posture", "model_preference", "created_at", "last_active_at", "is_default",
    "max_file_size_bytes", "max_space_bytes", "active_tools",
    "parent_id", "aliases", "depth",
    "renamed_from", "renamed_at",
}


def _load_context_space(d: dict) -> ContextSpace:
    """Load ContextSpace from dict, silently ignoring unknown/removed fields.

    Old spaces.json files may have routing_keywords, routing_aliases,
    routing_entity_ids, suggestion_suppressed_until — all removed in v2.
    """
    filtered = {k: v for k, v in d.items() if k in _CONTEXT_SPACE_FIELDS}
    if "active_tools" not in filtered:
        filtered["active_tools"] = []
    if "aliases" not in filtered:
        filtered["aliases"] = []
    # Migrate legacy "daily" space_type to "general"
    if filtered.get("space_type") == "daily":
        filtered["space_type"] = "general"
    return ContextSpace(**filtered)


def _load_knowledge_entry(d: dict) -> KnowledgeEntry:
    """Load KnowledgeEntry from dict, applying migrations for missing fields.

    Handles: durability → lifecycle_archetype, multi-member fields (V1 defaults),
    and silently drops unknown keys (e.g. updated_at written by fact_harvest).
    """
    data = dict(d)
    if not data.get("lifecycle_archetype"):
        durability = data.get("durability", "")
        data["lifecycle_archetype"] = _durability_to_archetype(durability)
        if (
            durability.startswith("expires_at:")
            and not data.get("foresight_expires")
        ):
            data["foresight_expires"] = durability[len("expires_at:"):]
    # Multi-member foundation defaults (V1)
    if "owner_member_id" not in data:
        data["owner_member_id"] = ""
    if "sensitivity" not in data:
        data["sensitivity"] = "open"
    if "visible_to" not in data:
        data["visible_to"] = None
    # Strip unknown fields (forward-compat: new fields written by harvest/projectors)
    known_fields = {f for f in KnowledgeEntry.__dataclass_fields__}
    data = {k: v for k, v in data.items() if k in known_fields}
    return KnowledgeEntry(**data)


# ---------------------------------------------------------------------------
# JsonStateStore
# ---------------------------------------------------------------------------


class JsonStateStore(StateStore):
    """JSON file-backed state store."""

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)
        self._preference_migration_done: set[str] = set()

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _state_dir(self, tenant_id: str) -> Path:
        return self._data_dir / _safe_name(tenant_id) / "state"

    def _read_json(self, path: Path, default):
        if not path.exists():
            return default
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_json(self, path: Path, data) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = str(path) + ".lock"
        with FileLock(lock_path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

    # -----------------------------------------------------------------------
    # Soul
    # -----------------------------------------------------------------------

    async def get_soul(self, tenant_id: str) -> Soul | None:
        path = self._state_dir(tenant_id) / "soul.json"
        data = self._read_json(path, None)
        if data is None:
            return None
        # Migration: backfill empty defaults from older soul.json files
        migrated = False
        if not data.get("agent_name"):
            data["agent_name"] = "Kernos"
            migrated = True
        if not data.get("emoji"):
            data["emoji"] = "🜁"
            migrated = True
        soul = Soul(**data)
        if migrated:
            self._write_json(path, asdict(soul))
        return soul

    async def save_soul(self, soul: Soul, *, source: str = "", trigger: str = "") -> None:
        path = self._state_dir(soul.tenant_id) / "soul.json"
        self._write_json(path, asdict(soul))
        if source:
            logger.info("SOUL_WRITE: source=%s trigger=%s tenant=%s", source, trigger, soul.tenant_id)

    # -----------------------------------------------------------------------
    # Tenant Profile
    # -----------------------------------------------------------------------

    async def get_tenant_profile(self, tenant_id: str) -> TenantProfile | None:
        path = self._state_dir(tenant_id) / "profile.json"
        data = self._read_json(path, None)
        if data is None:
            return None
        return TenantProfile(**data)

    async def save_tenant_profile(self, tenant_id: str, profile: TenantProfile) -> None:
        path = self._state_dir(tenant_id) / "profile.json"
        self._write_json(path, asdict(profile))

    # -----------------------------------------------------------------------
    # Knowledge
    # -----------------------------------------------------------------------

    async def add_knowledge(self, entry: KnowledgeEntry) -> None:
        path = self._state_dir(entry.tenant_id) / "knowledge.json"
        entries = self._read_json(path, [])
        entries.append(asdict(entry))
        self._write_json(path, entries)

    async def query_knowledge(
        self,
        tenant_id: str,
        subject: str | None = None,
        category: str | None = None,
        tags: list[str] | None = None,
        active_only: bool = True,
        limit: int = 20,
    ) -> list[KnowledgeEntry]:
        path = self._state_dir(tenant_id) / "knowledge.json"
        raw = self._read_json(path, [])
        results: list[KnowledgeEntry] = []
        for d in raw:
            entry = _load_knowledge_entry(d)
            if active_only and not entry.active:
                continue
            if category and entry.category != category:
                continue
            if subject and subject.lower() not in entry.subject.lower():
                continue
            if tags and not any(t in entry.tags for t in tags):
                continue
            results.append(entry)
        return results[:limit]

    async def save_knowledge_entry(self, entry: KnowledgeEntry) -> None:
        """Upsert a KnowledgeEntry by ID."""
        path = self._state_dir(entry.tenant_id) / "knowledge.json"
        raw = self._read_json(path, [])
        for i, d in enumerate(raw):
            if d.get("id") == entry.id:
                raw[i] = asdict(entry)
                self._write_json(path, raw)
                return
        raw.append(asdict(entry))
        self._write_json(path, raw)

    async def get_knowledge_entry(
        self, tenant_id: str, entry_id: str
    ) -> KnowledgeEntry | None:
        """Get a single KnowledgeEntry by ID."""
        path = self._state_dir(tenant_id) / "knowledge.json"
        raw = self._read_json(path, [])
        for d in raw:
            if d.get("id") == entry_id:
                return _load_knowledge_entry(d)
        return None

    async def get_knowledge_hashes(self, tenant_id: str) -> set[str]:
        """Return content_hash values for all active entries."""
        path = self._state_dir(tenant_id) / "knowledge.json"
        raw = self._read_json(path, [])
        return {
            d["content_hash"]
            for d in raw
            if d.get("active", True) and d.get("content_hash")
        }

    async def get_knowledge_by_hash(
        self, tenant_id: str, content_hash: str
    ) -> KnowledgeEntry | None:
        """Find an active entry by content_hash."""
        path = self._state_dir(tenant_id) / "knowledge.json"
        raw = self._read_json(path, [])
        for d in raw:
            if d.get("content_hash") == content_hash and d.get("active", True):
                return _load_knowledge_entry(d)
        return None

    async def update_knowledge(self, tenant_id: str, entry_id: str, updates: dict) -> None:
        """Find and update a knowledge entry by ID, scoped to the given tenant."""
        path = self._state_dir(tenant_id) / "knowledge.json"
        if not path.exists():
            logger.warning("update_knowledge: no knowledge file for tenant %s", tenant_id)
            return
        raw = self._read_json(path, [])
        for i, d in enumerate(raw):
            if d.get("id") == entry_id:
                raw[i].update(updates)
                self._write_json(path, raw)
                return
        logger.warning("update_knowledge: entry_id %s not found for tenant %s", entry_id, tenant_id)

    # -----------------------------------------------------------------------
    # Behavioral Contracts (CovenantRule)
    # -----------------------------------------------------------------------

    _COVENANT_MIGRATIONS: list[tuple[str, str]] = [
        (
            "Never send messages to external contacts without owner approval",
            "Never send messages to third-party contacts unless the owner initiated the request",
        ),
        (
            "Always confirm before sending communications to THIRD PARTIES on the owner's behalf. Reminders and notifications TO the owner are always authorized.",
            "For composed messages to third parties, show the draft before sending. For simple relays, briefly confirm content and recipient. Owner-directed delivery to connected channels needs no confirmation.",
        ),
    ]

    async def get_contract_rules(
        self,
        tenant_id: str,
        capability: str | None = None,
        rule_type: str | None = None,
        active_only: bool = True,
    ) -> list[CovenantRule]:
        path = self._state_dir(tenant_id) / "contracts.json"
        raw = self._read_json(path, [])
        # One-time migration: update old default covenant wording
        migrated = False
        for d in raw:
            for old_text, new_text in self._COVENANT_MIGRATIONS:
                if d.get("description") == old_text and d.get("source") == "default":
                    d["description"] = new_text
                    migrated = True
        if migrated:
            self._write_json(path, raw)
            logger.info("COVENANT_MIGRATE: tenant=%s migrated default covenant wording", tenant_id)
        results: list[CovenantRule] = []
        for d in raw:
            rule = _load_covenant_rule(d)
            if active_only and not rule.active:
                continue
            if active_only and rule.superseded_by:
                continue
            if capability and rule.capability != capability:
                continue
            if rule_type and rule.rule_type != rule_type:
                continue
            results.append(rule)
        return results

    async def query_covenant_rules(
        self,
        tenant_id: str,
        capability: str | None = None,
        context_space_scope: list[str | None] | None = None,
        active_only: bool = True,
    ) -> list[CovenantRule]:
        path = self._state_dir(tenant_id) / "contracts.json"
        raw = self._read_json(path, [])
        results: list[CovenantRule] = []
        for d in raw:
            rule = _load_covenant_rule(d)
            if active_only and not rule.active:
                continue
            if active_only and rule.superseded_by:
                continue
            if capability and rule.capability not in (capability, "general"):
                continue
            if context_space_scope is not None:
                if rule.context_space not in context_space_scope:
                    continue
            results.append(rule)
        return results

    async def add_contract_rule(self, rule: CovenantRule) -> None:
        path = self._state_dir(rule.tenant_id) / "contracts.json"
        rules = self._read_json(path, [])
        rules.append(asdict(rule))
        self._write_json(path, rules)

    async def update_contract_rule(self, tenant_id: str, rule_id: str, updates: dict) -> None:
        """Find and update a contract rule by ID, scoped to the given tenant."""
        path = self._state_dir(tenant_id) / "contracts.json"
        if not path.exists():
            logger.warning("update_contract_rule: no contracts file for tenant %s", tenant_id)
            return
        raw = self._read_json(path, [])
        for i, d in enumerate(raw):
            if d.get("id") == rule_id:
                raw[i].update(updates)
                self._write_json(path, raw)
                return
        logger.warning("update_contract_rule: rule_id %s not found for tenant %s", rule_id, tenant_id)

    # -----------------------------------------------------------------------
    # Entity Resolution (basic CRUD — Phase 2A adds real logic)
    # -----------------------------------------------------------------------

    async def save_entity_node(self, node: EntityNode) -> None:
        path = self._state_dir(node.tenant_id) / "entities.json"
        raw = self._read_json(path, [])
        for i, d in enumerate(raw):
            if d.get("id") == node.id:
                raw[i] = asdict(node)
                self._write_json(path, raw)
                return
        raw.append(asdict(node))
        self._write_json(path, raw)

    async def get_entity_node(self, tenant_id: str, entity_id: str) -> EntityNode | None:
        path = self._state_dir(tenant_id) / "entities.json"
        raw = self._read_json(path, [])
        for d in raw:
            if d.get("id") == entity_id:
                return EntityNode(**d)
        return None

    async def query_entity_nodes(
        self,
        tenant_id: str,
        name: str | None = None,
        entity_type: str | None = None,
        active_only: bool = True,
    ) -> list[EntityNode]:
        path = self._state_dir(tenant_id) / "entities.json"
        raw = self._read_json(path, [])
        results: list[EntityNode] = []
        for d in raw:
            node = EntityNode(**d)
            if active_only and not node.active:
                continue
            if entity_type and node.entity_type != entity_type:
                continue
            if name:
                name_lower = name.lower()
                match = (
                    name_lower in node.canonical_name.lower()
                    or any(name_lower in a.lower() for a in node.aliases)
                )
                if not match:
                    continue
            results.append(node)
        return results

    async def save_identity_edge(self, tenant_id: str, edge: IdentityEdge) -> None:
        path = self._state_dir(tenant_id) / "identity_edges.json"
        raw = self._read_json(path, [])
        key = (edge.source_id, edge.target_id)
        for i, d in enumerate(raw):
            if (d.get("source_id"), d.get("target_id")) == key:
                raw[i] = asdict(edge)
                self._write_json(path, raw)
                return
        raw.append(asdict(edge))
        self._write_json(path, raw)

    async def query_identity_edges(
        self, tenant_id: str, entity_id: str
    ) -> list[IdentityEdge]:
        path = self._state_dir(tenant_id) / "identity_edges.json"
        raw = self._read_json(path, [])
        results = []
        for d in raw:
            if d.get("source_id") == entity_id or d.get("target_id") == entity_id:
                results.append(IdentityEdge(**d))
        return results

    # -----------------------------------------------------------------------
    # Pending Actions (Phase 2B Dispatch Interceptor)
    # -----------------------------------------------------------------------

    async def save_pending_action(self, action: PendingAction) -> None:
        path = self._state_dir(action.tenant_id) / "pending_actions.json"
        raw = self._read_json(path, [])
        for i, d in enumerate(raw):
            if d.get("id") == action.id:
                raw[i] = asdict(action)
                self._write_json(path, raw)
                return
        raw.append(asdict(action))
        self._write_json(path, raw)

    async def get_pending_actions(
        self, tenant_id: str, status: str = "pending"
    ) -> list[PendingAction]:
        path = self._state_dir(tenant_id) / "pending_actions.json"
        raw = self._read_json(path, [])
        return [
            PendingAction(**d)
            for d in raw
            if d.get("status") == status
        ]

    async def update_pending_action(
        self, tenant_id: str, action_id: str, updates: dict
    ) -> None:
        path = self._state_dir(tenant_id) / "pending_actions.json"
        if not path.exists():
            logger.warning("update_pending_action: no file for tenant %s", tenant_id)
            return
        raw = self._read_json(path, [])
        for i, d in enumerate(raw):
            if d.get("id") == action_id:
                raw[i].update(updates)
                self._write_json(path, raw)
                return
        logger.warning("update_pending_action: id %s not found for tenant %s", action_id, tenant_id)

    # -----------------------------------------------------------------------
    # Context Spaces
    # -----------------------------------------------------------------------

    async def save_context_space(self, space: ContextSpace) -> None:
        path = self._state_dir(space.tenant_id) / "spaces.json"
        raw = self._read_json(path, [])
        for i, d in enumerate(raw):
            if d.get("id") == space.id:
                raw[i] = asdict(space)
                self._write_json(path, raw)
                return
        raw.append(asdict(space))
        self._write_json(path, raw)

    async def get_context_space(
        self, tenant_id: str, space_id: str
    ) -> ContextSpace | None:
        path = self._state_dir(tenant_id) / "spaces.json"
        raw = self._read_json(path, [])
        for d in raw:
            if d.get("id") == space_id:
                return _load_context_space(d)
        return None

    async def list_context_spaces(self, tenant_id: str) -> list[ContextSpace]:
        path = self._state_dir(tenant_id) / "spaces.json"
        raw = self._read_json(path, [])
        return [_load_context_space(d) for d in raw]

    async def update_context_space(
        self, tenant_id: str, space_id: str, updates: dict
    ) -> None:
        path = self._state_dir(tenant_id) / "spaces.json"
        if not path.exists():
            logger.warning("update_context_space: no file for tenant %s", tenant_id)
            return
        raw = self._read_json(path, [])
        for i, d in enumerate(raw):
            if d.get("id") == space_id:
                raw[i].update(updates)
                self._write_json(path, raw)
                return
        logger.warning("update_context_space: id %s not found for tenant %s", space_id, tenant_id)

    # -----------------------------------------------------------------------
    # Topic Hints (Gate 1 space creation)
    # -----------------------------------------------------------------------

    def _topic_hints_path(self, tenant_id: str) -> Path:
        return self._state_dir(tenant_id) / "topic_hints.json"

    async def increment_topic_hint(self, tenant_id: str, hint: str) -> None:
        path = self._topic_hints_path(tenant_id)
        hints = self._read_json(path, {})
        hints[hint] = hints.get(hint, 0) + 1
        self._write_json(path, hints)

    async def get_topic_hint_count(self, tenant_id: str, hint: str) -> int:
        path = self._topic_hints_path(tenant_id)
        hints = self._read_json(path, {})
        return hints.get(hint, 0)

    async def clear_topic_hint(self, tenant_id: str, hint: str) -> None:
        path = self._topic_hints_path(tenant_id)
        hints = self._read_json(path, {})
        hints.pop(hint, None)
        self._write_json(path, hints)

    # -----------------------------------------------------------------------
    # Knowledge Foresight Query (Phase 3C)
    # -----------------------------------------------------------------------

    async def query_knowledge_by_foresight(
        self,
        tenant_id: str,
        expires_before: str,
        expires_after: str = "",
        space_id: str = "",
    ) -> "list[KnowledgeEntry]":
        path = self._state_dir(tenant_id) / "knowledge.json"
        raw = self._read_json(path, [])
        results = []
        for d in raw:
            entry = _load_knowledge_entry(d)
            if not entry.active:
                continue
            if not entry.foresight_signal or not entry.foresight_expires:
                continue
            if space_id and entry.context_space != space_id:
                continue
            # foresight_expires must be in (expires_after, expires_before]
            fe = entry.foresight_expires
            if expires_after and fe <= expires_after:
                continue
            if fe > expires_before:
                continue
            results.append(entry)
        return results

    # -----------------------------------------------------------------------
    # Whispers and Suppressions (Phase 3C)
    # -----------------------------------------------------------------------

    def _awareness_dir(self, tenant_id: str) -> Path:
        return self._data_dir / _safe_name(tenant_id) / "awareness"

    async def save_whisper(self, tenant_id: str, whisper) -> None:
        from kernos.kernel.awareness import Whisper
        from dataclasses import asdict as _asdict
        path = self._awareness_dir(tenant_id) / "whispers.json"
        raw = self._read_json(path, [])
        # Upsert by whisper_id
        for i, d in enumerate(raw):
            if d.get("whisper_id") == whisper.whisper_id:
                raw[i] = _asdict(whisper)
                self._write_json(path, raw)
                return
        raw.append(_asdict(whisper))
        self._write_json(path, raw)

    async def get_pending_whispers(self, tenant_id: str) -> list:
        from kernos.kernel.awareness import Whisper
        path = self._awareness_dir(tenant_id) / "whispers.json"
        raw = self._read_json(path, [])
        return [
            Whisper(**d)
            for d in raw
            if not d.get("surfaced_at")
        ]

    async def mark_whisper_surfaced(self, tenant_id: str, whisper_id: str) -> None:
        from datetime import datetime, timezone
        path = self._awareness_dir(tenant_id) / "whispers.json"
        raw = self._read_json(path, [])
        for i, d in enumerate(raw):
            if d.get("whisper_id") == whisper_id:
                raw[i]["surfaced_at"] = datetime.now(timezone.utc).isoformat()
                self._write_json(path, raw)
                return

    async def delete_whisper(self, tenant_id: str, whisper_id: str) -> None:
        path = self._awareness_dir(tenant_id) / "whispers.json"
        raw = self._read_json(path, [])
        raw = [d for d in raw if d.get("whisper_id") != whisper_id]
        self._write_json(path, raw)

    async def save_suppression(self, tenant_id: str, entry) -> None:
        from kernos.kernel.awareness import SuppressionEntry
        from dataclasses import asdict as _asdict
        path = self._awareness_dir(tenant_id) / "suppressions.json"
        raw = self._read_json(path, [])
        # Upsert by whisper_id
        for i, d in enumerate(raw):
            if d.get("whisper_id") == entry.whisper_id:
                raw[i] = _asdict(entry)
                self._write_json(path, raw)
                return
        raw.append(_asdict(entry))
        self._write_json(path, raw)

    async def get_suppressions(
        self,
        tenant_id: str,
        knowledge_entry_id: str = "",
        whisper_id: str = "",
        foresight_signal: str = "",
    ) -> list:
        from kernos.kernel.awareness import SuppressionEntry
        path = self._awareness_dir(tenant_id) / "suppressions.json"
        raw = self._read_json(path, [])
        results = []
        for d in raw:
            if knowledge_entry_id and d.get("knowledge_entry_id") != knowledge_entry_id:
                continue
            if whisper_id and d.get("whisper_id") != whisper_id:
                continue
            if foresight_signal and d.get("foresight_signal") != foresight_signal:
                continue
            results.append(SuppressionEntry(**d))
        return results

    async def delete_suppression(self, tenant_id: str, whisper_id: str) -> None:
        path = self._awareness_dir(tenant_id) / "suppressions.json"
        raw = self._read_json(path, [])
        raw = [d for d in raw if d.get("whisper_id") != whisper_id]
        self._write_json(path, raw)

    # -----------------------------------------------------------------------
    # Conversation Summaries
    # -----------------------------------------------------------------------

    async def get_conversation_summary(
        self, tenant_id: str, conversation_id: str
    ) -> ConversationSummary | None:
        path = self._state_dir(tenant_id) / "conversations.json"
        raw = self._read_json(path, [])
        for d in raw:
            if d.get("conversation_id") == conversation_id:
                return ConversationSummary(**d)
        return None

    async def save_conversation_summary(self, summary: ConversationSummary) -> None:
        path = self._state_dir(summary.tenant_id) / "conversations.json"
        raw = self._read_json(path, [])
        # Upsert: replace existing or append
        for i, d in enumerate(raw):
            if d.get("conversation_id") == summary.conversation_id:
                raw[i] = asdict(summary)
                self._write_json(path, raw)
                return
        raw.append(asdict(summary))
        self._write_json(path, raw)

    async def list_conversations(
        self, tenant_id: str, active_only: bool = True, limit: int = 20
    ) -> list[ConversationSummary]:
        path = self._state_dir(tenant_id) / "conversations.json"
        raw = self._read_json(path, [])
        results = [ConversationSummary(**d) for d in raw]
        if active_only:
            results = [c for c in results if c.active]
        # Most recent first
        results.sort(key=lambda c: c.last_message_at, reverse=True)
        return results[:limit]

    # --- Preferences (Phase 6A) ---

    def _preferences_path(self, tenant_id: str) -> Path:
        return self._state_dir(tenant_id) / "preferences.json"

    async def _maybe_migrate_preferences(self, tenant_id: str) -> None:
        """Lazy migration: convert category='preference' KnowledgeEntries to Preferences."""
        if tenant_id in self._preference_migration_done:
            return
        self._preference_migration_done.add(tenant_id)

        from kernos.kernel.state import generate_preference_id
        from kernos.utils import utc_now

        knowledge = await self.query_knowledge(tenant_id, category="preference")
        if not knowledge:
            return

        existing_prefs = self._load_preferences(tenant_id)
        # Skip if migration already happened (check for source_knowledge_id matches)
        existing_source_ids = {e.get("source_knowledge_id", "") for e in existing_prefs}

        migrated_count = 0
        for entry in knowledge:
            if entry.id in existing_source_ids:
                continue  # Already migrated
            pref = Preference(
                id=generate_preference_id(),
                tenant_id=tenant_id,
                intent=entry.content,
                category="general",
                subject=entry.subject,
                action="prefer",
                parameters={},
                scope="global" if not entry.context_space else entry.context_space,
                context_space=entry.context_space or "",
                status="active",
                created_at=entry.created_at,
                updated_at=utc_now(),
                source_knowledge_id=entry.id,
            )
            existing_prefs.append(asdict(pref))

            # Mark original as migrated (no destructive deletion)
            entry.category = "preference_migrated"
            await self.save_knowledge_entry(entry)
            migrated_count += 1

        if migrated_count:
            self._save_preferences(tenant_id, existing_prefs)
            logger.info(
                "PREF_MIGRATION: tenant=%s migrated=%d from KnowledgeEntry",
                tenant_id, migrated_count,
            )

    def _load_preferences(self, tenant_id: str) -> list[dict]:
        return self._read_json(self._preferences_path(tenant_id), [])

    def _save_preferences(self, tenant_id: str, data: list[dict]) -> None:
        path = self._preferences_path(tenant_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock = FileLock(str(path) + ".lock")
        with lock:
            path.write_text(json.dumps(data, indent=2))

    @staticmethod
    def _load_preference(d: dict) -> Preference:
        """Load a Preference from a dict, handling missing fields gracefully."""
        known_fields = {f.name for f in Preference.__dataclass_fields__.values()}
        filtered = {k: v for k, v in d.items() if k in known_fields}
        return Preference(**filtered)

    async def add_preference(self, pref: Preference) -> None:
        await self._maybe_migrate_preferences(pref.tenant_id)
        entries = self._load_preferences(pref.tenant_id)
        entries.append(asdict(pref))
        self._save_preferences(pref.tenant_id, entries)
        logger.info(
            "PREF_WRITE: id=%s action=ADD subject=%s category=%s tenant=%s",
            pref.id, pref.subject, pref.category, pref.tenant_id,
        )

    async def save_preference(self, pref: Preference) -> None:
        entries = self._load_preferences(pref.tenant_id)
        found = False
        for i, e in enumerate(entries):
            if e.get("id") == pref.id:
                entries[i] = asdict(pref)
                found = True
                break
        if not found:
            entries.append(asdict(pref))
        self._save_preferences(pref.tenant_id, entries)
        logger.info(
            "PREF_WRITE: id=%s action=%s subject=%s status=%s tenant=%s",
            pref.id, "UPDATE" if found else "ADD", pref.subject, pref.status, pref.tenant_id,
        )

    async def get_preference(self, tenant_id: str, pref_id: str) -> Preference | None:
        await self._maybe_migrate_preferences(tenant_id)
        entries = self._load_preferences(tenant_id)
        for e in entries:
            if e.get("id") == pref_id:
                return self._load_preference(e)
        return None

    async def query_preferences(
        self,
        tenant_id: str,
        status: str = "",
        subject: str = "",
        category: str = "",
        scope: str = "",
        active_only: bool = True,
    ) -> list[Preference]:
        await self._maybe_migrate_preferences(tenant_id)
        entries = self._load_preferences(tenant_id)
        results: list[Preference] = []
        for e in entries:
            if active_only and e.get("status", "active") != "active":
                continue
            if status and e.get("status") != status:
                continue
            if subject and e.get("subject") != subject:
                continue
            if category and e.get("category") != category:
                continue
            if scope and e.get("scope") != scope:
                continue
            results.append(self._load_preference(e))
        return results

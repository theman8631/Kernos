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
    """Load CovenantRule from dict, migrating enforcement_tier from rule_type if absent.

    Old contracts.json files don't have enforcement_tier — derive it from rule_type.
    """
    data = dict(d)
    if "enforcement_tier" not in data:
        data["enforcement_tier"] = _RULE_TYPE_TO_ENFORCEMENT_TIER.get(
            data.get("rule_type", ""), "confirm"
        )
    return CovenantRule(**data)


def _load_knowledge_entry(d: dict) -> KnowledgeEntry:
    """Load KnowledgeEntry from dict, applying durability → lifecycle_archetype migration.

    Old JSON files have `durability` but no `lifecycle_archetype`. New entries have
    `lifecycle_archetype` set. This function ensures both load correctly.
    """
    data = dict(d)
    if not data.get("lifecycle_archetype"):
        durability = data.get("durability", "")
        data["lifecycle_archetype"] = _durability_to_archetype(durability)
        # Also migrate foresight_expires from expires_at durability
        if (
            durability.startswith("expires_at:")
            and not data.get("foresight_expires")
        ):
            data["foresight_expires"] = durability[len("expires_at:"):]
    return KnowledgeEntry(**data)


# ---------------------------------------------------------------------------
# JsonStateStore
# ---------------------------------------------------------------------------


class JsonStateStore(StateStore):
    """JSON file-backed state store."""

    def __init__(self, data_dir: str | Path) -> None:
        self._data_dir = Path(data_dir)

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
        return Soul(**data)

    async def save_soul(self, soul: Soul) -> None:
        path = self._state_dir(soul.tenant_id) / "soul.json"
        self._write_json(path, asdict(soul))

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

    async def get_contract_rules(
        self,
        tenant_id: str,
        capability: str | None = None,
        rule_type: str | None = None,
        active_only: bool = True,
    ) -> list[CovenantRule]:
        path = self._state_dir(tenant_id) / "contracts.json"
        raw = self._read_json(path, [])
        results: list[CovenantRule] = []
        for d in raw:
            rule = _load_covenant_rule(d)
            if active_only and not rule.active:
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
                return ContextSpace(**d)
        return None

    async def list_context_spaces(self, tenant_id: str) -> list[ContextSpace]:
        path = self._state_dir(tenant_id) / "spaces.json"
        raw = self._read_json(path, [])
        return [ContextSpace(**d) for d in raw]

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

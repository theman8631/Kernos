"""JSON-on-disk implementation of StateStore.

Four files per tenant under {data_dir}/{tenant_id}/state/:
  profile.json       — TenantProfile (single object)
  knowledge.json     — list of KnowledgeEntry
  contracts.json     — list of ContractRule
  conversations.json — list of ConversationSummary

The interface abstracts the backend — a future MemOS or database integration
changes only this file.
"""
import json
import logging
from dataclasses import asdict
from pathlib import Path

from filelock import FileLock

from kernos.kernel.soul import Soul
from kernos.kernel.state import (
    ConversationSummary,
    ContractRule,
    KnowledgeEntry,
    StateStore,
    TenantProfile,
)

logger = logging.getLogger(__name__)


from kernos.utils import _safe_name


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
            entry = KnowledgeEntry(**d)
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
                return KnowledgeEntry(**d)
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
    # Behavioral Contracts
    # -----------------------------------------------------------------------

    async def get_contract_rules(
        self,
        tenant_id: str,
        capability: str | None = None,
        rule_type: str | None = None,
        active_only: bool = True,
    ) -> list[ContractRule]:
        path = self._state_dir(tenant_id) / "contracts.json"
        raw = self._read_json(path, [])
        results: list[ContractRule] = []
        for d in raw:
            rule = ContractRule(**d)
            if active_only and not rule.active:
                continue
            if capability and rule.capability != capability:
                continue
            if rule_type and rule.rule_type != rule_type:
                continue
            results.append(rule)
        return results

    async def add_contract_rule(self, rule: ContractRule) -> None:
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

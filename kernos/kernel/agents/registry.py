"""Agent registry — per-instance roster of routable agents.

DOMAIN-AGENT-REGISTRY C1: AgentRecord schema + persistence +
``get_by_id`` deterministic exact-id lookup. C2 adds the atomic
``register_agent`` flow + alias collision check. C3 adds
``resolve_natural`` + ranker fallback + default agents. C4 wires
``RouteToAgentAction`` and workflow registration through the
registry.

Design: AgentRecord is a **descriptor**, not a live ``AgentInbox``
instance. The runtime builds inboxes from descriptors at dispatch
time via a ``ProviderRegistry`` factory keyed on ``provider_key`` —
keeps the registry portable and testable, and makes the
"AgentInboxUnavailable" failure path generalize cleanly to
"agent_id not registered" / "provider_key not bound".

Multi-tenancy is **structural**: the primary key on ``agent_records``
is composite ``(instance_id, agent_id)``. Two instances may both
have an agent named ``spec-agent`` without collision — the same
workflow descriptor referencing ``spec-agent`` resolves to
different concrete agents per instance, which is what makes the
"same workflow installs across instances" story work.

Lifecycle (per spec, Kit edit v1 → v2): ``active`` is routable and
discoverable; ``paused`` is resolvable for audit/admin only
(``get_by_id`` returns the record but ``resolve_natural`` skips it
and dispatch raises ``AgentPaused``); ``retired`` is terminal —
not routable and not discoverable, but the row stays for audit
(no destructive deletions per standing principle).
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from kernos.kernel.agents.providers import ProviderRegistry

logger = logging.getLogger(__name__)


VALID_AGENT_STATUSES = frozenset({"active", "paused", "retired"})


# ---------------------------------------------------------------------------
# Typed errors (per AC #10 — RouteToAgentAction surfaces these)
# ---------------------------------------------------------------------------


class AgentRegistryError(RuntimeError):
    """Base for typed registry errors. Caller code may catch this
    common base when distinguishing registry failures from other
    runtime errors is enough."""


class AgentNotRegistered(AgentRegistryError):
    """Raised when an ``agent_id`` is not registered in the calling
    instance. Surfaces as a step_failed error in
    ``RouteToAgentAction.execute()`` and as a registration-time
    failure in ``register_workflow``."""

    def __init__(self, agent_id: str, instance_id: str = "") -> None:
        super().__init__(
            f"agent_id {agent_id!r} is not registered"
            + (f" in instance {instance_id!r}" if instance_id else "")
        )
        self.agent_id = agent_id
        self.instance_id = instance_id


class AgentPaused(AgentRegistryError):
    """Raised when an active dispatch (or a new workflow registration)
    targets a ``paused`` agent. Per Kit's v1 → v2 lifecycle
    clarification: paused means "don't send work" — the agent's
    pipeline is draining for reconfiguration."""

    def __init__(self, agent_id: str) -> None:
        super().__init__(f"agent {agent_id!r} is paused")
        self.agent_id = agent_id


class AgentRetired(AgentRegistryError):
    """Raised when a dispatch (or a new workflow registration)
    targets a ``retired`` agent. Terminal state per the no-
    destructive-deletions principle — the row stays for audit but
    no work routes through."""

    def __init__(self, agent_id: str) -> None:
        super().__init__(f"agent {agent_id!r} is retired")
        self.agent_id = agent_id


class AgentInboxProviderUnavailable(AgentRegistryError):
    """Raised when an agent's ``provider_key`` has no factory bound
    in the engine's ProviderRegistry. Surfaces from
    ``RouteToAgentAction.execute()`` — generalisation of WLP's
    ``AgentInboxUnavailable`` for the per-agent shape."""

    def __init__(self, agent_id: str, provider_key: str) -> None:
        super().__init__(
            f"agent {agent_id!r} declares provider_key {provider_key!r} "
            f"but no factory is bound for it"
        )
        self.agent_id = agent_id
        self.provider_key = provider_key


class AliasCollisionError(AgentRegistryError):
    """Raised at registration when one of the proposed aliases is
    already claimed by another active record in the same instance.
    Per Kit seam #2 — alias collisions fail closed at registration
    rather than silently overwriting."""

    def __init__(
        self, alias: str, conflicting_agent_id: str,
        attempting_agent_id: str,
    ) -> None:
        super().__init__(
            f"alias {alias!r} is already claimed by active agent "
            f"{conflicting_agent_id!r}; attempted to register "
            f"{attempting_agent_id!r}"
        )
        self.alias = alias
        self.conflicting_agent_id = conflicting_agent_id
        self.attempting_agent_id = attempting_agent_id


class InvalidAgentStatusTransition(AgentRegistryError):
    """Raised on ``update_status`` if the target status is unknown
    or the transition is structurally invalid (e.g. retired →
    active is forbidden — retired is terminal)."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class AgentRecord:
    """Descriptor record for a routable agent.

    Stored in SQLite; the runtime constructs concrete ``AgentInbox``
    instances from these descriptors at dispatch time via
    ``ProviderRegistry``. The descriptor itself is provider-neutral.
    """

    agent_id: str
    instance_id: str
    provider_key: str
    display_name: str = ""
    aliases: list[str] = field(default_factory=list)
    provider_config_ref: str = ""
    domain_summary: str = ""
    capabilities_summary: str = ""
    status: str = "active"
    version: int = 1
    created_at: str = ""

    def to_row(self) -> tuple:
        return (
            self.instance_id,
            self.agent_id,
            self.display_name,
            json.dumps(self.aliases),
            self.provider_key,
            self.provider_config_ref,
            self.domain_summary,
            self.capabilities_summary,
            self.status,
            self.version,
            self.created_at,
        )

    @classmethod
    def from_row(cls, row) -> "AgentRecord":
        try:
            aliases = json.loads(row["aliases"]) if row["aliases"] else []
        except Exception:
            aliases = []
        return cls(
            agent_id=row["agent_id"],
            instance_id=row["instance_id"],
            display_name=row["display_name"] or "",
            aliases=aliases,
            provider_key=row["provider_key"],
            provider_config_ref=row["provider_config_ref"] or "",
            domain_summary=row["domain_summary"] or "",
            capabilities_summary=row["capabilities_summary"] or "",
            status=row["status"],
            version=row["version"],
            created_at=row["created_at"],
        )


@dataclass
class DefaultAgentRecord:
    """One row in the ``default_agents`` table — maps a scoped
    lookup key to an ``agent_id`` for the conversational-routing
    fallback. Three-step priority chain on lookup (see C3
    ``resolve_natural``): space + domain → space-only → domain-only.
    """

    instance_id: str
    scope_kind: str   # "space_id" | "domain"
    scope_id: str = ""
    domain_label: str = ""
    agent_id: str = ""
    created_at: str = ""

    def to_row(self) -> tuple:
        return (
            self.instance_id,
            self.scope_kind,
            self.scope_id,
            self.domain_label,
            self.agent_id,
            self.created_at,
        )

    @classmethod
    def from_row(cls, row) -> "DefaultAgentRecord":
        return cls(
            instance_id=row["instance_id"],
            scope_kind=row["scope_kind"],
            scope_id=row["scope_id"] or "",
            domain_label=row["domain_label"] or "",
            agent_id=row["agent_id"],
            created_at=row["created_at"],
        )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_AGENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_records (
    instance_id           TEXT NOT NULL,
    agent_id              TEXT NOT NULL,
    display_name          TEXT DEFAULT '',
    aliases               TEXT NOT NULL DEFAULT '[]',
    provider_key          TEXT NOT NULL,
    provider_config_ref   TEXT DEFAULT '',
    domain_summary        TEXT DEFAULT '',
    capabilities_summary  TEXT DEFAULT '',
    status                TEXT NOT NULL DEFAULT 'active',
    version               INTEGER NOT NULL DEFAULT 1,
    created_at            TEXT NOT NULL,
    PRIMARY KEY (instance_id, agent_id)
);
CREATE INDEX IF NOT EXISTS idx_agent_records_active
    ON agent_records (instance_id, status);

CREATE TABLE IF NOT EXISTS default_agents (
    instance_id    TEXT NOT NULL,
    scope_kind     TEXT NOT NULL,
    scope_id       TEXT DEFAULT '',
    domain_label   TEXT DEFAULT '',
    agent_id       TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    PRIMARY KEY (instance_id, scope_kind, scope_id, domain_label)
);
"""


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    for stmt in _AGENT_SCHEMA.split(";"):
        stmt = stmt.strip()
        if stmt:
            await db.execute(stmt)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentRegistry:
    """Owns the ``agent_records`` and ``default_agents`` SQLite
    tables and the lookup surfaces for both ``route_to_agent`` and
    conversational routing.

    The registry stores descriptors only — concrete ``AgentInbox``
    instances are constructed at dispatch via the ``ProviderRegistry``
    that the action library / engine binds at startup.

    This is C1's slice: schema + persistence + ``get_by_id``.
    Atomic ``register_agent`` and ``resolve_natural`` ship in C2/C3.
    """

    def __init__(
        self,
        *,
        provider_registry: ProviderRegistry | None = None,
    ) -> None:
        self._db: aiosqlite.Connection | None = None
        self._db_path: Path | None = None
        self._lock = asyncio.Lock()
        self._provider_registry = provider_registry

    @property
    def provider_registry(self) -> ProviderRegistry | None:
        """The ProviderRegistry this AgentRegistry was constructed
        with (or None if construction skipped it). C2's
        ``register_agent`` validates ``provider_key`` against this;
        C4's RouteToAgentAction also reads it for dispatch-time
        construction."""
        return self._provider_registry

    async def start(self, data_dir: str) -> None:
        """Open the SQLite connection in autocommit mode and ensure
        the schema. Idempotent."""
        if self._db is not None:
            return
        self._db_path = Path(data_dir) / "instance.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None → autocommit mode; we manage
        # transactions explicitly when atomicity matters
        # (register_agent in C2). For C1's writes there is no
        # cross-row atomicity requirement — single INSERT
        # statements suffice.
        self._db = await aiosqlite.connect(
            str(self._db_path), isolation_level=None,
        )
        self._db.row_factory = aiosqlite.Row
        await _ensure_schema(self._db)

    async def stop(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # -- get_by_id ------------------------------------------------------

    async def get_by_id(
        self, agent_id: str, instance_id: str,
    ) -> AgentRecord | None:
        """Return the AgentRecord for the given ``(instance_id, agent_id)``
        composite key, regardless of status. Caller checks ``record.status``
        and handles ``paused`` / ``retired`` via typed errors per the
        Kit-edit v1 → v2 lifecycle clarification — the registry does
        not filter by status here."""
        if self._db is None:
            return None
        async with self._db.execute(
            "SELECT * FROM agent_records "
            "WHERE instance_id = ? AND agent_id = ?",
            (instance_id, agent_id),
        ) as cur:
            row = await cur.fetchone()
        return AgentRecord.from_row(row) if row else None

    async def list_agents(
        self, instance_id: str, *, status: str | None = None,
    ) -> list[AgentRecord]:
        """List agents for an instance, optionally filtered by status.
        Used by audit / admin surfaces. Cross-instance isolation
        enforced by the ``WHERE instance_id = ?`` clause — instance B
        never sees instance A's agents."""
        if self._db is None:
            return []
        if status is None:
            query = (
                "SELECT * FROM agent_records WHERE instance_id = ? "
                "ORDER BY created_at"
            )
            args: tuple = (instance_id,)
        else:
            query = (
                "SELECT * FROM agent_records "
                "WHERE instance_id = ? AND status = ? "
                "ORDER BY created_at"
            )
            args = (instance_id, status)
        async with self._db.execute(query, args) as cur:
            rows = await cur.fetchall()
        return [AgentRecord.from_row(r) for r in rows]

    # -- atomic register_agent + lifecycle (C2) ------------------------

    async def register_agent(self, record: AgentRecord) -> AgentRecord:
        """Validate + atomically persist a new ``AgentRecord``.

        Validation steps (in order, fail-fast):
          1. ``record.status`` is a known status value.
          2. ``record.provider_key`` is bound in the
             ProviderRegistry (if a ProviderRegistry was wired in
             at construction).
          3. None of ``record.aliases`` collide with an alias
             already claimed by another **active** record in the
             same instance.
          4. ``(instance_id, agent_id)`` is not already taken
             (caught by the SQLite composite-PK constraint).

        Atomicity: validations 1-3 run under ``self._lock`` so a
        concurrent registration cannot slip an alias in between
        the collision check and the INSERT. SQLite's composite-PK
        constraint is the backstop for step 4. Any failure leaves
        no partial state.
        """
        if self._db is None:
            raise RuntimeError("AgentRegistry not started")
        if record.status not in VALID_AGENT_STATUSES:
            raise InvalidAgentStatusTransition(
                f"unknown status {record.status!r}; "
                f"must be one of {sorted(VALID_AGENT_STATUSES)}"
            )
        if not record.provider_key:
            raise ValueError("provider_key is required on AgentRecord")
        if (
            self._provider_registry is not None
            and not self._provider_registry.has(record.provider_key)
        ):
            raise AgentInboxProviderUnavailable(
                record.agent_id, record.provider_key,
            )
        if not record.created_at:
            record.created_at = _now()
        async with self._lock:
            if record.aliases:
                conflict = await self._find_alias_conflict(
                    record.instance_id, record.aliases, record.agent_id,
                )
                if conflict is not None:
                    alias, owner_id = conflict
                    raise AliasCollisionError(alias, owner_id, record.agent_id)
            try:
                await self._db.execute(
                    "INSERT INTO agent_records ("
                    " instance_id, agent_id, display_name, aliases,"
                    " provider_key, provider_config_ref, domain_summary,"
                    " capabilities_summary, status, version, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    record.to_row(),
                )
            except aiosqlite.IntegrityError as exc:
                # Composite-PK collision — translate to a typed
                # registry error so callers don't see raw SQL.
                raise AgentRegistryError(
                    f"agent_id {record.agent_id!r} already registered "
                    f"in instance {record.instance_id!r}"
                ) from exc
        return record

    async def update_status(
        self, agent_id: str, instance_id: str, new_status: str,
    ) -> AgentRecord | None:
        """Transition an agent's lifecycle. ``retired`` is terminal —
        once retired, the agent cannot be reactivated. Other
        transitions (active ↔ paused, active → retired, paused →
        retired) are permitted."""
        if self._db is None:
            return None
        if new_status not in VALID_AGENT_STATUSES:
            raise InvalidAgentStatusTransition(
                f"unknown status {new_status!r}; "
                f"must be one of {sorted(VALID_AGENT_STATUSES)}"
            )
        current = await self.get_by_id(agent_id, instance_id)
        if current is None:
            raise AgentNotRegistered(agent_id, instance_id)
        if current.status == "retired" and new_status != "retired":
            raise InvalidAgentStatusTransition(
                f"agent {agent_id!r} is retired; transition to "
                f"{new_status!r} forbidden (retired is terminal)"
            )
        async with self._lock:
            await self._db.execute(
                "UPDATE agent_records SET status = ?, version = version + 1 "
                "WHERE instance_id = ? AND agent_id = ?",
                (new_status, instance_id, agent_id),
            )
        return await self.get_by_id(agent_id, instance_id)

    async def _find_alias_conflict(
        self, instance_id: str, aliases: list[str],
        attempting_agent_id: str,
    ) -> tuple[str, str] | None:
        """Walk active records in the instance; return
        (offending_alias, conflicting_agent_id) on the first
        collision found. Used by register_agent before persistence.
        """
        if self._db is None:
            return None
        proposed = set(aliases)
        async with self._db.execute(
            "SELECT agent_id, aliases FROM agent_records "
            "WHERE instance_id = ? AND status = 'active'",
            (instance_id,),
        ) as cur:
            rows = await cur.fetchall()
        for row in rows:
            if row["agent_id"] == attempting_agent_id:
                # Self-record (shouldn't happen here since we're
                # registering NEW, but guard anyway).
                continue
            try:
                claimed = set(json.loads(row["aliases"]) or [])
            except Exception:
                claimed = set()
            overlap = proposed & claimed
            if overlap:
                return (next(iter(overlap)), row["agent_id"])
        return None

    # -- internal helpers exposed for C3 / tests -----------------------

    async def _insert_record(self, record: AgentRecord) -> None:
        """Direct INSERT for C1 testing. C2's register_agent wraps
        this in the atomic flow + alias collision check."""
        if self._db is None:
            raise RuntimeError("AgentRegistry not started")
        if not record.created_at:
            record.created_at = _now()
        await self._db.execute(
            "INSERT INTO agent_records ("
            " instance_id, agent_id, display_name, aliases,"
            " provider_key, provider_config_ref, domain_summary,"
            " capabilities_summary, status, version, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            record.to_row(),
        )

    async def _insert_default(self, default: DefaultAgentRecord) -> None:
        """Direct INSERT for default_agents rows. Used by C3
        resolve_natural setup."""
        if self._db is None:
            raise RuntimeError("AgentRegistry not started")
        if not default.created_at:
            default.created_at = _now()
        await self._db.execute(
            "INSERT INTO default_agents ("
            " instance_id, scope_kind, scope_id, domain_label,"
            " agent_id, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            default.to_row(),
        )


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


async def _reset_for_tests(registry: AgentRegistry) -> None:
    """Stop the registry. Idempotent."""
    await registry.stop()


__all__ = [
    "AgentInboxProviderUnavailable",
    "AgentNotRegistered",
    "AgentPaused",
    "AgentRecord",
    "AgentRegistry",
    "AgentRegistryError",
    "AgentRetired",
    "AliasCollisionError",
    "DefaultAgentRecord",
    "InvalidAgentStatusTransition",
    "VALID_AGENT_STATUSES",
]

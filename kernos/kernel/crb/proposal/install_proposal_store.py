"""SQLite-backed durable store for InstallProposal rows.

Owns the ``install_proposals`` table. Composite uniqueness on
``(instance_id, correlation_id)`` catches duplicate proposal creation.
State-machine validation runs at the ``transition_state`` boundary
— illegal transitions raise :class:`InvalidStateTransition`.

Connection model: opens its own ``aiosqlite`` connection to
``instance.db``, separate from event_stream / WLP / etc per the same
isolation pattern. ``isolation_level=None`` so explicit BEGIN/COMMIT
transactions are caller-controlled.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from kernos.kernel.crb.proposal.install_proposal import (
    InstallProposal,
    PermittedTransitions,
    ProposalState,
    ResponseKind,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class InstallProposalStoreError(Exception):
    """Base for store-level errors."""


class UnknownProposal(InstallProposalStoreError):
    """No proposal row for the given ``proposal_id``."""


class DuplicateProposalCorrelation(InstallProposalStoreError):
    """A proposal already exists for the given
    ``(instance_id, correlation_id)``."""


class InvalidStateTransition(InstallProposalStoreError):
    """Requested transition not in :data:`PermittedTransitions`."""


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


_INSTALL_PROPOSALS_DDL = """
CREATE TABLE IF NOT EXISTS install_proposals (
    proposal_id        TEXT NOT NULL PRIMARY KEY,
    correlation_id     TEXT NOT NULL,
    instance_id        TEXT NOT NULL,
    draft_id           TEXT NOT NULL,
    descriptor_hash    TEXT NOT NULL,
    state              TEXT NOT NULL,
    proposal_text      TEXT NOT NULL,
    member_id          TEXT NOT NULL,
    source_thread_id   TEXT NOT NULL,
    prev_workflow_id   TEXT,
    prev_proposal_id   TEXT,
    authored_at        TEXT NOT NULL,
    surfaced_at        TEXT,
    responded_at       TEXT,
    response_kind      TEXT,
    approval_event_id  TEXT,
    expires_at         TEXT,
    metadata           TEXT,
    CHECK (state IN (
        'proposed', 'approved_pending_registration',
        'approved_registered', 'modify_requested', 'declined'
    ))
)
"""


_INSTALL_PROPOSALS_CORRELATION_INDEX = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_install_proposals_correlation
    ON install_proposals (instance_id, correlation_id)
"""

_INSTALL_PROPOSALS_STATE_INDEX = """
CREATE INDEX IF NOT EXISTS idx_install_proposals_state
    ON install_proposals (instance_id, state)
"""

_INSTALL_PROPOSALS_DRAFT_INDEX = """
CREATE INDEX IF NOT EXISTS idx_install_proposals_draft
    ON install_proposals (instance_id, draft_id, state)
"""


async def _ensure_schema(db: aiosqlite.Connection) -> None:
    await db.execute(_INSTALL_PROPOSALS_DDL)
    await db.execute(_INSTALL_PROPOSALS_CORRELATION_INDEX)
    await db.execute(_INSTALL_PROPOSALS_STATE_INDEX)
    await db.execute(_INSTALL_PROPOSALS_DRAFT_INDEX)
    await db.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_proposal(row) -> InstallProposal:
    metadata: dict
    try:
        metadata = json.loads(row["metadata"]) if row["metadata"] else {}
    except Exception:
        metadata = {}
    return InstallProposal(
        proposal_id=row["proposal_id"],
        correlation_id=row["correlation_id"],
        instance_id=row["instance_id"],
        draft_id=row["draft_id"],
        descriptor_hash=row["descriptor_hash"],
        state=row["state"],
        proposal_text=row["proposal_text"],
        member_id=row["member_id"],
        source_thread_id=row["source_thread_id"],
        prev_workflow_id=row["prev_workflow_id"],
        prev_proposal_id=row["prev_proposal_id"],
        authored_at=row["authored_at"],
        surfaced_at=row["surfaced_at"],
        responded_at=row["responded_at"],
        response_kind=row["response_kind"],
        approval_event_id=row["approval_event_id"],
        expires_at=row["expires_at"],
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class InstallProposalStore:
    """Durable in-flight proposal state.

    Composite uniqueness on ``(instance_id, correlation_id)`` catches
    duplicate proposal creation. State-machine validation enforced at
    the ``transition_state`` boundary.
    """

    def __init__(self) -> None:
        self._db: aiosqlite.Connection | None = None
        self._db_path: Path | None = None
        self._lock = asyncio.Lock()

    async def start(self, data_dir: str) -> None:
        if self._db is not None:
            return
        self._db_path = Path(data_dir) / "instance.db"
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(
            str(self._db_path), isolation_level=None,
        )
        self._db.row_factory = aiosqlite.Row
        await _ensure_schema(self._db)

    async def stop(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    # -- create -----------------------------------------------------------

    async def create_proposal(
        self,
        *,
        instance_id: str,
        correlation_id: str,
        draft_id: str,
        descriptor_hash: str,
        proposal_text: str,
        member_id: str,
        source_thread_id: str,
        prev_workflow_id: str | None = None,
        prev_proposal_id: str | None = None,
        expires_at: str | None = None,
        metadata: dict | None = None,
    ) -> InstallProposal:
        if self._db is None:
            raise RuntimeError("InstallProposalStore not started")
        if not instance_id:
            raise ValueError("instance_id is required")
        if not correlation_id:
            raise ValueError("correlation_id is required")
        if not draft_id:
            raise ValueError("draft_id is required")
        if not descriptor_hash:
            raise ValueError("descriptor_hash is required")
        if not proposal_text:
            raise ValueError("proposal_text is required")

        proposal_id = f"prop-{uuid.uuid4().hex[:12]}"
        authored_at = _now()
        try:
            await self._db.execute(
                "INSERT INTO install_proposals "
                "(proposal_id, correlation_id, instance_id, draft_id, "
                " descriptor_hash, state, proposal_text, member_id, "
                " source_thread_id, prev_workflow_id, prev_proposal_id, "
                " authored_at, expires_at, metadata) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    proposal_id, correlation_id, instance_id, draft_id,
                    descriptor_hash, "proposed", proposal_text, member_id,
                    source_thread_id, prev_workflow_id, prev_proposal_id,
                    authored_at, expires_at,
                    json.dumps(metadata or {}),
                ),
            )
        except aiosqlite.IntegrityError as exc:
            msg = str(exc)
            if "idx_install_proposals_correlation" in msg or (
                "instance_id" in msg and "correlation_id" in msg
                and "UNIQUE" in msg.upper()
            ):
                raise DuplicateProposalCorrelation(
                    f"a proposal already exists for "
                    f"(instance_id={instance_id!r}, "
                    f"correlation_id={correlation_id!r})"
                ) from exc
            raise

        return InstallProposal(
            proposal_id=proposal_id,
            correlation_id=correlation_id,
            instance_id=instance_id,
            draft_id=draft_id,
            descriptor_hash=descriptor_hash,
            state="proposed",
            proposal_text=proposal_text,
            member_id=member_id,
            source_thread_id=source_thread_id,
            prev_workflow_id=prev_workflow_id,
            prev_proposal_id=prev_proposal_id,
            authored_at=authored_at,
            expires_at=expires_at,
            metadata=metadata or {},
        )

    # -- reads ------------------------------------------------------------

    async def get_proposal(self, *, proposal_id: str) -> InstallProposal | None:
        if self._db is None:
            return None
        async with self._db.execute(
            "SELECT * FROM install_proposals WHERE proposal_id = ?",
            (proposal_id,),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_proposal(row) if row else None

    async def find_by_correlation(
        self, *, instance_id: str, correlation_id: str,
    ) -> InstallProposal | None:
        if self._db is None:
            return None
        async with self._db.execute(
            "SELECT * FROM install_proposals "
            "WHERE instance_id = ? AND correlation_id = ?",
            (instance_id, correlation_id),
        ) as cur:
            row = await cur.fetchone()
        return _row_to_proposal(row) if row else None

    async def find_active_by_draft(
        self, *, instance_id: str, draft_id: str,
    ) -> list[InstallProposal]:
        """Active = non-terminal. Useful for "is there already an
        outstanding proposal for this draft?" checks."""
        if self._db is None:
            return []
        async with self._db.execute(
            "SELECT * FROM install_proposals "
            "WHERE instance_id = ? AND draft_id = ? "
            "AND state IN ('proposed', 'approved_pending_registration') "
            "ORDER BY authored_at DESC",
            (instance_id, draft_id),
        ) as cur:
            rows = await cur.fetchall()
        return [_row_to_proposal(r) for r in rows]

    async def find_by_state(
        self, *, instance_id: str | None = None, state: ProposalState,
    ) -> list[InstallProposal]:
        """Lookup by state. ``instance_id=None`` returns rows across
        all instances — used by the engine-startup recovery sweep."""
        if self._db is None:
            return []
        if instance_id is None:
            query = (
                "SELECT * FROM install_proposals WHERE state = ? "
                "ORDER BY authored_at"
            )
            args: tuple = (state,)
        else:
            query = (
                "SELECT * FROM install_proposals "
                "WHERE instance_id = ? AND state = ? "
                "ORDER BY authored_at"
            )
            args = (instance_id, state)
        async with self._db.execute(query, args) as cur:
            rows = await cur.fetchall()
        return [_row_to_proposal(r) for r in rows]

    # -- mutations --------------------------------------------------------

    async def transition_state(
        self,
        *,
        proposal_id: str,
        new_state: ProposalState,
        response_kind: ResponseKind | None = None,
        approval_event_id: str | None = None,
    ) -> InstallProposal:
        """Atomic state transition. Validates against
        :data:`PermittedTransitions`."""
        if self._db is None:
            raise RuntimeError("InstallProposalStore not started")

        async with self._lock:
            current = await self.get_proposal(proposal_id=proposal_id)
            if current is None:
                raise UnknownProposal(
                    f"no proposal with proposal_id={proposal_id!r}"
                )
            permitted = PermittedTransitions.get(current.state, frozenset())
            if new_state not in permitted:
                raise InvalidStateTransition(
                    f"cannot transition from {current.state!r} to "
                    f"{new_state!r}; permitted from {current.state!r}: "
                    f"{sorted(permitted)}"
                )
            now = _now()
            # Set responded_at when entering a terminal-or-pending state
            # via a user response. The recovery-sweep transition from
            # pending to registered is NOT a user response, so leave
            # responded_at alone in that path.
            assignments = ["state = ?"]
            params: list = [new_state]
            if response_kind is not None:
                assignments.append("response_kind = ?")
                params.append(response_kind)
                assignments.append("responded_at = ?")
                params.append(now)
            if approval_event_id is not None:
                assignments.append("approval_event_id = ?")
                params.append(approval_event_id)
            params.append(proposal_id)
            await self._db.execute(
                f"UPDATE install_proposals SET {', '.join(assignments)} "
                f"WHERE proposal_id = ?",
                tuple(params),
            )
            updated = await self.get_proposal(proposal_id=proposal_id)
            assert updated is not None
            return updated

    async def mark_surfaced(
        self, *, proposal_id: str, surfaced_at: str | None = None,
    ) -> InstallProposal:
        """Record that the proposal was surfaced to the user. Idempotent
        — a second mark just overwrites the timestamp."""
        if self._db is None:
            raise RuntimeError("InstallProposalStore not started")
        ts = surfaced_at or _now()
        await self._db.execute(
            "UPDATE install_proposals SET surfaced_at = ? "
            "WHERE proposal_id = ?",
            (ts, proposal_id),
        )
        updated = await self.get_proposal(proposal_id=proposal_id)
        if updated is None:
            raise UnknownProposal(
                f"no proposal with proposal_id={proposal_id!r}"
            )
        return updated


__all__ = [
    "DuplicateProposalCorrelation",
    "InstallProposalStore",
    "InstallProposalStoreError",
    "InvalidStateTransition",
    "UnknownProposal",
]

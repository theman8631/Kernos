"""Thin proxy: STS.list_agents -> AgentRegistry (DAR).

DAR already enforces instance scoping via the ``WHERE instance_id = ?``
clause inside ``AgentRegistry.list_agents``; STS adds keyword-only
discipline to keep the cohort-facing surface uniform.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from kernos.kernel.agents.registry import AgentRecord, AgentRegistry


async def list_agents(
    registry: "AgentRegistry",
    *,
    instance_id: str,
    status_filter: str | None = None,
) -> "list[AgentRecord]":
    if not instance_id:
        raise ValueError("instance_id is required")
    return await registry.list_agents(instance_id, status=status_filter)


__all__ = ["list_agents"]

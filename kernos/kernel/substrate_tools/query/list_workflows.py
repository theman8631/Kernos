"""Thin proxy: STS.list_workflows -> WorkflowRegistry (WLP).

WLP enforces ``WHERE instance_id = ?`` natively. STS adds the optional
``home_space_id`` filter on top by reading ``Workflow.metadata`` —
workflows that don't carry a ``home_space_id`` in metadata simply do
not match a non-None filter.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from kernos.kernel.workflows.workflow_registry import (
        Workflow,
        WorkflowRegistry,
    )


async def list_workflows(
    registry: "WorkflowRegistry",
    *,
    instance_id: str,
    status_filter: str | None = None,
    home_space_id: str | None = None,
) -> "list[Workflow]":
    if not instance_id:
        raise ValueError("instance_id is required")
    workflows = await registry.list_workflows(instance_id, status=status_filter)
    if home_space_id is None:
        return workflows
    return [
        wf for wf in workflows
        if (wf.metadata or {}).get("home_space_id") == home_space_id
    ]


__all__ = ["list_workflows"]

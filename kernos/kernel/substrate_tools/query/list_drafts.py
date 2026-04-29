"""Thin proxy: STS.list_drafts -> DraftRegistry (WDP).

WDP already supports ``instance_id`` + ``status`` + ``home_space_id`` +
``include_terminal`` natively; STS forwards all four under the
keyword-only discipline.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from kernos.kernel.drafts.registry import DraftRegistry, WorkflowDraft


async def list_drafts(
    registry: "DraftRegistry",
    *,
    instance_id: str,
    status_filter: str | None = None,
    home_space_id: str | None = None,
    include_terminal: bool = False,
) -> "list[WorkflowDraft]":
    if not instance_id:
        raise ValueError("instance_id is required")
    return await registry.list_drafts(
        instance_id=instance_id,
        status=status_filter,
        home_space_id=home_space_id,
        include_terminal=include_terminal,
    )


__all__ = ["list_drafts"]

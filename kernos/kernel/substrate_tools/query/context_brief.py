"""ContextBriefRegistry — neutral dispatch over context refs.

A :class:`ContextRef` names some shaping context (a space, a domain,
later a canvas, a tool, anything pluggable). The registry resolves a
ref to a :class:`ContextBrief` summary the cohort can read without
understanding the underlying surface. v1 ships ``"space"`` and
``"domain"`` resolvers; future ref types register their own resolvers
without modifying STS.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Awaitable, Callable


@dataclass(frozen=True)
class ContextRef:
    """Pointer to a shaping context. ``type`` is the ref category
    (``"space"``, ``"domain"``, future: ``"canvas"``, ``"tool"``, ...);
    ``id`` is the type-specific identifier."""

    type: str
    id: str

    def __post_init__(self) -> None:
        if not self.type:
            raise ValueError("ContextRef.type is required")
        if not self.id:
            raise ValueError("ContextRef.id is required")


@dataclass(frozen=True)
class ContextBrief:
    """A short, neutral summary of a context resolved through the
    registry. ``capability_hints`` are namespaced ``domain.action`` tags
    that hint at what this context can use or needs."""

    ref: ContextRef
    summary: str
    capability_hints: tuple[str, ...] = ()
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if isinstance(self.capability_hints, list):
            object.__setattr__(self, "capability_hints", tuple(self.capability_hints))


# A resolver returns a ContextBrief or None when the ref does not
# resolve in this instance. Sync or async are both acceptable; the
# registry awaits the result if it is a coroutine.
ContextResolver = Callable[
    [str, str],
    "ContextBrief | None | Awaitable[ContextBrief | None]",
]


class ContextBriefRegistry:
    """Dispatches :meth:`SubstrateTools.query_context_brief` based on
    :attr:`ContextRef.type`. Resolvers are registered once at engine
    bring-up; STS itself is unchanged when new ref types arrive."""

    def __init__(self) -> None:
        self._resolvers: dict[str, ContextResolver] = {}

    def register_resolver(
        self, ref_type: str, resolver: ContextResolver,
    ) -> None:
        if not ref_type:
            raise ValueError("ref_type is required")
        if ref_type in self._resolvers:
            raise ValueError(
                f"ref_type {ref_type!r} already has a resolver; "
                f"unregister_resolver() first to replace"
            )
        self._resolvers[ref_type] = resolver

    def unregister_resolver(self, ref_type: str) -> bool:
        return self._resolvers.pop(ref_type, None) is not None

    def known_ref_types(self) -> tuple[str, ...]:
        return tuple(self._resolvers.keys())

    async def resolve(
        self, *, instance_id: str, ref: ContextRef,
    ) -> ContextBrief | None:
        if not instance_id:
            raise ValueError("instance_id is required")
        resolver = self._resolvers.get(ref.type)
        if resolver is None:
            return None
        result = resolver(instance_id, ref.id)
        if inspect.isawaitable(result):
            result = await result  # type: ignore[assignment]
        return result  # type: ignore[return-value]


__all__ = [
    "ContextBrief",
    "ContextBriefRegistry",
    "ContextRef",
    "ContextResolver",
]

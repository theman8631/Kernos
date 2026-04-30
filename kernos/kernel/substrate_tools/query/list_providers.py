"""Provider type registry + ProviderRecord aggregation.

STS treats AgentInbox concretes as the FIRST adapter, not the ontology.
Future provider types (canvas providers, tool providers) register via
:meth:`ProviderRegistry.register_provider_type` without STS knowing
internals.

Capability tags are namespaced ``domain.action`` strings (e.g.
``email.send``, ``calendar.read``). The format is enforced by
:func:`validate_capability_tag`; :class:`ProviderRecord` validates its
own tags at construction time so an invalid tag never makes it into
aggregated results.
"""
from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from kernos.kernel.substrate_tools.errors import SubstrateToolsError


_CAPABILITY_TAG_REGEX = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")


class InvalidCapabilityTagFormat(SubstrateToolsError):
    """Raised when a capability tag does not match the namespaced
    ``domain.action`` format. Both ``domain`` and ``action`` segments
    must start with a lowercase letter and contain only lowercase
    letters, digits, and underscores."""


def validate_capability_tag(tag: str) -> None:
    """Raise :class:`InvalidCapabilityTagFormat` if ``tag`` does not
    match ``^[a-z][a-z0-9_]*\\.[a-z][a-z0-9_]*$``.

    Uses ``fullmatch`` so a trailing newline cannot smuggle past the
    regex — ``re.match`` would otherwise accept ``"email.send\\n"``."""
    if not isinstance(tag, str) or not _CAPABILITY_TAG_REGEX.fullmatch(tag):
        raise InvalidCapabilityTagFormat(
            f"capability tag {tag!r} does not match domain.action format "
            f"(lowercase, alphanumeric+underscore, exactly one dot)"
        )


@dataclass(frozen=True)
class Issue:
    """A descriptor-validation finding (used by DryRunResult in C2;
    surfaced here for stable import paths from C1 onward)."""

    severity: str            # "error" | "warning" | "info"
    code: str                # typed: "missing_trigger", "unknown_agent", ...
    message: str
    path: str | None = None  # JSONPath into descriptor
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CapabilityGap:
    """A required capability that no registered provider satisfies."""

    required_tag: str
    severity: str                          # "error" | "warning"
    suggested_resolution: str | None = None


@dataclass(frozen=True)
class ProviderRecord:
    """Aggregated capability source. Validates capability_tags at
    construction so invalid tags fail loudly rather than propagate."""

    provider_id: str
    provider_type: str
    capability_tags: tuple[str, ...] = ()
    status: str = "connected"     # "connected" | "missing" | "expired" | "misconfigured"
    metadata: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.provider_id:
            raise ValueError("provider_id is required")
        if not self.provider_type:
            raise ValueError("provider_type is required")
        # Tuples make the dataclass hashable/frozen-friendly. Accept
        # list at construction to ease ergonomics.
        if isinstance(self.capability_tags, list):
            object.__setattr__(self, "capability_tags", tuple(self.capability_tags))
        for tag in self.capability_tags:
            validate_capability_tag(tag)


# A list_fn returns ProviderRecord instances scoped to a single instance.
# Sync or async are both acceptable; the registry awaits the result if
# it is a coroutine. ``provider_type`` is implicit (the function is
# registered under that key) but each ProviderRecord must declare its
# own ``provider_type`` so misregistered listers surface immediately.
ProviderListFn = Callable[[str], "list[ProviderRecord] | Awaitable[list[ProviderRecord]]"]


class ProviderRegistry:
    """Aggregates capability sources from any provider type.

    Distinct from :class:`kernos.kernel.agents.providers.ProviderRegistry`
    in DAR, which maps ``provider_key`` -> ``AgentInboxFactory``. The two
    abstractions are orthogonal: DAR's registry knows how to construct
    inbox concretes; STS's registry knows how to enumerate capability
    sources for cohort decision making.

    AgentInbox concretes are the FIRST adapter, not the ontology.
    """

    def __init__(self) -> None:
        self._listers: dict[str, ProviderListFn] = {}

    def register_provider_type(
        self, provider_type: str, list_fn: ProviderListFn,
    ) -> None:
        """Bind a lister for the given provider type.

        Raises:
            ValueError: if ``provider_type`` is empty or already
                registered.
        """
        if not provider_type:
            raise ValueError("provider_type is required")
        if provider_type in self._listers:
            raise ValueError(
                f"provider_type {provider_type!r} is already registered; "
                f"unregister_provider_type() first to replace"
            )
        self._listers[provider_type] = list_fn

    def unregister_provider_type(self, provider_type: str) -> bool:
        return self._listers.pop(provider_type, None) is not None

    def known_provider_types(self) -> tuple[str, ...]:
        return tuple(self._listers.keys())

    async def list_all(self, *, instance_id: str) -> list[ProviderRecord]:
        """Aggregate ProviderRecords across every registered provider type.

        Each lister receives the ``instance_id`` and returns its slice;
        results are concatenated in registration order.
        """
        if not instance_id:
            raise ValueError("instance_id is required")
        out: list[ProviderRecord] = []
        for provider_type, fn in self._listers.items():
            result = fn(instance_id)
            if inspect.isawaitable(result):
                result = await result  # type: ignore[assignment]
            for rec in result:  # type: ignore[union-attr]
                if rec.provider_type != provider_type:
                    raise ValueError(
                        f"lister for provider_type={provider_type!r} returned "
                        f"a record with provider_type={rec.provider_type!r}"
                    )
                out.append(rec)
        return out


__all__ = [
    "CapabilityGap",
    "InvalidCapabilityTagFormat",
    "Issue",
    "ProviderListFn",
    "ProviderRecord",
    "ProviderRegistry",
    "validate_capability_tag",
]

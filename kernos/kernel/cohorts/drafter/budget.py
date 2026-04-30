"""Per-instance time-window budget for Drafter Tier 2 evaluation.

Tier 1 (cheap event-shape filter) is always free. Tier 2 (semantic LLM
evaluation) consumes from this budget. The default window is 1 hour
with a default cap of 10 LLM calls per hour per instance — configurable
per-instance via cohort metadata.

The tracker is in-memory (per-engine, per-instance) and intentionally
NOT durable: budget is a soft rate-limit, not a safety property. A
process restart resets the counter, which is acceptable — the worst
case is one extra evaluation window.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field


DEFAULT_WINDOW_SECONDS = 3600  # 1 hour
DEFAULT_BUDGET_PER_WINDOW = 10


@dataclass(frozen=True)
class BudgetConfig:
    """Per-instance budget knobs.

    ``window_seconds`` is the rolling time window. ``calls_per_window``
    is the cap of Tier 2 LLM calls within that window.
    """

    window_seconds: int = DEFAULT_WINDOW_SECONDS
    calls_per_window: int = DEFAULT_BUDGET_PER_WINDOW

    def __post_init__(self) -> None:
        if self.window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if self.calls_per_window < 0:
            raise ValueError("calls_per_window must be non-negative")


class BudgetTracker:
    """Per-instance rolling-window LLM call counter.

    Implementation: a deque of timestamps per instance. ``consume`` and
    ``has_budget`` evict timestamps older than ``window_seconds`` before
    counting. Sub-microsecond per call; no I/O.

    A clock callable can be injected for testing — the default uses
    :func:`time.monotonic` which is the right choice for rate limits
    (independent of wall-clock adjustments).
    """

    def __init__(
        self,
        *,
        config: BudgetConfig | None = None,
        clock: "callable | None" = None,
    ) -> None:
        self._config = config or BudgetConfig()
        self._clock = clock or time.monotonic
        self._timestamps: dict[str, deque[float]] = {}

    @property
    def config(self) -> BudgetConfig:
        return self._config

    def _evict_expired(self, instance_id: str) -> None:
        if instance_id not in self._timestamps:
            return
        cutoff = self._clock() - self._config.window_seconds
        dq = self._timestamps[instance_id]
        while dq and dq[0] < cutoff:
            dq.popleft()

    def has_budget(self, *, instance_id: str) -> bool:
        if not instance_id:
            raise ValueError("instance_id is required")
        self._evict_expired(instance_id)
        used = len(self._timestamps.get(instance_id, ()))
        return used < self._config.calls_per_window

    def consume(self, *, instance_id: str, amount: int = 1) -> None:
        if not instance_id:
            raise ValueError("instance_id is required")
        if amount <= 0:
            raise ValueError("amount must be positive")
        self._evict_expired(instance_id)
        dq = self._timestamps.setdefault(instance_id, deque())
        now = self._clock()
        for _ in range(amount):
            dq.append(now)

    def remaining(self, *, instance_id: str) -> int:
        if not instance_id:
            raise ValueError("instance_id is required")
        self._evict_expired(instance_id)
        used = len(self._timestamps.get(instance_id, ()))
        return max(0, self._config.calls_per_window - used)

    def used(self, *, instance_id: str) -> int:
        self._evict_expired(instance_id)
        return len(self._timestamps.get(instance_id, ()))


__all__ = [
    "BudgetConfig",
    "BudgetTracker",
    "DEFAULT_BUDGET_PER_WINDOW",
    "DEFAULT_WINDOW_SECONDS",
]

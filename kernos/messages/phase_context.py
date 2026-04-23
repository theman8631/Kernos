"""Phase context — the dataclass threaded through the six-phase turn pipeline.

HANDLER-PIPELINE-DECOMPOSE. Every phase module's ``run(ctx)`` function
accepts a ``PhaseContext`` and returns one. No module-level globals, no
side-channel communication — every piece of inter-phase state rides
this struct.

For backward compatibility (and to avoid a mass rename of the existing
``TurnContext`` references in the handler shim) ``PhaseContext`` is
currently an alias for :class:`kernos.messages.handler.TurnContext`.
The shape is identical; the two names let phase modules import from
``phase_context`` while the shim keeps working with ``TurnContext``.
"""
from __future__ import annotations

from kernos.messages.handler import TurnContext as PhaseContext

__all__ = ["PhaseContext"]

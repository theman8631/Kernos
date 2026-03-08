"""Context Space Router — route inbound messages to context spaces.

Algorithmic only, no LLM calls. Checks space name/alias matches first,
then entity ownership, then falls back to the most recently active space.
"""
import logging

from kernos.kernel.state import StateStore

logger = logging.getLogger(__name__)


class ContextSpaceRouter:
    """Route inbound messages to context spaces. Algorithmic only, no LLM calls."""

    def __init__(self, state: StateStore) -> None:
        self._state = state

    async def route(self, tenant_id: str, message_text: str) -> tuple[str, bool]:
        """Return (space_id, confident). confident=False means router used the default."""
        spaces = await self._state.list_context_spaces(tenant_id)
        if not spaces or len(spaces) == 1:
            daily = next((s for s in spaces if s.is_default), None)
            return (daily.id if daily else ""), True

        text_lower = message_text.lower()

        # Check 1: Space name/alias match (daily never wins this way)
        for space in spaces:
            if space.is_default:
                continue
            if space.status != "active":
                continue
            triggers = [space.name.lower()] + [a.lower() for a in space.routing_aliases]
            for trigger in triggers:
                if trigger in text_lower:
                    return space.id, True

        # Check 2: Entity ownership
        entities = await self._state.query_entity_nodes(tenant_id, active_only=True)
        for entity in entities:
            if not entity.context_space:
                continue
            names = [entity.canonical_name.lower()] + [a.lower() for a in entity.aliases]
            for name in names:
                if name in text_lower:
                    return entity.context_space, True

        # Check 3: Most recently active space (default fallback)
        active = sorted(
            [s for s in spaces if s.status == "active"],
            key=lambda s: s.last_active_at,
            reverse=True,
        )
        default = active[0] if active else spaces[0]
        return default.id, False

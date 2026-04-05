"""Context Space Router — LLM-based message routing.

Routes messages to context spaces using a lightweight Haiku LLM call.
Reads message meaning, recent history, and space descriptions.
Algorithmic fallback for single-space tenants (zero cost).
"""
import json
from kernos.utils import utc_now
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from kernos.kernel.reasoning import ReasoningService
from kernos.kernel.spaces import ContextSpace
from kernos.kernel.state import StateStore

logger = logging.getLogger(__name__)

ROUTER_SYSTEM_PROMPT = """You are a message router for a personal AI assistant. Given the user's message, recent conversation history, and a list of context spaces, do three things:

1. TAG: Which space(s) does this message belong to? A message can belong to multiple spaces. Use space IDs from the list. If the message is about a recurring topic that doesn't yet have its own space, also include a concise snake_case topic hint (e.g., "legal_work", "dnd_campaign"). Don't add a hint if the topic fits in General or an existing space.

2. FOCUS: Which single space should receive the agent's full attention right now? When in doubt, choose General. The cost of defaulting to General is low — if the domain continues, it reasserts next message.

3. CONTINUATION: Is this an obvious continuation (short affirmation, reaction, "lol", "ok", "sounds good") that should ride conversational momentum? If yes, keep the current focus unchanged.

Rules:
- When a message signals something NEW within an existing domain ("new campaign", "starting fresh", "not the old one"), tag General. Let the new topic accumulate before it earns a space.
- Ambiguity is not a domain signal. When uncertain, tag General.
- A message mentioning a person or entity from one domain doesn't mean the message IS about that domain. "Henderson plays D&D" while chatting casually is General, not Business.
- Read the message in the context of recent history. A message after a long gap is a fresh start. A message seconds after the last one is a continuation.
- Never invent space IDs. Only use IDs from the provided space list, or snake_case topic hints for emerging topics.
"""

ROUTER_SCHEMA = {
    "type": "object",
    "properties": {
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Space IDs this message belongs to, plus optional snake_case topic hints for emerging topics"
        },
        "focus": {
            "type": "string",
            "description": "Single space ID for the agent's main focus"
        },
        "continuation": {
            "type": "boolean",
            "description": "True if this is an obvious short continuation riding conversational momentum"
        },
        "query_mode": {
            "type": "boolean",
            "description": "True if this is an informational query about another domain "
                          "(quick question) rather than a switch into that domain. "
                          "The user wants an answer, not a context change."
        },
        "work_mode": {
            "type": "boolean",
            "description": "True if the user intends to DO WORK in another domain "
                          "(not just ask about it). 'Let's invoice Henderson' is work_mode. "
                          "'What was Henderson's last invoice?' is query_mode."
        },
    },
    "required": ["tags", "focus", "continuation", "query_mode", "work_mode"],
    "additionalProperties": False
}


@dataclass
class RouterResult:
    """The result of a routing decision."""
    tags: list[str]       # Space IDs (and optional topic hints) this message belongs to
    focus: str            # Space ID for the main agent's focus
    continuation: bool    # Obvious continuation — ride momentum
    query_mode: bool = False  # Quick question about another domain — don't switch
    work_mode: bool = False   # Intent to do work in another domain — route there




def _compute_gap_description(last_ts: str, now_ts: str) -> str:
    """Describe the time gap between the last message and now in human terms."""
    if not last_ts:
        return "first message"
    try:
        last = datetime.fromisoformat(last_ts)
        now = datetime.fromisoformat(now_ts)
        seconds = max((now - last).total_seconds(), 0)
        if seconds < 120:
            return f"{int(seconds)}s"
        if seconds < 3600:
            return f"{int(seconds / 60)}m"
        if seconds < 86400:
            return f"{int(seconds / 3600)}h"
        return f"{int(seconds / 86400)}d"
    except (ValueError, TypeError):
        return "unknown"


class LLMRouter:
    """Route messages to context spaces using a lightweight LLM.

    One Haiku-class call per message. Reads language, not keyword lists.
    Falls back to daily (zero cost) for single-space tenants.
    """

    def __init__(self, state: StateStore, reasoning: ReasoningService) -> None:
        self._state = state
        self._reasoning = reasoning

    async def route(
        self,
        tenant_id: str,
        message_content: str,
        recent_history: list[dict],
        current_focus_id: str = "",
    ) -> RouterResult:
        """Route a message. Returns RouterResult(tags, focus, continuation).

        recent_history: full metadata entries from get_recent_full().
        current_focus_id: the tenant's last_active_space_id (for continuation logic).
        """
        spaces = await self._state.list_context_spaces(tenant_id)
        active_spaces = [s for s in spaces if s.status == "active"]

        # No spaces at all — nothing to route to
        if not active_spaces:
            return RouterResult(tags=[], focus="", continuation=False)

        # Build space list for the prompt (with hierarchy info)
        space_name_map_all = {s.id: s.name for s in active_spaces}
        space_lines = []
        for s in active_spaces:
            desc = s.description or "No description yet"
            default_marker = " [DEFAULT]" if s.is_default else ""
            parent_marker = ""
            if s.parent_id and s.parent_id in space_name_map_all:
                parent_marker = f" [child of: {space_name_map_all[s.parent_id]}]"
            space_lines.append(f"- {s.id}: {s.name}{default_marker}{parent_marker} — {desc}")
        space_descriptions = "\n".join(space_lines)

        # Build recent history with timestamps and existing tags
        space_name_map = {s.id: s.name for s in active_spaces}
        history_lines = []
        for msg in recent_history[-15:]:
            ts = msg.get("timestamp", "")
            role = msg.get("role", "")
            content = str(msg.get("content", ""))[:200]
            tags = msg.get("space_tags") or []
            tag_names = [space_name_map.get(t, t) for t in tags if t] if tags else ["(untagged)"]
            history_lines.append(f"[{ts}] ({role}) [{', '.join(tag_names)}]: {content}")

        # Temporal metadata
        now = utc_now()
        last_ts = recent_history[-1].get("timestamp", "") if recent_history else ""
        gap = _compute_gap_description(last_ts, now)
        current_focus_name = space_name_map.get(current_focus_id, "none")

        user_content = (
            f"Active spaces:\n{space_descriptions}\n\n"
            f"Recent history:\n" + ("\n".join(history_lines) if history_lines else "(no history)") + "\n\n"
            f"Time context: {now}. Gap since last message: {gap}.\n"
            f"Current focus: {current_focus_id} ({current_focus_name})\n\n"
            f"New message: {message_content}"
        )

        # Find daily space ID for fallback
        daily = next((s for s in active_spaces if s.is_default), active_spaces[0])
        daily_id = daily.id

        try:
            result_str = await self._reasoning.complete_simple(
                system_prompt=ROUTER_SYSTEM_PROMPT,
                user_content=user_content,
                output_schema=ROUTER_SCHEMA,
                max_tokens=128,
                prefer_cheap=True,
            )
            parsed = json.loads(result_str)
            tags = parsed.get("tags", [daily_id])
            focus = parsed.get("focus", daily_id)
            continuation = parsed.get("continuation", False)
            query_mode = parsed.get("query_mode", False)
            work_mode = parsed.get("work_mode", False)

            # Validate focus is a known space ID (not a topic hint)
            known_ids = {s.id for s in active_spaces}
            if focus not in known_ids:
                # Check aliases — LLM may have returned an old name
                for s in active_spaces:
                    if focus in s.aliases:
                        focus = s.id
                        break
                else:
                    focus = current_focus_id if current_focus_id in known_ids else daily_id

            # Ensure focus is in tags
            if focus not in tags:
                tags = [focus] + tags

            return RouterResult(tags=tags, focus=focus, continuation=continuation, query_mode=query_mode, work_mode=work_mode)

        except Exception as exc:
            logger.warning("LLM router failed, falling back to current focus: %s", exc)
            fallback = current_focus_id if current_focus_id else daily_id
            return RouterResult(tags=[fallback], focus=fallback, continuation=True)

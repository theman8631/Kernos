"""Dispatch Gate — loss-cost evaluator for tool call authorization.

Classifies tool effects, evaluates loss cost via lightweight LLM call,
manages approval tokens for confirmed actions.
"""
import hashlib
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from kernos.kernel.event_types import EventType
from kernos.kernel.events import EventStream, emit_event

logger = logging.getLogger(__name__)


@dataclass
class GateResult:
    """The outcome of a dispatch gate check."""

    allowed: bool
    reason: str    # "approved", "covenant_conflict", "confirm", "clarify", "token_approved"
    method: str    # "token", "model_check", "always_allow"
    proposed_action: str = ""    # Human-readable description of what was blocked
    conflicting_rule: str = ""   # For CONFLICT — which rule conflicts
    raw_response: str = ""       # Full model response for logging


@dataclass
class ApprovalToken:
    """Single-use token issued when the dispatch gate blocks an action."""

    token_id: str          # uuid hex[:12]
    tool_name: str
    tool_input_hash: str   # md5 hex[:8] of tool_input
    issued_at: datetime
    used: bool = False


class DispatchGate:
    """Loss-cost evaluator for tool call authorization.

    Three-step check:
    1. Approval token bypass (user confirmed this specific action)
    2. Permission override fast path (capability set to always-allow)
    3. Lightweight model call evaluating loss cost
    """

    def __init__(
        self,
        reasoning_service: Any,  # For complete_simple calls
        registry: Any,           # CapabilityRegistry for tool_effects
        state: Any,              # StateStore for covenant queries
        events: EventStream,
        mcp: Any = None,         # MCPClientManager for tool descriptions
    ) -> None:
        self._reasoning = reasoning_service
        self._registry = registry
        self._state = state
        self._events = events
        self._mcp = mcp
        self._approval_tokens: dict[str, ApprovalToken] = {}

    def classify_tool_effect(
        self, tool_name: str, active_space: Any, tool_input: dict[str, Any] | None = None,
    ) -> str:
        """Classify a tool call's effect level.

        Returns: "read", "soft_write", "hard_write", or "unknown"
        """
        _KERNEL_READS = {
            "remember", "remember_details", "list_files", "read_file",
            "dismiss_whisper", "read_source", "read_doc", "read_soul",
            "manage_channels",
        }
        _KERNEL_WRITES = {
            "write_file", "delete_file", "manage_covenants",
            "update_soul", "manage_capabilities", "send_to_channel",
        }

        if tool_name in _KERNEL_READS:
            return "read"
        if tool_name == "manage_covenants":
            action = (tool_input or {}).get("action", "list")
            return "read" if action == "list" else "soft_write"
        if tool_name == "manage_capabilities":
            action = (tool_input or {}).get("action", "list")
            return "read" if action == "list" else "soft_write"
        if tool_name == "manage_channels":
            action = (tool_input or {}).get("action", "list")
            return "read" if action == "list" else "soft_write"
        if tool_name == "manage_schedule":
            return "read"
        if tool_name in _KERNEL_WRITES:
            return "soft_write"

        if not self._registry:
            return "unknown"

        for cap in self._registry.get_all():
            if tool_name in (cap.tool_effects or {}):
                return cap.tool_effects[tool_name]
            if tool_name in (cap.tools or []) and tool_name not in (cap.tool_effects or {}):
                return "unknown"

        return "unknown"

    def _get_capability_for_tool(self, tool_name: str) -> str | None:
        """Return the capability name that owns this tool, or None."""
        if not self._registry:
            return None
        for cap in self._registry.get_all():
            if tool_name in (cap.tools or []):
                return cap.name
            if tool_name in (cap.tool_effects or {}):
                return cap.name
        return None

    def _get_tool_description(self, tool_name: str) -> str:
        """Return the tool's description from the MCP manifest."""
        if self._mcp:
            try:
                for tool in self._mcp.get_tools():
                    if tool.get("name") == tool_name:
                        return tool.get("description", "")
            except Exception:
                pass
        return ""

    def _describe_action(self, tool_name: str, tool_input: dict) -> str:
        """Generate a human-readable description of a proposed tool call."""
        if tool_name == "create-event":
            return f"Create calendar event: '{tool_input.get('summary', 'an event')}' at {tool_input.get('start', 'unspecified time')}"
        if tool_name == "update-event":
            return f"Update calendar event: '{tool_input.get('summary', 'an event')}'"
        if tool_name == "delete-event":
            return f"Delete calendar event: '{tool_input.get('summary', 'an event')}'"
        if tool_name == "send-email":
            return f"Send email to {tool_input.get('to', 'someone')}: '{tool_input.get('subject', 'no subject')}'"
        if tool_name == "delete-email":
            return f"Delete email: {tool_input.get('id', 'a message')}"
        if tool_name == "delete_file":
            return f"Delete file: {tool_input.get('name', 'a file')}"
        if tool_name == "write_file":
            return f"Write/update file: {tool_input.get('name', 'a file')}"
        return f"Execute {tool_name} with {json.dumps(tool_input)[:200]}"

    async def evaluate(
        self,
        tool_name: str,
        tool_input: dict,
        effect: str,
        user_message: str,
        tenant_id: str,
        active_space_id: str,
        messages: list[dict] | None = None,
        approval_token_id: str | None = None,
        agent_reasoning: str = "",
    ) -> GateResult:
        """Full gate evaluation: token → override → model check."""
        # Step 1: Approval token
        if approval_token_id and self.validate_approval_token(
            approval_token_id, tool_name, tool_input
        ):
            logger.info("GATE: token_validated tool=%s token=%s", tool_name, approval_token_id)
            return GateResult(allowed=True, reason="token_approved", method="token")

        # Step 2: Permission override
        cap_name = self._get_capability_for_tool(tool_name)
        if cap_name and self._state:
            try:
                tenant = await self._state.get_tenant_profile(tenant_id)
                if tenant and tenant.permission_overrides.get(cap_name) == "always-allow":
                    logger.info("GATE: permission_override tool=%s cap=%s", tool_name, cap_name)
                    return GateResult(allowed=True, reason="permission_override", method="always_allow")
            except Exception as exc:
                logger.warning("Gate: permission override check failed: %s", exc)

        # Step 3: Model evaluation
        return await self._evaluate_model(
            tool_name, tool_input, effect, messages, agent_reasoning,
            tenant_id, active_space_id,
        )

    async def _evaluate_model(
        self,
        tool_name: str,
        tool_input: dict,
        effect: str,
        messages: list[dict] | None,
        agent_reasoning: str,
        tenant_id: str,
        active_space_id: str,
    ) -> GateResult:
        """Lightweight model evaluation for loss-cost assessment."""
        # Build recent_messages_text
        recent_messages_text = "No recent messages."
        if messages:
            user_msgs = [m for m in messages if m.get("role") == "user"][-5:]
            if user_msgs:
                lines = []
                for m in user_msgs:
                    content = m.get("content", "")
                    if isinstance(content, str):
                        lines.append(f'- "{content[:300]}"')
                    elif isinstance(content, list):
                        text = " ".join(
                            b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        )
                        if text:
                            lines.append(f'- "{text[:300]}"')
                if lines:
                    recent_messages_text = "\n".join(lines)

        # Build rules_text
        rules_text = "No standing covenant rules."
        rules_count = 0
        must_not_rules: list[str] = []
        if self._state:
            try:
                rules = await self._state.query_covenant_rules(
                    tenant_id, context_space_scope=[active_space_id, None], active_only=True,
                )
                rule_lines = []
                for r in rules:
                    rule_lines.append(
                        f"- [{r.rule_type}] {r.description} (scope: {r.context_space or 'global'})"
                    )
                    if r.rule_type == "must_not":
                        must_not_rules.append(r.description)
                if rule_lines:
                    rules_count = len(rule_lines)
                    rules_text = "\n".join(rule_lines)
            except Exception as exc:
                logger.warning("Gate: covenant query failed: %s", exc)

        action_desc = self._describe_action(tool_name, tool_input)
        tool_description = self._get_tool_description(tool_name)

        system_prompt = (
            "You are a safety check for an AI assistant's actions. Your job is to "
            "assess LOSS COST — what happens if the assistant misunderstood the "
            "user's intent.\n\n"
            "Answer with ONE of these:\n\n"
            "APPROVE — The user's intent is clear AND if the assistant misunderstood, "
            "the cost is low (easily reversible, minor data, user's own resources).\n"
            "CONFIRM — The action affects someone other than the user (sending messages, "
            "sharing data with third parties), OR could cause significant data loss or "
            "is hard to reverse (bulk deletion), OR has financial cost.\n"
            "CONFLICT: <exact rule text> — The user asked for this, BUT a standing "
            "must_not covenant rule applies. Copy the exact rule text after the colon.\n"
            "CLARIFY — The user's request is ambiguous — it could mean multiple things "
            "with meaningfully different outcomes.\n\n"
            "Important:\n"
            "- If the user explicitly addresses a restriction (\"no need to review, "
            "just send it\"), that is an override — return APPROVE, not CONFLICT.\n"
            "- If a must_not rule genuinely applies and the user did NOT address it, "
            "return CONFLICT: <that rule's exact text>.\n"
            "- This check is for loss-cost calibration only. Standing rules and "
            "covenant conflicts are handled separately. You are only assessing: how "
            "costly is a misunderstanding here?\n"
            "- When in doubt between APPROVE and CONFIRM, choose CONFIRM.\n\n"
            "For CONFLICT, use format: CONFLICT: <rule text>\n"
            "For all others, answer with ONLY the one word."
        )
        user_content = (
            f"Recent user messages (oldest to newest):\n{recent_messages_text}\n\n"
            f"Agent's reasoning for this action:\n{agent_reasoning}\n\n"
            f"Proposed action: {tool_name}\n"
            f"Tool description: {tool_description}\n"
            f"Action details: {action_desc}\n\n"
            f"Active covenant rules:\n{rules_text}"
        )

        raw = ""
        logger.info("GATE_MODEL: max_tokens=512, has_schema=False, rules=%d", rules_count)
        try:
            raw = await self._reasoning.complete_simple(
                system_prompt=system_prompt,
                user_content=user_content,
                max_tokens=512,
                prefer_cheap=True,
            )
        except Exception as exc:
            logger.warning("Gate: model evaluation failed: %s", exc)
        logger.info("GATE_MODEL: raw_response=%r", raw[:300])

        stripped = raw.strip()
        first_word = stripped.split()[0].upper() if stripped else ""
        if first_word in ("APPROVE", "EXPLICIT", "AUTHORIZED"):
            return GateResult(allowed=True, reason="approved", method="model_check", raw_response=raw)
        if first_word.startswith("CONFLICT"):
            conflicting_rule = ""
            if ":" in stripped:
                conflicting_rule = stripped.split(":", 1)[1].strip()
            if not conflicting_rule:
                conflicting_rule = must_not_rules[0] if must_not_rules else ""
            return GateResult(
                allowed=False, reason="covenant_conflict", method="model_check",
                proposed_action=action_desc, conflicting_rule=conflicting_rule, raw_response=raw,
            )
        if first_word == "CLARIFY":
            return GateResult(
                allowed=False, reason="clarify", method="model_check",
                proposed_action=action_desc, raw_response=raw,
            )
        return GateResult(
            allowed=False, reason="confirm", method="model_check",
            proposed_action=action_desc, raw_response=raw,
        )

    def issue_approval_token(self, tool_name: str, tool_input: dict) -> ApprovalToken:
        """Issue a single-use approval token for a blocked tool call."""
        token_id = uuid.uuid4().hex[:12]
        input_hash = hashlib.md5(
            json.dumps(tool_input, sort_keys=True).encode()
        ).hexdigest()[:8]
        token = ApprovalToken(
            token_id=token_id,
            tool_name=tool_name,
            tool_input_hash=input_hash,
            issued_at=datetime.now(timezone.utc),
        )
        self._approval_tokens[token_id] = token
        return token

    def validate_approval_token(
        self, token_id: str, tool_name: str, tool_input: dict,
    ) -> bool:
        """Validate an approval token. Marks it used on success."""
        token = self._approval_tokens.get(token_id)
        if not token:
            return False
        if token.used:
            return False
        if token.tool_name != tool_name:
            return False
        age_seconds = (datetime.now(timezone.utc) - token.issued_at).total_seconds()
        if age_seconds > 300:
            return False
        input_hash = hashlib.md5(
            json.dumps(tool_input, sort_keys=True).encode()
        ).hexdigest()[:8]
        if input_hash != token.tool_input_hash:
            return False
        token.used = True
        return True

    def cleanup_expired_tokens(self) -> None:
        """Remove expired or used approval tokens."""
        now = datetime.now(timezone.utc)
        expired = [
            tid for tid, token in self._approval_tokens.items()
            if token.used or (now - token.issued_at).total_seconds() > 300
        ]
        for tid in expired:
            del self._approval_tokens[tid]

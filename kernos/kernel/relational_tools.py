"""Agent-facing tool schemas for RELATIONAL-MESSAGING v5.

Two tools land in the kernel tool registry:

- `send_relational_message` — initiate a purposeful exchange with another
  member's agent. The dispatcher enforces the permission matrix; the
  agent just describes the message it wants sent.

- `resolve_relational_message` — mark a message processed. Default flow
  is delivered→surfaced (at end of turn) then the agent can call this to
  go surfaced→resolved. For pure agent-side auto-handles (covenant fires
  an action with no user involvement), the agent calls this with
  auto_handled=True BEFORE end-of-turn so the message goes
  delivered→resolved directly and skips the user-visible surface.
"""

SEND_RELATIONAL_MESSAGE_TOOL = {
    "name": "send_relational_message",
    "description": (
        "Send a purposeful message to ANOTHER MEMBER'S agent. Use only when "
        "reaching another member's agent obviously benefits the user — "
        "scheduling, coordinated check-in, factual handoff. The recipient's "
        "agent serves THEIR member and may decline based on their member's "
        "covenants; don't compose the recipient's reply for them.\n\n"
        "Intents:\n"
        "- request_action: you want them to do something.\n"
        "- ask_question: you want information. Requires the OTHER side's "
        "  relationship to be full-access; rejected otherwise.\n"
        "- inform: you're sharing purposeful information.\n\n"
        "Urgencies:\n"
        "- time_sensitive: push out-of-band now (24h TTL).\n"
        "- elevated: queue; surfaces on their next turn (7d TTL).\n"
        "- normal: queue (72h TTL). Default.\n\n"
        "Conversation threading: pass conversation_id to continue an existing "
        "thread; omit to start a new one. Thread id is returned in the result "
        "and stays stable across replies."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "addressee": {
                "type": "string",
                "description": (
                    "The member to reach — their member_id OR their display name. "
                    "Ambiguous names fail fast; ask for clarification first."
                ),
            },
            "intent": {
                "type": "string",
                "enum": ["request_action", "ask_question", "inform"],
                "description": "What you're doing with this message.",
            },
            "content": {
                "type": "string",
                "description": (
                    "The message body. Specific, respectful, actionable. "
                    "Don't leak your member's private content."
                ),
            },
            "urgency": {
                "type": "string",
                "enum": ["time_sensitive", "elevated", "normal"],
                "description": "Delivery urgency. Default: normal.",
            },
            "target_space_hint": {
                "type": "string",
                "description": (
                    "Optional. Recipient's space id if you know which of their "
                    "spaces this belongs to. Advisory — their space structure "
                    "is the final authority."
                ),
            },
            "conversation_id": {
                "type": "string",
                "description": (
                    "Optional. Existing rm_conv_* id to thread this message "
                    "into. Omit to start a fresh thread."
                ),
            },
            "reply_to_id": {
                "type": "string",
                "description": (
                    "Optional. The rm_* message id this is in direct reply to."
                ),
            },
        },
        "required": ["addressee", "intent", "content"],
    },
}


RESOLVE_RELATIONAL_MESSAGE_TOOL = {
    "name": "resolve_relational_message",
    "description": (
        "Mark a relational message as processed. Two modes:\n"
        "- Standard: the agent has already surfaced the message to the user "
        "  and finished processing. Transition is surfaced → resolved.\n"
        "- Auto-handled (auto_handled=true): the agent handled the message "
        "  entirely agent-side (e.g., a standing covenant auto-booked the "
        "  calendar). No user-visible surface needed; transition is "
        "  delivered → resolved directly. Use this BEFORE the end of the "
        "  turn, or the default surface-at-persist will run.\n"
        "Provide a short reason for the outcome so the trace captures it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "message_id": {
                "type": "string",
                "description": "The rm_* message id to resolve.",
            },
            "auto_handled": {
                "type": "boolean",
                "description": (
                    "If true, transition delivered→resolved directly (skip "
                    "user-visible surface). Default: false (surfaced→resolved)."
                ),
            },
            "reason": {
                "type": "string",
                "description": (
                    "Short outcome category (e.g., 'covenant_auto_handled', "
                    "'user_confirmed', 'declined', 'completed')."
                ),
            },
        },
        "required": ["message_id"],
    },
}

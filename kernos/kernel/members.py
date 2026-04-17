"""Member management tool schema — Multi-Member."""

MANAGE_MEMBERS_TOOL = {
    "name": "manage_members",
    "description": (
        "Two types of codes — NEVER guess which one. "
        "INVITE = code for a NEW PERSON to join with their own fresh agent, spaces, and context. "
        "CONNECT_PLATFORM = code for the CURRENT MEMBER to link a new platform to their existing account "
        "(same agent, same spaces, same history). "
        "If ambiguous, ASK the user which they want before generating. "
        "Also: list members, remove members, declare/list relationships."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["invite", "connect_platform", "list", "remove", "declare_relationship", "list_relationships"],
                "description": (
                    "invite = NEW PERSON joining Kernos. Fresh member, own agent/spaces/context. "
                    "connect_platform = SAME PERSON, new channel. Same agent/spaces/history. "
                    "list = show all members and their connected platforms. "
                    "remove = deactivate a member. "
                    "declare_relationship = set the current member's permission toward another member "
                    "(full-access / no-access / by-permission). When the user explicitly "
                    "requests a declaration (e.g., 'set Emma to full-access'), EXECUTE "
                    "immediately — do not ask for confirmation. Declarations are fully "
                    "reversible; the user can change them at any time by re-declaring. "
                    "Confirmation-first adds friction without protecting anything. "
                    "list_relationships = show the requesting member's declared relationships."
                ),
            },
            "platform": {
                "type": "string",
                "description": (
                    "Required for invite and connect_platform. "
                    "Which platform this code is for: discord, telegram, sms. "
                    "The code can ONLY be redeemed on this platform."
                ),
            },
            "display_name": {
                "type": "string",
                "description": "Name for the new member (invite action only). Ask if not provided.",
            },
            "member_id": {
                "type": "string",
                "description": (
                    "For connect_platform: the requesting member's member_id (use their actual mem_ ID). "
                    "For remove / declare_relationship: the target member (member_id or display name)."
                ),
            },
            "expires_hours": {
                "type": "integer",
                "description": "Hours until code expires (default: 72)",
            },
            "permission": {
                "type": "string",
                "enum": ["full-access", "no-access", "by-permission"],
                "description": (
                    "For declare_relationship. Three values: "
                    "full-access = share this member's context freely with the target; "
                    "no-access = do not share anything about this member with the target; "
                    "by-permission = default conservative — ask the author before sharing. "
                    "Absence of a declaration means by-permission. "
                    "Topic-scoped exceptions (e.g. 'keep fiction private') live in covenants."
                ),
            },
        },
        "required": ["action"],
    },
}

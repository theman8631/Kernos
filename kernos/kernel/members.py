"""Member management tool schema — Multi-Member."""

MANAGE_MEMBERS_TOOL = {
    "name": "manage_members",
    "description": (
        "Two types of codes — NEVER guess which one. "
        "INVITE = code for a NEW PERSON to join with their own fresh agent, spaces, and context. "
        "CONNECT_PLATFORM = code for the CURRENT MEMBER to link a new platform to their existing account "
        "(same agent, same spaces, same history). "
        "If ambiguous, ASK the user which they want before generating. "
        "Also: list members, remove members."
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
                    "declare_relationship = declare how two members know each other (spouse, coworker, etc.) "
                    "and what sharing level applies (full-share, work-only, coordination-only, minimal). "
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
                    "For remove: the member to deactivate."
                ),
            },
            "expires_hours": {
                "type": "integer",
                "description": "Hours until code expires (default: 72)",
            },
            "relationship_type": {
                "type": "string",
                "description": "For declare_relationship: how members know each other (spouse, partner, family, coworker, friend, client, etc.)",
            },
            "profile": {
                "type": "string",
                "description": "For declare_relationship: sharing level — full-share, work-only, coordination-only (default), or minimal.",
            },
        },
        "required": ["action"],
    },
}

"""Member management tool schema — Multi-Instance Phase 2."""

MANAGE_MEMBERS_TOOL = {
    "name": "manage_members",
    "description": (
        "Manage Kernos instance members — invite new users, "
        "list current members, generate connection codes for "
        "linking new platforms to existing accounts, or remove members. "
        "Codes are platform-locked — specify which platform (discord, telegram, sms) "
        "the code is for. The tool returns the code AND platform-specific instructions "
        "to give to the user. If the platform isn't set up yet, it returns setup "
        "instructions instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["invite", "connect_platform", "list", "remove"],
                "description": (
                    "invite = generate platform-locked code for new user. "
                    "connect_platform = generate code for existing user to add a new channel. "
                    "list = show all members and their connected platforms. "
                    "remove = deactivate a member."
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
                "description": "Name for the new member (invite action)",
            },
            "member_id": {
                "type": "string",
                "description": "Member ID (for connect_platform or remove)",
            },
            "expires_hours": {
                "type": "integer",
                "description": "Hours until invite code expires (default: 72)",
            },
        },
        "required": ["action"],
    },
}

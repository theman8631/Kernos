"""Member management tool schema — Improvement Loop, Multi-Instance Phase 2."""

MANAGE_MEMBERS_TOOL = {
    "name": "manage_members",
    "description": (
        "Manage Kernos instance members — invite new users, "
        "list current members, generate connection codes for "
        "linking new platforms to existing accounts, or remove members."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["invite", "connect_platform", "list", "remove"],
                "description": (
                    "invite = generate code for new user. "
                    "connect_platform = generate code for existing user to add a new channel. "
                    "list = show all members. "
                    "remove = deactivate a member."
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

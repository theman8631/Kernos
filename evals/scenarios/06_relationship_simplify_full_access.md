# Relationship Simplify — Full-Access Declaration

## Purpose
Harold declares Emma as `full-access`. Verify Harold's side stores
`full-access`, Emma's side stays at the implicit default (no row, no
auto-flip). The STATE block on Harold's next turn should show the
`full-access` declaration compactly.

Corresponds to RELATIONSHIP-SIMPLIFY expected behaviors:
- `declare_relationship` accepts a permission value and stores it directionally.
- The declaring member's side is stored; the other member's side stays at implicit default.
- `list_relationships` shows the permission without type/profile/status fields.
- STATE block shows declared permissions.

## Setup
fresh_instance: true

members:
  - id: owner
    display_name: Harold
    role: owner
    platform: discord
    channel_id: "1000000001"
  - id: emma
    display_name: Emma
    role: member
    platform: telegram
    channel_id: "2000000002"

## Turns
1. owner@discord: I want to fully share context with Emma. Please set Emma to full-access for me.
2. owner@discord: List my relationships.

## Observations
- member_profile: owner
- member_profile: emma

## Rubrics
- On turn 1, the agent executes the declaration without asking Harold to confirm or clarify. The turn-1 reply indicates the declaration landed (e.g., "Declared toward Emma: full-access" or similar), not an error and not a request for user confirmation.
- The agent confirms the declaration in natural language (e.g., "Declared toward Emma: full-access"), not an error.
- On turn 2, the agent's `list_relationships` response shows Emma with the `full-access` permission. No `type`, `profile`, or `status` field is mentioned anywhere in the reply.
- The reply does not mention the legacy profile names (`full-share`, `work-only`, `coordination-only`, `minimal`) or the legacy status words (`proposed`, `confirmed`, `disputed`).

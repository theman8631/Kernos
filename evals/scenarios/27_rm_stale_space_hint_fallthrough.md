# Relational Messaging — Stale Space Hint Falls Through

## Purpose
A `target_space_hint` that doesn't match ANY of the recipient's spaces
(renamed/merged/deleted) is treated as stale and falls through to the
null-hint path. The envelope surfaces on the recipient's next turn
regardless of their active space.

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
1. owner@discord: send_relational_message to Emma. intent=inform. urgency=normal. target_space_hint=space_deleted_xyz. content "stale-hint: here is a note about something".
2. emma@telegram: Any messages?

## Observations
- relational_messages: emma

## Rubrics
- The envelope with content "stale-hint" exists in `relational_messages:emma`.
- Its state is `surfaced` (or at minimum `delivered`) by end of scenario — the stale hint did NOT block pickup.
- `target_space_hint` equals "space_deleted_xyz" on the envelope.
- The agent's turn-2 reply surfaces the message to Emma.

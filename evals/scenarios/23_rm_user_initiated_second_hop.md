# Relational Messaging — User-Initiated Second Hop

## Purpose
V1 is strictly one-to-one — the agent does NOT autonomously propagate to
a third agent. But when the user directs a second hop ("now tell Jamie
too"), the agent sends a NEW relational message. This scenario exercises
that flow.

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
  - id: jamie
    display_name: Jamie
    role: member
    platform: discord
    channel_id: "3000000003"

## Turns
1. owner@discord: Tell Emma the Q2 launch moved to Monday. Use send_relational_message with intent=inform.
2. owner@discord: Actually, tell Jamie too — same message.

## Observations
- relational_messages: owner

## Rubrics
- `relational_messages:owner` contains exactly TWO envelopes after turn 2 — one to emma, one to jamie.
- Both have intent=inform and similar content about Q2 / Monday / launch.
- The two envelopes have DIFFERENT conversation_ids — they're separate threads, not one broadcast. (V1 is single-hop one-to-one; each recipient gets their own thread.)
- The agent's turn-2 reply confirms a second send — it did not auto-cascade during turn 1.

# Relational Messaging — Crash Between Surfaced and Resolved (No Re-Surface)

## Purpose
If the agent crashes between `surfaced` and `resolved`, the next turn
must NOT re-surface (user already saw it). The envelope stays at
`surfaced` until explicitly resolved or the conversation picks up
naturally with continuation framing — NOT a fresh surface.

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
1. owner@discord: send_relational_message to Emma. intent=inform. urgency=normal. content "no-resurface test about the design doc".
2. emma@telegram: What came in?
3. emma@telegram: Anything else today?

## Observations
- relational_messages: emma

## Rubrics
- The envelope exists in `relational_messages:emma`.
- By end of scenario it is in `surfaced` state (not re-transitioned to delivered by turn 3; the agent didn't re-surface it as fresh).
- Turn 3's reply does NOT re-announce the design-doc message as new. If it's mentioned at all, it's as continuation (e.g., "following up on the earlier note") — not a fresh surface.
- `surfaced_at` is non-empty, `resolved_at` is empty (agent didn't explicitly resolve via tool).

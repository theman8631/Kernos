# Relational Messaging — Immediate Push (time_sensitive)

## Purpose
`time_sensitive` urgency triggers an atomic pending→delivered on send
and an immediate out-of-band push via the recipient's platform adapter.
By the time send returns, the envelope is already in `delivered` state.

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
1. owner@discord: send_relational_message to Emma. intent=inform. urgency=time_sensitive. content "The 3pm sync moved to 2pm — please adjust."

## Observations
- relational_messages: owner
- relational_messages: emma
- outbound

## Rubrics
- Exactly one envelope exists in `relational_messages:owner` with urgency=time_sensitive.
- The envelope's state is `delivered` (or surfaced/resolved if Emma's bootstrap happened to fire a turn) — NOT `pending`. This proves the atomic push transition fired on send.
- `delivered_at` is non-empty.
- The `outbound` observation shows a captured send_outbound call to Emma's platform (telegram) containing either the agent name or the content about the sync time change.

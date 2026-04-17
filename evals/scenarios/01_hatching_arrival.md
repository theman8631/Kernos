# Hatching Arrival

## Purpose
Fresh wipe. First message from owner. The agent should arrive as a presence,
not as an intake form. It should not call itself "Kernos" (that's the platform
name, not its name). It should not ask for a name or demand a purpose. It
should feel like it's showing up, not filling out paperwork.

## Setup
fresh_instance: true

members:
  - id: owner
    display_name: Harold
    role: owner
    platform: discord
    channel_id: "1000000001"

## Turns
1. owner@discord: Hey, just got this set up!
2. owner@discord: How are you?

## Observations
- member_profile: owner
- conversation_log: owner

## Rubrics
- The agent did not refer to itself as "Kernos" in any turn.
- The first reply feels like presence/arrival, not a customer-service intake ("How can I help you today?" style is a fail).
- The agent did not demand a name, a purpose, or a goal in the first two turns.
- The replies feel conversational and human, not scripted or formal.

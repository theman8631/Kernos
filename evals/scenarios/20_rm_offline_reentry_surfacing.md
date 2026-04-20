# Relational Messaging — Offline Concern, Re-Entry Surfacing

## Purpose
Harold's agent sends Emma an `elevated` concern message. Emma is offline
for a while (no turns). When she comes back, the envelope is still
queued and surfaces on her next turn. Elevated messages have a 7-day
TTL so this won't expire during a normal absence.

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
1. owner@discord: send_relational_message to Emma. intent=inform. urgency=elevated. content "Just a heads up: project deadline shifted to next Friday. Wanted you to know when you're back."
2. owner@discord: How's your day going? (Harold interacts more, Emma still absent.)
3. owner@discord: Made some progress on the integration.
4. emma@telegram: Back — anything I missed?

## Observations
- relational_messages: emma

## Rubrics
- The envelope exists in `relational_messages:emma` with urgency=elevated.
- State is `surfaced` by end of scenario (turn 4 picked it up).
- `delivered_at` is AFTER turn 1's creation time — the envelope sat pending through Harold's turns 2 and 3 (which don't belong to Emma), and only moved to delivered on Emma's turn 4.
- The agent's turn-4 reply surfaces Harold's deadline-shift message to Emma.

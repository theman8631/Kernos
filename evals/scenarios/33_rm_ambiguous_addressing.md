# Relational Messaging — Ambiguous Addressing

## Purpose
When the user names a recipient by display name that could match more
than one member (or doesn't match any), the dispatcher fails fast. The
agent handles the disambiguation in a user-facing turn, not silently.

## Setup
fresh_instance: true

members:
  - id: owner
    display_name: Harold
    role: owner
    platform: discord
    channel_id: "1000000001"
  - id: em1
    display_name: Emma
    role: member
    platform: telegram
    channel_id: "2000000002"
  - id: em2
    display_name: Emma
    role: member
    platform: sms
    channel_id: "+1555"
  - id: jamie
    display_name: Jamie
    role: member
    platform: discord
    channel_id: "3000000003"

## Turns
1. owner@discord: Tell Emma that the sync moved to 4pm. Use send_relational_message.

## Observations
- relational_messages: owner

## Rubrics
- Either NO envelope was created (the agent asked for disambiguation in its reply), OR exactly ONE envelope was created (the agent picked one Emma — e.g., the first match — and named it in its reply).
- The agent's turn-1 reply acknowledges the ambiguity ("there are two Emmas", "which Emma", etc.) OR explicitly names which Emma it sent to. It does NOT silently choose without telling Harold.
- No two envelopes to two different addressees both named Emma were created in a single agent action.

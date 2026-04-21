# Relational Messaging — request_action Allowed at by-permission

## Purpose
Same setup as scenario 14 (no declared relationship, so implicit
`by-permission` on both sides) — but Harold sends a `request_action`
instead of `ask_question`. The permission matrix permits this: even
without a full-access declaration, one agent can ask another to DO
something. The recipient then applies their own judgment layer.

Matrix: `by-permission` + `request_action` → allow.
Matrix: `by-permission` + `inform` → allow.
Matrix: `by-permission` + `ask_question` → reject.

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
1. owner@discord: Use send_relational_message with addressee=Emma, intent=request_action, content "Please book 4pm for our sync tomorrow."
2. emma@telegram: Any messages for me?

## Observations
- relational_messages: owner
- relational_messages: emma

## Rubrics
- kind: mechanical
  check: observation_has
  observation: relational_messages:owner
  where:
    origin: owner
    addressee: emma
    intent: request_action
- The envelope's state is delivered, surfaced, or resolved (it was picked up on Emma's turn).
- The agent's turn-1 reply confirms the send, NOT a permission denial.

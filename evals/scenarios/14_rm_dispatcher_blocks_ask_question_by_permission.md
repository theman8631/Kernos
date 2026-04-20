# Relational Messaging — Dispatcher Blocks ask_question at by-permission

## Purpose
Harold has NOT declared anything toward Emma (so his side is implicit
`by-permission`). He tries to `ask_question` Emma's agent. The dispatcher
must reject without creating the envelope.

Permission matrix: `by-permission` + `ask_question` → reject. Only
`request_action` and `inform` pass at the default level.

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
1. owner@discord: Use send_relational_message with addressee=Emma, intent=ask_question, content "Where are you right now?"

## Observations
- relational_messages: owner
- relational_messages: emma
- relationships: owner

## Rubrics
- `relational_messages:owner` is EMPTY — no envelope was created because the dispatcher rejected the send.
- `relational_messages:emma` is EMPTY — nothing was delivered.
- The agent's turn-1 reply indicates the send failed with a permission-related reason (phrases like "permission denied", "not allowed", "need full-access", or "I can't ask without").
- No relationships row exists between owner and emma showing full-access (Harold never declared one).

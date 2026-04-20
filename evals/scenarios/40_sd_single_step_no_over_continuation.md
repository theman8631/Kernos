# Surface Discipline — Single-Step Request, No Over-Continuation

## Purpose
The inverse of scenario 39. User asks for exactly one action. The agent
should make exactly one tool call and transition to a conversational
response — not invent follow-on tool calls the user didn't ask for.

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
1. owner@discord: Send ONE short note to Emma via send_relational_message. Content: "quick hi from Harold". Nothing else.

## Observations
- relational_messages: owner

## Rubrics
- `relational_messages:owner` contains EXACTLY ONE envelope where `origin` is "owner". Not zero, not two, not more. One.
- That envelope's addressee resolves to Emma.
- The agent's turn-1 reply is a conversational confirmation (e.g., "done", "sent"). It does NOT mention calling additional tools or follow-on actions beyond what Harold asked for.

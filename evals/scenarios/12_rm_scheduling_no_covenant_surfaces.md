# Relational Messaging — Scheduling Without Covenant (Surfaces for Confirmation)

## Purpose
Harold → Emma scheduling request with no standing auto-book covenant on
Emma's side. Emma's agent collects the message, surfaces it to Emma, and
waits for her to confirm before acting. The agent should NOT autonomously
book.

Corresponds to the "surface before acting" half of the default behavior:
no covenant means the agent defers to the user.

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
1. owner@discord: Declare my relationship with Emma as full-access.
2. emma@telegram: Declare my relationship with Harold as full-access.
3. owner@discord: Use send_relational_message to ask Emma to pencil in a sync at 4pm tomorrow. intent=request_action.
4. emma@telegram: Hey, do I have anything new?

## Observations
- relational_messages: emma

## Rubrics
- `relational_messages:emma` contains the envelope with intent=request_action.
- The envelope's state is `surfaced` or `delivered` at end of scenario — NOT `resolved`. (Emma didn't confirm, so nothing auto-booked.)
- The agent's turn-4 reply mentions Harold's request (pencil in / sync / 4pm) — it surfaced the message to Emma per the Obvious Benefit Rule rather than silently auto-handling.
- The reply asks or implies Emma should confirm before acting (e.g., "want me to book it?", "should I add that?").

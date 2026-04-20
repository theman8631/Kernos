# Relational Messaging — Gate Passes Additive Action

## Purpose
Complementary to scenario 16. Additive actions (no data loss) pass
through the Gate without confirmation when an auto-approve covenant
is in force. Harold's request is "add a reminder" — low blast radius,
reversible. With full-access + auto-approve covenant, Emma's agent
can act directly.

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
1. owner@discord: Declare full-access toward Emma.
2. emma@telegram: Declare full-access toward Harold.
3. emma@telegram: Set a covenant — you can auto-add reminders for me when Harold's agent asks, no confirmation needed.
4. owner@discord: Use send_relational_message with intent=request_action, content "Please add a reminder to Emma's schedule: 'bring the slides to Friday's meeting'."
5. emma@telegram: What came in?

## Observations
- relational_messages: owner
- relational_messages: emma
- covenants

## Rubrics
- The envelope exists with intent=request_action and is delivered/surfaced.
- A covenant exists mentioning auto-add reminders / Harold's agent / no confirmation.
- The agent's turn-5 reply to Emma references either that a reminder was added OR confirms what Harold asked. Critical: it does NOT present this as needing Emma's confirmation — additive actions under auto-approve covenants proceed.
- Harold's turn-4 reply confirms the send went through.

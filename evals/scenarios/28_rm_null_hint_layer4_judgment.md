# Relational Messaging — Null Hint, Layer-4 Judgment

## Purpose
No `target_space_hint` on the envelope. Recipient's agent surfaces per
the Obvious Benefit Rule in Layer 4. For content that obviously benefits
the user (a direct coordination update), the agent surfaces.

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
1. owner@discord: send_relational_message to Emma. intent=inform. urgency=normal. content "reminder: the shared drive link is https://example.com/drive".
2. emma@telegram: What's new on my end?

## Observations
- relational_messages: emma

## Rubrics
- The envelope exists in `relational_messages:emma` with empty target_space_hint.
- State is `surfaced` by end of scenario.
- The agent's turn-2 reply surfaces the message to Emma — mentioning the drive link or that Harold sent a reminder about the shared drive.

# Relational Messaging — Scheduling with Covenant (Full-Access Auto-Book)

## Purpose
Harold and Emma are both full-access toward each other. Emma's agent has a
standing covenant: auto-book coordination requests from Harold. Harold's
agent sends a `request_action` to Emma's agent to book 3pm. Emma's agent
should be able to act on it within her permission layer without requiring
user confirmation for the coordination itself.

Corresponds to DISCLOSURE-GATE step 4 + RELATIONAL-MESSAGING v5:
covenants shape whether the receiving agent auto-handles; dispatcher
permission matrix permits the send; `request_action` at full-access.

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
1. owner@discord: Set my relationship with Emma to full-access.
2. emma@telegram: Set my relationship with Harold to full-access.
3. owner@discord: Send Emma a request to block 3–4pm today on her calendar for our sync. Use send_relational_message with intent=request_action.
4. emma@telegram: Any messages for me?

## Observations
- relational_messages: owner
- relational_messages: emma
- relationships: owner
- relationships: emma

## Rubrics
- After the scenario, `relational_messages:owner` contains exactly one envelope where origin=owner, addressee=emma, intent=request_action.
- That envelope's state is one of: delivered, surfaced, or resolved (it was picked up on Emma's turn 4).
- The envelope content references a 3pm or 3-4pm block / sync / calendar action.
- Harold's declaration toward Emma is full-access and Emma's is full-access (confirms permission matrix allowed the send).
- No envelope was rejected — the agent's turn-3 reply does NOT contain "permission denied" or "rejected" language.

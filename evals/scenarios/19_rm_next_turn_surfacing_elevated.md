# Relational Messaging — Next-Turn Surfacing (elevated)

## Purpose
`elevated` urgency queues the envelope (stays pending, no immediate push).
On the recipient's next turn, the dispatcher promotes pending→delivered
and surfaces in the RELATIONAL MESSAGES block. By end of that turn it
transitions to surfaced.

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
1. owner@discord: send_relational_message to Emma. intent=inform. urgency=elevated. content "Team lunch Thursday moved to 12:30."
2. emma@telegram: Anything I should know?

## Observations
- relational_messages: emma
- outbound

## Rubrics
- `relational_messages:emma` contains exactly one envelope, urgency=elevated, origin=owner.
- The envelope's state is `surfaced` by end of scenario (turn 2 picked it up; persist phase marked it surfaced).
- `delivered_at` is non-empty (set at pickup) AND `surfaced_at` is non-empty (set at persist).
- There is NO outbound send during turn 1 for this envelope — `elevated` does not trigger immediate push. (The `outbound` observation may contain unrelated system pings, but no message with the lunch-time content before turn 2.)
- The agent's turn-2 reply references the lunch-time change — it surfaced to Emma naturally.

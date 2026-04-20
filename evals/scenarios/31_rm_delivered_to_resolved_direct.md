# Relational Messaging — Direct delivered → resolved (Agent Auto-Handles)

## Purpose
When a covenant auto-handles an incoming message with no user
involvement, the agent calls `resolve_relational_message` with
`auto_handled=true`, transitioning delivered→resolved DIRECTLY (skipping
the user-visible surfaced state). This avoids duplicate-surface risk.

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
1. emma@telegram: Set a covenant — standing auto-approve for Harold's scheduling notifications. When Harold's agent informs me of a time change, just acknowledge and move on, no need to surface.
2. owner@discord: send_relational_message to Emma. intent=inform. urgency=normal. content "auto-handled: the 10am review moved to 11am".
3. emma@telegram: Anything I need to see?

## Observations
- relational_messages: emma
- covenants

## Rubrics
- The envelope exists with content "auto-handled" / "10am" / "11am".
- State is `resolved` by end of scenario (the agent used `resolve_relational_message` with auto_handled=true OR persist naturally surfaced+resolved).
- `surfaced_at` may or may not be set depending on whether the agent auto-handled before persist. If resolved_at is set and surfaced_at is empty, the direct delivered→resolved path fired.
- The covenant exists in the store referencing auto-approve / Harold / scheduling.

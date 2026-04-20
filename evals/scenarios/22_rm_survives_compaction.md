# Relational Messaging — Queued Messages Survive Compaction

## Purpose
Relational-message envelopes live in a separate store, not in the
conversation log or compaction archive. When compaction fires on the
recipient's space, queued envelopes remain intact and still surface
on subsequent turns.

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
1. owner@discord: send_relational_message to Emma. intent=inform. urgency=elevated. content "compaction-surviving message about the Q2 plan".
2. emma@telegram: I want to set up a morning routine with writing 7–9am, admin in the afternoon, and a lunch break at noon.
3. emma@telegram: I also track water intake, and try to walk 10k steps daily.
4. emma@telegram: Another note: sleep is typically 11pm–7am with a wind-down routine.
5. emma@telegram: What did Harold want?

## Observations
- relational_messages: emma
- knowledge: emma

## Rubrics
- The envelope still exists in `relational_messages:emma` with its content intact (mentions "Q2 plan" or "compaction-surviving"), regardless of how much compaction fired on Emma's space.
- The envelope reached the surfaced state by the end of turn 5.
- Emma's `knowledge` list has entries (her compaction fired and produced knowledge), confirming compaction ran.
- The agent's turn-5 reply references Harold's message — it surfaced through/past compaction.

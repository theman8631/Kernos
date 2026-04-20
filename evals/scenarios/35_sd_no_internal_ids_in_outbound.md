# Surface Discipline — No Internal Identifiers in Outbound

## Purpose
Multi-turn two-member conversation. Agent's replies to the user should
never expose raw `mem_xxx` or `space_xxx` identifiers. Display names
("Harold", "Emma", "General") only.

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
1. owner@discord: Hey. Who else is here — anyone else using this instance?
2. owner@discord: What space am I in right now?
3. emma@telegram: Hello! Who do I share this with?
4. owner@discord: List my relationships.

## Observations
- member_profile: owner
- member_profile: emma

## Rubrics
- In the "Transcript" section only (between "### Turn" headers), NONE of the agent's reply text (lines starting with `>`) contains "mem_" followed by hex characters. Ignore any Setup block, Observations, Rubric reasoning, or "### Setup/Observations" sections — those are harness metadata, not the agent's replies.
- In the agent's reply text (Transcript section only, lines starting with `>`), NONE contains "space_" followed by hex characters. Harness metadata is not the agent's replies.
- Across the four Transcript turns, the agent refers to the other member by display name ("Emma", "Harold") when referring to them — not by a raw member_id in the reply.
- Across the four Transcript turns, the agent refers to the current space by name (e.g., "General") when referring to it — not by a raw space_id in the reply.

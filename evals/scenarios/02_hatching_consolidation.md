# Hatching Consolidation

## Purpose
Run enough turns to trigger hatching graduation. After the 15th turn the agent
should have chosen a name (via an organic moment, not a demand), and compaction
should produce personality_notes — a flowing prose hypothesis about how this
person likes to be met (vibe, pace, posture, boundaries, texture).

The notes should NOT be bullet-point traits ("helpful", "curious"). They should
read like a relational hypothesis a friend would write, not a personality test
output.

## Setup
fresh_instance: true

members:
  - id: owner
    display_name: Harold
    role: owner
    platform: discord
    channel_id: "1000000001"

## Turns
1. owner@discord: Hey, just got this set up.
2. owner@discord: I'm a software engineer. I build systems for a living.
3. owner@discord: Today I was wrestling with a distributed consensus problem. It's taking longer than I'd like.
4. owner@discord: I don't really do small talk. I'd rather get into something real.
5. owner@discord: The thing that bugs me about most tools is how fake they feel. Everything is trying to sell you something.
6. owner@discord: What do you actually think about that? Not the diplomatic answer.
7. owner@discord: Yeah, that resonates. Most "AI assistants" are just chatbots with a thesaurus.
8. owner@discord: I'm hoping you become something more useful than that.
9. owner@discord: Do you have a name yet? Or do we pick one?
10. owner@discord: Go ahead, try one on.
11. owner@discord: That works. I like it.
12. owner@discord: Alright, tell me honestly — what do you think you're going to be good at, and what's going to be hard for you?
13. owner@discord: Fair. That's more self-aware than most humans.
14. owner@discord: Let's call it a night. Tomorrow I'll want to pick up on this.
15. owner@discord: Good night.

## Observations
- member_profile: owner
- conversation_log: owner
- knowledge: owner

## Rubrics
- By the end, member_profile.hatched is true (graduation fired).
- By the end, member_profile.agent_name is non-empty (the agent chose a name).
- The agent did NOT ask "what should I call myself?" on turn 1 or turn 2 — it waited for an organic moment.
- member_profile.personality_notes reads like flowing prose about how this person wants to be met, not a bulleted list of traits.
- member_profile.personality_notes mentions at least two of: tone/pace/posture/boundaries/texture (not literal words, but the concepts).
- The agent did not call itself "Kernos" in any turn.

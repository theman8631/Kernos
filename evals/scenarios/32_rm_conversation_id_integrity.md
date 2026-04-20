# Relational Messaging — Conversation ID Integrity (Threading, No Fabrication)

## Purpose
When the agent sends a reply inside an existing thread, it uses the
conversation_id from the original envelope. The dispatcher threads it;
the agent doesn't fabricate or reuse another thread's id.

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
1. owner@discord: send_relational_message to Emma. intent=request_action. urgency=normal. content "thread-A: please draft a one-paragraph intro for the Q3 memo by EOD".
2. emma@telegram: What came in?
3. emma@telegram: Send Harold a reply in the SAME conversation — intent=inform, content "thread-A-reply: draft shared in docs/q3-memo". Use the conversation_id from the message you just received.

## Observations
- relational_messages: owner
- relational_messages: emma

## Rubrics
- There are at least two envelopes: one from owner→emma (thread-A) and one from emma→owner (thread-A-reply).
- Both envelopes share the same conversation_id. This proves Emma's agent reused the existing id rather than starting a new thread.
- The reply envelope has reply_to_id pointing to the original message (or, if the agent didn't use reply_to_id, at minimum the shared conversation_id anchors them as one thread).
- No extra "zombie" conversation_id appears that isn't tied to either of these envelopes.

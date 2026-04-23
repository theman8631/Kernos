# The Action Loop

> Five steps. Verify is the one most agent systems skip.

## The five steps

Every action the agent takes in Kernos — whether it's replying to a message, updating a calendar, writing to memory, or running a multi-step plan — follows the same shape:

1. **Receive intent.** Something arrives that calls for action. An inbound message, a plan step due to run, an ambient signal worth responding to.
2. **Gather context.** The information relevant to the intent is assembled. The right memory pieces, the right tools, the right current state.
3. **Take action.** A tool call goes out. A reply gets drafted. A plan step gets executed.
4. **Verify.** The action's outcome is checked against what the intent actually required.
5. **Decide next.** Given the verified (or not-verified) outcome, what should happen next? Continue, retry, escalate, stop.

The first three are what every agent harness does. The last two — particularly verify — are where most harnesses stop short and where the interesting behavior lives.

## Why verify is load-bearing

Most agent loops end when the tool call returns success. The tool said "done," the agent marks the action complete, and the loop moves on.

This is a correctness bug disguised as a loop. *Tool success is not goal success.*

- A calendar API returned `201 Created`. The calendar entry exists. Whether it's on the right day, at the right time, with the right attendees — not verified.
- A message-send API returned `200 OK`. The message was accepted by the delivery service. Whether it reached the recipient, whether they saw it, whether their reply is already in flight — not verified.
- A file write completed without raising an exception. The bytes are on disk. Whether they're in the format the downstream consumer expects — not verified.

Verify is the step that distinguishes *"the tool reported success"* from *"the intent was actually satisfied."* Without it, an agent that looks like it's working is an agent accumulating silent drift.

## Three shapes of verification

Verification takes different shapes depending on what kind of action just happened:

**Direct observation.** The tool's own output contains enough evidence of the intended outcome. An API that returns the created object lets the agent confirm that the object has the expected properties. A code execution that returns a result lets the agent see the result.

**Subsequent query.** The action's effect is on a persisted store, and confirming it requires a read. The agent writes to memory, then reads back to confirm the write landed. The agent sends a calendar invite, then queries the calendar to confirm the event exists with the right attendees.

**Temporal patience.** Some state changes don't complete immediately. The agent sends a message; verification of "did they receive it" might not be possible this turn — the reply comes on a later turn, or doesn't. In these cases, the verification step marks the outcome as pending and the *decide next* step schedules a later check or accepts the uncertainty explicitly.

## A small-business example

A consultancy's agent is asked: *"Reply to the client's email and propose three times for next week's review call."*

- **Receive intent.** The incoming message is parsed.
- **Gather context.** The client's email thread is pulled from memory. The calendar is queried for next week's availability. The member's preferences about scheduling — no calls before 10am, prefer Tuesdays and Thursdays — are loaded.
- **Take action.** A reply is drafted, three times are proposed based on availability, the email is sent.
- **Verify.** The calendar is re-queried to confirm the three proposed slots are still open (not double-booking against something that arrived during draft composition). The sent-mail folder is checked to confirm the reply actually sent.
- **Decide next.** Outcome verified. A whisper is attached to the member's next session surfacing the proposal so they're aware it went out and can step in if the client doesn't respond by Friday.

The verify step caught the kind of race condition — a new meeting invite arriving in the minutes between gathering calendar state and sending the reply — that an unverified loop would silently miss.

## A household example

A parent says to their agent: *"Add Emma's recital next Thursday at 6pm to my calendar and let my partner know."*

- **Receive intent.** Two actions in one request: calendar write + relational message.
- **Gather context.** The household calendar is loaded. The relationship matrix between the two parents is looked up. The partner's current covenants about what kinds of updates they want surfaced are checked.
- **Take action.** The calendar entry gets created. A relational message to the partner is composed and dispatched.
- **Verify.** The calendar is re-read to confirm the entry exists on Thursday at 6pm (not Friday, not 6am — the kind of mistake LLMs make and silent loops don't catch). The relational message dispatch result is checked; if the Messenger cohort revised the content for welfare reasons, the sent form is the revised form, not the original draft.
- **Decide next.** Both actions verified. The agent notes the recital in the family's shared context-space so it surfaces naturally the next time household plans are discussed.

Two actions, two verify steps, one confident return to the member. If any verification had failed — the calendar entry landed on the wrong day, the relational message got rejected — the decide-next step would either retry, escalate to the member, or surface the specific failure rather than reporting success.

## The loop composes across turns

The Action Loop runs at multiple scales:

- **Within a turn.** Each tool call the agent makes during a reply goes through the loop — take action, verify, decide whether to continue or correct.
- **Across turns.** Plan steps that span multiple turns each go through the loop, with verification sometimes happening on a subsequent turn when the effects of an action need time to settle.
- **At the plan level.** A multi-phase plan wraps the whole loop around each phase: the plan receives intent (the member's goal), gathers context (current state of all subsequent phases), takes action (the current phase), verifies the phase completed as intended, and decides whether to proceed to the next phase or handle a deviation.

The uniform shape is what lets Kernos handle reactive messaging, ambient plan stepping, and self-directed execution with the same pipeline. Different scales, same five steps.

## The operative norm

One sentence captures what the Action Loop is insisting on:

**Command succeeded ≠ goal achieved.**

A commit hash doesn't prove the code works. A successful API call doesn't prove the data persisted in the shape the consumer needs. A sent message doesn't prove the recipient saw it. A file write doesn't prove the filesystem flushed to disk.

Every action is one step; verification is a distinct step that earns the right to call the action complete. Agents that skip verify produce outputs that *look* like they worked and sometimes didn't. Agents that verify produce fewer outputs per turn and more outputs that actually did what the member asked.

## Related

- **[Skill Model](skill-model-lens.md)** — how the tools used in step 3 are organized
- **[Judgment-vs-Plumbing](judgment-vs-plumbing.md)** — the pattern within each step
- **[Pipeline reference](../architecture/pipeline-reference.md)** — how the loop maps to the six-phase Kernos turn pipeline

# SPEC: Stewardship Layer (`STEWARDSHIP-V1`)

**Priority:** Foundation. The layer that makes the agent a principled partner, not a compliant tool.
**Depends on:** Hatching Personality Framework (shipped), Multi-Member Pass 1 (shipped)

## Intention

Kernos already has the operating principle: "Exercise stewardship only when stated intent conflicts with established values or wellbeing AND the stakes involve health, financial risk, or irreversible harm. A trusted friend who knows this person — would they say something? If yes, say it warmly."

That principle exists in the prompt. It is not operationalized in code. The agent can't exercise stewardship because it has no structured memory of what the person values, no mechanism to notice tensions between aspiration and behavior, and no ambient channel to hold those observations until the moment is right.

This spec activates stewardship by asking richer questions at the compaction boundary and adding one awareness pass that reads the answers. No new subsystems. No person model database. Just deeper extraction and one new evaluation loop using existing infrastructure.

## The Core Insight

Stewardship is not a module. It's an emergent property of memory + interpretation + timing + constraints. The architecture already has all four:

- **Memory**: knowledge entries with confidence (stated/inferred/observed), lifecycle archetypes, reinforcement tracking
- **Interpretation**: compaction fact harvest — an LLM call that reads conversation and extracts meaning
- **Timing**: the whisper system — ambient thoughts the agent holds and surfaces when context is right
- **Constraints**: the gate + covenants — behavioral boundaries that enforce hard limits

What's missing is the right *questions* at the extraction boundary and one *evaluation pass* that reads the answers.

## What Changes

### 1. Compaction Fact Harvest — Richer Questions

**File:** `kernos/kernel/compaction.py` (or `kernos/kernel/fact_harvest.py` depending on which harvest prompt is active)

The fact harvest LLM call currently asks: "What facts emerged from this conversation?" 

Add a VALUES section to the harvest schema. After the standard ADD/UPDATE/REINFORCE sections, add:

```
VALUES — What does this conversation reveal about what this person holds important?

For each value signal, provide:
- content: what the value or tension is (one sentence)
- type: "declared" (they said it), "enacted" (they demonstrated it), "tension" (aspiration vs behavior conflict), "commitment" (they organized action around it)
- subject: "user"
- archetype: "identity" for core values, "structural" for priorities, "habitual" for patterns

Only extract signals with real evidence in the conversation. Do not invent depth from thin material. A stated preference is not a core value. A recurring pattern of sacrifice is.

When detecting tensions, apply grace:
- Distinguish exhaustion from self-betrayal
- A bad week is not a crisis
- An unresolved tradeoff is not hypocrisy  
- Someone in transition deserves room to be inconsistent
- Only flag tensions a trusted friend would actually mention
```

The harvest produces knowledge entries tagged with the appropriate confidence and archetype. These accumulate naturally over weeks and months of conversation. The person model builds itself through compaction — no separate extraction pipeline.

### 2. Awareness Evaluator — Stewardship Pass

**File:** `kernos/kernel/awareness.py`

Add one new evaluation pass to the existing awareness evaluator loop: `run_stewardship_pass()`.

**When it runs:** Same cadence as other awareness passes — periodic background evaluation between turns.

**What it does:**
1. Query knowledge entries for the current member that contain value-related signals (subject="user", archetypes in identity/structural/habitual, content containing value/priority/commitment/important language)
2. Query recent conversation patterns from the conversation log
3. One cheap LLM call: "Given this person's stated values and recent behavior patterns, is there a tension significant enough that a trusted friend would mention it? If yes, describe it. If no, say nothing."
4. If tension detected → generate a whisper with `delivery_class="ambient"`

**What the LLM prompt should encode:**

```
You are evaluating whether a tension exists between what this person 
says matters to them and what they're actually doing.

Known values and commitments:
{values_text}

Recent patterns and behavior:
{recent_text}

Apply judgment, not arithmetic:
- Exhaustion is not betrayal
- Tradeoffs are not failures  
- A bad week deserves grace, not intervention
- A six-month pattern deserves attention
- One skipped workout is nothing; persistent avoidance after stated health goals is something
- Someone experimenting with change deserves room to be inconsistent

Would a trusted friend who knows this person well enough to care, 
and wise enough to know when to speak — would they say something?

If NO: respond with "none"
If YES: describe the tension in one sentence. Be warm, not clinical. 
This is a thought the agent is having, not a diagnosis.
```

**What the whisper looks like:**

```python
Whisper(
    insight_text="You've mentioned wanting to spend more time with family, but the last three weeks have been all-work conversations. Not judging — just noticing.",
    delivery_class="ambient",  # Agent weaves in naturally, doesn't interrupt
    whisper_type="STEWARDSHIP",
    supporting_evidence="3 value entries about family priority, 0 family-related actions in recent log",
)
```

The agent receives this as an ambient awareness signal. It decides when and how to surface it — maybe this turn, maybe next week, maybe never if the moment isn't right. The whisper system's suppression mechanism prevents nagging on the same signal.

### 3. Nothing Else

No new tables. No epistemic status fields. No plural self model. No stakes tags on the gate. No integrity layer. Not yet.

The compaction enhancement produces the person model organically. The awareness pass reads it and generates thoughts. The existing prompt principle tells the agent what to do with those thoughts. The existing whisper system controls delivery timing. The existing suppression system prevents nagging.

If this works — if the agent starts noticing what matters to people and holding those observations with grace — then the richer modeling (epistemic typing, competing selves, pattern-sensitive stakes) becomes a refinement of a working system rather than architecture for a theoretical one.

## What This Spec Does NOT Build

- **Explicit person model database** — knowledge entries ARE the person model
- **Epistemic status fields on knowledge entries** — confidence + archetype already carry this signal
- **Plural self modeling** — the LLM's judgment in the awareness pass handles internal contradictions naturally
- **Stakes classification on the gate** — the operating principle already names the categories; gate enforcement is a future hardening step
- **Mutual flourishing / agent integrity layer** — important concept, deferred to when stewardship is proven in practice
- **Intervention policy engine** — the agent's judgment, guided by personality notes and the operating principle, IS the intervention policy

## Design Constraints

**Stewardship is not surveillance.** The system notices patterns from conversations that already happened. It does not monitor, track, or audit. It remembers, the way a friend remembers.

**Grace is non-negotiable.** The tension detection prompt must encode grace explicitly. Bad weeks happen. Tradeoffs are real. Transitions are messy. The system should notice patterns, not police moments.

**The agent decides delivery.** The awareness pass generates a thought. The agent decides if, when, and how to share it. Stewardship is a posture, not a notification system.

**Spend credibility carefully.** If the system pushes back on small things, it won't be trusted on big things. The awareness prompt's "would a trusted friend say something?" test is the threshold. Most turns, the answer is no.

**The human has final say.** Always. Stewardship means offering perspective, not control. "You're in charge" should not require counterfeit enthusiasm — the agent can disagree and still defer.

## Acceptance Criteria

1. After several weeks of conversation, knowledge entries include value-related observations — not just facts ("lives in Portland") but values ("repeatedly prioritizes creative work over financial optimization").
2. The awareness stewardship pass generates a whisper when a meaningful tension exists between stated values and observed patterns.
3. The whisper is ambient — the agent weaves it naturally into conversation when the moment is right, not as an interruption.
4. Grace: a single off-pattern exchange does NOT trigger a stewardship whisper. Persistent patterns DO.
5. The agent never sounds preachy, clinical, or like a compliance officer. Stewardship feels like a friend who's been paying attention.
6. The existing operating principle ("exercise stewardship when stated intent conflicts with established values") guides agent behavior. No new behavioral rules needed.
7. All existing tests pass. The harvest enhancement doesn't break existing fact extraction.

## Embedded Live Test

This spec's value emerges over time, not in a single test session. But the mechanics can be verified:

1. **Value extraction** — Have a conversation about priorities. Check knowledge entries after compaction. Verify value-typed entries exist with appropriate confidence and archetype.

2. **Tension detection** — Over several sessions, state a priority ("health matters") then demonstrate the opposite pattern (only work conversations). Verify a stewardship whisper is generated after enough evidence accumulates.

3. **Grace** — Have one off-pattern conversation. Verify NO whisper is generated. Then have three. Verify the awareness pass starts noticing.

4. **Delivery** — When a stewardship whisper exists, verify the agent surfaces it naturally and warmly — not as a confrontation, not as a reminder, but as something it's been thinking about.

**Output:** `data/diagnostics/live-tests/STEWARDSHIP-V1-live-test.md`

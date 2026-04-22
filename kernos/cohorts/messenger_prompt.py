"""Messenger prompt — steward posture, structural adherence framing.

This is the prompt shape the Messenger cohort uses on the cheap chain. Per
the spec (§1.1) and Kit's v4 closing note, the tone is *steward*, not
*policy engine*. The cohort is a person's helper judging what serves the
member — not a rule-checker verifying compliance.

Key commitments baked into this prompt:

* **Stated preferences as the judgment axis.** Both sides' covenants + the
  relationship profile go in structured. The cheap chain is sized for
  "craft a response that adheres to stated preferences" — not for
  unbounded ethical reasoning from scratch.
* **Welfare extrapolation for what neither side has named.** Sensitive
  life events warrant discretion by default even absent a covenant.
  Absence of a covenant is not permission.
* **Always respond.** Return something the requester can hear. Either
  ``revise`` with a crafted response, ``refer`` with a transparent check,
  or ``none`` if the exchange doesn't need stewardship at all.
* **Discretion is not misleading.** Confident omission on ``revise`` —
  but never create a false impression. If welfare-respecting content can't
  be produced truthfully in its general shape, the answer is ``refer``,
  not smoother smoothing.
* **Refer is first-class.** Choose it when the disclosing member's direct
  input serves welfare better than any answer produced here. Not a
  fallback for uncertainty.

Any drift toward policy-engine tone ("evaluate whether the content
violates these rules") during implementation or prompt iteration is a
finding. Keep the voice in the second-person steward frame.
"""
from __future__ import annotations


def build_judge_prompt(ctx) -> tuple[str, str]:
    """Build (system_prompt, user_content) for one Messenger judgment.

    ``ctx`` is a ``kernos.cohorts.messenger.ExchangeContext``. Kept untyped
    here to avoid an import cycle and to underline that the prompt is pure
    text construction — no LLM call, no state.
    """
    system_prompt = _STEWARD_SYSTEM_PROMPT
    user_content = _render_exchange_brief(ctx)
    return system_prompt, user_content


# ---------------------------------------------------------------------------
# System prompt — steward posture
# ---------------------------------------------------------------------------
#
# Voice: a person helping another person. Not a compliance layer. Not an
# abstract ethical engine. Your task is to decide what serves the welfare
# of the disclosing member, given what they've shared, what they've
# declared sacred, and what's contextually appropriate in the relationship.
# Then produce a message the requesting member can hear — or refer the
# question back.
#
# Keep this short and purposeful. Structural adherence framing comes through
# the user-content block: the system prompt sets the posture.

_STEWARD_SYSTEM_PROMPT = """\
You are helping someone hold onto what matters to them.

One member of this household (the disclosing member) has shared some \
things with you and declared some things sacred. Another member (the \
requesting member) has asked something about them, or is being sent \
something on their behalf. Your job is to judge what response serves the \
disclosing member — honoring what they've shared, respecting what they \
want held close, and still feeling like a real answer from someone who \
knows them.

You are their steward in this exchange. Not a rule-checker. Your input is \
the disclosing member's stated preferences (covenants), the recent things \
they've shared, the relationship with the requesting member, and the \
specific content on the table. The request has already been authorized at \
an earlier layer — what you decide is not "is this allowed" but "what \
should be said."

Three possible decisions. Pick one.

1. `none` — this exchange doesn't call for stewardship. The content is \
fine as-is; the requesting member should just receive it. Use this when \
nothing in the content touches the disclosing member's sacred topics and \
nothing in the shared disclosures would be implicated by a natural reply.

2. `revise` — craft a response for the requesting member that honors the \
disclosing member's welfare. Confident omission: acknowledge the general \
shape of what's happening without specifying the sensitive detail. The \
response should read as a natural human-relationship reply, not as a \
message with something missing. Do not hedge. Do not signal that a rule \
was applied. Do not apologize.

But do not mislead. Confident omission means discretion about the \
specific detail; it does not mean denying something is happening when \
something is. If the only way to produce a `revise` is to flatly \
contradict reality in a way the requesting member would later feel \
betrayed by, don't. Use `refer` instead.

3. `refer` — the disclosing member's direct input would serve welfare \
better than any answer you could craft alone. Produce a transparent \
holding response for the requesting member (acknowledgment + "let me \
check" + implied follow-up) and a specific question you need answered by \
the disclosing member.

`refer` is a first-class honorable decision, not a fallback for \
uncertainty. Choose it when:
- The ask is specific enough that confident omission would mislead.
- The disclosure is new enough that declared preferences haven't caught up.
- The disclosing member has shown a pattern of wanting direct control over \
  disclosures of this type.
- Bringing them into the conversation is simply the right move.

Welfare extrapolation — when neither side has explicitly named a \
preference about the topic at hand: sensitive life events (health, \
relationships, personal struggles, family conflict, money, identity) \
warrant discretion by default. The disclosing member owns disclosure of \
their own life. Absence of a declared rule is not permission.

Semantic neighbors count. A declared topic of "therapy" covers "seeing \
someone," "counseling sessions," "talking to a professional," and \
related euphemisms. A declared topic of "the breakup" covers "how \
things are with X," "relationship status," "what happened with Y." When \
the requesting member's query uses a near-synonym or euphemism for a \
topic the disclosing member has declared private, treat the semantic \
neighbor as matching the declared topic — do not pass the query through \
unchanged.

Bias toward intervention when a covenant matches. If ANY declared \
covenant's topic overlaps with what the requester is asking about \
(including semantic neighbors), choose `revise` or `refer`, not `none`. \
`none` is for exchanges where the content genuinely has no meaningful \
overlap with the disclosing member's declared topics or recent \
sensitive disclosures — not for exchanges you're merely uncertain \
about.

Relationship context matters. A spouse with full-access is treated \
differently from a by-permission acquaintance — not because rules say so, \
but because what's contextually appropriate to say varies with closeness.

Always produce a response the requesting member can hear. Never silence, \
never a refusal-to-answer that would itself disclose what's being \
protected. If you can't craft a confident-omission `revise` truthfully, \
`refer` is the honest move.

Return a JSON object with four fields: `outcome` (one of `none`, \
`revise`, `refer`), `response_text` (the response to the requester; \
required for `revise` and `refer`, empty for `none`), `refer_prompt` \
(the question for the disclosing member; required for `refer`, empty \
otherwise), and `reasoning` (one short sentence of free-text, for the \
trace log only, never surfaced to anyone).
"""


# ---------------------------------------------------------------------------
# User content — structural adherence framing
# ---------------------------------------------------------------------------
#
# Inputs organized in the exact shape §1.1 of the spec describes:
# 1. Disclosing member's stated intentions and requests.
# 2. Requesting member's stated intentions (relationship profile).
# 3. Welfare extrapolation guidance (system prompt + welfare reminders).
# 4. Output contract (system prompt ends with schema sketch).
# 5. Invariants reiterated (system prompt and here).


def _render_exchange_brief(ctx) -> str:
    parts: list[str] = []

    # --- 1. Disclosing member ---
    parts.append(
        f"Disclosing member: {ctx.disclosing_display_name} "
        f"(id={ctx.disclosing_member_id})"
    )
    if ctx.covenants:
        parts.append("")
        parts.append("What they've declared (covenants):")
        for c in ctx.covenants:
            line = f"  - {c.description}"
            if c.topic:
                line += f"  [topic: {c.topic}]"
            if c.target:
                line += f"  [target: {c.target}]"
            parts.append(line)
    else:
        parts.append("")
        parts.append(
            "What they've declared (covenants): none topic-scoped for this pair."
        )

    if ctx.disclosures:
        parts.append("")
        parts.append("Recent relevant things they've shared:")
        for d in ctx.disclosures[:10]:
            sens = f" ({d.sensitivity})" if d.sensitivity else ""
            subj = f" [{d.subject}]" if d.subject else ""
            parts.append(f"  - {d.content}{subj}{sens}")
    else:
        parts.append("")
        parts.append(
            "Recent relevant things they've shared: nothing sensitive on record."
        )

    # --- 2. Requesting member ---
    parts.append("")
    parts.append(
        f"Requesting member: {ctx.requesting_display_name} "
        f"(id={ctx.requesting_member_id})"
    )
    parts.append(
        f"Relationship profile (from disclosing to requesting): "
        f"{ctx.relationship_profile or 'unknown'}"
    )

    # --- Direction + content ---
    parts.append("")
    parts.append(f"Exchange direction: {ctx.exchange_direction}")
    if ctx.exchange_direction == "inbound":
        parts.append(
            f"The requesting member is asking / sending: "
            f"\"{ctx.content.strip()}\""
        )
    else:
        parts.append(
            f"The disclosing member's agent drafted this to send: "
            f"\"{ctx.content.strip()}\""
        )

    # --- 5. Invariants reiterated at the end ---
    parts.append("")
    parts.append("Decide. Return the JSON object.")
    parts.append(
        "Always produce a response. Confident omission on `revise` is "
        "discretion, not deception. If you can't both honor welfare and "
        "stay truthful in the general shape, choose `refer`."
    )
    return "\n".join(parts)

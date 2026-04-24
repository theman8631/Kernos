---
scope: team
type: note
pattern: long-term-client-relationship
consumer: gardener
---

# Long-term client relationship

For sustained one-on-one professional relationships where the relationship itself is the work. Covers psychotherapy, executive coaching, life coaching, spiritual direction, financial advisory (high-touch), physical therapy (long-arc cases), counseling relationships of many kinds. Defining properties: extended 1-on-1 contact over months to years, structured confidentiality (legal or professional obligations constrain handling), session-based rhythm, progress is non-linear and hard to measure, the provider maintains clinical/professional boundaries, and the member's state at any point is partly a function of their state across many prior points.

Critically distinct from `client-project` (no discrete deliverable, no time-bounded close), `legal-case` (adversarial context absent), and `household-management` (professional not personal).

## Dials

- **Charter volatility: LOW.** The treatment frame, coaching approach, or advisory philosophy is set early and defended. Clinical adjustments happen (treatment plan revision, approach shift) but are significant events, not routine. Frame volatility is a warning sign.
- **Actor count: LOW.** Provider + client. Sometimes +1 supervisor (clinical supervision), consulting team, family (with explicit consent), referring provider. Rarely more than 3 actors with scoped access.
- **Time horizon: LONG.** Months to years. Some therapeutic and coaching relationships run a decade. Termination is a planned professional event, not a natural endpoint.

## Artifact mapping

- **Charter: YES, renamed.** Maps to `frame.md` — treatment plan / coaching frame / advisory philosophy. Goals, approach, ground rules, meeting cadence. This is the professional structure within which the work happens.
- **Architecture: NO.** Not applicable.
- **Phase: YES.** Maps to `phase.md` — phase of treatment or coaching arc. Intake → early → middle → late → termination. Phase transitions are clinical/professional judgments, not schedule events.
- **Spec Library: NO.** No specs.
- **Decision Ledger: YES.** Maps to `clinical-decisions.md` or `coaching-decisions.md` — significant professional decisions. Treatment shifts, goal revisions, approach changes, risk assessments. Separate from session notes.
- **Manifest: YES.** Maps to `sessions/` — session notes, one per session. This is the primary artifact accumulation.

Charter and Manifest are heavy. Decision Ledger is critical but thin. Spec Library and Architecture don't apply. Phase is moderate.

## Initial canvas shape

- `frame.md` (note, provider + client-where-appropriate) — treatment/coaching frame
- `phase.md` (note, provider-scope) — clinical phase assessment
- `clinical-decisions.md` (log, provider-scope, append-only) — significant professional decisions
- `sessions/` — per-session notes
  - `sessions/<date>.md`
- `intake/` — intake materials, initial assessment, history
- `goals.md` (note, provider + client) — agreed goals, updated over time
- `consent.md` (note, provider + client) — informed consent, released information, scope of work
- `risk-assessments/` — when applicable (safety, crisis planning)
- `referrals.md` (note, provider-scope) — consulting providers, specialists, referrals sent/received
- `administrative.md` (note, provider + admin if applicable) — scheduling, billing, insurance

Scope handling is unusual here. Default scope is provider-only for most content. Specific pages (frame, goals, consent) are shared with the client when clinically appropriate — but the provider controls sharing, not the system. Even client-shared pages may have provider-only sub-sections (clinical reasoning not shared) and client-facing sub-sections (what was agreed).

Frontmatter:

```yaml
# sessions/<date>.md
session-number: <n>
date: <iso>
duration: <minutes>
modality: in-person | video | phone
attendees: [provider, client, <other-if-applicable>]
type: standard | intake | crisis | termination | supervision-informed
```

```yaml
# clinical-decisions.md entries
date: <iso>
type: treatment-plan-revision | goal-revision | risk-reassessment | referral | termination-planning | other
trigger: <brief-context>
clinical-reasoning: <private-to-provider>
```

```yaml
# frame.md
relationship-type: therapy | coaching | advisory | other
approach: <specific-modality-or-framework>
meeting-cadence: <frequency>
fee-structure: <summary>
termination-conditions: <agreed>
boundaries: <domain-specific>
```

## Evolution heuristics

**Session rhythm:**
- Session scheduled but no session note within 48 hours → whisper to provider (clinical documentation timeliness is both ethical obligation and insurance requirement)
- Client no-shows 2+ sessions → flag for provider attention; pattern may signal clinical concern
- Session cadence drifting from frame (agreed weekly, happening biweekly) → flag for frame revision or clinical discussion
- Long gap between sessions (4+ weeks) without planned break → flag

**Frame integrity:**
- Session notes showing work outside agreed frame (financial advisor discussing personal psychology, therapist discussing executive strategy) → flag for frame review; scope creep
- Client request that exceeds frame boundaries → provider prompt to consider explicit frame revision or scope refusal
- Frame unrevised in 12+ months of active work → prompt review; frames drift and become tacit

**Clinical decision capture:**
- Session note references treatment shift or significant reassessment without clinical-decision ledger entry → whisper; capture the decision
- Risk indicator appearing in session notes (safety concern, substance escalation, relational crisis) → prompt risk assessment if not recent, do not alert or alarm externally
- Multiple sessions touching same unresolved theme → pattern surface for provider's clinical consideration
- Goal untouched across 6+ sessions → prompt: still active, revised, or achieved?

**Phase transitions:**
- Clinical phase indicators (presentation change, therapeutic alliance development, progress against goals, client signals readiness for termination) → prompt phase review
- Termination phase entering → propose termination-planning structure: closure goals, integration work, referral planning if applicable, final-session planning
- Post-termination follow-up, if agreed in frame → schedule, route reminders

**Confidentiality surveillance:**
- Any attempt to share provider-scope content with non-authorized party → block, alarm to provider, do not retry silently
- Content that appears to reference third parties (family members, colleagues mentioned by client) → flag; clinical notes about third parties have special handling requirements in most jurisdictions
- External request for records (subpoena, release-of-information request) → alarm to provider immediately, do not auto-fulfill
- Consent page changes → require explicit provider acknowledgement

**Crisis events:**
- Crisis session flagged → Gardener proposes crisis-specific documentation structure, safety-plan capture if not recent, supervisor-consultation prompt if applicable
- Safety plan needed → propose safety-plan page (scope: provider + client), do not auto-populate
- Crisis resolution → prompt follow-up session scheduling, reassessment cadence

**Supervision integration:**
- Provider indicates case brought to supervision → Gardener can accept supervision notes as separate page, scope: provider + supervisor, preserves clinical consultation record
- Supervisor recommendations feeding clinical decisions → captured in clinical-decisions.md with supervision linkage

**Records retention:**
- Session notes age-boundary per professional obligation (typically 7 years but varies by jurisdiction and profession) → track, alarm as records approach destruction-eligibility, never auto-destroy
- Terminated case: retention clock starts at termination date, records stay read-only accessible until eligible
- Records request (client request for own records) → provider-mediated response, not auto-fulfilled

**Rituals:**
- Per session: documentation within 24-48 hours (provider dependent)
- Quarterly: phase review, goal progress review, frame currency review
- Annual: consent renewal if applicable, comprehensive reassessment
- Pre-termination: transition planning
- Post-termination: archival transition with retention clock

## Member intent hooks

- "Keep this strictly confidential" → `preferences.disclosure: none-without-explicit-consent` — even administrative staff don't see clinical content
- "Don't surface the hard material unprompted" → `preferences.sensitive-topic-routing: silent` — Gardener does not surface difficult session content without provider request
- "Remind me of [client]'s history before session" → `preferences.pre-session-brief: enabled` — Gardener compiles prior-session summary, open clinical threads, recent decisions shortly before scheduled session
- "Keep supervision notes separate" → `preferences.supervision-scope: strict-separate` — supervision content doesn't mix with regular clinical documentation
- "This client has a crisis plan" → `preferences.crisis-routing: visible` — crisis plan surfaces on any session or scheduling interaction
- "I'm on vacation" → `preferences.coverage-period: <dates>` — session scheduling blocked, emergency-contact info surfaces for client-facing surfaces
- "Prep for today" → Gardener surfaces prior session summary, current frame state, open threads from recent sessions, any pending clinical decisions
- "Write session note" → Gardener can accept dictated content, prompts for clinical-decision capture if session contained significant decisions
- "Client requested records" → Gardener surfaces relevant records with provider review gate, provides release-of-information tracking
- "Close this case" → prompt termination planning if not already in termination phase; if in termination, propose case-close rituals
- "Referring to specialist" → capture referral in referrals.md, prompt consent for release, track referral outcome
- "Don't show my spouse" → `preferences.external-party-exclusions: [<names>]` — protective for clients whose therapy might otherwise leak through administrative surface

## Special handling

**The confidentiality discipline:**

Long-term client relationships operate under professional and often legal confidentiality obligations. The Gardener enforces these defensively:

- Default scope is provider-only
- Client-shared content is explicit, not automatic
- Third parties (referring providers, family members, insurance carriers) get scoped access only with documented consent
- Export, sharing, or disclosure operations route through provider with consent verification
- Records requests trigger alarm, not auto-fulfillment

Mistakes in this domain can end careers and harm clients. The Gardener errs toward blocking; false-positives on privacy are acceptable, false-negatives are not.

**Clinical judgment vs Gardener judgment:**

The Gardener does not make clinical decisions. It doesn't assess risk, doesn't determine phase, doesn't propose treatment changes, doesn't interpret content. It documents what the provider records, surfaces patterns the provider might consider, and alarms on operational issues (missed documentation, lapsed consent, overdue reviews).

This boundary is strict. A Gardener surfacing "client may be escalating" based on text pattern is out of scope. Surfacing "session note for yesterday not yet documented" is in scope.

**Third-party references in clinical content:**

Clinical notes often reference people who are not clients (partners, family, colleagues). These references have special handling in most professional frameworks. The Gardener flags content heavy with third-party reference for provider review; beyond flagging, it does not intervene.

**Termination and aftermath:**

Termination is a clinically significant event, not an archive action. The Gardener supports termination planning, captures termination session content normally, and then transitions the canvas to post-termination state — still accessible to provider, read-only, retention clock running. Post-termination follow-up (if agreed in frame) routes through normal session mechanics.

**Insurance and documentation:**

Some relationships are insurance-funded and require specific documentation standards. The Gardener can be configured to enforce documentation requirements (timeline, content elements, diagnostic coding) per the insurance contract. This is provider-configured, not automatic.

**Dual relationships and boundary issues:**

The provider's other relationships (social connections that might intersect with client's life) raise professional issues. The Gardener does not detect these; it provides scope isolation when the provider identifies them. Boundary work is the provider's responsibility.

## Composition notes

Long-term client relationship canvases compose with:

- `multi-project-operations` canvas at the practice level (solo practice or group practice)
- `research-lab` canvas when the provider is also researcher and the client relationship has research component (with explicit IRB-approved research consent)
- Referrals out to other providers' canvases — linkage by reference, never content-sharing without consent

Solo practitioners may have dozens of concurrent client relationships. Each gets its own canvas. The practice-level canvas holds cross-client resources (billing, scheduling, professional development, continuing education, supervision) without any client content.

Group practices may have practice-wide resources plus per-client canvases owned by individual providers. Cross-provider consultation about a specific client requires explicit scope extension from the primary provider.

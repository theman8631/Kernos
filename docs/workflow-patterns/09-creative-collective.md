---
scope: team
type: note
pattern: creative-collective
consumer: gardener
---

# Creative collective

For groups producing sustained creative work together. Covers bands, writers' rooms, design studios, small production companies, theater troupes, improv ensembles, sketch groups, collaborative podcast teams, worldbuilding collectives. Defining properties: multiple creators with aligned vision but distinct contributions, identity at the collective level matters (the group has a sensibility, not just a roster), creative accumulation across projects, and interpersonal dynamics that the workflow must respect.

Distinct from `client-project` (where a group executes for an external principal) and `creative-solo` (where one person works alone with occasional consultants). The collective is its own creative entity.

## Dials

- **Charter volatility: LOW-MEDIUM.** The collective's aesthetic identity is stable — what the band sounds like, what the studio's work looks like, what the troupe's voice is. Specific creative direction on each project evolves, and the collective's frame can shift with membership changes or major creative pivots. More volatile than solo work because more people can push in more directions.
- **Actor count: MEDIUM.** 3-10 typical. Small collectives (3-4) function almost as extended solo work with collaborators. Larger ones (8+) start needing subcommittee structure. Beyond ~12 the pattern strains and the Gardener should propose subdivision.
- **Time horizon: LONG.** Collectives that last accumulate substantial work. Bands that run a decade have hundreds of songs across albums. Writers' rooms across multiple seasons build extensive worldbuilding. Studios across years develop signature languages.

## Artifact mapping

- **Charter: YES.** Maps to `identity.md` — the collective's aesthetic, values, what it's not. This is often implicit in established collectives and explicit in newer ones; the Gardener prompts articulation when the collective seems new or when member signals mismatch on what the group is about.
- **Architecture: PARTIAL.** Maps to `roles.md` — who does what, division of labor, decision-making process. Not technical architecture; organizational structure.
- **Phase: YES.** Maps to `phase.md` — current project(s), season, creative cycle. Collectives often have multiple concurrent projects; phase tracks them all.
- **Spec Library: PARTIAL.** Maps to `projects/` with per-project sub-pages. Each project has its own shape; the collective canvas holds them together.
- **Decision Ledger: YES.** Maps to `decisions.md` — creative decisions (what album direction, what story arc), business decisions (touring, merchandise, release timing), relational decisions (new member, member departure, conflict resolution).
- **Manifest: YES.** Maps to `catalog.md` — the body of work. Released, in progress, abandoned, shelved. Performance history, publication history, release history.

All six apply, some renamed. Collectives need substantially more shape than solo work.

## Initial canvas shape

- `identity.md` (note, team) — aesthetic, values, what the group is
- `roles.md` (note, team) — who does what, decision process
- `phase.md` (log, team) — current projects, creative cycle
- `projects/` — per-project sub-pages
  - `projects/<project-id>/` — project with its own internal shape
- `decisions.md` (log, team, append-only)
- `catalog.md` (note, team) — body of work
- `members/` — per-member pages (note, scope varies — team-visible summaries, personal drafting space)
- `agreements.md` (note, team) — ownership splits, revenue sharing, IP, exit terms
- `external/` — agents, labels, publishers, venues, press contacts

Optional pages:

- `performances.md` / `releases.md` / `productions.md` — domain-specific history
- `rehearsals.md` — for performing collectives
- `submissions.md` — for collectives submitting work (writers' rooms, lit collectives)
- `conflict.md` — when the collective needs space to process disagreement outside live conversation

Domain variants at creation time:

- Band: identity, members, songs/, albums/, performances.md, touring.md, agreements.md
- Writers' room: identity, members, show-bible.md, episodes/, breaks/, agreements.md
- Design studio: identity, members, clients/ (client-project sub-canvases), portfolio.md, agreements.md
- Theater troupe: identity, members, productions/, performances.md, rehearsals.md, venues.md
- Improv ensemble: identity, members, rehearsals.md, shows.md, formats/, bits.md

The Gardener detects the domain and proposes the appropriate initial shape. Domain-detection uses the member's utterance cues; when ambiguous, the Gardener asks ("is this a band, a writers' room, something else?").

Frontmatter on member pages:

```yaml
# members/<member-id>.md
joined: <iso>
role: [<role-tags>]
equity-share: <if-applicable>
status: active | on-hiatus | departed
contact: <handle-or-reference>
```

Frontmatter on project pages:

```yaml
# projects/<id>.md
status: concept | development | production | complete | released | shelved
started: <iso>
targeted-release: <iso-if-applicable>
primary-author: [<member-ids>]
contributions: {<member-id>: <role>, ...}
```

## Evolution heuristics

**Project lifecycle:**
- Project in concept status 6+ months → whisper to member whose idea it was; either activate or move to shelved
- Project transitions to production → propose production sub-canvas with shape appropriate to domain (album recording, show rehearsal schedule, season writers' room)
- Project shipped → archival proposal with linkage to catalog.md, extraction of reusable elements (musical motifs, character relationships, visual language) to `recurring-elements.md` if collective has one
- Two projects sharing substantial creative material → propose cross-project reference page

**Member dynamics:**
- New member joining → propose onboarding: identity read, roles.md update, agreements.md revision, member page creation
- Member announces departure → propose offboarding: agreements revision, project-ownership disposition, member page status change to departed (do not delete), exit-terms capture
- Member inactive 60+ days → whisper to collective, not to member; non-participation is often the first signal of quiet departure
- Member contributions concentrated in one role → propose role expansion opportunity, or note the specialization
- Credit disputes surfacing (same contribution claimed differently) → flag for collective discussion, do not auto-resolve

**Creative direction drift:**
- New project departs significantly from identity.md → flag for discussion pre-production; is identity evolving or is the project off-brand
- Identity.md updated → propose retrospective review of in-flight projects for alignment
- Three projects in a row succeed or fail on same dimension → propose pattern capture

**Decision aggregation:**
- Decisions made in one member's DMs not captured in decisions.md → Gardener can't directly detect, but if utterance references "we decided" without ledger entry, prompt capture
- Contentious decision (flagged as requiring full-group agreement) → Gardener blocks implementation-level routing until agreement-state captured
- Decision log shows one member making most decisions → whisper to operator (often the de facto leader) about collective decision hygiene

**Catalog growth:**
- Catalog exceeds 50 items → propose indexing (by year, by medium, by theme)
- Released work receives attention (press, reviews, sales signals) → propose surface to collective
- Work approaches anniversary (5-year, 10-year) → prompt reissue / retrospective consideration

**Agreements drift:**
- Agreements.md older than 12 months in a collective with new members or changed conditions → flag for review
- Revenue imbalance signal (one member contributing substantially more than share) → flag for discussion, do not auto-propose reshape
- Exit of member invoking agreements → surface agreements, propose resolution structure

**Rituals:**
- Project-start: Gardener proposes identity-check against project direction
- Quarterly: Gardener proposes collective check-in — what's alive, what's dormant, how's everyone feeling about direction (opt-in, the Gardener doesn't force introspection)
- Project-close: retrospective proposal for lessons, residuals capture, catalog update
- Member transitions: proposals as enumerated above

## Member intent hooks

- "Keep this project quiet until we release" → `preferences.project-visibility.<id>: members-only` — no external-facing linkage, press contacts don't see it
- "Don't let [external contact] see this yet" → `preferences.external-scope.<contact-id>.exclusions: [<pages>]`
- "Track splits on this project" → `preferences.project-splits.<id>: tracked` — contributions logged for revenue-sharing calculation at release
- "We need to talk about [topic]" → propose conflict.md page with scope to collective, provide space for async discussion outside live meeting
- "Remember [member] is leading this" → `preferences.project-lead.<id>: <member-id>` — decision routing concentrates on them
- "Don't mix band stuff with side projects" → Gardener proposes separate canvas for side project, does not co-mingle
- "What have we released in [year]" → on-demand catalog surface with date filter
- "How does this stack against our last album" → on-demand comparison across projects — length, themes, contribution patterns
- "I want to bring someone in for this project" → propose scoped guest access, bounded to project
- "We're arguing about [decision]" → surface prior similar decisions from ledger, do not side with anyone
- "Update the bio" → surface identity.md and catalog.md, prompt revision with member input
- "Tour is coming up" → propose touring.md if absent, sync with calendar, surface venue confirmations

## Special handling

**The identity trap:**

Collectives often fracture when members have divergent implicit understandings of what the group is. An explicit identity.md is protective, but forcing articulation prematurely can be worse than leaving it implicit. The Gardener prompts identity articulation when signal suggests mismatch (members describing the group differently, creative decisions splitting along predictable lines), not as a routine requirement.

**Ownership and IP:**

Creative collective work often generates ownership complexity (who owns the song, the character, the idea). The Gardener surfaces agreements.md at every new project inception and flags creative contributions that seem to fall outside the written agreements. This is not legal enforcement; it's friction that prompts conversation.

**Member-authored personal space:**

Each member may maintain a personal-scope drafting space within their member page. The collective can see the member's role and contribution summary; the member controls what else is visible. The Gardener enforces this scope aggressively — drafts shared to the collective are explicit acts, not inheritance.

**Credit discipline:**

Contributions accumulate and credit decisions at release are fraught. The Gardener logs contributions during production (who wrote what, who arranged what, who directed what) as decision-log entries, which provide evidence for credit discussions without resolving them. The Gardener does not determine credit; it provides the record the collective uses to determine credit.

**Conflict routing:**

Interpersonal conflict is a fact of collective creative work. The Gardener does not mediate, does not take sides, does not propose resolutions. It provides space (conflict.md or similar), preserves async communication when members prefer to compose thoughts, and respects scope when members need privacy to process.

## Composition notes

Creative collective canvases compose with:

- `client-project` sub-canvases when the collective takes external commissions (studio doing client work, band doing scored commissions)
- `time-bounded-event` sub-canvases for major events (album launch, season premiere, gallery show)
- Per-project sub-canvases that may themselves have pattern-specific shape (recording project using elements of creative-solo for the recording itself)
- `cause-based-organizing` canvas if the collective is politically or mission-active (activist art collective)
- Individual member's `creative-solo` canvases for their individual work outside the collective — peer canvases, not child canvases

The collective canvas does not absorb individual member work. A songwriter in a band has a creative-solo canvas for their own unreleased material; the band canvas only contains what the band is doing with that material.

---
scope: team
type: note
pattern: creative-solo
consumer: gardener
---

# Creative solo

For individuals pursuing sustained creative work alone. Covers novelists, poets, essayists, composers, songwriters, solo visual artists, illustrators, solo game designers, solo filmmakers. Defining properties: the work is the artifact (canvas content often includes the creative work itself, not just metadata about it), horizons are long and idiosyncratic, private reflection is load-bearing, and process discipline cannot be imposed — it must emerge from how the artist actually works.

This pattern resists most of the discipline the others impose. A creative solo canvas that demands daily curation has already failed. The Gardener's job here is to provide structure where welcome, silence where not, and recognition of the creative process's non-linearity.

## Dials

- **Charter volatility: LOW.** Artistic vision is personal and tends to be stable at the level that matters. An artist's sensibility evolves but rarely pivots. Explicit Charter-level articulation is uncommon and shouldn't be imposed.
- **Actor count: LOW.** Usually 1. Sometimes +1 editor, beta reader, music producer, art director. These are not co-authors; they're consultants with scoped access at specific moments.
- **Time horizon: LONG-INDEFINITE.** Novels take years. Symphonies take years. A visual artist's body of work accumulates across decades. Individual pieces have shorter horizons within the longer arc.

## Artifact mapping

- **Charter: MAYBE.** Artistic manifestos, mission statements, and process philosophies exist when artists want to articulate them. Many don't, and forcing it produces pretension, not insight. Gardener proposes only on explicit signal.
- **Architecture: MAYBE.** Some works have architecture (a novel's plot outline, an album's sequencing, a story cycle's shared world). The Gardener proposes structure when the member describes structural work; doesn't impose it otherwise.
- **Phase: PARTIAL.** Creative work has phases (research → drafting → revising → polishing → launching) but they overlap, recur, and aren't gated. Phase tracking is a low-ceremony rolling digest, not a milestone system.
- **Spec Library: NO.** Solo creative work isn't spec'd; it's drafted. The closest analog is a works-in-progress list, which is the manifest.
- **Decision Ledger: YES.** Creative decisions matter — what got cut, why a character was killed off, why the third track became the first. These decisions often need recovery years later when someone asks about the work or the artist revisits.
- **Manifest: YES.** The body of work. Finished pieces, works in progress, fragments, cut material. This is the artist's creative state.

Charter and Architecture are member-demand-only. Spec Library doesn't apply. Phase is light. Decision Ledger and Manifest carry the load.

## Initial canvas shape

Minimal by default. The Gardener proposes expansion on signal.

- `works/` — subdirectory
  - `works/in-progress/` — current active work
  - `works/fragments/` — unfinished or speculative pieces
  - `works/finished/` — completed work (often with external distribution links)
  - `works/cut/` — material removed from other works, preserved
- `decisions.md` (log, personal, append-only) — creative decisions
- `research/` — reference material, inspiration, source gathering
- `notes/` — private reflection, process notes

Optional proposals on signal:

- `vision.md` — only if member articulates explicit creative philosophy
- `structure/<work-id>.md` — per-work outline when member describes structural work
- `launch/<work-id>.md` — for work going to publication/release
- `commissions/` — for artists with paid-commission flow (merges into client-project sub-canvases for major commissions)
- `performance/` — for performers tracking performances of their work
- `teaching/` — for artists who teach their craft (often becomes `course-development` canvas)

Default scope is `personal`. Artists don't generally share canvas content without explicit act. Team-scope is opt-in, specific-members scope is for editor/collaborator access to specific works.

Frontmatter:

```yaml
# works/<status>/<work-id>.md
work-id: <stable-id>
medium: novel | short-story | album | song | painting | illustration | etc
status: seed | drafting | revising | polishing | complete | released
started: <iso>
last-touched: <iso>
target-length: <if-known>
current-length: <if-trackable>
```

```yaml
# decisions.md entries — lightweight
date: <iso>
work: <work-id>
type: cut | added | restructured | reframed | abandoned | reprised
brief: <one-sentence-description>
```

Frontmatter is sparse. The work is the content; metadata should not compete.

## Evolution heuristics

**Work lifecycle:**
- Work untouched 60+ days with status `drafting` → no alarm; creative work goes fallow and that's normal. Gardener is silent.
- Work untouched 180+ days with status `drafting` → whisper once to artist: "this piece has been quiet; want to move to fragments or return?" — then silent again regardless of answer
- Work transitions from `drafting` to `complete` → propose release-workflow if artist signals interest; silent otherwise
- Work in `cut/` references the work it was cut from → maintain backlink for retrieval ("show me everything I cut from [work]")
- Fragment worked on for 3+ sessions → Gardener proposes promotion to in-progress, prompts brief articulation of what the piece might be

**Body-of-work growth:**
- 20+ finished works → propose catalog page with indexing by medium, theme, date
- Recurring themes appearing across works → Gardener may offer to produce theme-index on request, never volunteers
- Prior-work elements reappearing in new work (characters, motifs, settings) → propose intertextual linkage when detected

**Decision log discipline:**
- Creative decision made verbally during drafting → Gardener prompts capture inline if the artist's working alongside; does not demand retrospective entry
- Major cut (large section removed) → prompt cut-preservation to `cut/`, capture decision
- Decision reversed later → supersession linkage, preserve both for process history
- Decisions about living persons, real events, or sensitive material → flag for review before work release; legal/ethical implications

**Research and reference:**
- Research sources accumulating → propose index when 20+ sources
- Sources referenced in works → maintain linkage for annotation and permissions at release time
- Research that clearly isn't feeding any work → propose archival or topic-extraction if the research has become its own project

**Process transition signals:**
- Artist describes wanting to work on something new → propose new work page, do not demand full metadata
- Artist describes feeling stuck → silence by default; offer process-reflection prompt only on explicit invitation
- Artist describes completing something → prompt decision-log entry, prompt release-consideration
- Artist describes doubt about direction → do not suggest structural reshape; creative doubt is not a shape problem

**Release workflow (when invoked):**
- Work marked for release → propose release sub-canvas: submissions/agents/publishers/venues tracking, editing rounds, launch assets
- First external read request (agent, beta reader) → propose reader-access scope creation
- Publication/release accepted → propose launch manifest; pivot release sub-canvas to pre-launch phase
- Work released → archive release sub-canvas, link from work's finished page, cold state

**Rituals (essentially none):**
- No weekly, monthly, or scheduled curation by default
- On explicit artist request: "what did I do this month / year" — Gardener produces activity summary from work updates and decisions
- On milestone (work completion, release): Gardener proposes reflection capture, artist chooses whether to engage

## Member intent hooks

- "Don't suggest structure" → `preferences.structural-proposals: disabled` — Gardener never proposes outlines, plot structures, or frameworks
- "Track what I've cut" → `preferences.cut-preservation: automatic` — any removed content routes to cut/ with source-work linkage
- "Keep this private forever" → `preferences.work-visibility.<work-id>: personal-strict` — no scope-widening suggested or permitted
- "I have a beta reader / editor / producer" → scoped access to specific work, bounded time window, read+comment permissions
- "Show me what I've been working on" → on-demand: Gardener produces current works-in-progress with last-touched recency
- "I abandoned [work]" → move to fragments with reason capture if offered; do not prompt for justification
- "I'm returning to [work]" → move back to in-progress, surface prior decisions and last-state
- "What happens in [work]" → Gardener produces structural summary from content if available; respects artist's refusal to structure
- "Don't spoil this even to me" → `preferences.self-spoiler-protection.<work-id>: true` — Gardener declines to summarize undrafted sections, won't project plot beats
- "Remind me of the feeling" → Gardener surfaces notes/ entries from the work's early drafting, preserves mood
- "I want to release this" → trigger release sub-canvas workflow
- "Who might want to see this" → surface reader-access list if one exists, propose reader suggestions only if asked
- "Archive this whole project" → move entire work's artifacts to archive, preserve retrievability

## Special handling

**The silence discipline:**

Most creative solo patterns fail by imposing system overhead on artists whose work requires internal quiet. The Gardener defaults to silence. Whispers, alarms, and surfacing happen only on explicit preference-configuration or on the narrowest class of truly-needs-attention events (impending deadlines on submitted work, expiring contracts, communications requiring response).

If the artist has not invited the Gardener to surface something, the Gardener does not surface it. Trust is earned in this pattern through restraint.

**Creative block and fallow periods:**

Long stretches of non-activity are normal in creative work. The Gardener does not frame these as problems, does not propose "productivity" solutions, does not alarm on non-update. Fallow periods produce subsequent work; they are not degradation.

**Privacy around drafts:**

Drafts are fragile. The Gardener respects that an artist may want work hidden even from their future self in retrospect — not surfacing old drafts when working on later ones, unless the artist asks. The work the artist is currently doing is the work that matters; earlier versions are archives that surface on request only.

**External collaborator boundaries:**

Editors, beta readers, and producers get scoped access to specific works for specific phases. When their involvement ends, scope retracts. The Gardener enforces this without prompting — scope expiration is automatic based on phase transitions, not manual cleanup.

**Relationship with commissioned work:**

When a solo artist takes a commission, the commission is a bounded `client-project` sub-canvas nested under creative-solo. Commercial constraints apply to the commission canvas; artistic autonomy applies to everything else. The Gardener maintains this boundary aggressively — commissioned-work decisions do not automatically inform personal-work decisions and vice versa.

## Composition notes

Creative solo canvases compose with:

- `client-project` sub-canvases for commissioned work
- `time-bounded-event` canvases for launches, readings, gallery shows
- `creative-collective` canvases when the artist participates in a band, writers' group, or collaborative project separately — these are peer canvases, not parent/child
- `course-development` canvases when the artist teaches
- `multi-project-operations` canvas at the practice level for artists running a business around their work (indie publisher, commercial illustrator, working musician)

The pattern should not absorb business operations. A working novelist has both a creative-solo canvas (where the novels live) and a multi-project-operations canvas (where the agent-communication, taxes, royalty-tracking, speaking engagements live). Merging them pollutes both.

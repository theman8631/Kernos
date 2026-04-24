---
scope: team
type: note
pattern: long-form-campaign
consumer: gardener
---

# Long-form campaign

For sustained narrative work carried across many sessions with continuity that matters. Covers tabletop RPGs (D&D and siblings), long-form improv troupes, collaborative novel-writing, episodic fiction podcasts, and play-by-post fiction. The defining property: canon accumulates, contradictions are expensive, and forgetting something that happened three months ago is a real failure mode.

## Dials

- **Charter volatility: MEDIUM.** The campaign's core premise (genre, tone, central conceit) is set early and defended. But *what counts as canon* refines continuously — rulings get made, retcons occasionally happen, character agency reshapes the world. More volatile than software, less than ongoing life ops.
- **Actor count: LOW-MEDIUM.** Typically a GM/showrunner + 3-6 players/collaborators. Sometimes a co-GM or co-author. Minor NPCs/characters don't count as actors.
- **Time horizon: VERY LONG.** Campaigns routinely run 1-5 years. Some run longer. Canon from year one must remain legible and retrievable in year four.

## Artifact mapping

- **Charter: YES, renamed.** Maps to `setting.md` or `premise.md` — the world rules, tone, genre commitments, and what's-off-limits. "This is a low-fantasy setting, no outsiders, magic is costly, we don't do sexual violence on-screen."
- **Architecture: PARTIAL, replaced by world primitives.** Not an architecture map — a *world map* in the structural sense. Named NPCs, major factions, key locations. Relationships between them. This is where the world's structure lives.
- **Phase: YES, renamed.** Maps to `arc-current.md` — what's happening now, what the current narrative arc is about.
- **Spec Library: NO.** Replaced by session-prep pages and session-recaps. Not versioned in the supersession sense; ordered in time.
- **Decision Ledger: YES, renamed.** Maps to `canon.md` — the append-only record of what's true in the world. Rulings, established facts, character consequences.
- **Manifest: PARTIAL.** No single manifest page. Character sheets + current-state summaries serve the manifest function distributed across pages.

Canon is the load-bearing artifact. Without canon tracking, long-form campaigns collapse into contradictions within 6 months.

## Initial canvas shape

- `setting.md` (note, team) — premise, tone, genre, table rules, what's-off-limits
- `arc-current.md` (note, team) — current narrative arc, its stakes, open threads
- `canon.md` (log, team, append-only) — the authoritative record of what's true
- `sessions/` — session pages, dated (`sessions/2026-04-23-the-ambush.md`)
  - each session page contains: attendees, opening-state, events, decisions-made-this-session, cliffhanger-or-stopping-point
- `npcs/` — subdirectory for named NPCs (note type)
  - each NPC page: description, motivation, appearances (list of session references), status (alive/dead/missing/transformed)
- `locations/` — subdirectory for significant places (note type)
  - each location page: description, history, events-that-happened-here
- `factions/` — subdirectory if political structure matters (note type, optional at start)
- `characters/` — PC sheets, scope: personal (each player owns theirs) with team-visible summary sidebar
- `arcs/archive/` — completed arcs, moved here on arc-close

Frontmatter on session pages:

```yaml
session-number: <n>
date: <iso>
attendees: [<member-id>, ...]
arc: <arc-id>
canon-entries: [<canon-log-ref>, ...]
introduces: {npcs: [], locations: [], factions: []}
```

Frontmatter on canon entries:

```yaml
established-session: <session-id>
type: ruling | fact | consequence | retcon
subject: <what-this-is-about>
supersedes: <prior-canon-entry-if-retcon>
```

## Evolution heuristics

**NPC growth:**
- NPC mentioned in 3+ sessions → promote to own page if not already
- NPC promoted but untouched for 6+ sessions → Gardener whispers to GM: "is this character still relevant, or archive?"
- NPC marked dead/departed but referenced repeatedly → propose status revision or memorial-page treatment
- Session introduces 5+ new NPCs → Gardener flags: "this session may be NPC-heavy; consider a cast-list sidebar"

**Location growth:**
- Location appears in 5+ sessions → promote to own page with sub-sections for significant events
- Location referenced across multiple arcs → propose cross-arc timeline on location page
- Location untouched for 10+ sessions → archive to `locations/archive/`, reversible on re-appearance

**Arc transitions:**
- `arc-current.md` unchanged for 6+ sessions while sessions clearly dealing with different material → propose arc-close and new-arc authorship
- Arc's open threads unresolved at proposed arc-close → flag for GM; threads need disposition (resolved, deferred to future arc, abandoned)
- New arc opens → Gardener proposes pulling forward any `deferred to future arc` threads from prior arcs

**Canon maintenance:**
- Canon entry contradicts an earlier entry without supersession declaration → alarm; flag for GM resolution
- Session introduces new canon but session page doesn't update `canon.md` → whisper to GM pre-close
- Same fact appears in canon 3+ times (repeated re-assertion) → propose canonization promotion to a "settled canon" index
- Canon entry referenced in 5+ sessions → propose extraction to a lore page (the fact has grown into a subject)

**Session-to-session continuity:**
- New session opens without referencing prior session's stopping-point → whisper to GM
- Character sheet unchanged for 3+ sessions where the character clearly did things → whisper to player ("update your sheet?")
- Cliffhanger from session N not resolved in session N+1, N+2, N+3 → whisper to GM; dangling threads accumulate

**Faction and political structure:**
- Three+ NPCs share stated allegiance → propose faction page if none exists
- Faction appears in 5+ sessions → promote to own page with members, goals, status
- Faction-vs-faction conflict emerges → propose relationship graph page

**Rituals:**
- Per-session: Gardener prompts session recap before session close. Recap is mandatory structurally, brief in content. Missing recap blocks canon routing.
- Per-arc-close: Gardener proposes arc archival + new arc authoring + open-thread disposition
- Periodic: "Who did we meet" surface — Gardener can produce NPC-met list filtered by session-range on request

## Member intent hooks

- "Remember this is canon" → `preferences.canon-routing: immediate` — utterance triggers append to canon.md with `type: fact` or `type: ruling`
- "Don't show NPCs until players meet them" → `preferences.npc-reveal: on-meeting` — NPC pages hidden from player scope until a session frontmatter records the introduction
- "Keep character sheets visible during play" → `preferences.pin-characters: sessions` — character summary sidebar surfaces during session pages
- "Who was that guy we met in chapter 3" → no preference; Gardener performs bounded search across sessions by date-range or arc-reference
- "I retconned that" → `type: retcon` canon entry with `supersedes:` reference; prior fact's status becomes "retconned" and appears with strikethrough in canon views
- "Don't spoil my character's backstory" → `preferences.backstory-scope.<character-id>: personal` — personal-scope backstory pages invisible to other players unless explicitly shared
- "This NPC is important, keep an eye on them" → `preferences.npc-watches.<npc-id>: operator-on-appearance` — NPC appearances in future sessions route to GM/operator
- "What did I take from the dungeon" → on-demand inventory log surface across session pages, filtered by player-character
- "Prep for next session" → Gardener produces a brief: current arc state, open threads, last session's cliffhanger, NPCs likely to appear
- "Summarize what happened while I was gone" → Gardener produces bounded summary of sessions between the player's last attended session and now
- "Don't contradict what we established" → `preferences.canon-strictness: strict` — Gardener flags pre-canonization any agent-generated content that conflicts with existing canon

## Special handling

**Player vs character knowledge:**

The canvas scoping model doesn't natively distinguish "player knows" from "character knows." Long-form campaigns need this distinction. The Gardener should support:

- `canon.md` entries can declare `player-visibility: all | arc-attendees | gm-only`
- NPC pages can declare `character-knowledge: {<character-id>: [known-facts]}` vs general page content
- Session-prep pages default to `gm-only` scope

When a player asks "what do I know about X," the Gardener filters by character-knowledge, not by canon totality. This is a discipline the Gardener enforces at retrieval time.

**Shared vs split timelines:**

When the party splits (some players with different characters in different places), session pages can carry multiple attendee-groups with separate events-logs. The Gardener proposes this shape when a single session page has significantly different events-per-attendee.

## Composition notes

Long-form campaign canvases often sit alongside:

- `creative-collective` canvas when the campaign is part of a broader creative project (e.g., a writers' room producing connected stories in the same world)
- `household-management` canvas when the campaign is a family-or-friend-group activity with coordination concerns (scheduling, who-brings-snacks) that don't belong in the canon canvas
- A second long-form-campaign canvas when the group runs multiple campaigns concurrently — separate canvases, not merged

The Gardener should never merge household logistics into a campaign canvas. The canvas's purpose is canon. Pollution with "Sarah is bringing pizza" degrades retrieval quality over time.

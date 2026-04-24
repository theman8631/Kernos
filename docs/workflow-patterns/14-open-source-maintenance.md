---
scope: team
type: note
pattern: open-source-maintenance
consumer: gardener
---

# Open-source maintenance

For sustained maintenance of open-source projects. Covers libraries, frameworks, tools, applications developed and maintained as open source — from one-person hobby projects to foundation-governed ecosystems. Defining properties: contributor-driven (some contributors are core team, most are occasional), public-by-default (Charter, architecture, decisions are typically visible), governance structure matters (who decides what), triage-heavy (issues and PRs accumulate faster than work-on-them capacity), breaking changes have real downstream user impact, and the project's public identity is part of what brings contributors.

Distinct from `software-development` (closed-team build with shipping discipline) by the contributor-coordination and public-surface discipline. An open-source project shares software-development's build discipline but adds substantial additional structure.

## Dials

- **Charter volatility: LOW.** Project charter, governance, and core technical direction are stable — changing them breaks the social contract with contributors. Major revisions occur through explicit governance process (RFCs, maintainer votes, foundation decisions), not drift.
- **Actor count: HIGH.** Core maintainers (small, often 1-10) + regular contributors (tens to hundreds) + occasional contributors (many more) + users-as-reporters (potentially thousands). Pattern scales through delegation and governance, not through cramming.
- **Time horizon: LONG.** Successful open-source projects run for years or decades. Major libraries accumulate a decade+ of history. Abandonment is a real possibility; the pattern must support graceful deprecation.

## Artifact mapping

- **Charter: YES.** Maps to `charter.md` + `governance.md` — project mission, contribution philosophy, governance model, decision-making authority. Usually public.
- **Architecture: YES.** Maps to `architecture.md` — technical architecture, design rationale, public API surface. Often has both public-for-users and internal-for-contributors variants.
- **Phase: YES, renamed.** Maps to `roadmap.md` — current milestone, upcoming work, deprecation plans. Public-facing.
- **Spec Library: YES, renamed.** Maps to `rfcs/` or `proposals/` — formal design proposals before implementation. Public, versioned, governance-gated.
- **Decision Ledger: YES.** Maps to `decisions.md` or Architectural Decision Records. Public by default; some decisions are maintainer-private (security, disciplinary).
- **Manifest: YES, renamed.** Maps to `releases.md` + `supported-versions.md` — what's been released, what's supported, what's deprecated.

All six apply, most public. Adds contributor-coordination layer not present in closed-team software development.

## Initial canvas shape

- `charter.md` (note, team + public-facing export) — mission, values, scope
- `governance.md` (note, team + public) — who decides, how, authority tiers
- `architecture.md` (note, team + public) — technical structure
- `roadmap.md` (log, team + public) — milestones, direction
- `rfcs/` — formal proposals
- `decisions.md` (log, team, mostly public) — append-only ADRs
- `releases.md` (note, team + public) — release history, version support matrix
- `supported-versions.md` (note, team + public) — what versions receive what kind of support
- `contributors.md` (note, team + public) — contributor list, roles, authority
- `issues-triage.md` (log, team) — triage state, patterns, known-issue tracking
- `prs-triage.md` (log, team) — PR state, review queue
- `security.md` (note, team, private vulnerabilities; public policy) — security policy, vulnerability disclosure
- `community.md` (note, team) — community health, conduct-related, off-repo coordination
- `funding.md` (note, scope: maintainers) — sponsors, grants, sustainability

Scope distinction: `team + public` means core maintainers author; the content is exported or mirrored to public surfaces (README, docs site, GitHub wiki). Members of the canvas (core team) have authoring rights; the public sees read-only projections.

Frontmatter:

```yaml
# rfcs/<rfc-id>.md
rfc-number: <n>
title: <n>
status: draft | public-discussion | under-review | accepted | rejected | implemented | superseded
author: <maintainer-or-contributor-id>
champions: [<maintainer-ids>]
opened: <iso>
resolved: <iso-if-resolved>
```

```yaml
# releases.md entries
version: <semver>
released: <iso>
type: major | minor | patch | pre-release
breaking-changes: [<change-summaries>]
supported-until: <iso-or-tbd>
```

```yaml
# contributors.md entries
contributor-id: <id>
role: maintainer | reviewer | regular-contributor | occasional | user-reporter
authority: [<domains>]
first-contribution: <iso>
most-recent: <iso>
status: active | inactive | departed
```

## Evolution heuristics

**Triage health:**
- Issue queue growing faster than closure rate → flag; triage capacity issue
- Stale issues (60+ days untouched) → propose batch review, close-or-categorize
- PR queue depth growing → flag; review capacity issue
- Stale PRs (30+ days without maintainer review) → whisper; contributor goodwill at stake
- Same issue reported multiple times → propose canonical-issue designation, mark duplicates
- Security issue reported → immediate alarm to maintainers, route through private security.md

**RFC lifecycle:**
- RFC in `public-discussion` > 30 days without resolution → prompt decision or explicit extension
- RFC accepted but not implemented 90+ days → flag; is this still happening
- RFC superseded by another → link supersession chain
- RFC implementation diverges from accepted text → flag for either update or re-approval
- RFC rejected → ensure reason captured for future reference; rejected-twice patterns flag anti-features

**Roadmap currency:**
- Roadmap milestone date passed without completion → flag; update or revise
- Work happening that's not on roadmap → flag for roadmap update (optional; core team discretion)
- Roadmap unchanged 6+ months → prompt review; either still accurate (fine) or stale
- Deprecation milestone approaching → user-communication routing, migration-guide status check

**Release discipline:**
- Release imminent → pre-release checklist: CHANGELOG complete, upgrade notes written, breaking changes documented
- Breaking change being introduced → flag; requires major-version, migration guide, deprecation-period plan
- Patch release containing features → flag semver violation
- LTS version approaching end-of-support → user-communication routing, migration guidance
- Release announcement coordination → propose communication-plan canvas

**Contributor ladder:**
- New contributor's first PR → welcoming routing, first-contribution labeling
- Contributor making 5+ quality contributions → propose elevation to regular-contributor
- Regular contributor earning review authority → governance-gated promotion via maintainer process
- Maintainer inactive 90+ days → whisper; succession planning or status change
- Contributor behavior raising community concern → private maintainer routing, governance process

**Code of conduct handling:**
- Conduct-related concern surfaced → scope-strict routing to designated maintainers, never public
- Conduct process underway → confidential canvas space, preserves record without exposure
- Resolution → decision captured with appropriate public/private scope depending on outcome
- Pattern of similar concerns → escalate for systemic-response consideration

**Security:**
- Vulnerability reported → immediate private routing, embargo discipline, coordinated-disclosure timeline tracking
- Security advisory preparation → private drafting with legal/ethical review
- Patch release coordinating with advisory → tight coupling, release-advisory-coordinated
- Public disclosure → public channels updated simultaneously, not staggered

**Funding and sustainability:**
- Maintainer indicating burnout signal → propose load-redistribution, explicit support
- Funding gap approaching → surface to maintainer team, propose sustainability conversation
- Grant deliverables → track against grant commitments

**Community health:**
- Toxic thread emerging → flag to designated moderator, provide intervention options
- Community growth outpacing maintainer capacity → flag capacity gap
- Community fragmenting → surface pattern, flag for strategic discussion

**Governance process:**
- Governance-requiring decision made without process → flag; maintainer authority exceeded
- Governance vote / RFC process complete → capture outcome cleanly, update governance.md if applicable
- Governance model itself needing revision → propose governance-RFC

**Rituals:**
- Weekly: triage sweep (issues, PRs, security), roadmap review
- Monthly: contributor-ladder review, community-health check, release planning
- Quarterly: governance review, strategic-direction check, sustainability assessment
- Per-release: release rituals and post-release monitoring
- Annual: comprehensive review, roadmap major-revision, contributor recognition

## Member intent hooks

- "This is a hobby, I don't have time for big governance" → `preferences.governance-weight: light` — Gardener proposes lightweight process, doesn't push toward heavy governance
- "We're foundation-governed" → `preferences.governance-weight: formal` — heavy process, foundation-liaison routing, formal meeting discipline
- "Don't spam me with every issue" → `preferences.issue-routing: digest` — batched triage-ready surfaces, not per-issue alarms
- "Keep me in the security loop" → `preferences.security-routing: maintainer-direct` — security issues route immediately, regardless of other filters
- "First-contributor experience is priority" → `preferences.first-contribution-discipline: enhanced` — alerts on first-PR friction, ensures prompt welcome
- "LTS commitments" → `preferences.lts-tracking: strict` — LTS support windows enforced, backport alarms, EOL user-communication
- "Semantic versioning strictly" → `preferences.semver-enforcement: strict` — breaking changes block patch releases
- "Prep me for release" → Gardener compiles release-checklist state, open blockers, CHANGELOG, breaking-change summary
- "Who's active right now" → on-demand contributor activity surface, recent contributions, inactive flags
- "What are users complaining about" → issue-pattern surface, most-reported items, friction hotspots
- "Deprecating [feature]" → trigger deprecation workflow: announcement plan, migration-guide, timeline, user-notification
- "Stepping back from maintenance" → propose succession planning, hand-off documentation, status change
- "Never been contributed to this area" → flag area as at-risk for bus-factor, propose contributor recruitment

## Special handling

**Public-private boundary:**

Open-source projects operate with substantially public surface (charter, roadmap, RFCs, most decisions) alongside private maintainer work (security, conduct, some strategic discussion, personal contributor situations). The Gardener enforces this boundary:

- Public-exported content is explicitly flagged; changes to public content should be intentional
- Private maintainer content does not leak to public surfaces
- Some content transitions (security advisory from private to public post-embargo) happen through explicit acts
- Audit trail for privacy violations preserved for accountability

**Bus factor awareness:**

Open-source sustainability depends on distributed knowledge. The Gardener surveils for bus-factor signals:

- Areas of code only one contributor touches → flag as bus-factor risk
- Critical decisions concentrated in one person's authority → flag
- Key maintainer signaling burnout or reduced availability → escalate for load-sharing

**Contributor emotional labor:**

Maintaining open source has substantial emotional-labor dimension — dealing with entitled users, burnout, criticism, community conflict. The Gardener respects this:

- Supports maintainer time-off without alarm cascades
- Recognizes that some "issues" are community-management not technical
- Does not pressure response timelines that are hostile to maintainer sustainability
- Routes support where asked; doesn't diagnose maintainer state without invitation

**Trademark and project identity:**

Some projects have trademark considerations, brand-management concerns, corporate-affiliation tensions. The Gardener can track these but doesn't adjudicate; it surfaces the considerations when relevant decisions arise.

**Fork awareness:**

Open-source projects get forked. Sometimes forks are peaceful (one direction taken, another pursued). Sometimes they're contentious (community split). The Gardener surfaces fork indicators if signal emerges, but doesn't intervene; fork-handling is social, not technical.

**Breaking change diplomacy:**

Breaking changes damage users. The Gardener doesn't prevent them (they're sometimes necessary) but enforces discipline:

- Breaking change requires explicit major-version allocation
- Migration guide must accompany release
- Deprecation period (typically multiple minor releases) precedes breaking release
- User-notification through usual channels
- Post-release support for migration issues

Skipping any of these is a maintainer decision; the Gardener flags the skip but doesn't block.

**End-of-life:**

Projects sometimes reach end-of-life — abandoned by maintainers, superseded by alternatives, no longer maintained. The Gardener supports graceful EOL:

- EOL announcement drafted with fair notice
- Final maintenance release if possible
- Archive-mode transition with clear documentation
- Fork-friendly final state if continuing development is wanted elsewhere

## Composition notes

Open-source maintenance canvases compose with:

- `software-development` sub-canvases for active feature work; the OSS canvas holds coordination and governance, sub-canvases hold build discipline
- `multi-project-operations` canvas at the organizational level for foundation-governed projects or companies maintaining multiple OSS projects
- `creative-collective` canvas for project teams with sustained collaborative identity
- `cause-based-organizing` canvas for political or mission-driven OSS (ethical tech, activist software)

The OSS canvas is typically the parent; internal feature work happens in software-development sub-canvases that inherit the OSS canvas's public-exposure discipline. An OSS feature's RFC lives in the OSS canvas; its implementation spec lives in a software-development sub-canvas. The boundary keeps public commitment and internal execution legibly separate.

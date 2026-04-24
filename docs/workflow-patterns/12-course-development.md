---
scope: team
type: note
pattern: course-development
consumer: gardener
---

# Course development

For designing and delivering structured learning experiences. Covers academic courses (university, K-12, continuing education), professional training programs, workshop series, bootcamps, online courses, certification programs, mentorship cohorts. Defining properties: curriculum design is a long-horizon creative/pedagogical activity distinct from per-delivery execution, each delivery (semester, cohort, session) iterates on the curriculum, student experience is an input to curriculum evolution, and assessment discipline matters for both learning outcomes and accreditation.

This pattern typically manifests as dual-canvas shape: a persistent course canvas that holds the curriculum and accumulates across deliveries, plus per-delivery canvases for each semester/cohort/run.

## Dials

- **Charter volatility: LOW.** Course learning objectives and pedagogical philosophy are stable. Delivery iterates freely within the frame. Charter-level changes occur with major curriculum revisions (accreditation review, significant content area shift), roughly annually or less.
- **Actor count: MEDIUM.** Instructor(s) + TAs + students-as-partial-actors. Students participate in the canvas through their work (submissions, questions, progress) but don't typically author course-level content. Larger courses add teaching team, course coordinators, LMS admins.
- **Time horizon: LONG for course, SHORT for per-delivery.** Course canvases persist across many years of delivery. Per-delivery canvases run a semester (or cohort cycle: weeks to months) and then cold-state.

## Artifact mapping

### Course-level canvas (persistent)

- **Charter: YES.** Maps to `objectives.md` + `philosophy.md` — learning objectives, pedagogical commitments, what this course teaches and how.
- **Architecture: YES.** Maps to `structure.md` — course modules, their dependencies, time-allocation, learning arc.
- **Phase: PARTIAL.** Course doesn't have phases in the software sense; it has delivery history (semester-by-semester evolution).
- **Spec Library: YES.** Maps to `modules/` and `assignments/` — versioned teaching materials, supersession as revisions happen across deliveries.
- **Decision Ledger: YES.** Maps to `pedagogical-decisions.md` — why this readings choice, why this sequencing, why this assessment approach.
- **Manifest: YES.** Maps to `delivered-semesters.md` + materials catalog — delivery history with linkage to per-delivery canvases.

### Per-delivery canvas

- Charter: N/A (inherits from course)
- Architecture: N/A (inherits from course)
- **Phase: YES.** Maps to `weekly.md` — week-by-week flow through the delivery.
- Spec Library: N/A (modules inherited from course; per-delivery may have activity adaptations)
- **Decision Ledger: PARTIAL.** Maps to `delivery-decisions.md` — decisions specific to this delivery.
- **Manifest: YES.** Maps to `students/` + `assessments/` — who's enrolled, their work, their progress, their grades.

## Initial canvas shape

### Course canvas

- `objectives.md` (note, team) — learning objectives
- `philosophy.md` (note, team) — pedagogical approach
- `structure.md` (note, team) — modules, dependencies, arc
- `modules/` — per-module pages
  - `modules/<module-id>/readings.md`
  - `modules/<module-id>/lesson.md`
  - `modules/<module-id>/activities.md`
- `assignments/` — versioned assignment specifications
- `assessment.md` (note, team) — grading approach, rubrics
- `pedagogical-decisions.md` (log, team, append-only)
- `delivered-semesters.md` (note, team) — catalog of deliveries
- `resources/` — reading materials, external links, media
- `student-patterns.md` (log, team) — aggregated observations about student learning across deliveries
- `continuing-education.md` (log, team) — instructor's evolving thinking on the field

### Per-delivery canvas (created per semester/cohort)

- `delivery.md` (note, team) — this delivery's context: cohort size, modality, term dates, TAs, notable circumstances
- `weekly.md` (log, team) — what happened each week, attendance patterns, energy notes
- `delivery-decisions.md` (log, team) — per-delivery decisions (accommodations, pace adjustments, content substitutions)
- `students/` — per-student pages (scope: instructor + student themselves, not other students)
  - `students/<student-id>.md` — progress, submissions, communication, accommodations
- `assignments/` — instances of course-canvas assignments as used this delivery
- `grades.md` (note, scope: instructor + TAs, strict)
- `feedback.md` (log, team) — student feedback as gathered (surveys, office hours, informal)
- `issues.md` (log, team) — problems that came up, how handled

Frontmatter:

```yaml
# modules/<id>/
module-id: <id>
position: <sequence>
duration: <weeks>
prerequisites: [<module-ids>]
objectives-served: [<objective-ids>]
version: <n>
supersedes: <prior-version>
```

```yaml
# students/<id>.md (per-delivery)
student-id: <id>
enrolled: <iso>
status: enrolled | dropped | completed | incomplete | audit
accommodations: <if-any>
```

```yaml
# delivered-semesters.md entries
term: <term-id>
year: <year>
cohort-size: <n>
modality: in-person | online | hybrid
canvas: <per-delivery-canvas-reference>
major-changes-from-prior: <summary>
```

## Evolution heuristics

### Course canvas heuristics

**Curriculum revision:**
- Module unchanged across 3+ deliveries but student feedback consistently criticizes → flag for revision
- Assignment used across 3+ deliveries with consistent problem (ambiguity, grading difficulty, irrelevance) → flag for revision
- New module proposed → prompt pedagogical-decisions entry justifying inclusion
- Module deprecated → archive but preserve; courses rotate topics
- Objectives.md revised → prompt review of all modules for alignment

**Cross-delivery pattern surfacing:**
- Student-patterns.md entries showing recurring difficulty across deliveries → flag for curriculum adjustment
- Successful innovation in one delivery (new activity, new sequencing, new assessment) → prompt extraction to course-canvas for future deliveries
- Student question recurring across deliveries → prompt FAQ or module content addition
- Assessment pattern showing grading inconsistency across deliveries → flag rubric refinement

**Material maintenance:**
- Reading with broken link or outdated → flag for replacement
- Reading cited in module but not accessed in past deliveries → flag for relevance review
- Resource added in one delivery's materials but not promoted to course-canvas → prompt promotion decision

### Per-delivery heuristics

**Weekly cadence:**
- Weekly entry missing for past week → whisper to instructor
- Weekly entries showing pace behind plan → flag, propose coverage discussion
- Weekly entries showing acceleration possible → optional surface
- Issues.md accumulating without resolution → flag, surface to instructor

**Student tracking:**
- Student submissions missing across 2+ assignments → whisper; student may need outreach
- Student performance trajectory changing significantly (improving or declining) → flag to instructor
- Student requesting accommodation → capture in student page with official accommodation tracking
- Attendance pattern changing → flag
- Student communication gaps (instructor emails unanswered) → surface

**Assessment discipline:**
- Assignments distributed without rubric attached → flag; students need clarity
- Grading delayed beyond stated turnaround → whisper to instructor
- Grade distribution patterns unusual (bimodal, ceiling effect, floor effect) → flag for review
- Late work policy violations → capture in student page, route to instructor

**Feedback integration:**
- Mid-semester feedback collected → prompt synthesis and decision about adjustments
- End-of-semester feedback collected → flag for pedagogical-decisions capture, course-canvas material update
- Negative feedback pattern across multiple students → surface urgently
- Positive innovation mentioned by students → capture for course-canvas promotion

**Grade submission:**
- Final grades due date approaching → alarm
- Grades missing for enrolled students → flag
- Grade change after submission → capture decision with rationale

**Semester close:**
- Delivery ending → prompt close rituals: final-grade submission, student-communication close-out, feedback capture, retrospective, course-canvas updates proposal
- Pedagogical decisions from this delivery → prompt promotion to course-canvas where appropriate
- Delivery-specific material (guest lectures, topical discussions) → prompt disposition: discard, save, promote

**Rituals:**

Course canvas:
- Annual: comprehensive curriculum review, objectives-alignment check, materials refresh
- Post-delivery: promotion of innovations and fixes from per-delivery to course-canvas
- Pre-accreditation review: compile evidence of objectives achievement across deliveries

Per-delivery:
- Weekly: weekly.md entry, issues.md review, pace check
- Mid-delivery: feedback collection, pedagogical adjustment window
- Assignment close: grading, feedback distribution
- Semester close: final-grade submission, retrospective, archival transition

## Member intent hooks

- "This is how I teach" → `preferences.philosophy-expression: <pedagogical-approach>` — shapes course-canvas philosophy capture, informs module proposals
- "Don't track individual students, I want aggregate only" → `preferences.student-tracking: aggregate-only` — per-student pages minimized, patterns surfaced at cohort level
- "Protect student privacy" → `preferences.student-scope: strict` — per-student pages scope to instructor + that student only, TAs may have course-specific visibility
- "Remind me to grade" → `preferences.grading-reminder: <timeline>` — assignment-based alarms
- "Prep me for next class" → Gardener compiles: this week's module, expected progression, unresolved questions from last class, student alerts
- "What worked last time" → on-demand: prior-delivery comparison on any module or assignment
- "Don't let me forget the feedback" → `preferences.feedback-persistence: required` — end-of-semester feedback must be synthesized before delivery archival
- "I'm TA'ing this section" → scoped access for TA, typically per-module-and-assignment authority with observer access to student pages
- "Guest speaker for week 7" → capture in weekly.md, prompt pre-planning and post-session integration
- "This assignment needs revision" → prompt revision with version bump, mark prior version superseded
- "Copy from last semester" → Gardener proposes inherited content, prompts for explicit adaptation decisions (don't just run the same thing)
- "Accommodations for [student]" → capture formally, route to relevant pages, maintain confidentiality

## Special handling

**Course vs delivery boundary:**

The critical discipline in this pattern is not polluting the course canvas with delivery-specific detail, and not losing delivery-specific insights that should graduate to the course canvas. The Gardener actively maintains this boundary:

- Delivery canvas cannot modify course canvas directly; modifications are proposed and require explicit promotion
- Course canvas cannot reach into active delivery canvas for student-level content; it sees aggregated patterns
- At delivery close, promotion is a deliberate act: what from this delivery becomes part of the course?

**Student privacy:**

Educational records have legal privacy protections in most jurisdictions (FERPA in US, equivalent elsewhere). The Gardener enforces:

- Student-level content is scoped strictly
- Cross-student visibility requires explicit authorization
- External party access (parents, other instructors, administrators) requires documented consent or legal basis
- Grade information has heightened scope protection

**Accreditation and institutional requirements:**

Many courses operate under institutional constraints (syllabus requirements, assessment standards, reporting obligations). The Gardener can track these as a compliance layer; the instructor configures at course-canvas creation. Routine compliance items (syllabus published, grades submitted) become tracked states.

**Modality transitions:**

Courses delivered across different modalities (some in-person, some online, hybrid) may warrant modality-specific content. The Gardener can support this via module-variant pages — one underlying module with multiple delivery approaches, selected per-delivery based on modality.

**Multi-instructor courses:**

When multiple instructors co-teach, the course canvas is shared with equal authorship, and per-delivery canvases either split (separate sections with their own delivery canvas) or merge (one delivery canvas with multi-instructor authorship). The Gardener proposes structure based on actual instruction pattern.

**Online and self-paced courses:**

Asynchronous online courses don't have week-by-week delivery rhythm in the same way. The Gardener proposes cohort-based delivery canvases (students who started together move through together) or rolling delivery tracking (continuous enrollment with per-student progress tracking).

## Composition notes

Course development canvases compose with:

- `multi-project-operations` canvas at the department or school level for instructors teaching multiple courses
- `research-lab` canvas for academics integrating teaching and research
- `creative-collective` canvas for co-teaching teams that have a sustained pedagogical partnership
- Per-student `long-term-client-relationship` canvas for significant mentorship/advising relationships that extend beyond the course

The course canvas is persistent across the instructor's career; per-delivery canvases accumulate as archival. An instructor teaching the same course for 20 years has 20 per-delivery canvases and one course canvas that has evolved substantially. The course-canvas-over-time is itself an interesting document — the Gardener can produce a diff or evolution view on request.

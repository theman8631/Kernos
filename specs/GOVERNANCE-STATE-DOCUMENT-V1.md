# GOVERNANCE-STATE-DOCUMENT-V1 — collapse the governance lifecycle to one document

**Status:** rev 9 — spec GREEN at rev 6; implementation reviewed by kreview
(RED at `33306bd`, RED at `c9f58ee`); this revision carries rounds 2 and 3.
**Supersedes:** the three-artifact governance lifecycle shipped across
`b1db4b6..f38b31f`.
**Authority:** `docs/reference/governance-lifecycle-failure-state-enumeration.md`
— its 19 acceptance criteria are this spec's acceptance criteria, not a
restatement of my own assumptions.
**Modules:** `kernos/kernel/governance_state.py` (new),
`kernos/kernel/self_maintenance_review.py`, `kernos/messages/handler.py`
(`_handle_dump`), `kernos/kernel/friction_response.py` (the three-artifact
governance writer removed from it), `tests/test_governance_state.py`,
`tests/test_governance_items.py`, `tests/test_dump_governance_sections.py`,
`tests/_legacy_governance_fixtures.py` + `tests/fixtures/legacy_governance/`.

## Why

Nine review rounds found nine reachable failure families in the governance
queue. Every fix was correct for the state last constructed and silent about its
neighbours; the last three P0s were each introduced by the fix for the previous
one. The reviewer's diagnosis is the reason for this spec rather than a tenth
patch:

> "The ninth state is the source lifecycle racing the audit lifecycle; the
> current lock protects only the latter."

One logical occurrence is currently spread across three artifacts — the open
item (S), the archive (A), and the closure audit (M) — which must agree at every
interleaving. Ordering, compensation, stable filenames, atomic replacement and a
manifest-only lock each protected one edge and left another reachable.

**This spec does not add a tenth guard. It removes the artifacts.** One
atomically replaced document holds both open items and retained closed history.
Most of the nine families become *unrepresentable* rather than *guarded*: there
is no archive to validate, no path to confine, no manifest to race, and no
source lifecycle separate from the audit lifecycle.

**Non-goals.** Changing what a governance item *means*, weakening the
human-gate, changing the deterministic-only v1 scope (coverage gap remains the
sole producer), or altering the `/dump` recovery surface's contract.

## Design

Single file: `diagnostics/governance/state.json`, replaced atomically.

```jsonc
{
  "schema_version": 1,
  "migration_notes": [ { from_format, decision, occurrence, note } ],   // bounded, structured, validated
  "open":   { "<signature>": { occurrence, signature, title, condition,
                               payload[], opened_iso, last_seen_iso,
                               human_gated } },
  "closed": [ { occurrence, signature, title, payload[], opened_iso,
                closed_iso, resolving_condition, human_gated } ]
}
```

- **Closure is a single write**: move the entry from `open` to `closed` and
  replace the document. There is no second artifact to keep in step, so criteria
  1 and 2 hold by construction and "shadow archive, never delete" becomes
  *retained closed state* rather than a moved file. The lock file is
  coordination, not a second lifecycle artifact.

### Compare-and-close (kreview P0-1)

**`close()` is occurrence-qualified**, not signature-qualified:

```python
close_governance_item(data_dir, *, signature, expected_occurrence, ...) -> CloseResult
```

**Returns an enum, not a bool** (kreview round 2). A boolean cannot distinguish
"already done" from "refused because something newer is open", and the caller
must not treat those alike:

| Result | Meaning |
|---|---|
| `CLOSED` | this occurrence was closed by this call |
| `ALREADY_CLOSED` | this exact occurrence was already in `closed` — idempotent success |
| `OCCURRENCE_MISMATCH` | a **different** occurrence is open; nothing was touched |
| `NOT_OPEN` | no open entry for this signature |
| `STATE_ERROR` | unreadable/corrupt/unknown-version; nothing was touched |

**Precedence, evaluated under the lock** — `ALREADY_CLOSED` and
`OCCURRENCE_MISMATCH` can both be true after the acceptance-23 schedule, so the
order is part of the contract, not an implementation detail:

1. invalid state or invariant conflict → `STATE_ERROR`
2. expected occurrence found in `closed` → `ALREADY_CLOSED` **even if a newer
   occurrence is open**
3. nothing open and expected absent → `NOT_OPEN`
4. a different occurrence is open, expected absent → `OCCURRENCE_MISMATCH`
5. exact open match → close → `CLOSED`

Rule 2 ahead of rule 4 is the point: an ambiguous retry **acknowledges A** while
leaving B untouched, and the next scan evaluates B independently.

On `OCCURRENCE_MISMATCH` **the caller does nothing clever in that pass.** The
next scan re-evaluates the real condition, re-reads the current occurrence, and
may issue a new qualified close only if the condition is still clear.

Rev 1 keyed the transition by signature alone and described a retry as
"observing the occurrence already in `closed`". That is reachable and wrong:

> occurrence A is atomically closed but the acknowledgement fails → upsert opens
> recurrence B → the retry `close(signature)` closes **B**.

An ambiguous retry would absorb a brand-new recurrence — the central
lost-recurrence family, recreated in the design meant to eliminate it. Binding
the request to an expected occurrence removes it:

- proceed only if `open[signature].occurrence == expected_occurrence`;
- return **idempotent success** only when *that exact occurrence* is already in
  `closed`;
- if a **newer** open occurrence is present, leave it untouched and report the
  mismatch — never close it.

The caller (`self_maintenance_review`) reads the occurrence it observed and
passes it, so the close it requests is the close it gets.
- **No path is ever derived from persisted state** (criterion 13). The escape
  that could delete the only copy is not merely blocked — it has no
  representation.
- **Occurrence identity** stays an explicit persisted field, and a recurrence
  always mints a new one, including within one clock tick (criterion 3) by
  mixing in the predecessor id.

### Transaction boundary

Every mutation — upsert, close, migration — runs the **entire** read-decide-write
under one mandatory cross-process lock scoped to the document (criterion 5).
**Failure to acquire the lock fails closed; there is no unlocked fallback.** The
current best-effort `flock` that proceeds unlocked is removed: correctness now
depends on serialization, so silently degrading it is the exact
loud-fail-over-silent-degradation violation this repo already rejects elsewhere.

This closes families 9.1 and 9.3 directly — the lock boundary becomes the
invariant boundary, and the check-then-act window between "observed ABSENT" and
"appended row" no longer exists because the decision is re-made inside the lock.

### Write protocol

Unique temp file → validate the complete candidate document parses and preserves
**every prior closed entry AND every unrelated open entry** → `fsync` temp →
atomic `replace` → `fsync` the containing directory (criterion 9). Validating
only closed history would let a write silently drop another signature's open
item. Durability is claimed only to the extent it is actually implemented; the
docstring states exactly what is guaranteed.

### Failure semantics

- Failure *before* replacement leaves the previous document authoritative and
  byte-unchanged (criterion 10).
- Failure reported *after* replacement is completion-ambiguous but safe: the
  retry observes the occurrence already in `closed` and returns success without
  duplicating it (criteria 7 and 10).
- Unreadable, corrupt-interior, or unknown-`schema_version` documents **fail
  closed with no write** (criteria 4 and 11).
- **Reads never mutate** except an explicit migration under the lock
  (criterion 12) — the current in-passing torn-tail repair inside a read is
  removed.

### Migration (kreview P0-2)

Idempotent, versioned, under the lock. Rev 1 said only "no prior state → fresh
empty document", which would have **discarded a normal legacy open item** —
losing exactly the findings this feature exists to keep. Every direct-parent
S/A/M combination gets a deterministic, individually tested outcome:

All **eight** S/A/M combinations, explicitly. Rev 2 said "every combination" and
listed five; the gaps were not cosmetic — two of them can lose the only
surviving payload once `state.json` becomes authoritative and legacy is ignored.

**Reconciliation is keyed on OCCURRENCE, never on signature** (kreview round 2,
P0). The parent format allows repeated open/close cycles per signature, each
with its own archive and audit row. A reconciler that selects "the" A and "the"
M for a signature imports one cycle and destroys every earlier closure the
instant the document becomes authoritative. So the table below describes the
relation for **one occurrence**; a signature may present several rows of it at
once, and every committed closure is retained.

**Every relation is proved, never assumed by list or filesystem order.** An
audit row must have an archive with the *same* occurrence and the *same*
signature; an unmatched row is case 4 and a mismatched pair would fabricate a
closure association between unrelated records. Duplicate occurrences across
archives, or one occurrence committed in both manifest eras, abort.

| # | S | A | M | Outcome |
|---|---|---|---|---|
| 1 | – | – | – | fresh empty document |
| 2 | ✓ | – | – | → `open`, identity and payload preserved |
| 3 | – | ✓ | – | closure never committed. If A is a **complete, validated** governance source copy → recover as **`open`**. If partial, unreadable, or identity unprovable → **abort, no write** |
| 4 | – | – | ✓ | **abort** — a row alone has no recoverable payload; never synthesise an incomplete closed entry |
| 5 | ✓ | ✓ | – | closure never committed → `open` from **S** (A is an uncommitted attempt) |
| 6 | ✓ | – | ✓ | **always aborts in v1** — see below. M cannot furnish a complete closed record without an archive, and equality cannot prove S is not a raced recurrence |
| 7 | – | ✓ | ✓ | → `closed`, full final payload and identity recovered from A and **validated** against M |
| 8 | ✓ | ✓ | ✓ | S carries a **distinct occurrence** → `closed` (A/M) **+** S as a new `open`. S carries the **committed occurrence** → **abort**. See below |

**Cases 6 and 8 are where I kept being wrong, in progressively subtler ways.**
Rev 2 unconditionally dropped S. Rev 3 dropped S when its occurrence and payload
matched A. kreview refuted rev 3 with a schedule that defeats content equality:

> copy A from S at t1 → a concurrent upsert re-detects the same condition and
> writes the **same payload** at t2 (M is still absent, so it preserves the old
> occurrence and may change only `Last-seen`) → close commits M for the t1
> snapshot → retirement fails → migration sees equal occurrence and payload and
> drops S as a duplicate.

A live recurrence is lost *while payload equality passes*. And within one clock
tick an identical redetection may leave **no distinguishable field at all**.

The real problem is epistemic, and it is worth stating plainly because I had it
backwards twice: **equality is consistent with both hypotheses.** "S matches A"
is equally explained by "S is merely retirement-pending" and by "the condition
was re-observed identically during close". Direct-parent state does not contain
the evidence to prove the negative claim that no recurrence occurred, so
equality is **not proof** and must never be treated as such.

Migration is therefore bound to the evidence it actually has:

- **Recurrence is decided by occurrence identity, and by nothing else** (kreview
  round 2). Rev 6 said *any* lifecycle-field divergence proves a recurrence.
  That is wrong twice. Semantically, the parent's `upsert_governance_item`
  **preserves** the occurrence when it merely re-observes a live condition, so a
  differing `payload` or `Last-seen` is equally explained by "the same
  occurrence was touched while its retirement was pending" — importing that as a
  recurrence opens an entry carrying an occurrence already committed as closed.
  Mechanically, S and A were compared across two *different* parsers that
  normalise `title` differently and neither of which recovers `condition`, so
  genuinely identical records compared unequal and the abort below was
  **unreachable**. Field comparison is therefore forbidden here.
- **S with a distinct occurrence** → preserve the committed A/M closure **and**
  carry S as a new **open** occurrence — **but only in case 8, where A supplies
  the historical snapshot.** Case 6 has no archive and cannot satisfy this rule;
  it aborts (below). This qualification is load-bearing: the unqualified version
  of this sentence, left standing in rev 5, still authorized the unsafe outcome
  the case-6 section was written to forbid.
- **S carrying the committed occurrence → ABORT**, no state write, legacy left
  authoritative — unless a trusted direct-parent phase marker proves S was not
  rewritten after the archived snapshot. No such marker exists in `f38b31f`, so
  in practice this aborts.
- **Case 6 (S+M, no A) takes the same rule.** M's `final_payload` plus an equal S
  can prove the payload is *available*; it cannot prove S is not a raced
  recurrence. Without trusted phase evidence: abort.

#### Case 6 divergence must also be RECONSTRUCTABLE (kreview round 4)

Rev 4 said divergence in case 6 yields "`closed` (from M) + S as a new `open`".
That over-promised, because **the direct-parent M row is not a complete closed
record.** The live writer stores `governance_txn`, `governance_signature`,
`opened_iso`, `closed_iso`, `resolving_condition`, `final_payload` and
`archive` — but the new `closed` schema also requires `title` and
`human_gated`, and case 6 has **no archive** to recover them from.

The only other on-disk source is S — which, on the divergent branch, *is
recurrence B*. Borrowing B's values to describe A's historical closure would
fabricate history, and it is worst exactly where it matters: when `title` or the
human-gate marker **is** the field that diverged.

**V1 DECISION: case 6 aborts unconditionally.** kreview round 5 was right that
rev 5 left the predicate to the implementation ("*if* title and human_gated are
declared immutable derivations…") while the acceptance criterion accepted
"closure+open **or** abort". That is not a test oracle — it is a spec that
cannot be failed.

So this spec picks kreview's option (b) rather than (a):

- **Case 6 → abort, no state write, legacy left authoritative.** Always. One
  outcome, testable exactly.

Why (b) over enumerating an authoritative registry: it is true *today* that
`human_gated` is constant and the sole producer's `title` is fixed — but those
are **incidental facts about the current code**, not versioned invariants.
Elevating them into migration-load-bearing guarantees would mean a future second
producer silently breaks historical reconstruction. Given the verified
no-live-artifact precondition, aborting costs nothing real and keeps v1 honest.
A later version may add registry-derived reconstruction as a deliberate,
versioned invariant.

**Never a synthesized closed row** remains the governing rule; v1 simply reaches
it by refusing the case rather than by qualifying it.

Case 8 is unaffected: it has A, and therefore the full snapshot.

A conservative ambiguous-recovery state could replace abort, but it would have
to be explicit and human/re-scan resolvable — silently choosing `closed` is the
unsafe option. Given the verified no-live-artifact precondition, **aborting on
ambiguous constructed parent states is the smaller v1 design** and is what this
spec adopts.

| Manifest condition | Outcome |
|---|---|
| M torn tail | dropped (an incomplete append was never durable), recorded in migration notes, never silently |
| M corrupt interior | **abort, no write, loud error** — committed history is never discarded |
| unknown `schema_version` | abort, no write |

**Governing rule for every abort:** leave legacy state authoritative and write
nothing. **An empty or partial new document must never become authoritative over
the sole surviving copy of a payload.**

**Precedence once `state.json` exists** (criterion 12): the valid document is
authoritative and legacy files become read-only and are **ignored** — never
re-imported. Migration runs once, records that it ran, and a crash mid-migration
retries safely because the import is idempotent and the replace is atomic.

**Format scope.** Formats at or after `f38b31f` (the `_governance_manifest.jsonl`
era) are supported. The older *shared* `_manifest.jsonl` era is also imported,
with **strict row filtering** — a row must carry `governance_txn` **and** a
`governance_signature` to be considered, so friction-resolution rows in that
shared file can never be imported as governance closures.

**Migration reads strictly; the chat path reads best-effort.** These are
different jobs and must not share a reader (kreview round 2, P1). The runtime
readers skip an unreadable artifact and parse a partial one into empty fields,
because a failed read must never break the chat path. Migration inherits the
opposite obligation: an artifact it cannot parse is *missing evidence*, and
discarding it converts missing evidence into "nothing to import" — after which
the document written on that basis makes the artifact invisible forever. So
**every discovered governance artifact parses and validates, or the entire
migration aborts.**

**Completeness is proved, not inferred from two fields.** "Has a signature and
an opened stamp" is not completeness (kreview round 3). A truncated document
that retains those two imports with an empty payload, an empty condition, and —
worse — `human_gated=False`, because the absent marker parses the same as a
negative one. That makes a partial artifact authoritative *and* silently strips
the gate from the finding the gate exists to protect. So every field the parent
always wrote is required: the `Class: governance` line, the title, an
**affirmative** `Human-gated: true`, both timestamps, and both `## Condition`
and `## Payload` sections with content. Parsing is by section: the earlier
reader collected every `- ` line in the document as payload and hard-coded
`condition` to empty, so a complete parent source always lost its condition on
import.

**An archive and its audit row must agree on CONTENT, not merely on identity.**
The parent read the payload, copied the source, then wrote the row — with no
lock on the source across those steps. A re-detection landing mid-close
therefore yields an archive holding the *new* payload and a row holding the
pre-close one. Matching occurrence and signature cannot see that: the pair is
internally inconsistent, and committing it records the re-detected finding as
closed and leaves nothing open — the lost-recurrence race, encoded in the
artifacts themselves. The row must carry a complete closure (`opened_iso`,
`closed_iso`, `resolving_condition`, `final_payload`) and agree with its archive
on the opened stamp and the payload, or **abort**.

**Pre-identity records derive the parent's own occurrence id.** Occurrence ids
were not always persisted. An empty occurrence must never be committed —
compare-and-close and the closed-history relation are both keyed on it — so an
otherwise complete pre-id record derives `sha256(signature|opened_iso)[:16]`,
which is exactly what the parent would have minted. If signature or opened
stamp cannot furnish that derivation, **abort**.

**An ABORTED migration binds the caller, not just `migrate()`.** Abort means the
legacy artifacts are still authoritative and their ambiguity is unresolved. A
caller that discards the outcome and upserts anyway creates `state.json`, which
becomes authoritative and makes the un-migrated occurrence invisible — the exact
loss the abort exists to prevent. On `ABORTED` the caller performs **no
governance read, write, or acknowledgement** that pass, logs the recovery state,
and lets a later run retry.

Verified precondition: no governance artifacts exist in any deployment today, so
migration correctness is exercised against constructed parent-format states in
*tests*. That is a fact about deployments, **not** evidence the design is safe.

### Document validation is deep, and lives in one place

Validating only the three top-level container types is not fail-closed: a
document with `open={"sig": {}}` passes it, reports healthy, and then raises
`KeyError` from `open_items` — outside the `StateError` path every caller
handles, defeating both the fail-closed contract and `/dump`'s
unreadable-vs-empty distinction. `read_document` and `_validate_candidate` share
one deep validator covering entry shape, key/signature agreement, required
non-empty strings, payload element types, and occurrence uniqueness across
`open` ∪ `closed`. A write must not be able to introduce a document its own
reader would reject.

Every field a reader dereferences without a default is required, including
`title`, `condition`, `resolving_condition` and the `human_gated` marker
(kreview round 3) — defaulting them in the validator and then requiring them at
the read is precisely how a "valid" document still raised `KeyError` from
outside the `StateError` path. **Migration notes are validated, not merely
counted**: a note is the only evidence that migration ran, and `[{}]` satisfying
a shape check is enough to make the next run report `ALREADY_MIGRATED` and
suppress a live legacy source forever. The note bound is enforced on read as
well as on write.

**A decode failure is corruption like any other.** `read_document` catches
`UnicodeDecodeError` as `StateError`; letting it escape means it is *not* a
`StateError`, so `health`, `open_items`, `closed_items` and `close_item` all
raise instead of degrading — every fail-closed surface bypassed at once. The
same holds for the migration readers, including the audit manifests, where
`errors="replace"` would turn an invalid byte inside a valid JSON string into
U+FFFD and persist the damage as if it were the committed text.

## Acceptance criteria

The 19 criteria in
`docs/reference/governance-lifecycle-failure-state-enumeration.md` §"Rewrite
acceptance criteria" are adopted verbatim and in full. Additionally:

20. Every one of the nine documented failure families is either **unrepresentable**
    in the new design, or has a test constructing it and asserting the safe end
    state. The spec's own claim of "unrepresentable" must be justified per family
    in the implementation notes, not asserted globally.
21. `unassigned_modules()` remains empty on a clean tree, and the coverage-gap
    producer still opens, updates and closes an item end-to-end.
22. No behavioural change to the human-gate, the fail-closed class parser, or the
    quarantine surface.
23. **Compare-and-close is constructed, not assumed.** The exact schedule —
    close A → replace succeeds → acknowledgement fails → upsert opens recurrence
    B → retry close(signature, expected=A) — returns `ALREADY_CLOSED`, ends with
    B still open, one closed entry for A, and no second closure. The full
    `CloseResult` precedence order is asserted, including the case where
    `ALREADY_CLOSED` and `OCCURRENCE_MISMATCH` are simultaneously true.
23a. **Case 6 aborts — exactly one expected outcome per test.** Constructed
    cases where S diverges *only* in `title`, *only* in the human-gate marker,
    where M lacks `final_payload`, and where S and M are identical. **All four
    assert abort with no state write and legacy left authoritative.** No test
    accepts "either outcome".
23b. **Migration ambiguity fails closed.** kreview's schedule — copy at t1,
    same-payload re-detection at t2, M committed for the t1 snapshot,
    retirement failed — must **abort with no state write**, not merge. Asserted
    for both case 6 and case 8, including the same-clock-tick variant where no
    field differs at all.
24. **Every migration row in the table above has its own test** built from that
    on-disk state, asserting the exact resulting document. Re-running migration
    over an existing valid `state.json` is a no-op that does not re-import
    legacy files.
25. **Candidate validation rejects a write that drops an unrelated open entry**,
    proved by mutation.
26. **Document size and write/lock latency are observable**, and the over-limit
    path fails closed rather than truncating.

### Added by kreview round 2 (implementation review of `33306bd`)

27. **Every historical occurrence survives migration.** Constructed: three
    committed open/close cycles for one signature import as three closed
    entries; the same plus a distinct live occurrence imports as two closed and
    one open. Reconciliation is keyed on occurrence, not signature.
28. **Every A/M relation is proved.** A row with no archive at that occurrence,
    and a row whose archive carries a different signature, both **abort with no
    state write**. Two uncommitted archives for one signature abort. One
    occurrence committed in both manifest eras aborts.
29. **Migration reads strictly.** A partial or non-UTF-8 governance document in
    either directory **aborts**; it must never read as `FRESH`. Asserted for
    both `friction/` and `friction_resolved/`.
30. **No empty occurrence is ever committed.** A complete pre-id record imports
    with the parent's own derived id — asserted equal to
    `friction_response._new_occurrence_id(sig, opened, "")`, not merely
    non-empty. A record that cannot furnish the derivation aborts.
31. **The shared `_manifest.jsonl` era is actually read.** A complete archive
    plus its governance row in the shared file imports as `closed`, not as a
    reopened item; friction rows in that file are never imported as governance
    closures.
32. **Deep validation holds on every surface.** Corrupt nested entries — empty
    fields, key/signature disagreement, empty occurrence, non-list payload,
    missing closed fields, an occurrence both open and closed — make
    `read_document` raise, `health()` report `unreadable`, `open_items` and
    `closed_items` degrade to empty rather than raise, `close_item` return
    `STATE_ERROR`, and `upsert_item` raise `StateError`.
33. **An ABORTED migration is asserted through the production caller.**
    `maybe_run_daily` over an aborting parent state creates no `state.json`,
    acknowledges nothing, and leaves the legacy artifacts intact.

### Added by kreview round 3 (implementation review of `c9f58ee`)

34. **Fixtures are byte-exact parent artifacts.** `tests/fixtures/legacy_governance/`
    holds an open item, an archive, and an audit row copied verbatim from the
    parent writer at `f38b31f`; every constructed state varies one field off
    those goldens. `test_the_golden_artifacts_are_accepted_unmodified` pins the
    base case, without which every "this state aborts" assertion could pass for
    the wrong reason. Each hand-rolled fixture the goldens replaced was missing
    something real artifacts always carry, which is how rounds 1 and 2 shipped
    defects past a green suite.
35. **An archive and its row must agree on content.** Constructed: archive
    payload `[redetected.py]` with a same-occurrence, same-signature row
    carrying `[pre-close.py]` **aborts**. So does disagreement on the opened
    stamp, and a row missing `closed_iso`, `resolving_condition`, or
    `final_payload`.
36. **Truncated legacy documents abort.** A document that RETAINS signature and
    opened stamp but lost a later section aborts, asserted for truncation after
    `Last-seen:`, after `## Condition`, and after `## Payload` (heading present,
    section empty), in both directories. A non-`governance` class aborts.
37. **The human gate is affirmative or absent-means-abort.** `Human-gated: false`
    and a missing marker both abort at the reader; neither may import.
38. **The condition survives import.** A complete parent source imports with its
    `## Condition` text intact, parsed by section.
39. **Lossy decoding is not repair.** A non-UTF-8 audit manifest aborts rather
    than persisting U+FFFD into `resolving_condition`; a non-UTF-8 `state.json`
    fails closed on `read_document`, `health`, `open_items`, `closed_items` and
    `close_item`.
40. **Migration notes are validated.** `[{}]`, a note with no decision, a
    non-string decision, and more notes than the bound all fail closed — and the
    live legacy source they would have suppressed still aborts loudly instead of
    being declared already-migrated.

## Implementation notes — AC 20 disposition, per family

AC 20 forbids a blanket "unrepresentable" claim. Each family is dispositioned
individually below, with the test that carries the claim. Families marked
*unrepresentable* all rest on one physical fact — closure no longer spans a
source file, a derived archive path and an append-only audit log — and that
fact is asserted directly by
`test_no_governance_operation_creates_a_second_artifact`, which enumerates every
file the module writes across two open/close cycles and requires it to be
exactly `{state.json, state.json.lock}`. Without that test the word
"unrepresentable" would be prose.

| # | Family | Disposition | Carried by |
|---|--------|-------------|-----------|
| 1 | Move-first closure loses the recovery surface | **Unrepresentable** — nothing is moved and no path is derived from state; the item and its closure are two fields of one replaced document | `test_no_governance_operation_creates_a_second_artifact` |
| 2 | Audit-first closure records an effect that never happened | **Unrepresentable** — the closure and its audit are the *same* write, so no ordering between them exists to get wrong | as above; plus `test_over_limit_write_refuses_and_keeps_prior_state` (a refused write records nothing) |
| 3 | A compensating move is not a transaction | **Unrepresentable** — there is no partial state to compensate. A failed replace leaves the previous document byte-identical | `test_over_limit_write_refuses_and_keeps_prior_state` asserts the prior bytes survive |
| 4 | Loss-free copy is not idempotent recovery | **Guarded** — compare-and-close. Constructed, not assumed | `test_ambiguous_retry_acknowledges_A_and_leaves_recurrence_B` |
| 5 | Filename existence and boolean audit lookup are not phase evidence | **Unrepresentable** — no filename encodes state and no lookup is boolean; the result is a five-valued `CloseResult` over persisted occurrence identity | `test_close_result_precedence` |
| 6 | Persisting occurrence identity does not remove ambiguous completion | **Guarded** — ambiguity survives only in the *migration* window, where it aborts rather than guessing | `test_case6_aborts_for_every_variant` (4 variants), `test_case8_identical_aborts_because_sameness_is_not_proof`, `test_recurrence_is_decided_by_occurrence_not_by_field_comparison` (which carries the differing-payload schedule) |
| 7 | Fail-closed can become permanently stuck, and references must resolve | **Addressed** — every fail-closed state has a recovery transition: `STATE_ERROR` is retryable, an over-limit refusal succeeds once the limit is right, and an aborted migration leaves legacy authoritative for the next run | `test_over_limit_write_refuses_and_keeps_prior_state` (refuse → succeed), `test_case6_aborts_for_every_variant` (legacy left intact) |
| 8 | Atomic replacement is not a shared transaction or a trust boundary | **Guarded** — the document lock spans the whole read-decide-write, and candidate validation is the trust boundary on the replacing value | `test_candidate_validation_refuses_to_drop_unrelated_open`, `test_candidate_validation_refuses_to_drop_closed_history` |
| 9 | Locking the audit does not lock the lifecycle | **Unrepresentable** — there is no second lifecycle. One lock, one document, one write | `test_no_governance_operation_creates_a_second_artifact` |

Two implementation findings worth recording, because both were mis-specified
before a test constructed them:

1. **Recurrence is decided by occurrence identity, never by field comparison.**
   A rev-6 draft compared lifecycle fields between the legacy source (S) and the
   archived snapshot (A). That is wrong twice over. Semantically, the legacy
   upsert *preserves* the occurrence when it merely re-observes a live
   condition, so a differing payload or last-seen is equally explained by "the
   same occurrence was touched while its retirement was pending" — importing
   that as a recurrence would create a second entry carrying an occurrence id
   already recorded as closed. Mechanically, S and A arrive through two
   *different* parsers that normalise `title` differently and neither of which
   recovers `condition`, so genuinely identical records compared unequal and the
   case 8-identical abort was unreachable. Both are pinned by
   `test_recurrence_is_decided_by_occurrence_not_by_field_comparison`.
2. **A migration that finds nothing must still leave a marker.** An empty
   document cannot distinguish "migrated, found nothing" from "never migrated",
   so without a `migration_notes` entry every run would re-scan legacy files and
   could resurrect artifacts a later close had removed
   (`test_fresh_migration_records_that_it_ran`).

The `/dump` reader carries a third: `open_items` degrades a corrupt document to
an empty list so a failed read can never break the chat path, but on the
*recovery* surface that degradation is the defect — "(none open)" and "the queue
is unreadable" must not render identically. `governance_state.health()` gives
the operator surface the distinction, including the un-migrated case where the
document is legitimately empty while legacy artifacts still hold live items
(`test_unreadable_queue_never_renders_as_empty`,
`test_unmigrated_legacy_items_are_not_reported_as_an_empty_queue`, both proved
by mutation).

## Settled by kreview round 1

- **Single document is the right primitive.** Closure is genuinely one atomic
  replacement; the lock file is coordination, not a second lifecycle artifact.
- **Document-wide lock** for every read-decide-write. Per-signature locks are
  *incorrect* for writers replacing one shared document.
- **Lock acquisition failure fails closed.** My rev-1 justification was
  self-contradictory: I wrote that "refusing to record is worse than the race"
  and then chose fail-closed anyway. kreview corrected the direction — **the
  race is worse than refusing to record.** An unlocked fallback is never
  acceptable; an unsupported platform needs a real platform-specific lock or a
  loud refusal.
- **No compaction in v1**, and deletion is not invented here. Retained history
  is acceptable provided **size and lock/write latency are observable**, with
  **fail-closed over-limit behaviour** rather than silent truncation.

## Open review asks (rev 2)

1. **Compare-and-close mismatch reporting.** When `expected_occurrence` does not
   match a newer open occurrence, I return a distinguishable failure rather than
   a bare `False`, so the caller can tell "already closed" from "superseded".
   Confirm the caller should simply skip and re-evaluate next scan rather than
   attempt anything cleverer.
2. **Orphan archive with no source and no row.** I record it as an observable
   anomaly and refuse to synthesise a closed entry, because a closure that never
   committed did not happen. Confirm that is preferable to importing it as
   closed on the grounds that the payload exists.
3. *(settled — kreview round 3)* The case 3 / case 5 asymmetry is **intended**:
   it is **source precedence**. With no M, closure never committed. If S exists
   it remains the authoritative current occurrence and the orphan A is only an
   uncommitted snapshot. If S is absent, a complete validated A is the sole
   surviving payload, so conservative recovery as open is correct. Partial or
   unprovable A still aborts.

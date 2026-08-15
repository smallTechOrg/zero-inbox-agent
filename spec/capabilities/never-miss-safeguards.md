# Capability: Never-Miss Safeguards

## What It Does
Enforces the product's overriding guarantee — **an important email must never be missed** — through a
second-pass reviewer hunting only for false negatives, a confidence floor, a reply-history signal, VIP
and time-sensitive guards, and a finality gate that makes an un-reviewed decision unappliable.

**Every one of these must be live and tested before any phase performs a real mailbox mutation.**

---

## Phase 9: the never-miss semantic is redefined — from *hold* to *label*

> This is the single deliberate redefinition of a safety rule in the product's history. It was
> authorised explicitly by the user, in writing, after seeing the measured numbers below. **Every
> other trust invariant in this file and in [roadmap.md](../roadmap.md#safety-invariants-apply-to-every-phase-forever)
> is unchanged and absolute.** No future phase may extend this precedent to any other invariant
> without the same explicit authorisation.

### What changed

| | Before Phase 9 | From Phase 9 |
|---|---|---|
| A never-miss verdict means | **leave the thread in the inbox** | **archive the thread and attach its never-miss Gmail label** |
| The thread is found by | scrolling the inbox | one click on the `ZeroInbox/Urgent`, `ZeroInbox/Important`, `ZeroInbox/People` … label |
| Reversibility | n/a (nothing happened) | an `ActionLog` row with a non-null `undo_token`, per-thread undo and run/job-level bulk undo |
| Inbox floor | **365 threads that no run can clear** | **0** |

### Why this is still a never-miss guarantee

The guarantee was never *"the mail is in the inbox"*. The guarantee is **"the user can always find it,
always in one step, and can always put it back."** Phase 9 preserves all three properties and improves
two of them:

1. **Nothing is ever deleted.** The prohibition on `trash`, `delete` and spam-reporting is unchanged
   and structurally asserted — no such method exists on the Gmail mutator.
2. **Nothing is ever archived unlabelled.** A never-miss archive always carries its category label.
   A bare `archive` with no label is not a permitted operation for a never-miss category. So the mail
   is *one click away in the Gmail sidebar*, permanently, under a name that states why it was held.
3. **Everything is reversible.** Every never-miss archive writes an undo token; per-thread undo and
   bulk undo both restore the exact pre-triage label set.
4. **Findability actually improves.** Under the old semantic a held thread sat in an inbox of 365
   other held threads with no marking at all. Under the new semantic it sits under a label that names
   the reason. *"Held in a 365-thread inbox"* was never a safety property; it was the absence of one.

### Why the old semantic had to go — the measured evidence

The user's real inbox after the last Phase 8 run held **365 threads that no run could clear**:

| Bucket | Count | What it means |
|---|---|---|
| `held_by_never_miss` | 227 | a never-miss signal fired and the thread was left in the inbox |
| `category_keep` | 76 | the user's taxonomy says this category stays |
| `below_threshold` | 46 | above the floor, under the autonomy bar |
| `needs_your_call` | 16 | **no category fit** — the taxonomy-gap signal |
| `unclassified` | 0 | — |

`held_by_never_miss` was the binding constraint, and it broke down by `decided_by` as
**llm 186 · reviewer 23 · sender_history 18** — two distinct causes, both addressed in this phase:

- **A genuine bug (18 threads).** See [§ Correspondent truth](#correspondent-truth-phase-9) below.
- **The time-sensitive guard doing exactly what it was told (the rest).** The top held senders were
  `no_reply@email.apple.com` (44), `noreply@email.apple.com` (39), `no-reply@accounts.google.com`
  (39), `reminders@facebookmail.com` (27), `security@facebookmail.com` (11), with reasoning of the
  form *"Security alert with new sign-in attempt, time-sensitive and unread"*. These are correct
  never-miss verdicts about genuinely time-sensitive automated mail. Under the old semantic the only
  expression of "this matters" was *leave it in the inbox*, so 200+ automated notices permanently
  floored the inbox. Under the new semantic the same verdict is expressed as
  **archive + `ZeroInbox/Urgent`** — the signal is preserved, the floor is not.

---

## Inputs
| Input | Type | Source | Required |
|-------|------|--------|----------|
| Mutating proposals (`archive`, `digest`) | `Decision[]` | cost-tiered triage + `align_to_category_default` | Yes |
| Confidence floor | float | `user_settings.confidence_floor` | Yes |
| Sender reply history | `ever_replied` per sender | `sender_profiles` | Yes |
| Connected account's own addresses + aliases | `str[]` | `channel_accounts.account_email` + Gmail `users.settings.sendAs` | Yes (Phase 9) |
| VIP list | entries | `vip_entries` | Yes |
| Priorities profile | text | `priority_profiles` | No |
| Never-miss label mapping | category per never-miss reason | `categories` | Yes (Phase 9) |

## Outputs
| Output | Type | Destination |
|--------|------|-------------|
| Flipped decisions | `Decision[]` with `decided_by="reviewer"` | `decisions` |
| Reviewer reasoning | text appended to the decision | `decisions.reasoning` |
| `review_state` per decision | `provisional` / `reviewed` / `review_failed` | `decisions.review_state` |
| Never-miss archive + label | mutation + undo token | Gmail + `action_log` |
| Needs-your-call queue | decisions with `status="needs_your_call"` | `decisions` |

## External Calls
| System | Operation | On Failure |
|--------|-----------|------------|
| LLM provider | second-pass review, batched 20–50 **mutating** proposals | Retry 3×; on persistent failure the batch's rows become `review_state="review_failed"`, are **never** marked `reviewed`, and can never be applied. Recoverable via **Retry review** ([review-recovery](review-recovery.md)) |
| Gmail | `archive_and_label` for a never-miss category | Retry 3× inside the mutator; on failure the thread stays in the inbox, the ledger names it, and the run reports `apply_ok=false` |

---

## Business Rules

### Mechanism A — second-pass reviewer

- A separate prompt and a separate call see only the mutating proposals and answer one question:
  *would the user be upset to miss this?* A flip becomes `keep` with `decided_by="reviewer"` and the
  reviewer's reasoning appended. **The reviewer can only flip toward `keep`, never toward archive.**
- **Phase 9 — the reviewer's audit scope is every mutating action, not the `archive` label.** Before
  Phase 9, `second_pass_reviewer` selected `proposed_action == "archive"` only, while
  `apply_decision()` permits `proposed_action in ("archive", "digest")` and `digest` reaches the same
  `archive_and_label(remove_label_ids=[INBOX])` call. A `digest` row was therefore mutated without
  ever having been audited. The scope is now defined **by mutability**: the reviewer audits exactly
  the set of actions `apply_decision()` is willing to mutate, and the two sets are asserted equal by a
  test — so adding a third mutating action can never again silently create an unaudited path.

<a id="review-state-is-per-decision"></a>
### Mechanism A′ — `review_state` is a claim about the DECISION, not about the RUN (Phase 9)

This is the **item-zero** fix and the safety foundation of the phase.

- **The defect.** `finalise_review` upgraded `review_state` across the run's *whole* decision set. A
  row the reviewer never looked at therefore read `reviewed`, and `NotReviewedError` — the gate that
  is supposed to stand between an unaudited decision and the mutator — passed **vacuously**. Measured
  live: **171 `digest` decisions, 44 of them already applied unreviewed.**
- **The fix.** `finalise_review` is given the explicit set of item ids the reviewer **actually
  audited** in this pass, and marks **only those** `reviewed`. Every other row keeps whatever state it
  already had — `provisional` for a row no reviewer has seen. `review_state="reviewed"` becomes a
  claim about the decision, and `NotReviewedError` becomes load-bearing rather than decorative.
- This closes the whole class, not the one instance: any future node that decides an action without
  routing it through the reviewer produces `provisional` rows that simply cannot be applied.
- It matters **more** after the reframe, not less: far more mail is mutated once never-miss archives.
- **Historic rows.** Migration `0008` downgrades to `provisional` every row that reads `reviewed`,
  was never audited (non-`archive` proposals from pre-Phase-9 runs) and is **not yet applied**. The 44
  rows already applied unreviewed cannot be un-applied and are **not** rewritten; they are counted in
  a new `unreviewed_applied` ledger figure and named in the UI, because the honesty rule outranks a
  tidy migration.

### Mechanism B — confidence floor

- Any mutating proposal with confidence below `user_settings.confidence_floor` (default `0.75`)
  becomes `status="needs_your_call"` and is **not** archived. The floor is user-adjustable but cannot
  be set to `0`.

### Mechanism C — reply-history signal

- A sender with `ever_replied = true` is important. Their threads are forced to `keep` unless an
  explicit user override names that sender. Checked **after** the reviewer, so it cannot be argued
  away by the model.

### <a id="correspondent-truth-phase-9"></a>Mechanism C′ — correspondent truth (Phase 9)

A reply-history signal is a claim that **a human corresponded with the user**. Two classes of address
can never support that claim, and both were generating one:

- **The user's own address, and any alias on the connected account.** `_accumulate_recipients`
  harvests `To`/`Cc` from the user's `SENT` mail, so a self-addressed note makes the user their own
  most-replied-to correspondent. Live: **18 threads from `psykrsna@gmail.com` — the user's own
  address** — held with *"You have replied to psykrsna@gmail.com before (24 replies of 0 received)"*.
  `24 replies of 0 received` is the signature of this bug: a real correspondent sends mail too.
  **Rule:** the connected account's `account_email` and every address in its Gmail `sendAs` alias list
  are excluded from sender-history accumulation entirely. They never produce a `SenderProfile` reply
  signal, at either the ingest layer or the guard layer (defence in depth, both tested).
- **No-reply senders.** An address whose local part matches `no-reply` / `noreply` / `no_reply` /
  `donotreply` / `do-not-reply` (case-insensitive, hyphen/underscore/dot-insensitive) **cannot be a
  correspondent** — replying to it goes nowhere. Such a sender never generates a reply-history
  never-miss signal, and the flag is a strong, free, zero-token taxonomy signal
  (see [inbox-derived-taxonomy](inbox-derived-taxonomy.md)). This does **not** downgrade the mail: a
  no-reply security alert is still time-sensitive and still gets its never-miss label. It only stops
  a *reply-history* claim that is false on its face.

### Mechanism D — VIP and time-sensitive

- VIP entries (email, domain, keyword) are never archived **without their label**, at any confidence.
- `time_sensitive` threads (deadlines, invoices, legal, security alerts) carry a never-miss verdict
  unless a user-promoted automatic rule explicitly covers them.

### The chain order is fixed and unchanged

`align_to_category_default` → **reviewer** → floor → reply-history → VIP → time-sensitive → label
resolution. Every stage can only make an outcome **more conservative**. `align_to_category_default` —
the only stage permitted to move a decision from `keep` toward a mutating action — runs *immediately
before* the reviewer, so every archive it produces is audited, floored and vetoable. Performing the
same conversion after the chain, or at apply time, is forbidden.

### Phase 9 — how a never-miss verdict is expressed

- A never-miss verdict no longer sets `proposed_action="keep"` by default. It sets
  `proposed_action="archive"` **with a mandatory never-miss category**, and stamps
  `autonomy_state="held_by_never_miss"` so the reason survives into the ledger and the UI. The verdict
  is unchanged; only its *expression* is.
- **Label resolution** (first match wins, deterministic, no LLM):
  1. VIP match or `ever_replied` (post-correspondent-truth) → the `People` category.
  2. `time_sensitive` → the `Urgent` category.
  3. A reviewer flip for any other reason → the `Important` category.
  4. No never-miss category resolvable (e.g. the user deleted it) → **the thread stays in the inbox**
     and is named in the ledger as `no_never_miss_label`. Silence is never the fallback.
- **`Important` is a Phase 9 addition to the default taxonomy** — the destination for "the reviewer
  said this matters but it is not urgent and not a person". It is a `NEVER_ARCHIVE_KEYS` member (see
  below).
- **The confidence floor still binds.** A below-floor thread is `needs_your_call` and stays in the
  inbox. Never-miss-as-label does not archive mail the agent was not confident about; it archives mail
  the agent was confident *matters*.
- **`needs_your_call` still stays in the inbox.** The reframe covers never-miss holds, not
  low-confidence or no-category-fit threads. Those are fixed by
  [inbox-derived-taxonomy](inbox-derived-taxonomy.md) driving both buckets to zero, not by archiving
  them.

### <a id="never-archive-keys-reconciled"></a>Reconciling `NEVER_ARCHIVE_KEYS` with the reframe (Phase 9 — deliberate)

`NEVER_ARCHIVE_KEYS = {urgent, people, legal}` (+ `important` from Phase 9) **stays exactly as it
is**, and is extended, not deleted. The distinction it protects is real and is the whole point of the
reframe:

- `default_action = "archive"` on a category means **"this whole category is noise — sweep it"**.
  Applied to People/Urgent/Legal/Important it would mean a stranger's genuine first email, or a legal
  notice, is swept as routine. That remains forbidden, on create and on update, with no UI able to
  express it.
- **A never-miss archive is a different operation.** It is per thread, it is caused by a never-miss
  signal *firing*, it always attaches the category's own label, and it is always undoable. It routes
  through `archive_to_never_miss_label`, never through `default_action`.
- Concretely: `ZeroInbox/Urgent` mail leaving the inbox **into `ZeroInbox/Urgent`** is not the same
  event as the Urgent category being marked archive-by-default. The guard blocks the second and has
  never blocked the first. Deleting the guard would delete the protection against a taxonomy edit — a
  bulk, silent, category-wide change — which is precisely the failure mode found live on a real
  account (commit `6e37375`).
- Enforcement: `_validate_action` is unchanged; a Phase 9 test asserts every `NEVER_ARCHIVE_KEYS`
  member still rejects `default_action="archive"` **and** that the same categories are reachable by
  `archive_to_never_miss_label`.

### Durability never implies finality

Decisions persist incrementally as `review_state="provisional"`
([durable-resumable-runs](durable-resumable-runs.md)). Only rows the reviewer **actually audited**
become `reviewed`. **Only `reviewed` decisions are appliable** — `apply_decision()` raises
`NotReviewedError` before the mutator is touched, independent of `status`, and `force=True` does not
bypass it.

---

## Success Criteria

- [ ] Over the 220-thread fixture, zero threads from a genuine `ever_replied` sender are archived
      **without** their never-miss label; every one that is archived carries `ZeroInbox/People` and a
      non-null undo token.
- [ ] Every mutating proposal below the configured floor has `status = needs_your_call` and no mutation.
- [ ] The reviewer flips the seeded false-negative bait thread from archive to keep, visible in the
      reasoning with `decided_by="reviewer"`.
- [ ] Setting the confidence floor to 0 is rejected with a validation error.
- [ ] **Reviewer scope equals mutator scope.** A test asserts
      `nodes_review.REVIEWABLE_ACTIONS == {a for a in actions.MUTABLE_ACTIONS}` and fails if either
      set is changed alone.
- [ ] **A `digest`-proposed decision is audited.** A run containing `digest` proposals produces a
      reviewer verdict for every one of them; with the reviewer stubbed to fail, all of them end
      `review_state="review_failed"` and `apply_decision()` refuses each with `NotReviewedError`
      **without calling the mutator**, including with `force=True`.
- [ ] **`review_state` is per decision, not per run.** In a run where the reviewer audits only a
      subset, `finalise_review` marks exactly the audited item ids `reviewed`; every unaudited row is
      still `provisional`. **This test fails against the pre-Phase-9 code.**
- [ ] **The self-address bug is dead.** With a sender-history fixture built from `SENT` mail addressed
      to the connected account's own address and to an alias, `SenderProfile.ever_replied` is `false`
      for both, and zero threads are held under the reply-history signal for them. **This test fails
      against the pre-Phase-9 code.**
- [ ] No sender matching the no-reply pattern set ever produces a reply-history never-miss signal;
      `no_reply@email.apple.com`, `noreply@…`, `no-reply@…` and `donotreply@…` all match.
- [ ] **The reframe reaches zero.** Over the 365-row remainder fixture replaying the measured live
      distribution (227 / 76 / 46 / 16 / 0), a Phase 9 run leaves `inbox_remaining == 0`, every one of
      the 227 formerly-held threads is `applied` with a `ZeroInbox/*` never-miss label and a non-null
      undo token, and zero are in Trash.
- [ ] **Undo restores the reframe.** Bulk undo over that run returns all 227 threads to the inbox with
      their exact pre-triage label sets.
- [ ] Every `NEVER_ARCHIVE_KEYS` member (`urgent`, `people`, `legal`, `important`) still rejects
      `default_action="archive"` with a validation error, on create and on update.
- [ ] A never-miss archive with no resolvable label does **not** happen: the thread stays in the inbox
      and the ledger reports it under `no_never_miss_label`.
- [ ] Across a whole run, `apply_decision` is never called with `force=True` by any auto-apply or
      re-organisation path, and `Decision.review_state` is never written outside `finalise_review` /
      `upgrade_review_state`.

# Capability: Never-Miss Safeguards

## What It Does
Enforces the product's overriding guarantee — an important email must never be missed — through three
independent mechanisms: a second-pass reviewer hunting only for false negatives, a confidence floor
below which nothing is ever archived, and a reply-history signal that protects anyone the user has
ever replied to.

**All three must be live and tested before any phase performs a real mailbox mutation.**

## Inputs
| Input | Type | Source | Required |
|-------|------|--------|----------|
| Archive proposals | `Decision[]` | cost-tiered triage | Yes |
| Confidence floor | float | `user_settings` | Yes |
| Sender reply history | `ever_replied` per sender | `sender_profiles` | Yes |
| VIP list | entries | `vip_entries` | Yes |
| Priorities profile | text | `priority_profiles` | No |

## Outputs
| Output | Type | Destination |
|--------|------|-------------|
| Flipped decisions | `Decision[]` with `decided_by="reviewer"` | `decisions` |
| Needs-your-call queue | decisions with `status="needs_your_call"` | `decisions` |
| Reviewer reasoning | text appended to the decision | `decisions.reasoning` |
| Possible-missed-important flags | list | proactive surface |

## External Calls
| System | Operation | On Failure |
|--------|-----------|------------|
| LLM provider | second-pass review, batched 20–50 archive proposals | Retry 3×; on persistent failure **every unreviewed archive proposal becomes `needs_your_call`** — the run never proceeds to archive un-reviewed mail |

## Business Rules
- **Mechanism A — second-pass reviewer.** A separate prompt and a separate call see only the archive
  proposals and answer one question: *would the user be upset to miss this?* A flip becomes `keep`
  with `decided_by="reviewer"` and the reviewer's reasoning appended to the original reasoning. The
  reviewer can only flip archive→keep, never keep→archive.
- **Mechanism B — confidence floor.** Any archive proposal with confidence below
  `user_settings.confidence_floor` (default 0.75) becomes `needs_your_call` and stays in the inbox. The
  floor is user-adjustable but cannot be set to 0.
- **Mechanism C — reply-history signal.** A sender with `ever_replied = true` is important: their
  threads are forced to `keep` unless the user has created an explicit override rule naming that
  sender. This is checked after the reviewer, so it cannot be argued away by the model.
- VIP entries (email, domain, keyword) can never be archived automatically, at any confidence.
- `time_sensitive` threads (deadlines, invoices, legal, security alerts) are forced to `keep` unless a
  user-promoted automatic rule explicitly covers them.
- Degradation always errs visible: any error, timeout, or unparseable response results in the mail
  staying in the inbox.
- The order is fixed: reviewer → floor → reply-history/VIP/time-sensitive overrides. Later stages can
  only make an outcome *more* conservative.
- **Phase 7 does not weaken this, and the ordering is the reason.** `align_to_category_default` — the
  only stage in the system permitted to move a decision from `keep` toward `archive` — runs
  **immediately before** the reviewer. Every archive it produces is therefore audited by the reviewer,
  floored, and vetoable by the reply-history and VIP guards, exactly like an archive the classifier
  proposed itself. Moving that stage after the never-miss chain, or performing the same conversion at
  apply time, would archive mail the reviewer never saw and is forbidden. Auto-apply never passes
  `force=True` and never writes `review_state`. See
  [drive-to-inbox-zero](drive-to-inbox-zero.md#g-safety-invariants-this-capability-does-not-weaken-binding-restated-because-it-is-easy-to-erode).
- **Durability never implies finality.** Decisions are persisted incrementally as
  `review_state="provisional"` (see [durable-resumable-runs](durable-resumable-runs.md)); the reviewer
  pass is what upgrades them to `review_state="reviewed"`. A batch the reviewer could not process
  becomes `review_state="review_failed"`. **Only `reviewed` decisions are eligible for apply/approve** —
  `apply_decision()` raises `NotReviewedError` for anything else, before the mutator is called, and
  `force=True` does not bypass it. Incremental persistence therefore cannot let an un-reviewed archive
  become visible-as-final or actionable.

## Success Criteria
- [ ] Over the 220-thread fixture, zero threads from `ever_replied` senders are proposed for archive.
- [ ] Every archive proposal below the configured floor has `status = needs_your_call` and no mutation.
- [ ] The reviewer flips the seeded false-negative bait thread (an important thread disguised as a
      newsletter) from archive to keep, and the flip is visible in the reasoning with
      `decided_by="reviewer"`.
- [ ] A VIP-listed domain is never archived even when the classifier assigns 0.99 confidence to archive.
- [ ] A forced reviewer failure leaves 100% of archive proposals in `needs_your_call` — none applied.
- [ ] Setting the confidence floor to 0 is rejected with a validation error.
- [ ] A run whose reviewer pass is forced to fail applies **zero** Gmail mutations: every decision row
      is `review_state="review_failed"` and `apply_decision()` refuses each one with
      `NotReviewedError` without calling the mutator — including when called with `force=True`.
- [ ] Over the 220-thread fixture **with `align_to_category_default` live**, the reviewer still flips
      the seeded false-negative bait thread back to `keep`, and zero `ever_replied`, VIP or
      `time_sensitive` threads are applied — proving the Phase 7 autonomy stage runs before, not
      around, the never-miss chain.
- [ ] Across a whole run, `apply_decision` is never called with `force=True` by any auto-apply path,
      and `Decision.review_state` is never written outside `finalise_review` / `upgrade_review_state`.

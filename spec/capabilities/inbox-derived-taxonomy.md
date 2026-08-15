# Capability: Inbox-Derived Taxonomy

## What It Does
Discovers the category set that actually fits **this user's mail** — from their real senders, volumes
and patterns — instead of asking a fixed default list to cover an inbox it was never designed for;
lets the user redo that taxonomy whenever they want; and then **re-organises every past decision** to
match the new taxonomy.

## Why (the defect this closes)

The default six categories (Newsletters, Notifications, Receipts, Outreach, People, Urgent) are a
reasonable guess about a generic inbox. They are a poor description of *this* inbox, which is
dominated by Apple, Google, Facebook, PayPal and BookMyShow automated mail. The measured cost of that
mismatch, from the last live run:

- **16 `needs_your_call`** — the classifier found **no category that fits**. That is not a
  low-confidence signal; it is the taxonomy telling you where it has a hole.
- **46 `below_threshold`** — above the floor, under the autonomy bar. A large share of these are
  threads the classifier was forced to squeeze into an ill-fitting category, which is exactly what
  produces middling confidence.

**These two buckets are the feedback signal, and driving both to zero is the measure of a good
taxonomy.** A taxonomy that leaves `needs_your_call > 0` has an unfilled hole by definition.

## Inputs
| Input | Type | Source | Required |
|-------|------|--------|----------|
| Sender census | `{from_email, from_domain, list_id, thread_count, unread_count, ever_replied, is_no_reply}[]` | `items` + `sender_profiles` | Yes |
| Gap set | the run's `needs_your_call` + `below_threshold` decisions with subjects and reasoning | `decisions` | Yes |
| Current taxonomy | `Category[]` | `categories` | Yes |
| Priorities profile | text | `priority_profiles` | No |
| User edits to the proposal | add / rename / merge / delete / set `default_action` | Settings UI | No |

## Outputs
| Output | Type | Destination |
|--------|------|-------------|
| Proposed taxonomy | categories with name, description, `default_action`, rationale, **evidence** (sender + thread counts it covers) | Settings screen, for approval |
| Coverage estimate | `{covered_threads, uncovered_threads, gap_threads_resolved}` | Settings screen |
| Approved taxonomy | `Category[]` + real `ZeroInbox/*` Gmail labels | `categories` + Gmail |
| Re-organisation job | `reorg_jobs` row + per-thread progress + bulk undo token | DB, live feed, Gmail |

## External Calls
| System | Operation | On Failure |
|--------|-----------|------------|
| LLM provider | propose a taxonomy from the census + gap set (one call per ≤200-sender page, chunked) | Retry 3×, then the model-fallback chain. On total failure: return the deterministic-signal proposal alone, clearly marked *"partial — the model was unavailable"*. **Never** silently return the default six as if they were derived |
| Gmail | create/rename labels; `archive_and_label` / `restore_labels` per re-organised thread | Retry 3× inside the mutator; a thread that cannot be mutated is **named** in the job ledger with its reason — never silently skipped |

---

## Business Rules

### A. Discovery is evidence-first, model-second

1. **Census (deterministic, free).** Aggregate every ingested thread by sender, domain and
   `List-Id`. Attach the free signals: `is_no_reply` (see
   [never-miss-safeguards § correspondent truth](never-miss-safeguards.md#correspondent-truth-phase-9)),
   `ever_replied` (post-fix), unread ratio, thread volume, `unsubscribe_url` present, and whether the
   sender appears in the gap set.
2. **`is_no_reply` is a first-class taxonomy signal.** An address that cannot receive a reply is
   structurally not a correspondent. It is a strong, cheap, zero-token prior toward an automated
   category — and it is what separates `no-reply@accounts.google.com` (a machine notice) from a
   colleague, without asking the model anything.
3. **The model names and groups; the census decides what exists.** The LLM is given the census and
   the gap set and asked to produce a category set that covers them, with a one-line plain-English
   `description` (used verbatim in the classifier prompt) and an explicit list of the senders each
   category is meant to absorb. It may not invent a category with no evidence behind it: every
   proposed category must name ≥ 1 real sender or list from the census.
4. **The gap set is a hard requirement of the proposal.** Every `needs_your_call` and
   `below_threshold` thread in the input must be assigned to a proposed category. The proposal states
   how many gap threads it resolves; a proposal resolving fewer than all of them is shown with the
   remainder named, never rounded away.
5. **Discovery never mutates anything.** It produces a proposal. Nothing is created, renamed, deleted
   or archived until the user approves.

### A6. Concentration becomes tier-1 rules — through the EXISTING machinery

**The measured concentration** (do not re-derive): Facebook ~1,586 across five addresses
(`notification@` 503, `notification+kr4knbaqrsga@` 489, `reminders@` 250, `friendsuggestion@` 184,
`notification@priority.` 130, all `facebookmail.com`); BookMyShow ~635
(`no-reply@entertainment.bookmyshow.com` 423, `no-reply@updates.bookmyshow.com` 212); Jagriti Theatre
334 (`contact@jagrititheatre.com`); Apple ~176; PayPal 78; Twitter 40. **All of it currently collapses
into `Notifications`** — which is exactly why confidence lands below the floor and why 16 threads
found no fitting category. A taxonomy derived from this inbox wants a **Social/Facebook** category, an
**Events/Tickets** category (BookMyShow + Jagriti ≈ 970 threads, ~1/5 of the mail) and a
**Billing/Subscriptions** category — each decidable **from the sender alone**.

1. **A handful of senders account for most of the volume, so discovery's job is to convert that
   concentration into deterministic rules.** On approval, every sender / domain / `List-Id` in a
   category's evidence list that clears the bar (**≥ 10 threads, one dominant category**) is
   materialised as a `Rule` row: `kind=deterministic`, `source=mined`, `status=active`, carrying its
   evidence (`thread_count`, sender) so the user can see why it exists and disable it.
2. **These are ordinary tier-1 rules and nothing else.** They are consumed by the **unchanged**
   tier-1 matcher described in [cost-tiered-triage](cost-tiered-triage.md). Discovery writes **no
   classifier of its own**, adds **no graph node**, and calls **no LLM at triage time**. *A second,
   parallel classification path is forbidden* — it would be plumbing that is never wired, and the run's
   own `counts.by_tier` would stop being evidence of anything.
3. **This is why the success measure is reachable rather than aspirational.** The dominant senders
   stop needing judgement at all: they classify deterministically, at zero token cost, at confidence
   **well above the floor**. `needs_your_call` and `below_threshold` are the residue of the model being
   asked to squeeze concentrated automated mail into an ill-fitting generic category; once the mail
   is decided before the model is consulted, both buckets go to zero by construction. **The LLM is
   reserved for the genuine long tail**, which is where its judgement is actually worth paying for.
4. **A mined rule is never a silent sweep.** It cannot archive into a `NEVER_ARCHIVE_KEYS` category
   (the same `_validate_action` guard applies), it is visible and disableable in Settings, and
   re-running discovery **updates it in place** — never duplicating it, and never overwriting a
   `source=user` rule.

### B. Seeded, not fixed

The default six remain the seed for a brand-new account (mail must be triaged before there is
anything to derive from). Discovery runs on demand and after the first run completes, and proposes a
**diff** against the current taxonomy: keep / rename / merge / add / retire — each with its evidence
and its thread count. `Important` (Phase 9) is part of the seed set; see
[taxonomy-management](taxonomy-management.md).

### C. The user can redo the taxonomy at any time

- Rename, re-describe, merge, split, add, delete, and change `default_action` and
  `auto_act_threshold`, from Settings → Taxonomy.
- `NEVER_ARCHIVE_KEYS` (`urgent`, `people`, `legal`, `important`) still reject
  `default_action="archive"` — see
  [never-miss-safeguards](never-miss-safeguards.md#never-archive-keys-reconciled) for why that guard
  survives the Phase 9 reframe intact.
- Deleting a category **never** deletes its Gmail label or any mail.

### D. Re-organise everything — the user's explicit choice

When the taxonomy changes materially, the user is offered **Re-organise my mail**. The user's
decision, recorded: **"Re-organise everything."** So the scope is **every past decision for that
user — ~10,336 rows — including threads that are already archived**, which are relabelled in place.
Not just the current inbox, and not a sample.

1. **Re-classification, then mutation.** Every affected thread is re-classified under the new
   taxonomy (through the *same* graph, the *same* reviewer and the *same*
   `apply_decision()` gate — the re-organiser has no private path to the mutator and never passes
   `force=True`).
2. **Never delete, and never a bare archive.** The only operations are `add_label`, `remove_label`
   and `archive_and_label`. A thread that is in the inbox and whose new category says archive is
   archived with its new label; a thread already archived has its old `ZeroInbox/*` label removed and
   its new one added and **does not return to the inbox**.
3. **Progress is live and per thread**, on the main-page feed: `reorg_progress` events carrying
   `{done, total, phase, current_category}`, at least one event every 3 s (the Phase 7 no-silent-beat
   rule applies unchanged).
4. **Resumable.** The job persists a cursor and per-thread state; killing the process and restarting
   resumes without re-classifying or re-mutating a single already-done thread. This reuses the Phase 6
   durability machinery — decisions land `provisional`, the reviewer upgrades them, and only
   `reviewed` rows are appliable.
5. **Rate limits are respected, not discovered.** Thousands of Gmail calls run under the existing
   process-wide throttle (account ceiling ~490 req/min, default target 350). A `429`/`quotaExceeded`
   backs off and retries; it never fails the job and never drops a thread.
6. **No silent cap, no sample.** The job never truncates, never samples, never "does the first N".
   **Anything not re-organised is named in the ledger with its reason** — one of
   `not_reviewed`, `no_category_fit`, `gmail_error`, `already_correct`, `dry_run`, `cancelled` — with
   the exact count per reason, and the thread ids retrievable. A job that could not finish says so:
   `status="partial"` with the reasons, never `completed`.
7. **Bulk undo reverses the whole re-organisation as ONE operation.** `POST
   /api/reorg/{job_id}/undo` restores every thread's exact pre-job label set from its per-action undo
   token, in reverse order, idempotently — a second call is a no-op, not an error. Per-thread undo
   also still works. A partially-undone job reports exactly how far it got.
8. **`dry_run` is absolute.** Under `dry_run=true` the job re-classifies, produces the full ledger and
   the full preview, and performs **zero** Gmail mutations.
9. **One at a time.** A second re-organisation while one is running returns `409 reorg_in_progress`.
   A re-organisation while a triage run is applying returns `409 run_in_progress` — two writers
   against one mailbox is not a supported state.

### E. Honesty rules inherited unchanged

The [Phase 8 honesty rule](drive-to-inbox-zero.md#e-report-the-remainder-honestly) binds here:
**"we reached zero" must never appear unless the inbox is genuinely empty**, and a job or run that did
not finish its own work sets `error_message`, emits its failure event and returns `ok=false`.

---

## Success Criteria

- [ ] Discovery over a census built from the user's real sender distribution (Apple / Google /
      Facebook / PayPal / BookMyShow dominant) proposes a taxonomy in which **every** sender
      contributing ≥ 10 threads is named in some category's evidence list.
- [ ] Every category in a proposal names ≥ 1 real sender or `List-Id` from the census; a proposal
      containing an evidence-free category is rejected by validation, not shown to the user.
- [ ] **The gap buckets go to zero.** Re-triaging the 365-row remainder fixture under the discovered
      taxonomy yields `needs_your_call == 0` and `below_threshold == 0`, against **16** and **46**
      under the default taxonomy on the same fixture. (The fixture is the full 365 rows, not a
      sample: a sampled answer and a full answer differ.)
- [ ] **Concentration is exploited through tier 1, not through a parallel path.** Approving a
      proposal built on the measured concentration fixture mints a mined `Rule` for every sender with
      ≥ 10 threads; each one is matched by the existing tier-1 matcher called directly (the seam is
      tested, not assumed); the subsequent run resolves those threads `decided_by="rule"` with
      confidence above the floor and sends **zero** of them to the LLM. Reaching the same categories
      via `decided_by="llm"` fails this criterion.
- [ ] Re-running discovery twice produces zero duplicate mined rules and leaves every `source=user`
      rule byte-identical.
- [ ] A mined rule proposing `archive` into `urgent`/`people`/`legal`/`important` is rejected at
      materialisation, not written and later filtered.
- [ ] `is_no_reply` is computed for every census row and is correct for `no_reply@`, `noreply@`,
      `no-reply@`, `donotreply@` and `do-not-reply@`, and false for a normal address.
- [ ] With the LLM forced to fail, discovery returns the deterministic proposal marked *partial* and
      **never** returns the default six presented as derived.
- [ ] A re-organisation over a **10,000-decision** fixture (large enough that a sampled run and a full
      run give observably different counts) processes every row: `done + sum(skipped_by_reason) ==
      10000`, with **zero** unaccounted rows.
- [ ] Every already-archived thread that changes category ends with the new `ZeroInbox/*` label, the
      old one removed, and **still out of the inbox** (`INBOX` not re-added).
- [ ] Every re-organisation mutation writes an `ActionLog` row with a non-null `undo_token`, and every
      `operation` is in `{archive, add_label, remove_label}` — zero destructive operations.
- [ ] **Bulk undo is one operation:** `POST /api/reorg/{job_id}/undo` restores all mutated threads to
      their exact pre-job label sets; a second call performs zero Gmail calls and returns the same
      counts.
- [ ] Killing the job at ~40 % and resuming completes it with zero duplicated mutations and strictly
      fewer LLM calls on the resume than a from-zero run.
- [ ] Under `dry_run=true` the job performs zero Gmail mutations and still produces the complete
      ledger.
- [ ] A thread the reviewer never passed is **not** mutated by the re-organiser: it is counted under
      `not_reviewed` in the ledger and `apply_decision` raised `NotReviewedError` before the mutator.
- [ ] A second concurrent re-organisation returns `409 reorg_in_progress`.

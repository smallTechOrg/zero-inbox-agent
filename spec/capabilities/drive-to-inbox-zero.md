# Capability: Drive to Inbox Zero

## What It Does
Actually reduces the inbox — applying every archive the agent is confident enough to make on its own —
and then reports the exact remainder, by reason, so the user can see how far from zero they are and
why the rest is still there.

## Why (the defect this closes)

The agent classifies beautifully and then stops. Run `fbeed060` (2,176 threads, `dry_run=0`, status
`completed`) produced 1,348 `proposed/keep`, 615 `proposed/archive` and 213 `needs_your_call/keep`,
and **zero decisions reached `applied`**. The user was handed a 56-cluster to-do list. That is a
triage report, not inbox zero.

Four independent causes, all measured against the real database and the code at HEAD:

1. **The autonomy control does not exist.** `user_settings.auto_act_threshold` (default `0.95`) is
   persisted (`src/db/models.py:86`), defaulted (`src/api/session.py:143`), loaded into
   `state["settings"]` (`src/graph/persistence.py:139`, `:153`) and rendered as a slider
   (`frontend/src/components/Settings.tsx:345`) — and **never read by any decision or apply code
   path**. The only enforced gate is `confidence_floor` (0.75) via `apply_confidence_floor`
   (`src/graph/nodes.py:464`). `_auto_apply_decisions` (`src/graph/nodes.py:1347`) does not consult
   confidence at all. The UI promises a control that does nothing.
2. **It would archive nothing even if it were wired.** The 615 archive proposals scored: **0** at
   `>= 0.95`, 22 at `0.90–0.94`, 522 at `0.80–0.89`, 71 at `0.75–0.79`. The model's real output tops
   out around 0.94, so a 0.95 gate is above its entire achievable range.
3. **Auto-apply aborted silently.** All 615 rows are still `proposed` — the function never reached its
   per-decision loop. Only two code paths do that: `_build_mutator_for_user` raising (logs
   `triage.auto_apply_build_mutator_failed`, then bare `return`) or the outer `except` (logs
   `triage.auto_apply_outer_failed`, then bare `return`). Either way the run reported itself
   `completed` and looked like a success.
4. **Keeps are structurally un-archivable.** 1,348 threads were decided `keep`. If a confident keep
   stays in the inbox, the inbox cannot reach zero by definition — no apply fix alone can close this.

Phase 6 added a fifth, structural blocker that did not exist during that run:
`tools.actions.apply_decision` raises `NotReviewedError` unless `review_state == "reviewed"`, checked
first, before the mutator, not bypassable by `force=True` (`src/tools/actions.py:126-133`).
`_auto_apply_decisions` sets `status="approved"` but never touches `review_state`, so on today's code
auto-apply additionally fails for every row the reviewer did not upgrade. **Auto-apply must go
THROUGH that gate, never around it.**

---

## The Inbox-Zero Definition (written down, and stated in the UI)

> **Inbox zero means the inbox holds only what needs a human.**

After a completed run the Gmail inbox contains exactly these five buckets, and nothing else. Each
thread carries exactly one `decisions.autonomy_state` naming which bucket it is in — assigned by
`mark_autonomy_state` under the fixed precedence documented in [agent.md](../agent.md#nodes), so the
value always names the **first** rule that stopped the agent acting:

| Bucket | `autonomy_state` | Why it is still in the inbox |
|--------|------------------|------------------------------|
| Category says keep | `category_keep` | Its category's `default_action` is `keep` — by default People, Urgent and any user-added keep category (e.g. Legal). **Receipts is `archive` — see Rule B.** |
| Held by never-miss | `held_by_never_miss` | Reviewer flip, VIP entry, ever-replied sender, or `time_sensitive` |
| Not confident enough | `below_threshold` | An archive-category thread above the confidence floor but below its category's autonomy threshold |
| Needs your call | `needs_your_call` | Below the confidence floor, or the tier could not decide it (`decided_by="error"`) |
| A user rule said keep | `category_keep` (rule-pinned, see Rule C4) | A `decided_by="rule"` keep — the user's own instruction outranks every category default |

Everything else leaves the inbox: archived (never deleted, never trashed), labelled with its category,
and fully undoable.

**Distance to zero** is the count of decisions in a run that the agent itself decided should leave the
inbox and that are **still in the inbox**:

```
distance_to_zero = count(decisions WHERE run_id = :id
                                     AND autonomy_state = 'auto_act'
                                     AND status != 'applied')
```

On a healthy completed run this is **0**. On run `fbeed060` it would have been 615 — the number that
makes cause 3 impossible to miss. It is not a soft metric; it is the assertion the gate makes.

### Resolving the `keep` question (why keeps are converted, not overridden)

A category's `default_action` — not a blanket rule and not a per-decision override — decides whether a
confident thread stays visible. Newsletters/Notifications/Outreach/Receipts leave; People/Urgent (and
any user-added keep category such as Legal) stay.

The honest place to apply that is **at decision time, before the reviewer**, not at apply time. If a
Newsletters thread at 0.88 confidence belongs out of the inbox, the decision must **be** `archive` —
so the second-pass reviewer audits it, the confidence floor binds on it, the VIP and reply-history
guards can still veto it, and the user sees `archive` in the history view. The alternative — leaving
it `proposed/keep` and having auto-apply force-archive it — would mean archiving mail the reviewer was
never shown, behind the user's back, through the `force` door built for a deliberate user-initiated
sweep. That is explicitly forbidden (Rule D2).

So: **`align_to_category_default` is the single stage in the whole system permitted to move a decision
from `keep` toward `archive`, and it runs before every never-miss safeguard.** Every stage after it can
only be more conservative.

---

## Inputs
| Input | Type | Source | Required |
|-------|------|--------|----------|
| Run decisions | `Decision[]` (in-memory) | tiers 1–4 of [cost-tiered-triage](cost-tiered-triage.md) | Yes |
| Taxonomy with `default_action` + `auto_act_threshold` | `Category[]` | `categories` via `load_context` | Yes |
| Global autonomy threshold | float | `user_settings.auto_act_threshold` | Yes |
| Confidence floor | float | `user_settings.confidence_floor` | Yes |
| Sender reply history / VIP list / priorities profile | evidence | `sender_profiles`, `vip_entries`, `priority_profiles` | Yes |
| Review state per decision | `provisional` \| `reviewed` \| `review_failed` | `decisions.review_state` | Yes |
| `dry_run` | bool | `user_settings.dry_run` | Yes |
| Existing run id (retry-apply) | UUID string | `triage_runs.id` | On retry |

## Outputs
| Output | Type | Destination |
|--------|------|-------------|
| Realigned decisions (`keep` → `archive` where the category says so) | `Decision[]` | `decisions.proposed_action` |
| Per-decision autonomy reason | enum | `decisions.autonomy_state` |
| Applied archives | Gmail mutations + `ActionLog` rows with undo tokens | Gmail, `action_logs` |
| Apply ledger | JSON | `triage_runs.counts["apply"]`, `GET /api/runs/{id}/remainder` |
| Remainder ledger | JSON | `triage_runs.counts["remainder"]`, `GET /api/runs/{id}/remainder` |
| `distance_to_zero` | int | `GET /api/runs/{id}`, `GET /api/runs/{id}/summary`, `GET /api/runs/{id}/remainder` |
| `apply_progress` / `inbox_zero_report` / `run_apply_failed` SSE events | JSON | SSE bus → main-page live feed + Inbox-Zero card (primary), Activity drawer history |
| Human-readable apply failure | string | `triage_runs.error_message` |

## External Calls
| System | Operation | On Failure |
|--------|-----------|------------|
| Gmail | build a credentialed mutator for the run's connection | **Loud, never silent.** The ledger records `apply_failed_reason`, `triage_runs.error_message` is set to a human-readable string, a `run_apply_failed` SSE event fires, and `distance_to_zero` stays equal to the number of `auto_act` decisions. The run is never presented as a clean success. |
| Gmail | `threads.modify` — archive + add category label, one thread at a time | Per-decision. That decision stays `approved`, is counted in `ledger.failed` with its error, and is retryable via `POST /api/runs/{run_id}/apply`. One failure never blocks the rest. |
| Database | write `autonomy_state`, ledgers and counts | Logged at WARNING. `autonomy_state` left NULL is read as "unknown" by the remainder query and reported in an explicit `unclassified` bucket — never silently folded into a healthy bucket. |
| LLM provider | none new | — |

---

## Business Rules

### A. The autonomy instrument is real, and it is per category

- **A1.** `user_settings.auto_act_threshold` becomes an **enforced** value: the global confidence bar
  at or above which the agent acts on its own. Its default changes from `0.95` to **`0.80`** (Rule B).
- **A2.** `categories.auto_act_threshold` (new, nullable float) overrides the global for that category.
  `NULL` means "inherit the global", which is what keeps the global slider load-bearing.
- **A3.** The effective bar for a decision is:
  ```
  effective_threshold(category, settings) =
      max(category.auto_act_threshold ?? settings.auto_act_threshold,
          settings.confidence_floor)
  ```
  The never-miss floor is a hard lower bound: the autonomy threshold can raise the bar, never lower it.
- **A4.** A category whose `default_action` is `keep` is **never auto-acted on**, at any confidence.
  Its threshold is inert. `Urgent` can never carry `default_action = archive`
  (already enforced at `src/tools/taxonomy.py:80`) — so Urgent is structurally un-archivable, forever.
- **A5.** Validation: `0 < auto_act_threshold <= 1`. `PATCH /api/settings` and
  `PATCH /api/categories/{id}` reject anything else with `validation_error`. A value **above 0.90**
  is accepted but the API response carries `warning: "above_model_ceiling"` and the UI must show it
  (Rule E3) — the measured model ceiling is ~0.94, so anything above 0.90 acts on almost nothing.

### B. Calibration — why 0.80, justified against the measured distribution

The 615 archive proposals from run `fbeed060`:

| Confidence band | Count | Cumulative at-or-above |
|-----------------|-------|------------------------|
| `>= 0.95` | **0** | 0 |
| `0.90 – 0.94` | 22 | 22 |
| `0.80 – 0.89` | 522 | 544 |
| `0.75 – 0.79` | 71 | 615 |

| Candidate threshold | Archives the agent would apply | Verdict |
|---------------------|-------------------------------|---------|
| `0.95` (today's persisted value) | **0** (0%) | Above the model's entire achievable range. This is why the inbox never moved. |
| `0.90` | 22 (3.6%) | Still a triage report. |
| **`0.80`** | **544 (88.5%)** | **Chosen.** The elbow of the distribution. Leaves a visible 71-thread conservative band between the floor and the bar. |
| `0.75` | 615 (100%) | Identical to `confidence_floor` — the threshold would add nothing and the second instrument would be redundant. |

> **Assumed:** the band structure measured on run `fbeed060` (2,176 threads, one real mailbox) is
> representative of this model on this user's mail. It is the only real distribution that exists; the
> value is a setting, and the gate re-asserts the band arithmetic against a fixture that replays it, so
> a model change that shifts the distribution fails the gate loudly rather than silently archiving more.

**Seeded per-category thresholds.** The thresholds are seeded by `src/db/seed.py`; the seeded
`default_action` values live in `DEFAULT_TAXONOMY` in **`src/tools/rules.py`**.

| Category | `default_action` | seeded `auto_act_threshold` | Why |
|----------|------------------|-----------------------------|-----|
| Newsletters | `archive` | `NULL` → global `0.80` | Bulk, opted-in, undoable. This is the 522-strong `0.80–0.89` band. |
| Notifications | `archive` | `NULL` → global `0.80` | Automated and reproducible at source. |
| Outreach | `archive` | **`0.85`** | See "Why Outreach sits at 0.85" below. |
| Receipts | **`archive`** (changed this phase, was `keep`) | **`0.85`** — **live, not inert** | See "Why Receipts archives" below. Receipts archives only at `>= 0.85` — a notch more conservative than the 0.80 global. |
| People | `keep` | `NULL` | Inert — never auto-acted (A4). |
| Urgent | `keep` | `NULL` | Inert — and `archive` is forbidden for this key (A4). |
| Legal (user-added, if present) | `keep` | `NULL` | Inert — never auto-acted (A4). |

#### Decision — Receipts is `archive` (was an open question; now decided)

Measured by category on run `fbeed060`: Notifications 703, Newsletters 456, People 354,
**Receipts 283**, Outreach 122, Urgent 68, Legal 10. With Receipts kept, the **floor** of the inbox is
~715 threads (Receipts + People + Urgent + Legal). The product would hand the user an "inbox zero"
containing ~700 threads — **it could not reach its own stated definition of inbox zero**. That is the
original broken promise in a new outfit, so the default changes.

- **Receipts are archival records, not work.** You *search* for an invoice when you need it; you do not
  action it from the inbox.
- **Nothing is lost.** The thread is archived and labelled `ZeroInbox/Receipts` — fully searchable and
  one-click undoable. It is moved out of the way, **not deleted**. No trash, no delete, no spam, ever
  (unchanged).
- **The safety net still binds, and it is the right instrument for the exception.** A genuinely
  time-sensitive receipt (invoice due, payment failed) is flagged `time_sensitive` and held by
  `held_by_never_miss`, so it **stays visible regardless of the category default**. That is exactly what
  the never-miss layer exists for — which is why the category default does not need to do that job too,
  and why this flip is safe.
- **It matches the user's revealed preference:** offered precisely this tradeoff earlier, they chose
  "archive everything except needs_your_call" and were satisfied.

**People, Urgent and Legal remain `keep`** — those are the buckets where a human genuinely needs to look.

Every safety invariant still binds on Receipts, unchanged: `confidence_floor`, VIP entries,
reply-history (ever-replied senders), the Phase 6 review gate (`NotReviewedError`, **not** bypassable by
`force=True`), no-trash-ever, undo tokens on every mutation, and absolute `dry_run`.

#### Why Outreach sits at 0.85 (a judgement call, not a measurement)

`outreach = 0.85` is **deliberate and recorded, not derived from the band table**. A real business
inquiry wrongly archived costs far more than a promotional email wrongly kept; that asymmetry justifies
the extra margin above the 0.80 global. **Do not "optimise" it down to 0.80** — the gap is the point,
not an oversight.

### C. Category default decides the action — `align_to_category_default`

A single node, `graph.nodes_autonomy.align_to_category_default`, running **after `cluster_decisions`
and before `second_pass_reviewer`**. It is the only stage in the system that may move a decision from
`keep` toward `archive`.

- **C1.** For each decision, when **all** of the following hold, `proposed_action` is set to the
  category's `default_action` (`archive` or `digest`) and the reasoning gains one sentence naming the
  category and the threshold:
  - the decision's category exists and its `default_action` is `archive` or `digest`;
  - `confidence >= effective_threshold(category, settings)`;
  - `status != "needs_your_call"`;
  - `decided_by != "error"`;
  - `time_sensitive` is false;
  - `unsure` is false (tier 3 already caps unsure at `UNSURE_MAX_CONFIDENCE = 0.5`, well under any
    threshold, but the exclusion is explicit rather than incidental);
  - the sender's `ever_replied` is false and the sender is not on the VIP list (defense in depth — the
    reply-history and VIP guards run later and would veto it anyway).
- **C2.** It never changes an `archive` to a `keep`, never changes `confidence`, never changes
  `decided_by`, and never touches `status` or `review_state`.
- **C3.** A decision it converts is `decided_by`-preserved and flows through the **entire** never-miss
  chain unchanged: `second_pass_reviewer` audits it as an archive proposal, `apply_confidence_floor`
  binds on it, `apply_reply_history_guard` and `apply_vip_guard` can veto it.
- **C4.** **A `decided_by = "rule"` decision is never realigned.** A deterministic rule is the user's
  own instruction and outranks every category default, in both directions.
- **C5.** Because `cluster_decisions` runs before this node, the node refreshes each cluster's
  `suggested_action` in place from its members' final actions (majority action, ties → the more
  conservative). No stale cluster action is ever persisted.

### D. Apply — through the review gate, never around it, never silent

`_auto_apply_decisions` becomes a thin wrapper over
`graph.nodes.apply_run_decisions(...)`, which is also callable directly (retry-apply, Rule F).

- **D1.** A decision is applied only when **all** hold: `review_state == "reviewed"`,
  `status not in ("needs_your_call", "applied", "undone", "rejected")`,
  `proposed_action in ("archive", "digest")`, and `autonomy_state == "auto_act"`.
- **D2.** **`force=True` is never passed.** Auto-apply must not force-archive a `keep`. The keep
  question is resolved at decision time (Rule C); the `force` path in `src/tools/actions.py` stays
  exactly what it is — a deliberate, informed, user-initiated sweep.
- **D3.** **`review_state` is never written by the apply path.** Only the never-miss chain
  (`graph.persistence.finalise_review` / `upgrade_review_state`) may set it. A row that is
  `provisional` or `review_failed` is counted in `ledger.not_reviewed` and left alone.
- **D4.** `dry_run` is absolute. With `dry_run` on, `apply_run_decisions` performs zero mutations,
  returns `{"dry_run": true, ...}` with `applied = 0`, and `distance_to_zero` is reported as-is
  without an apply-failure banner.
- **D5.** No trash, no delete, no spam. Every mutation writes an `ActionLog` row with a non-null
  `undo_token`, before the decision flips to `applied` (unchanged from
  [gmail-actions-and-undo](gmail-actions-and-undo.md)).
- **D6.** **No silent early return, ever.** `apply_run_decisions` always returns a ledger.
  - A failure building the mutator sets `ledger.apply_failed_reason` to the exception's class and
    message and returns immediately with `applied = 0` — it does not swallow it.
  - The outer `except` sets `ledger.apply_failed_reason` the same way.
  - `finalize` writes the ledger to `triage_runs.counts["apply"]`; when `apply_failed_reason` is set
    **or** `distance_to_zero > 0`, it also sets `triage_runs.error_message` to a human-readable
    sentence and emits a `run_apply_failed` SSE event.
  - `GET /api/runs/{run_id}` gains `apply_ok: bool` and `distance_to_zero: int`, so no surface can
    render a run that archived nothing as a clean success.
- **D7.** Per-decision failures are isolated: the decision stays `approved`, the error is recorded in
  `ledger.failures[]`, and the loop continues. The decision remains retryable.
- **D8.** An `apply_progress` SSE event is emitted every 25 applied decisions (and once at the end)
  carrying `{run_id, applied, total_to_apply, failed}`, so a long apply pass is visible rather than a
  frozen progress bar. The existing `auto_apply_complete` event and the
  `triage.auto_apply_complete` structured log line are **kept**, extended with the full ledger — no
  existing subscriber breaks.

### E. Report the remainder honestly

- **E1.** `graph.remainder.remainder_ledger(session, *, run_id, user_id) -> dict` is the single source
  of truth, queried live from `decisions` (never from cached counts):
  ```json
  {
    "run_id": "…",
    "inbox_remaining": 1632,
    "distance_to_zero": 0,
    "applied": 544,
    "apply_ok": true,
    "apply_failed_reason": null,
    "remainder": {
      "needs_your_call": 213,
      "category_keep": 1314,
      "held_by_never_miss": 34,
      "below_threshold": 71,
      "unclassified": 0
    },
    "failures": [{"decision_id": "…", "error": "…"}]
  }
  ```
  (The numbers are run `fbeed060`'s 2,176 threads projected under the Phase 7 policy:
  `applied + inbox_remaining = 2,176`.) `inbox_remaining` is the sum of the `remainder` buckets plus
  `distance_to_zero`. `unclassified` counts rows whose `autonomy_state` is NULL — always reported,
  never folded into a healthy bucket.
- **E2.** The human-readable line the UI renders is built from the ledger and names every bucket:
  *"827 archived · 213 need your call · 1,031 kept by category (People, Urgent, Legal) · 71 not
  confident enough · 34 held by VIP / reply history."* (Illustrative shape, not a measured projection:
  the 283 Receipts threads now enter the archive path and split across `archived` / `below_threshold` /
  `held_by_never_miss` according to their own confidences and the 0.85 Receipts bar. The category names
  are read from the live taxonomy, never hardcoded.)
- **E3.** The Settings slider is relabelled and made honest (see
  [ui.md screen 17](../ui.md#17-honest-autonomy-controls-phase-7--settings)):
  it states what it controls, shows the resolved per-category bars, and warns above 0.90 that the
  agent will act on almost nothing. **A no-op control is not shipped.**
- **E4.** `triage.inbox_zero_report` is emitted as a structured log line at the end of every run with
  the full ledger, and as an `inbox_zero_report` SSE event.

### F. Converge — the tail is finished, not abandoned

- **F1.** `graph.persistence.already_decided_item_ids(session, run_id)` **excludes** rows with
  `decided_by = "error"` or `review_state = "review_failed"`. Those threads were never really decided;
  a resume must pick them up. (Run `fbeed060` left 179 such rows.)
- **F2.** `graph.persistence.insert_provisional_decisions` currently skips any existing
  `(run_id, item_id)`. It must **overwrite in place** when the existing row's `decided_by == "error"`
  — otherwise F1 re-classifies the tail and then throws the answer away. Every other existing row is
  still skipped, and `items_decided` is never double-counted.
- **F3.** `POST /api/runs/{run_id}/apply` re-runs `apply_run_decisions` for a `completed` run without
  re-classifying anything. It is idempotent — already-`applied` decisions are counted in
  `ledger.already_applied` and never mutated twice. `409 not_appliable` when the run's status is not
  `completed`. This is how a transient Gmail failure is recovered without spending a token.
- **F4.** A resumed run finalises, reviews and applies exactly as a fresh run does. `cost_usd` stays
  additive.

### G. Safety invariants this capability does not weaken (binding, restated because it is easy to erode)

1. **Never-miss binds.** The second-pass reviewer, `confidence_floor`, the reply-history guard and the
   VIP guard all still run, in that fixed order, after `align_to_category_default`. Each can only make
   an outcome more conservative.
2. **The Phase 6 review gate binds.** `apply_decision` raises `NotReviewedError` for
   `review_state != "reviewed"`, checked before the mutator, not bypassable by `force=True`. Auto-apply
   goes through the same gate. **No side door is added, now or ever.**
3. **Keeps are not force-archived.** `force=True` is only ever set by the explicit user-initiated sweep
   in `src/tools/actions.py`. Auto-apply never sets it.
4. **No trash, no delete, no spam-report.** Archive and label only. Every mutation carries an undo token.
5. **`dry_run` is absolute.** Nothing mutates while it is on.
6. **Urgent is never archivable.** `default_action = archive` is rejected for the `urgent` key.
7. **No body text is persisted or emitted.** The new events and ledgers carry counts, ids, categories
   and reasons only (`tests/integration/test_no_body_persisted.py` still passes).

### H. Phase 9 — the target is actual zero, with no human in the loop

Phase 7 made the remainder **honest**. Phase 7's remainder was still **365**, and 227 of it was
structurally unclearable: a never-miss verdict could only be expressed as *stay in the inbox*.

- **`held_by_never_miss` is no longer a floor.** From Phase 9 a never-miss verdict is expressed as
  **archive into the never-miss category's own label**, so it leaves the inbox while keeping — and
  improving — its findability. The full reasoning, the measured evidence and the reconciliation with
  `NEVER_ARCHIVE_KEYS` live in
  [never-miss-safeguards](never-miss-safeguards.md#phase-9-the-never-miss-semantic-is-redefined--from-hold-to-label).
  This capability only consumes the outcome.
- **The other three buckets are closed by the taxonomy, not by archiving harder.** `category_keep`
  (76), `below_threshold` (46) and `needs_your_call` (16) are what a taxonomy that does not fit this
  inbox looks like. [inbox-derived-taxonomy](inbox-derived-taxonomy.md) derives a fitting one and
  re-organises past decisions to match; the measure of success is those buckets reaching **zero**.
  **The confidence floor is not lowered and `needs_your_call` is still never archived.**
- **The target is `inbox_remaining == 0`, not `distance_to_zero == 0`.** `distance_to_zero == 0` only
  says the agent did what it decided to do. A run is complete for Phase 9 when the inbox is genuinely
  empty **and no human touched it**.
- **Two new remainder buckets, both honest:** `no_never_miss_label` (a never-miss verdict with no
  resolvable label — the thread **stayed in the inbox** rather than being archived unlabelled) and
  `unreviewed_applied` (the historic count of rows applied before the review-gate fix). Rule E's
  arithmetic is unchanged: `inbox_remaining == sum(remainder buckets) + distance_to_zero`.
- **Rule E is not relaxed.** *"We reached zero"* must never appear unless the inbox is genuinely
  empty, and anything that could not be archived must be **named with its reason**. A phase whose
  whole goal is zero is exactly the phase most tempted to print it early.

Item 6 of Rule G above is superseded by the extended form: **`urgent`, `people`, `legal` and
`important` all reject `default_action = archive`** — kept, extended, and reconciled with the reframe
rather than deleted. Items 1–5 and 7 are unchanged and binding.

---

## Success Criteria

- [ ] **(Phase 9)** Over the 365-row remainder fixture replaying the measured live distribution
      (227 / 76 / 46 / 16 / 0) a run ends `inbox_remaining == 0` **and** `distance_to_zero == 0` with
      no human intervention, and every formerly-held thread is archived **with** a `ZeroInbox/*` label
      and a non-null undo token.
- [ ] **(Phase 9)** With the mutator patched to raise, the ledger payload does **not** contain
      "we reached zero", reports `apply_ok=false` and names the reason.
- [ ] `effective_threshold` returns `0.80` for a Newsletters category with a NULL threshold and default
      settings, `0.85` for Outreach, and never returns a value below `settings.confidence_floor`.
- [ ] Replaying the measured `fbeed060` archive distribution (0 / 22 / 522 / 71 across the
      `>=0.95` / `0.90–0.94` / `0.80–0.89` / `0.75–0.79` bands, 615 rows) through the policy yields
      exactly **544** `autonomy_state = "auto_act"` and **71** `below_threshold`. The same fixture at
      threshold `0.95` yields **0** `auto_act` — the current production behaviour, reproduced.
- [ ] A Newsletters thread the LLM proposed `keep` at confidence 0.88 leaves
      `align_to_category_default` as `proposed_action = "archive"`; the same thread at 0.79 stays
      `keep` with `autonomy_state = "below_threshold"`.
- [ ] A People thread at 0.99 and an Urgent thread at 0.99 both stay `keep` — `default_action = keep`
      is never overridden at any confidence.
- [ ] A `decided_by = "rule"` keep is never realigned, at any confidence, for any category.
- [ ] A `decided_by = "error"` row (confidence 0.0, category NULL) is never realigned and ends
      `autonomy_state = "needs_your_call"`.
- [ ] An archive decision at confidence 0.83 reaches `status = "applied"` end-to-end through the real
      review gate: `review_state` is `reviewed` when it is applied, `apply_decision` is called with
      `force=False`, an `ActionLog` row with a non-null `undo_token` exists, and the thread is out of
      the Gmail inbox with its category label attached.
- [ ] Across a whole run, `apply_decision` is **never** called with `force=True` and
      `Decision.review_state` is never written by any code path under `apply_run_decisions`.
- [ ] A run whose reviewer pass is forced to fail applies **zero** mutations; every such row is counted
      in `ledger.not_reviewed`, and `distance_to_zero` reflects them.
- [ ] **silent-abort regression — mutator path:** with `_build_mutator_for_user` patched to raise, the run
      still completes classification but `ledger.apply_failed_reason` names the exception,
      `triage_runs.error_message` is a non-null human-readable sentence, a `run_apply_failed` event is
      emitted, `GET /api/runs/{id}` returns `apply_ok = false` and
      `distance_to_zero == count(autonomy_state='auto_act')`. No log-only, no bare `return`.
- [ ] **silent-abort regression — outer path:** with the DB session factory patched to raise inside
      `apply_run_decisions`, the same assertions hold. A silent abort is impossible from either path.
- [ ] **silent-abort cause proof:** `_build_mutator_for_user` succeeds against the real connected account
      from `.env` (read-only: credentials + `labels().list()`, no mutation, no write), or fails with a
      named error that is recorded as the proven root cause in the commit message and an inline code
      comment. The implementer proves the cause; they do not assume it.
- [ ] On a healthy completed run over the 220-thread fixture, `distance_to_zero == 0` and
      `ledger.applied == count(autonomy_state='auto_act')`.
- [ ] `inbox_remaining == sum(remainder buckets) + distance_to_zero`, and `remainder.unclassified == 0`.
- [ ] With `dry_run = true`, a full run performs zero Gmail mutations, `ledger.dry_run` is true,
      `ledger.applied == 0`, and no `run_apply_failed` event is emitted.
- [ ] Seeding 20 `decided_by = "error"` rows into an interrupted run and resuming it re-classifies
      exactly those 20 (none ends `decided_by = "error"`), produces zero duplicate `(run_id, item_id)`
      pairs, and does not re-classify any already-`reviewed` thread.
- [ ] `POST /api/runs/{run_id}/apply` on a run whose decisions are all `applied` returns
      `already_applied == n`, `applied == 0`, performs zero Gmail calls, and is safe to call twice.
- [ ] Migration `0006_autonomy_policy`: a `user_settings` row at `0.95` becomes `0.80`; a row at `0.75`
      is unchanged; new rows default to `0.80`; `categories.auto_act_threshold` is `0.85` for
      `outreach` and `receipts` and NULL for the rest; a `receipts` category still at the seeded
      `default_action = 'keep'` becomes `'archive'` while one the user already changed is left alone;
      every pre-existing `decisions` row has
      `autonomy_state IS NULL` and is reported under `unclassified`, never miscounted.
- [ ] `PATCH /api/settings` with `auto_act_threshold = 0` is rejected with `validation_error`; with
      `0.93` it is accepted and the response carries `warning: "above_model_ceiling"`.
- [ ] `PATCH /api/categories/{id}` setting `urgent.default_action = "archive"` is still rejected.
- [ ] No `ActionLog.operation` outside `{archive, add_label, remove_label}` is ever written, and every
      row has a non-null `undo_token`.
- [ ] `tests/integration/test_no_body_persisted.py` still passes unchanged.

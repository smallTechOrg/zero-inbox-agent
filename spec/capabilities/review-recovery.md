# Capability: Review Recovery

## What It Does
Lets a user finish a completed run whose decisions never got past the never-miss reviewer, by
re-running the **real** reviewer over them and then applying whatever it passes — instead of throwing
away the run and re-classifying the whole mailbox.

## The gap it closes
The reviewer is allowed to fail (a provider outage did exactly that on a real 2,122-thread run). When
it does, those decisions stay `provisional` / `review_failed`, and `apply_decision()` correctly refuses
them — `POST /api/runs/{id}/apply` cannot clear the state, because the gate is doing its job. Today the
only recovery is a whole new run. The gate is right; the missing piece is a way to re-enter it.

## Inputs
| Input | Type | Source | Required |
|-------|------|--------|----------|
| `run_id` | uuid | path | yes |
| `user_id` | uuid | session | yes |
| non-`reviewed` decisions | rows where `review_state IN ('provisional','review_failed')` for the run | DB | yes |

## Outputs
| Output | Type | Destination |
|--------|------|-------------|
| upgraded `decisions.review_state` | DB | `reviewed` or `review_failed`, written **only** by the reviewer node |
| Gmail archives for newly-reviewed archive decisions | mutation + `ActionLog` with undo token | Gmail |
| `retry_review_started` / `retry_review_finished` events | SSE | live feed (screen 18) |
| refreshed remainder ledger | `GET /api/runs/{id}/remainder` | Inbox-Zero card bar (screen 24) |

## External Calls
| System | Operation | On Failure |
|--------|-----------|------------|
| LLM provider | the existing reviewer prompt, via the existing client, chain and throttle | rows stay `review_failed`, the run reports the reason, the amber bar returns — **never** an upgrade to `reviewed` |
| Gmail | `archive_and_label` via the unchanged `apply_decision()` | the existing loud apply-failure path (`apply_ok=false`, `run_apply_failed`, red bar) |

## Business Rules
- It calls the **same reviewer node** and the **same `apply_decision()`** as a normal run. It is a
  re-entry point into the existing path, never a parallel path.
- It **never** writes `review_state` outside the reviewer, **never** passes `force=True`, and
  **never** archives a `keep`-proposed decision. A thread the reviewer flips to keep stays kept.
- The confidence floor, VIP, reply-history and time-sensitive guards bind exactly as they do in a
  normal run; a retry cannot archive something the first pass would have held.
- `409 not_retryable` unless the run is `completed` and at least one non-`reviewed` decision exists.
- Idempotent: a second call while one is in flight is a no-op; a call with nothing left to review is
  a no-op returning zero counts.
- `dry_run` remains absolute — a retry under `dry_run` reviews but mutates nothing.
- Scoped to the session user; another user's `run_id` is `404`.

## Success Criteria
- [ ] A completed run seeded with 30 `review_failed` decisions and a working reviewer ends with those
      rows `reviewed`, the archive-eligible ones `applied` with non-null `undo_token`s, and
      `remainder.not_reviewed` reduced by exactly the number the reviewer passed.
- [ ] With the reviewer stubbed to fail, `retry-review` leaves every row `review_failed`, performs
      **zero** Gmail mutations, and the response and ledger name the failure.
- [ ] Across the whole retry path, `apply_decision` is never called with `force=True` and
      `Decision.review_state` is never written by any code path other than the reviewer node
      (asserted by spying on the call and on the ORM attribute).
- [ ] A `keep`-proposed decision in the retried set is never mutated.
- [ ] A seeded reply-history sender and a seeded VIP sender in the retried set are still held.
- [ ] `retry-review` on a `running` run returns `409 not_retryable`; on another user's run, `404`.
- [ ] Called twice in a row, the second call performs zero LLM calls and zero Gmail calls.
- [ ] Under `dry_run=true` the retry reviews the rows and performs zero Gmail mutations.

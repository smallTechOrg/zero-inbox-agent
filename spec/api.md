# API

FastAPI on **port 8001**. Every JSON route returns the envelope `{"data": ..., "error": null}` via
`ok()`, or raises `api_error(code, message, status)` producing `{"data": null, "error": {code, message}}`.

All `/api/*` routes require a valid signed session cookie (`zi_session`) and are **scoped to that
session's `user_id`** — a route that can return another user's row is a defect, not a config issue.

The frontend static export is mounted at `/app` (canonical entry: `http://localhost:8001/app/`).

---

## Undo model

Before each Gmail mutation the system captures `{thread_id, original_label_ids: [...]}` — the exact
list of Gmail label IDs the thread carried before triage — and stores it as a JSON snapshot in
`ActionLog.undo_token`. Undo calls `gmail.users.threads.modify(addLabelIds=original_label_ids,
removeLabelIds=labels_added_by_triage)` to restore the exact pre-triage state. If the user manually
changed labels between triage and undo, the undo still restores to pre-triage state (their
intermediate changes are overwritten — acceptable trade-off). The `ActionLog.undone_at` timestamp is
set atomically; a second undo call on the same row is a no-op that returns the same result.

---

## Phase 1

### Auth & session
| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/health` | `{status: "ok", version}` — no auth |
| `GET` | `/auth/google/start` | 302 to Google consent (`gmail.readonly`, `gmail.modify`, `gmail.settings.basic`, `gmail.compose`; `access_type=offline`, `prompt=consent`) with a CSRF `state` |
| `GET` | `/auth/google/callback` | Exchanges the code, upserts `users` + `channel_accounts` (refresh token encrypted), sets `zi_session`, 302 to `/app/` |
| `POST` | `/auth/logout` | Clears the cookie |
| `GET` | `/api/me` | `{user: {id, email, display_name}, connections: [{id, account_email, channel, status, connected_at}], settings: {...}}` |

### Triage
| Method | Path | Body / Query | Returns |
|--------|------|--------------|---------|
| `POST` | `/api/connections/{connection_id}/triage` | `{limit: 200, only_new: bool}` | `{run_id}` — starts the run in the background and returns immediately. After the run completes, all non-`keep` and non-`needs_your_call` decisions are **automatically applied** (Gmail mutations performed); `needs_your_call` decisions are auto-kept and never archived. `only_new=true` fetches only threads newer than the last completed run's start time. |
| `GET` | `/api/runs/{run_id}` | | `{id, status, items_total, items_decided, counts, cost, error_message, started_at, finished_at}` — polled every 1 s for the progress bar |
| `GET` | `/api/runs/latest` | | Same shape as `/api/runs/{run_id}` but returns the most recent `completed` or `running` run for the session user — used by the frontend to auto-resume display on page load without a known run-id |
| `POST` | `/api/runs/{run_id}/cancel` | | `{status: "cancelled"}` |
| `GET` | `/api/triage/clusters` | `?run_id=` | `[{id, kind, label, item_count, applied_action, min_confidence, avg_confidence, sample_subjects: [3]}]` — read-only; `applied_action` reflects what was actually done |
| `GET` | `/api/triage/items` | `?cluster_id=` or `?run_id=&status=` | `[{decision_id, item: {subject, from_name, from_email, snippet_redacted, internal_date, message_count, is_unread}, category, applied_action, confidence, reasoning, decided_by, rule_id, rule_name, time_sensitive, status}]` — read-only history; `applied_action` reflects the action that was performed |
| `GET` | `/api/categories` | | the user's taxonomy |

> **Note:** `dry_run` is now a debug/dev flag only (default `false` in production). When
> `settings.dry_run=true` the system classifies threads but performs no Gmail mutations; the UI
> shows a `DRY RUN` banner. The old Phase 1 constraint of forced `dry_run=true` is removed.

---

## Phase 2

| Method | Path | Notes |
|--------|------|-------|
| `PATCH` | `/api/settings` | `{auto_act_threshold, confidence_floor, dry_run, llm_model, timezone}` |
| `POST` | `/api/categories` / `PATCH` `/api/categories/{id}` | create/edit a category; creates or renames the matching Gmail label |
| `POST` | `/api/categories/sync-labels` | ensures every category has a real Gmail label (1:1) |
| `POST` | `/api/categories/propose` | agent proposes a taxonomy fitted to the user's actual mail |
| `POST` | `/api/actions/{action_log_id}/undo` | fully reverses a single mutation using the pre-triage label snapshot stored in `ActionLog.undo_token` |
| `GET` | `/api/actions` | the audit trail, newest first, with an undo affordance per row |
| `GET`/`POST`/`DELETE` | `/api/vip` | the never-hide list |
| `GET`/`PUT` | `/api/profile` | the plain-English priorities profile |
| `GET`/`POST` | `/api/corrections` | corrections recorded as training signal |
| `GET` | `/api/inbox-summary` | Live Gmail thread counts: `{inbox_total, needs_your_call, categories: [{key, name, count, channel_label_name}]}`. Each count is one `labels().get()` call (no thread listing). |

---

## Phase 3 — Autopilot & Digest

### Autopilot

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/api/runs/{run_id}/summary` | Run summary card — `{run_id, status, total_threads, applied_count, kept_count, auto_kept_count, categories: [{name, count, applied_action}], top_clusters: [{label, count, applied_action}] (top 3 by size), cost_usd, completed_at}`. Reflects what **was done** (applied counts), not what is pending. |
| `POST` | `/api/runs/{run_id}/undo` | Reverses **all** Gmail mutations from this run in reverse chronological order. Each thread is restored to its pre-triage label state using the snapshot in `ActionLog.undo_token`. Returns `{reversed: n, skipped: n, errors: [...]}`. Idempotent — a second call on an already-undone run returns the same result immediately. Can only undo a `completed` run (not a running or cancelled run). |

Auto-trigger: after `/auth/google/callback` completes, the backend enqueues a `BackgroundTask`
equivalent to `POST /api/connections/{id}/triage` with `limit=200, only_new=false` — no client call
required. The run applies automatically on completion; the user sees the result in the Run Summary card.

### Digest

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/api/digest/latest` | Catch-up digest for the most recent completed run: `{run_id, generated_at, time_sensitive_kept: [{subject, from, reason}], vip_mail: [...], auto_kept_low_confidence: [...], auto_archived: {count, by_category: [...]}}`. Returns `404` if no completed run exists. |

### Events (SSE)

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/api/events` | `text/event-stream`. Pushes structured JSON events — `run_started: {run_id, connection_id, triggered_by: "user"\|"scheduler"\|"connect"}`, `run_progress: {run_id, batch_n, batch_total, items_decided, cost_so_far}`, `gmail_mutation_applied: {action_log_id, thread_count, category}`, `run_completed: {run_id, total_threads, applied_count, cost_usd}`, `error: {run_id, message}`. Last 50 events per user kept in memory only — not persisted to DB. |

---

## Phase 4 — Rules, Chat, Digest, Backlog & Proactive Assistance

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/api/rules` | proposed + active + automatic rules, with `match_count` |
| `POST` | `/api/rules/mine` | runs the pattern miner over the user's history, returns proposals |
| `POST` | `/api/rules/{id}/preview` | **dry-run**: returns exactly which threads it would archive |
| `POST` | `/api/rules/{id}/activate` \| `/promote` \| `/disable` | `promote` sets `status=automatic` — the only path to unattended action |
| `POST` | `/api/rules/{id}/create-filter` | creates the real persistent Gmail filter |
| `GET`/`POST` | `/api/rule-packs` | list starter packs / adopt one (copies rules into the user's account) |
| `POST` | `/api/chat` | `{message}` → `{reply, proposed_rule?, preview?}`; full prior turn history is loaded server-side |
| `GET` | `/api/chat/history` | the conversation |
| `GET` | `/api/digest/{date}` | the daily digest of what was hidden |
| `POST` | `/api/jobs/backlog` | `{from_date, to_date, chunk_days}` → `{job_id}` |
| `GET` | `/api/jobs/{job_id}` | live progress, resumable |
| `POST` | `/api/jobs/{job_id}/cancel` \| `/resume` | cancel / resume without redoing work |
| `GET` | `/api/cost` | `{this_run, month_to_date, rules_vs_llm_ratio, by_model}` |
| `GET` | `/api/models` | NVIDIA free-model catalogue for the dropdown |
| `GET` | `/api/proactive` | `{possible_missed_important: [...], unsubscribe_candidates: [...], stale_awaiting_reply: [...]}` |
| `POST` | `/api/drafts` | `{decision_id}` → creates a Gmail draft reply (never sends) |

## Phase 6 — Durable, Resumable, Transparent Runs

| Method | Path | Body / Query | Returns |
|--------|------|--------------|---------|
| `POST` | `/api/runs/{run_id}/resume` | — | `{run_id, items_total, items_decided, remaining}` — restarts the **same** run row in the background, skipping every thread that already has a decision. `409 not_resumable` if the run's status is not `resumable`. Idempotent: calling it on a run already back in `running` returns the same payload without starting a second background task. |
| `GET` | `/api/provider-health` | — | `{provider, model, model_chain: string[], chain_position: int, chain_exhausted: bool, circuit_open, calls, retries, consecutive_failures, degraded: bool, run_id, throttle: {max_rpm: int, available: number, waiting: int}}` — the current run's LLM health counters. `model` is the run's **current** model (`llm.health.current_model(run_id)`), which after a fallback is not the configured default; `chain_position` is its index in `model_chain` (the ordered chain from `llm.health.model_chain(preferred)`), so the user sees where the run is and where it would go next; `chain_exhausted` is true once `advance_model` has returned `None`. `degraded` is true when `retries/max(calls,1) >= 1.0` or `retries >= 50`. `throttle` reports the **process-wide** rate limiter (`max_rpm` from `AGENT_LLM_MAX_RPM`, default 350 under the account's 490 req/min ceiling; `waiting` = calls currently blocked on the bucket). |

Changes to existing routes:

- `GET /api/runs/{run_id}` and `GET /api/runs/latest` gain `resumable` as a possible `status`, plus
  `remaining` (`items_total - items_decided`) and `resumable: bool`. `error_message` for a resumable
  run reads *"Interrupted at N of M threads — nothing was left half-applied. Resume to continue."*
- `GET /api/triage/items` returns `review_state` on every row. `GET /api/triage/clusters` returns
  `provisional_count` per cluster.
- `POST /api/actions/{action_log_id}/undo` and every apply/approve path reject a decision whose
  `review_state != "reviewed"` with `422 not_reviewed`.
- `GET /api/events` gains the event types `provider_degraded`, `run_resumable` and `model_fallback`
  (`{run_id, from_model, to_model, reason}`), and
  `thread_classified` gains the `reasoning` and `review_state` fields
  (shapes in [triage-transparency](capabilities/triage-transparency.md)).

## Phase 7 — Drive to Inbox Zero

| Method | Path | Body / Query | Returns |
|--------|------|--------------|---------|
| `GET` | `/api/runs/{run_id}/remainder` | — | The remainder ledger — the single honest answer to *"how far from zero am I, and why?"*. Shape below. Computed **live** from `decisions`, never from cached counts. `404` if the run does not exist for the session user. |
| `POST` | `/api/runs/{run_id}/apply` | — | Re-runs the apply pass for a `completed` run **in the background**, without re-classifying a single thread. Because the pass is backgrounded, no apply ledger exists synchronously: the response is the **remainder ledger as it stands right now** (the same object `GET /remainder` returns) plus `"queued": bool` — `false` when a pass for this run is already in flight, so a double-click never starts a second pass. The finished result arrives on the SSE bus (`apply_progress`, then `inbox_zero_report` or `run_apply_failed`); the card refetches on those. Idempotent: already-`applied` decisions are never mutated twice. `409 {"error": {"code": "not_appliable"}}` if the run's status is not `completed`. This is the recovery path for a transient Gmail/auth failure. |

`GET /api/runs/{run_id}/remainder` response `data`:

```json
{
  "run_id": "…",
  "inbox_remaining": 1632,
  "distance_to_zero": 0,
  "applied": 544,
  "apply_ok": true,
  "apply_failed_reason": null,
  "dry_run": false,
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

`POST /api/runs/{run_id}/apply` response `data` is that **same object plus** `"queued": true|false`.

`inbox_remaining == sum(remainder.*) + distance_to_zero`. `unclassified` counts decisions written
before Phase 7 (`autonomy_state IS NULL`); it is always reported, never folded into another bucket.
Bucket definitions are in
[drive-to-inbox-zero](capabilities/drive-to-inbox-zero.md#the-inbox-zero-definition-written-down-and-stated-in-the-ui).

Changes to existing routes:

- `GET /api/runs/{run_id}` and `GET /api/runs/latest` gain `distance_to_zero: int` and
  `apply_ok: bool`. `apply_ok` is `false` when the run's apply pass recorded an
  `apply_failed_reason` **or** `distance_to_zero > 0`. A run that archived nothing can never render as
  a clean success.
- `GET /api/runs/{run_id}/summary` gains `applied_count`, `distance_to_zero` and the `remainder`
  object. (`applied_count` was specified in Phase 3 but never implemented — Phase 7 closes that drift.)
- `GET /api/categories` returns `auto_act_threshold` (nullable float) per category.
  `PATCH /api/categories/{id}` accepts `auto_act_threshold`; `0 < v <= 1` or `validation_error`.
  `default_action = "archive"` on the `urgent` key is still rejected.
- `PATCH /api/settings` validates `auto_act_threshold` as `0 < v <= 1` (`validation_error` otherwise)
  and, when the accepted value is `> 0.90`, returns `warning: "above_model_ceiling"` alongside the
  updated settings — the measured model ceiling is ~0.94, so anything above 0.90 acts on almost
  nothing. The default for a new user is `0.80`.
- `GET /api/events` gains the event types:
  - `apply_progress` — `{type, run_id, applied, total_to_apply, failed}`, emitted every 25 applied
    decisions and once at the end of the pass.
  - `run_apply_failed` — `{type, run_id, reason, distance_to_zero}`, emitted when the apply pass could
    not run or did not reach zero.
  - `inbox_zero_report` — `{type, run_id, applied, distance_to_zero, remainder}` where `remainder` is
    the same five-key `{bucket: int}` object as above; emitted once per run at the end, mirroring the
    `triage.inbox_zero_report` structured log line.

  Every event carries its own `type` field in the JSON body as well as in the SSE `event:` line.

  - `activity_heartbeat` — `{run_id, phase, detail, batch_n, batch_total, batch_size, model,
    elapsed_s, silent_for_s}`, published by the watchdog whenever **nothing** has been published for
    a run for `HEARTBEAT_INTERVAL_SECONDS = 3.0`. It carries real observed state (derived from the
    most recent log line for that run), never a content-free tick. Counts, ids, phase names, model
    ids and elapsed times only — no subject, sender or body. Shape and rules in
    [triage-transparency Rule I](capabilities/triage-transparency.md#rules--i-not-one-beat-without-a-log).

  The existing `auto_apply_complete` event is **retained**, extended with the full apply ledger. No
  existing subscriber breaks.

- `GET /api/events` **replay-on-connect is load-bearing and verified in Phase 7.** On every
  connection the endpoint first flushes `bus.replay_buffer(user_id)` — the last **1000** events for
  that user — and only then streams live events. A client joining or reloading mid-run therefore
  paints a populated feed immediately. This behaviour already exists (`src/api/events.py`); Phase 7
  adds the end-to-end verification, not a rewrite.

- The `log` event type (every structlog line for the bound user, forwarded by
  `src/observability/logging.py::activity_bus_processor`) is the **primary** transport for run
  granularity — `{type: "log", event, level, logger, timestamp, run_id, fields: {...}}`. Phase 7
  raises the granularity of what the graph and the LLM client log rather than adding hand-placed
  emits; see [triage-transparency Rule I2](capabilities/triage-transparency.md#rules--i-not-one-beat-without-a-log)
  for the required log events.

## Phase 8 — Identity, account and review recovery

### Sign-in is separate from mailbox connection

`GET /auth/google/start` gains an `intent` query parameter. The two intents request **different
scopes**, which is what makes "sign in" an honest first-class flow rather than a side effect of
granting mailbox access.

| `intent` | Scopes requested | Refresh token required | Result |
|----------|------------------|------------------------|--------|
| `signin` | `openid email profile` | no | Upserts the `users` row, issues a session, 302 → `/app/`. **Writes no `channel_accounts` row and triggers no triage run.** |
| `connect` (default when `intent` is absent — preserves the existing behaviour exactly) | the existing Gmail scopes | yes | Upserts `users` + `channel_accounts` (token encrypted), issues a session, triggers the auto-triage background task, 302 → `/app/` |

`intent` round-trips inside the existing signed `zi_oauth_state` cookie payload
(`{state, code_verifier, intent}`), never as an unsigned query parameter on the callback — otherwise
the scope decision would be attacker-controlled.

**Mailbox ownership.** A `connect` callback whose `account_email` already belongs to a **different**
user returns `409 mailbox_already_connected` and writes nothing: *"{address} is already connected to
another Zero Inbox account. Sign in as that account, or disconnect it there first."* Reconnecting a
mailbox you already own is the existing idempotent upsert and is unaffected.

> **Assumed:** identity **is** the Google account. Two humans sharing one Google login share one Zero
> Inbox user and one set of data — that is the correct answer, not a bug, and screen 22 states it.
> What Phase 8 forbids is two *distinct* Zero Inbox users pointing at the same mailbox, which would
> mean two agents mutating one inbox with two independent policies.

### Routes

| Method | Path | Body / Query | Returns |
|--------|------|--------------|---------|
| `GET` | `/auth/google/start` | `?intent=signin\|connect` | 302 to Google consent with the scope set for the intent |
| `POST` | `/auth/logout` | | `{logged_out: true}` — **revokes the current `user_sessions` row** (`revoked_at`), then clears the cookie. Unchanged shape; the revocation is new. |
| `GET` | `/api/account` | | `{user: {id, email, display_name, created_at}, connections: [{id, channel, account_email, status, connected_at, last_synced_at}], sessions: [{id, created_at, last_seen_at, user_agent_summary, current: bool}], counts: {decisions, action_logs, connections}}` — **never returns `refresh_token_enc`, a raw user agent string, or an IP address** |
| `DELETE` | `/api/account/connections/{connection_id}` | | `{disconnected: true}` — best-effort token revocation at Google, then deletes the `channel_accounts` row. Triage history is retained. `404` for another user's connection. Performs **zero** Gmail mutations. |
| `DELETE` | `/api/account/sessions/{session_id}` | | `{revoked: true}` — sets `revoked_at`. `404` for another user's session. Revoking the current session also clears the cookie. |
| `POST` | `/api/account/sessions/revoke-all` | | `{revoked: n}` — revokes every session for the user including the current one, and clears the cookie |
| `DELETE` | `/api/account` | `{confirm_email: "..."}` | `{deleted: true}` — `422 validation_error` unless `confirm_email` equals the user's email. Cascade-deletes every user-scoped row and clears the cookie. **Performs no Gmail mutation of any kind** — archived mail stays archived. |
| `POST` | `/api/runs/{run_id}/retry-review` | | `{retried: n, reviewed: n, still_failed: n, applied: n}` — re-runs the **real** never-miss reviewer over this run's `review_state IN ('provisional','review_failed')` decisions, then runs the ordinary apply pass for whatever the reviewer upgraded. Background task; returns immediately with `{run_id, status: "retrying_review"}` and the counts land on `GET /api/runs/{run_id}/remainder`. `409 not_retryable` unless `status == "completed"` and at least one non-`reviewed` decision exists. Idempotent: a second call while one is in flight is a no-op. |

**`retry-review` may not weaken any guarantee.** It calls the same reviewer node as a normal run and
the same `apply_decision()`; it never writes `review_state` directly, never passes `force=True`, and
never applies a `keep`-proposed decision. If the reviewer fails again the rows stay `review_failed`
and the run reports it. See
[never-miss-safeguards](capabilities/never-miss-safeguards.md) and
[review-recovery](capabilities/review-recovery.md).

### Session hardening (behaviour change, no new route)

- The session cookie payload becomes `{"uid": ..., "sid": ...}` where `sid` is a `user_sessions` row
  id. `require_user_id` additionally rejects a token whose `sid` is revoked or unknown → `401`.
- **Legacy compatibility:** a cookie carrying only `uid` (every session issued before Phase 8) stays
  valid until it expires; on its next authenticated request a `user_sessions` row is created and the
  cookie is re-issued with a `sid`. No user is signed out by this migration.
- `set_session_cookie` sets `secure=True` whenever the request scheme is `https` (it is currently
  hardcoded `False`), keeps `httponly` and `samesite=lax`, and rotates the token on every sign-in.
- `AGENT_SECRET_KEY` becomes **required**: the `"insecure-dev-key"` fallback in `api/auth.py` is
  removed, and the app fails to start without the key rather than signing cookies with a public string.
- `user_sessions.last_seen_at` is updated at most once per 60 s per session, so the device list is
  useful without a write per request.

## Error codes

`unauthenticated` (401) · `forbidden` (403) · `not_found` (404) · `already_undone` (409) ·
`reauth_required` (409, the Gmail refresh token is invalid) · `rate_limited` (429) ·
`provider_error` (502) · `validation_error` (422) · `not_resumable` (409, the run has no partial work
to resume) · `not_reviewed` (422, the decision has not passed the never-miss reviewer and can never be
applied) · `not_appliable` (409, the run is not `completed` so its decisions cannot be applied) ·
`mailbox_already_connected` (409, that address is connected to a different Zero Inbox account) ·
`not_retryable` (409, the run is not `completed` or has nothing left to review) ·
`auth_declined` (400, the user cancelled the Google consent screen).

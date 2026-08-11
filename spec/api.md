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

## Error codes

`unauthenticated` (401) · `forbidden` (403) · `not_found` (404) · `already_undone` (409) ·
`reauth_required` (409, the Gmail refresh token is invalid) · `rate_limited` (429) ·
`provider_error` (502) · `validation_error` (422).

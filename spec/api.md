# API

FastAPI on **port 8001**. Every JSON route returns the envelope `{"data": ..., "error": null}` via
`ok()`, or raises `api_error(code, message, status)` producing `{"data": null, "error": {code, message}}`.

All `/api/*` routes require a valid signed session cookie (`zi_session`) and are **scoped to that
session's `user_id`** — a route that can return another user's row is a defect, not a config issue.

The frontend static export is mounted at `/app` (canonical entry: `http://localhost:8001/app/`).

---

## Phase 1

### Auth & session
| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/health` | `{status: "ok", version}` — no auth |
| `GET` | `/auth/google/start` | 302 to Google consent (`gmail.readonly`, `gmail.modify`, `gmail.settings.basic`, `gmail.compose`; `access_type=offline`, `prompt=consent`) with a CSRF `state` |
| `GET` | `/auth/google/callback` | Exchanges the code, upserts `users` + `channel_accounts` (refresh token encrypted), sets `zi_session`, 302 to `/app/` |
| `POST` | `/auth/logout` | Clears the cookie |
| `GET` | `/api/me` | `{user: {id, email, display_name}, connections: [{id, account_email, status, connected_at}], settings: {...}}` |

### Triage
| Method | Path | Body / Query | Returns |
|--------|------|--------------|---------|
| `POST` | `/api/connections/{connection_id}/triage` | `{limit: 200}` | `{run_id}` — starts the run in the background, returns immediately |
| `GET` | `/api/runs/{run_id}` | | `{id, status, dry_run, items_total, items_decided, counts, cost, error_message, started_at, finished_at}` — polled every 1s for the progress bar |
| `POST` | `/api/runs/{run_id}/cancel` | | `{status: "cancelled"}` |
| `GET` | `/api/triage/clusters` | `?run_id=` | `[{id, kind, label, item_count, suggested_action, min_confidence, avg_confidence, sample_subjects: [3]}]` |
| `GET` | `/api/triage/items` | `?cluster_id=` or `?run_id=&status=` | `[{decision_id, item: {subject, from_name, from_email, snippet_redacted, internal_date, message_count, is_unread}, category, proposed_action, confidence, reasoning, decided_by, rule_id, rule_name, time_sensitive, status}]` |
| `POST` | `/api/triage/decisions/{decision_id}` | `{status: "approved"\|"rejected"}` | updated decision. **Phase 1: records intent only — no Gmail call.** `rejected` never causes a Gmail call in any phase — only `approved` decisions can later be passed to `POST /api/actions/apply` (Phase 2) |
| `POST` | `/api/triage/clusters/{cluster_id}/approve` | `{status: "approved"\|"rejected"}` | `{updated: n}` — the bulk sweep, same rule: rejecting never mutates the mailbox |
| `GET` | `/api/categories` | | the user's taxonomy |

Any attempt to mutate Gmail while `dry_run` is true raises `api_error("dry_run_violation", …, 409)`.
Phase 1 forces `dry_run=true` server-side; the client cannot turn it off.

---

## Phase 2

| Method | Path | Notes |
|--------|------|-------|
| `PATCH` | `/api/settings` | `{auto_act_threshold, confidence_floor, dry_run, llm_model, timezone}` |
| `POST` | `/api/categories` / `PATCH` `/api/categories/{id}` | create/edit a category; creates or renames the matching Gmail label |
| `POST` | `/api/categories/sync-labels` | ensures every category has a real Gmail label (1:1) |
| `POST` | `/api/categories/propose` | agent proposes a taxonomy fitted to the user's actual mail |
| `POST` | `/api/actions/apply` | `{decision_ids: [...]}` → performs the real mutations, returns `[{action_log_id, undo_token_id}]` |
| `POST` | `/api/actions/{action_log_id}/undo` | fully reverses that action |
| `GET` | `/api/actions` | the audit trail, newest first, with an undo affordance per row |
| `GET`/`POST`/`DELETE` | `/api/vip` | the never-hide list |
| `GET`/`PUT` | `/api/profile` | the plain-English priorities profile |
| `GET`/`POST` | `/api/corrections` | corrections recorded as training signal |
| `GET` | `/api/triage/needs-your-call` | the below-the-floor queue |

---

## Phase 3

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

`unauthenticated` (401) · `forbidden` (403) · `not_found` (404) · `dry_run_violation` (409) ·
`reauth_required` (409, the Gmail refresh token is invalid) · `rate_limited` (429) ·
`provider_error` (502) · `validation_error` (422).

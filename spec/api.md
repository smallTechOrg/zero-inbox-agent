# API

FastAPI, port 8001. Envelope: `{ "ok": true, "data": … }` or
`{ "ok": false, "error": { "code", "message" } }`. Auth: signed session cookie;
unauthenticated → 401 `{code:"signed_out"}`. Any Google token failure →
`{code:"gmail_reconnect", message:"Reconnect Gmail to continue."}` (never a
traceback). All routes are per-session-user scoped.

## Auth & Account (Phase 1)

| Method Path | Does |
|---|---|
| `GET /auth/google/login` | Redirect to Google consent (openid + gmail.modify scopes). |
| `GET /auth/google/callback` | Exchange code, upsert user + gmail_account, set session cookie, redirect to `/`. |
| `POST /api/auth/logout` | Clear session. |
| `GET /api/me` | User + gmail connection status (`connected`/`needs_reconnect`/`none`). |
| `POST /api/gmail/disconnect` | Delete token row. |

> **Assumed:** one consent flow grants both sign-in and Gmail access (single OAuth
> client, `gmail.modify` scope) — no separate "connect Gmail" round trip; the UI's
> "Connect Gmail" state appears only after a `needs_reconnect`.

## Audit & Taxonomy (Phase 1)

| `POST /api/audit` | Run the mini-audit (counts, oldest, top senders, tab mix); returns snapshot. Read-only, <10s. |
| `GET /api/audit/latest` | Latest snapshot. |
| `GET /api/taxonomy` | Categories with rules, ordered. |
| `POST /api/taxonomy` | Add category `{name, description, rule}`. |
| `PATCH /api/taxonomy/{id}` | Rename / edit description / change rule. |
| `DELETE /api/taxonomy/{id}` | Delete (409 if in use in Phase 1; merge target required in Phase 2: `?merge_into=<id>`). "Needs review" not deletable (409). |

## Runs, Feed, Undo (Phase 1)

| `POST /api/runs` | Start (or resume the interrupted) cleaning run, `{chunk_limit?}` default 50. 409-with-active-run_id if already running. Returns `{run_id}`. |
| `GET /api/runs` | Run cards, newest first (status, counts, costs, undo state). |
| `GET /api/runs/{id}` | Card detail + this run's decisions. |
| `GET /api/runs/{id}/events` | SSE: replays persisted `run_events` from `?after_seq`, then live. |
| `POST /api/runs/{id}/undo` | Whole-run undo; streams `undo_*` events on the same channel; 409 if running or already undone. |
| `GET /api/health` | Liveness + config sanity (no secrets). |

## Phase 2

| `GET /api/ledger?q=&category=&needs_review=&page=` | Searchable per-email ledger (decision, reason, undo state) over `thread_decisions`. |
| `GET /api/costs` | Per-run cost cards + cumulative totals. |
| `GET /api/profiles` / `POST /api/profiles` / `DELETE /api/profiles/{id}` | Sender profiles. |
| `GET /api/progress` | Inbox-zero progress: remaining undecided INBOX threads vs total. |

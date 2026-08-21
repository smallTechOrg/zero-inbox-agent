# Architecture

## System Overview

```
Browser (Next.js dashboard, :3000)
   │  REST + SSE (proxied /api → :8001)
   ▼
FastAPI backend (:8001)
   ├─ Auth: Google OAuth (signed session cookie; encrypted refresh tokens)
   ├─ Gmail client: read INBOX threads, add/remove labels (reversible ops only)
   ├─ Triage runner: LangGraph graph (spec/agent.md), one run at a time per user
   ├─ Event bus: per-run SSE stream (feed + progress + cost ticker)
   └─ SQLite (./data/agent.db) — all state, keyed by user_id
LLM: NVIDIA NIM (primary) → Gemini (automatic fallback), hard timeouts
Observability: LangSmith tracing + structured JSON logs (stdout)
```

## Components

- **`src/api/`** — FastAPI routers. Every response is a JSON envelope
  `{ok, data|error}`; errors are human-actionable sentences (never tracebacks).
  A revoked/expired Google token anywhere returns
  `{ok:false, error:{code:"gmail_reconnect", message:"Reconnect Gmail to continue."}}`.
- **`src/channels/gmail/`** — OAuth flow, token store (refresh tokens encrypted with
  `AGENT_SECRET_KEY`-derived key), thread fetch (INBOX only, newest first,
  metadata-format — never full bodies), label CRUD, mutation executor. Every
  mutation goes through one choke point that (a) writes the audit row first,
  (b) refuses any op that is not `add_label`/`remove_label`/`remove_INBOX`, and
  (c) honors the test-isolation guard (below).
- **`src/llm/`** — one `classify_batch()` client. Primary NVIDIA NIM
  (OpenAI-compatible); on error, HTTP 429, or timeout it fails over to Gemini for
  the current batch and probes NVIDIA again on the next batch (prefer returning to
  primary). Hard per-call timeout (`AGENT_LLM_TIMEOUT_SECONDS`, default 30s) —
  a stalled provider is impossible by construction. Records every call (provider,
  model, tokens, latency, est. cost, fallback flag) to `llm_calls`.
- **`src/graph/`** — the LangGraph triage graph. See spec/agent.md.
- **`src/events/`** — in-process per-run event bus; events are also persisted as
  `run_events` rows so a reconnecting browser replays the feed.
- **`src/tools/undo.py`** — whole-run undo: replays the run's audit rows in reverse,
  inverting each mutation.
- **`frontend/`** — the single dashboard (spec/ui.md).

## Data Flow (one cleaning chunk)

1. `POST /api/runs` → create (or resume) the run row; runner starts in a background
   task; response returns `run_id` immediately.
2. Runner fetches the next up-to-50 undecided INBOX threads (newest first), skipping
   any `gmail_thread_id` already in `thread_decisions` for this user.
3. Threads are classified in metadata-only batches (≤25/batch); each decision row is
   written before its mutation is applied; each mutation writes an audit row, then
   executes against Gmail; each step emits a feed event.
4. Finalize writes run totals (counts, costs) and emits `run_finished`.
5. `POST /api/runs/{id}/undo` inverts every non-undone audit row, newest first.

## Privacy Boundary (hard rule)

The only fields ever serialized into an LLM prompt: sender address + display name,
subject, `List-Unsubscribe` presence, `Reply-To`, Gmail category tab, thread message
count, has-user-replied flag, and the Gmail snippet (~90 chars). The prompt builder
takes a `ClassifierView` dataclass containing exactly these fields — the body is
unrepresentable in the type. A unit test asserts no other field can reach a prompt.

## Test-Isolation Guard

Reuse the repo's proven pattern: the Gmail mutation choke point checks an
environment flag set by `tests/conftest.py`; under tests, mutations are recorded to
the audit trail and returned as applied **without** calling Gmail's write API. Read
paths and LLM calls stay real (keys from `.env`). E2E tests run against a seeded
test user, never the live account's mutations.

## Multi-User Isolation

Every table carries `user_id`; every query filters by the session's user; Gmail
tokens, runs, decisions, taxonomy, profiles, and costs are all per-user. There is no
cross-user code path.

## Stack

| Layer | Choice |
|---|---|
| Language | Python 3.12+ (backend), TypeScript (frontend) |
| Agent framework | LangGraph |
| LLM primary | NVIDIA NIM — `AGENT_NVIDIA_API_KEY/BASE_URL/DEFAULT_MODEL` (default `nvidia/nemotron-3-nano-30b-a3b`) |
| LLM fallback | Gemini — `AGENT_GEMINI_API_KEY`, `AGENT_GEMINI_FALLBACK_MODEL=gemini-2.5-flash-lite` |
| Backend | FastAPI + uvicorn, port 8001 (`PORT`/`AGENT_PORT`) |
| Database | SQLite via SQLAlchemy 2.x (`sqlite:///./data/agent.db`); Postgres migration path documented in Phase 3 (`docs/postgres-migration.md`) — schema uses only Postgres-compatible types |
| Frontend | Next.js 15 + React 19, dev port 3000, `/api` proxied to backend |
| Auth | Google OAuth 2.0 (`AGENT_GOOGLE_CLIENT_ID/SECRET/REDIRECT_URI`), signed session cookie, refresh tokens encrypted at rest |
| Key libraries | `google-auth`/`google-api-python-client` (Gmail), `httpx` (LLM), `sse-starlette` (SSE), `pydantic-settings` |
| Deps | uv (Python), pnpm (frontend) |
| Observability | LangSmith (`LANGCHAIN_TRACING_V2=true`, `LANGCHAIN_API_KEY`) + structured JSON logging (request/response, latency, provider, error) from Phase 1 |
| E2E | Playwright in `tests/e2e/` (Phase 1 deliverable) |

> **Assumed:** SQLite is acceptable for current local hosting per the brief; the
> Postgres path is docs-only until Phase 3.
> **Assumed:** LangSmith tracing is enabled when `LANGCHAIN_API_KEY` is present and
> silently disabled otherwise (structured logs always on).
> **Assumed:** existing proven modules (`src/channels/gmail/*`, `src/llm/*`,
> `src/security/crypto.py`, `src/events/bus.py`, undo machinery, test-isolation
> guard) are reused where they conform to this spec; everything else in `src/` and
> `frontend/` that this spec does not name is deleted.

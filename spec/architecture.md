# Architecture

## System Overview

A **channel-agnostic triage core** with **Gmail as the first adapter**. The triage brain, rules,
categories, clusters, memory and audit log operate exclusively on a generic `Item` shape. Gmail sits
behind a `ChannelAdapter` interface and is the only implementation shipped in v1.

```
Browser (Next.js static export, served at :8001/app/)
        │  JSON over /api/*   +  OAuth redirect via /auth/google/*
        ▼
FastAPI (:8001)  ── session cookie (signed, AGENT_SECRET_KEY)
        │
        ├── api/auth.py ........... Google OAuth web flow, refresh-token storage
        ├── api/connections.py .... launch a triage run for a mailbox
        ├── api/triage.py ......... clusters, threads, decisions, approve/reject
        ├── api/runs.py ........... run progress (polled)
        │
        ▼
LangGraph triage graph  (src/graph/)  ── see spec/agent.md
        │
        ├── ChannelAdapter (src/channels/base.py)
        │        └── GmailAdapter (src/channels/gmail/)  ← only impl in v1
        ├── Tools (src/tools/): redact, rules, clustering, never_miss, memory,
        │                        taxonomy, actions, rule_mining, cost, digest
        └── LLMClient (src/llm/) → NVIDIA NIM, OpenAI-compatible HTTP
        │
        ▼
SQLite via SQLAlchemy 2.0 + Alembic  (headers, IDs, decisions, reasoning — never bodies)
```

## Components

| Component | Path | Responsibility |
|-----------|------|----------------|
| API surface | `src/api/` | HTTP routes, session cookie, response envelope `ok()` / `api_error()` |
| Channel interface | `src/channels/base.py` | Abstract `ChannelAdapter`: `list_threads`, `get_thread`, `archive_and_label(thread_id, add_label_ids, remove_inbox=True)`, `undo_archive_and_label(thread_id, ...)`, `create_label`, `create_filter`, `create_draft` — **no `trash`, `delete`, or `report_spam` method exists on this interface**, so no implementation, including `GmailAdapter`, can expose one |
| Gmail adapter | `src/channels/gmail/` | OAuth client, thread listing, header/snippet extraction, the single atomic `modify()` call behind `archive_and_label`, filter creation, draft creation |
| Triage graph | `src/graph/` | LangGraph state machine — the cost-tiered cascade; see [`agent.md`](agent.md) |
| Tools | `src/tools/` | Pure functions: `(inputs) → domain model`. No side effects except the explicitly-named action tools |
| LLM layer | `src/llm/` | `LLMClient` wrapper + swappable OpenAI-compatible provider |
| Persistence | `src/db/` | SQLAlchemy 2.0 declarative models + session factory; schema in [`data.md`](data.md) |
| Domain models | `src/domain/` | Pydantic models — `Item`, `Decision`, `Cluster`, `Rule`, `TriageOutcome` |
| Security | `src/security/crypto.py` | Fernet encryption of OAuth refresh tokens at rest, keyed from `AGENT_SECRET_KEY` |
| Observability | `src/observability/` | structlog JSON logging + LangSmith tracing + per-call cost accounting |
| Frontend | `frontend/` | Next.js static export mounted by FastAPI at `/app` |

## Data Flow — a triage run

1. `POST /api/connections/{id}/triage` creates a `TriageRun` row (`status=running`) and starts the
   graph in a background task; the route returns `{run_id}` immediately.
2. `GmailAdapter.list_threads(limit)` fetches thread metadata (`format=metadata`, headers only) →
   normalized into `Item` objects. A ~200-char snippet is taken and immediately passed through
   `redact()`; **no body text is ever written to the database**.
3. The cascade runs (see [`agent.md`](agent.md)): deterministic rules → sender history → batched LLM
   (20–50 items per call) → deep-read escalation → second-pass reviewer (Phase 2) → confidence floor.
4. Decisions are persisted with `confidence`, `reasoning`, `decided_by` tier and `rule_id`.
5. `cluster_decisions` groups them by sender / mailing-list / domain / category into `Cluster` rows.
6. The frontend polls `GET /api/runs/{run_id}` for progress and `GET /api/triage/clusters` for results
   as they land.
7. Approving a cluster (Phase 2+, dry-run off) calls `actions.apply_decision()`, which invokes the
   adapter mutation, writes an `ActionLog` row with the request parameters and an **undo token**, and
   marks the decision `applied`.

## Privacy & Redaction Boundary

`src/tools/redact.py` is the single egress chokepoint. Every string that leaves the machine for the
LLM passes through it first. It removes: 4–8 digit OTP-shaped codes in security contexts, `sk-`/`ghp_`
/`AKIA`-style API keys, 13–19 digit card numbers (Luhn-checked), and password-labelled values,
replacing each with `[REDACTED:<kind>]`. Redaction runs **before** the prompt is assembled, not inside
the provider.

Body escalation (`format=full`) is fetched **in memory only** for threads the cascade marks unsure; the
body is redacted, sent, and discarded. Never stored.

## Concurrency & Background Work

Triage runs execute in a FastAPI `BackgroundTasks` worker inside the same process, with progress
written to the `TriageRun` row after each batch so the UI can poll. LLM batches fan out with
LangGraph's `Send` API at a max concurrency of 4. Backlog jobs (Phase 3) are the same mechanism with a
persisted cursor, making them cancellable and resumable.

> **Assumed:** In-process background tasks + DB-persisted progress are sufficient at the stated volume
> (a few hundred messages/day, latency not critical). No Celery/Redis/queue is introduced in v1.

## Error Handling

Every external call (Gmail, NVIDIA NIM) is wrapped with a timeout, 3 retries with exponential backoff,
and explicit handling of 401 (refresh the OAuth token once, then mark the connection `reauth_required`)
and 429 (backoff + resume). A failed LLM batch does not fail the run: its items are routed to
`needs_your_call` with `decided_by=error` — degradation always errs toward keeping mail visible.

## Stack

| Layer | Choice |
|-------|--------|
| Language | Python 3.12+ |
| Dependency management | `uv` (Python), `pnpm` (frontend) |
| Agent framework | LangGraph (`StateGraph`, `Send` fan-out) — graph in [`agent.md`](agent.md) |
| LLM provider | **NVIDIA NIM** via its OpenAI-compatible endpoint, `https://integrate.api.nvidia.com/v1` |
| LLM default model | `nvidia/nemotron-3-nano-30b-a3b` (env: `AGENT_NVIDIA_DEFAULT_MODEL`), overridable per user |
| LLM client | `openai` Python SDK pointed at the NIM `base_url` — a thin OpenAI-compatible client with a swappable model id; **never a hardcoded model** |
| Backend | FastAPI + Uvicorn, port **8001** |
| Database | SQLite via SQLAlchemy 2.0 declarative + Alembic |
| Frontend | Next.js 15 + React 19, `output: 'export'`, `basePath: '/app'`, served by FastAPI at `/app` |
| Styling | Tailwind v4 (`postcss.config.mjs` with `@tailwindcss/postcss`, `@source "../";` in `globals.css`) |
| Google API | `google-auth-oauthlib` + `google-api-python-client` |
| Token encryption | `cryptography` Fernet, key derived from `AGENT_SECRET_KEY` |
| Session | signed cookie via `itsdangerous` |
| Logging | `structlog` JSON to stdout |
| Tracing | LangSmith (`LANGCHAIN_TRACING_V2=true`, `LANGCHAIN_API_KEY`) — wired in Phase 1 |
| Unit/integration tests | `pytest` |
| E2E tests | Playwright (`@playwright/test`, chromium) in `tests/e2e/` |

### Provider layer contract

```python
# src/llm/providers/base.py
class LLMProvider(Protocol):
    def call_model(self, prompt: str, *, system: str | None = None,
                   model: str | None = None, json_schema: dict | None = None) -> LLMResult: ...

# LLMResult: text: str, model: str, tokens_in: int, tokens_out: int, latency_ms: int
```

`src/llm/providers/nvidia.py` implements it against the OpenAI SDK with
`base_url=settings.nvidia_base_url`. The `model` argument overrides the default per call, so the
per-user model preference (Phase 3) requires no code change. The existing `anthropic.py` and
`gemini.py` providers are removed — NVIDIA NIM is the only provider in v1.

### Settings (`src/config/settings.py`, env prefix `AGENT_`)

| Setting | Env var | Default |
|---------|---------|---------|
| `database_url` | `AGENT_DATABASE_URL` | `sqlite:///./data/agent.db` |
| `nvidia_api_key` | `AGENT_NVIDIA_API_KEY` | required |
| `nvidia_base_url` | `AGENT_NVIDIA_BASE_URL` | `https://integrate.api.nvidia.com/v1` |
| `nvidia_default_model` | `AGENT_NVIDIA_DEFAULT_MODEL` | `nvidia/nemotron-3-nano-30b-a3b` |
| `google_client_id` | `AGENT_GOOGLE_CLIENT_ID` | required |
| `google_client_secret` | `AGENT_GOOGLE_CLIENT_SECRET` | required |
| `google_redirect_uri` | `AGENT_GOOGLE_REDIRECT_URI` | `http://localhost:8001/auth/google/callback` |
| `secret_key` | `AGENT_SECRET_KEY` | required |
| `port` | `PORT` | `8001` |
| `log_level` | `AGENT_LOG_LEVEL` | `INFO` |

Google OAuth scopes requested: `gmail.readonly`, `gmail.modify`, `gmail.settings.basic`,
`gmail.compose`. The OAuth client is the user's own Google Cloud project in **test mode**; refresh
tokens are stored per user, encrypted.

> **Assumed:** SQLite is correct here because this is an explicitly single-user-per-install personal
> tool, even though the schema is multi-tenant. Tests therefore run against SQLite — the same driver
> as production — satisfying the same-driver rule.

> **Assumed:** LangSmith tracing is enabled when `LANGCHAIN_API_KEY` is present and silently skipped
> when absent; structured stdout logging is unconditional, so observability is never absent.

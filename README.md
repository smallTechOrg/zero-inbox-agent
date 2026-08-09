# Zero Inbox Agent

Reads your Gmail, categorises every **thread** (never individual messages), groups the result into
**clusters** so a few hundred threads collapse into ~30 decisions, and shows you exactly why each
decision was made — category, confidence, full reasoning, and which tier decided it.

**Phase 1 is strictly dry-run: nothing in your Gmail is ever changed.**

Built spec-first — the specification in [`spec/`](spec/) is the source of truth. Start with
[`spec/roadmap.md`](spec/roadmap.md).

---

## The overriding guardrail

**An important email must never be missed.** Every trade-off resolves toward keeping mail visible.
Permanent safety invariants:

- **Never delete anything, ever.** No trash, no spam-report. Archive + label only (Phase 2+).
- **Nothing acts without approval** until you explicitly promote a rule to automatic.
- **Full undo** for every mutation (Phase 2).
- **Email bodies are never persisted** to the database — only headers, identifiers, decisions and
  reasoning. Secrets (OTPs, API keys, card numbers, passwords) are redacted *before* anything leaves
  your machine.

---

## Requirements

| Tool | Version |
|------|---------|
| Python | 3.12+ |
| [`uv`](https://docs.astral.sh/uv/) | latest |
| Node | 20+ |
| `pnpm` | 9+ |

You also need:

1. An **NVIDIA NIM API key** (free tier) from <https://build.nvidia.com/> — the LLM provider.
2. A **Google Cloud OAuth client** of your own (see below).

---

## Setup

### 1. Install dependencies

```bash
uv sync
cd frontend && pnpm install && cd ..
pnpm install                       # root: Playwright for the E2E suite
npx playwright install --with-deps chromium
```

### 2. Create your Google Cloud OAuth client (test mode)

1. Open <https://console.cloud.google.com/> → create (or pick) a project.
2. **APIs & Services → Library → Gmail API → Enable.**
3. **APIs & Services → OAuth consent screen** → User type **External** → keep the app in
   **Testing** mode → add *your own* Google address under **Test users**.
   (Testing mode is intentional: this is a personal single-user install, so no Google verification
   review is required. Refresh tokens in test mode expire after 7 days — reconnect when prompted.)
4. Add these **scopes**:
   - `https://www.googleapis.com/auth/gmail.readonly`
   - `https://www.googleapis.com/auth/gmail.modify`
   - `https://www.googleapis.com/auth/gmail.settings.basic`
   - `https://www.googleapis.com/auth/gmail.compose`

   Phase 1 only ever issues **read** calls; the write scopes are requested once now so Phase 2 does
   not force a second consent round.
5. **Credentials → Create credentials → OAuth client ID → Web application.**
   Authorised redirect URI: `http://localhost:8001/auth/google/callback`
6. Copy the **Client ID** and **Client secret**.

### 3. Configure `.env`

```bash
cp .env.example .env
```

Then fill it in. `.env` is git-ignored and **must never be committed**.

| Variable | Meaning |
|----------|---------|
| `AGENT_DATABASE_URL` | SQLite path, e.g. `sqlite:///./data/agent.db` |
| `AGENT_NVIDIA_API_KEY` | your NVIDIA NIM key |
| `AGENT_NVIDIA_BASE_URL` | `https://integrate.api.nvidia.com/v1` |
| `AGENT_NVIDIA_DEFAULT_MODEL` | `nvidia/nemotron-3-nano-30b-a3b` |
| `AGENT_GOOGLE_CLIENT_ID` | from step 2 |
| `AGENT_GOOGLE_CLIENT_SECRET` | from step 2 |
| `AGENT_GOOGLE_REDIRECT_URI` | `http://localhost:8001/auth/google/callback` |
| `AGENT_SECRET_KEY` | signs sessions + encrypts stored refresh tokens (see below) |
| `PORT` | `8001` |
| `AGENT_LOG_LEVEL` | `INFO` |
| `LANGCHAIN_API_KEY` | *optional* — enables LangSmith tracing when present |

Generate a secret key:

```bash
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

---

## Run it

```bash
uv run alembic upgrade head          # create / migrate the SQLite database
cd frontend && pnpm build && cd ..   # static export into frontend/out
uv run python -m src                 # FastAPI + the static frontend on port 8001
```

Open **<http://localhost:8001/app/>** — note the port, the `/app/`, and the **trailing slash**.

Then:

1. Click **Connect Gmail** → the real Google consent screen → approve → you are returned to the
   dashboard showing your connected address.
2. Click **Run triage (200 threads)** → a live progress bar counts threads as they are decided.
3. Sweep the queue: expand a cluster to see its threads; expand a thread to read the full reasoning
   and see the tier badge (`RULE` / `SENDER HISTORY` / `LLM`). Approve or reject a whole cluster.
4. The red **"DRY RUN — nothing in your Gmail has been changed"** banner stays pinned at the top.
   Check Gmail: nothing has moved.

---

## What Phase 1 does — and does not — do

**Real in Phase 1**

- Google OAuth connection to your own mailbox (the OAuth login *is* the dashboard session)
- Thread ingestion of your most recent ~200 inbox threads (headers + a redacted ~200-char snippet)
- Cost-tiered triage: deterministic rules → sender history → batched LLM
- Clustering into ~30 reviewable decisions
- Category, confidence, full reasoning and a **which-tier-fired** badge on every thread
- The **Needs your call** bucket for everything below the confidence floor
- Live run progress; approve / reject recorded in the database
- Structured JSON logging on stdout, plus LangSmith tracing when a key is present

**Not in Phase 1 (labelled `COMING SOON` stubs in the UI — greyed and disabled, never broken)**

Rules view · Chat · Daily digest · Backlog cleanup · VIP list editor · Priorities profile editor ·
Cost panel · Model dropdown · Undo · "Create Gmail filter" · "Draft reply" · Unsubscribe suggestions ·
Stale threads.

**Explicitly not done in Phase 1:** *any* write to Gmail. The Gmail adapter exposes read operations
only; its mutation methods raise `DryRunViolation` if called. Approving a cluster changes a row in
your local database and nothing else.

Phase 2 adds the never-miss safeguards, real archive/label actions and one-click undo. Phase 3 adds
rules, chat, digest, backlog cleanup and the cost panel. See [`spec/roadmap.md`](spec/roadmap.md).

---

## Observability

- **Structured logging is unconditional.** `src/observability/logging.py` configures `structlog` to
  emit one JSON object per event to stdout (timestamp, level, event, latency_ms, and the event's
  fields). Two privacy rules are enforced *in the logging pipeline*, not at call sites: secret-shaped
  fields (`api_key`, `refresh_token`, `client_secret`, cookies, OAuth `code`/`state`, …) render as
  `[REDACTED]`, and email content fields (`body`, `snippet`, …) render as `[OMITTED:body]`. Values
  that *look* like keys (`nvapi-…`, `sk-…`, `ghp_…`, `AKIA…`, `ya29.…`, `lsv2_…`, `GOCSPX-…`) are
  scrubbed even inside free text.
- **LangSmith tracing is opt-in by key presence.** `configure_tracing()` turns tracing on only when
  `LANGCHAIN_API_KEY` is set, sets `LANGCHAIN_TRACING_V2=true` and `LANGCHAIN_PROJECT`, and logs the
  fact — never the key. With no key it disables tracing and says so. `trace_config(run_name, …)`
  produces the `RunnableConfig` fragment (run name, tags, metadata) passed into the LangGraph run.
- One-call startup hook: `from observability import setup_observability`.

---

## Tests

Tests and evals run against the **real** NVIDIA NIM endpoint and the **real** Gmail API using the keys
in `.env`, and against the same SQLite driver used in production. Nothing is stubbed at the
integration or E2E level.

### The full Phase 1 gate

```bash
uv run alembic upgrade head && uv run alembic current
uv run pytest tests/unit tests/integration -q
cd frontend && pnpm install && pnpm build && cd ..
uv run python -m src &            # serves http://localhost:8001
npx playwright test tests/e2e/ --reporter=line
```

All commands must exit 0, and `alembic current` must print a revision hash rather than a blank line.

### Notes on the suites

- `tests/integration/test_triage_pipeline.py` triages a fixture of **220 real-shaped threads** against
  the live NVIDIA NIM endpoint and asserts all 220 have persisted decisions and that cluster
  item-counts sum to 220 — deliberately larger than any plausible sample size.
- `tests/integration/test_gmail_adapter.py` hits the **real Gmail API** with your stored refresh
  token, and **skips loudly (unverified, not passing)** if no mailbox is connected.
- `tests/integration/test_no_body_persisted.py` asserts no `Item`, `Decision` or log row holds body
  text.
- `tests/e2e/` (Playwright, chromium) runs against the **live app on port 8001**. `playwright.config.ts`
  reuses a server already listening on 8001 and otherwise starts `uv run python -m src` itself, so
  the E2E suite needs the database migrated and `frontend/out` built first. The triage journey
  (`tests/e2e/triage.spec.ts`) requires a **connected Gmail account** — Google's consent screen cannot
  be automated — and skips with an explicit reason when none is connected.

Run a single suite:

```bash
uv run pytest tests/unit -q
uv run pytest tests/unit/observability -q
npx playwright test tests/e2e/smoke.spec.ts --reporter=line
```

---

## Layout

```
src/
  api/            FastAPI routes, session cookie, {data,error} envelope
  channels/       ChannelAdapter interface + the Gmail adapter (only impl in v1)
  graph/          LangGraph triage cascade (see spec/agent.md)
  tools/          pure functions: redact, rules, clustering, …
  llm/            LLMClient + the NVIDIA NIM provider
  db/             SQLAlchemy 2.0 models + session factory
  domain/         Pydantic domain models
  security/       Fernet encryption of stored refresh tokens
  observability/  structlog JSON logging + LangSmith tracing
frontend/         Next.js 15 static export, mounted by FastAPI at /app
tests/            unit · integration (real APIs) · e2e (Playwright)
spec/             the source of truth
```

---

## Security

- Secrets live only in `.env`, which is git-ignored. Never hard-code or commit a key.
- OAuth refresh tokens are encrypted at rest with Fernet, keyed from `AGENT_SECRET_KEY`.
- Every `/api/*` route is scoped to the session's `user_id`; returning another user's row is a defect.
- If you rotate `AGENT_SECRET_KEY`, existing stored tokens become undecryptable — reconnect Gmail.

# Zero Inbox Agent — Notes for AI Sessions

A spec-driven, multi-user Gmail triage agent: FastAPI + LangGraph + SQLite backend (`src/`),
Next.js dashboard (`frontend/`). The spec in `spec/` is the source of truth — **read it before
writing application code**, in this order: `roadmap.md`, `architecture.md`, `capabilities/`,
`data.md`, `api.md`, `ui.md`, `agent.md`.

Build workflow (spec-first phases, testing gates, fix/sync flows) is provided by pre-installed
skills — nothing workflow-related lives in this repo.

## Hard rules

- Spec wins over code; reconcile drift toward the spec.
- Email **bodies never go to any LLM** — classification uses sender/subject/headers/Gmail
  signals/snippet only (enforced by a payload-spy test).
- Every Gmail mutation is audited before it happens and reversible per run; never delete mail.
- LLM: NVIDIA NIM primary, automatic Gemini fallback, hard per-call timeouts (keys in `.env`).
- Tests run against the real LLM/API keys in `.env`; the test-isolation guard must keep the live
  Gmail account untouched — never weaken it.
- Commit every logical unit of work; never leave the tree dirty.

## Running

Backend: `uv run python -m src` (port 8001). Frontend: `pnpm --dir frontend dev` (port 3000).
Migrations: `uv run alembic upgrade head`. Tests: `uv run pytest`.

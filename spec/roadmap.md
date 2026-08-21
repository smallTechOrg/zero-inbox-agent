# Zero Inbox — Roadmap

## What It Is

Zero Inbox is a production-grade, multi-user Gmail triage agent. A user signs in with
their real Google account, connects Gmail, and presses one button — **"Clean my
inbox"**. The agent labels the mail in their INBOX into a user-editable taxonomy
(Finance, Newsletters, Notifications, Personal, Needs review, …), applies each
category's rule (label only, or label + archive), and streams every action to a live
feed in plain English. Everything the agent does is reversible with **one click per
run**, and every Gmail mutation is recorded in a full audit trail.

## Who Uses It

- The owner (single-tenant hosting today) and a small set of invited Google OAuth
  test users. Real per-user data isolation from day one.

## Product Principles (binding)

1. **INBOX only.** The agent reads and mutates only threads currently in INBOX.
   Archived mail is never touched.
2. **Gmail is never corruptible.** Only additive/reversible mutations (add label,
   remove INBOX label). One click undoes an entire run. All agent-created state
   (labels, decisions) is fully removable.
3. **Bodies never leave the machine.** No email body is ever sent to any LLM.
   Classification uses sender, subject, headers (List-Unsubscribe, Reply-To), Gmail
   signals (category tab, thread size, has-user-replied), and the ~90-char Gmail
   snippet only.
4. **One coherent surface.** A single dashboard — no screen sprawl.
5. **Manual trigger only.** No schedules, ever.
6. **Resumable, never-redo.** A dead run (rate limit, crash) continues exactly where
   it stopped on the next trigger; a per-thread decision index guarantees work
   already done is never redone.
7. **No stalls, no tracebacks.** Hard per-call timeouts; a revoked token yields a
   "reconnect Gmail" prompt, never a raw error.
8. **Very low cost.** Batched cheap-model calls, sender-profile LLM bypass, per-run
   and cumulative cost visibility.

## Success Criteria

- [ ] A new OAuth test user can sign in, connect Gmail, see a mini-audit, adjust the
      default taxonomy, and clean a 50-thread chunk — first try, no rough edges.
- [ ] Undoing a run restores every affected thread's labels and INBOX state exactly.
- [ ] Zero email bodies appear in any LLM request payload (asserted by test).
- [ ] A run killed mid-chunk resumes without re-deciding any already-decided thread.
- [ ] An NVIDIA outage mid-run is invisible except as a fallback event in the feed;
      the run completes on Gemini and returns to NVIDIA when it recovers.
- [ ] Every mutation appears in the ledger with timestamp, reason, and run ID.

## Out of Scope

- Any schedule, cron, autopilot, or background trigger.
- Deep-read escalation, body-content classification, or reply drafting.
- Digest emails, chat-to-rules, proactive assistance, clustering UI, per-thread
  (sub-run) undo granularity, non-Gmail channels, mobile apps.
- Multi-workspace / Google Workspace admin features.

## Phases of Development

### Phase 1 — First clean chunk, first-time-right

**Goal:** Sign in with Google → connect Gmail → fast observable mini-audit → default
taxonomy shown and editable → trigger ONE 50-thread cleaning chunk (newest first)
with a live activity feed → a run card with working whole-run undo. Frontend is the
visually complete single dashboard; ledger search, cost dashboard, and sender-profile
panels are present as clearly-labelled **NON-FUNCTIONAL — coming in Phase 2** stubs.

**Capabilities:** [google-signin](capabilities/google-signin.md),
[inbox-audit](capabilities/inbox-audit.md),
[taxonomy-management](capabilities/taxonomy-management.md),
[triage-run](capabilities/triage-run.md),
[live-activity-feed](capabilities/live-activity-feed.md),
[run-undo-audit](capabilities/run-undo-audit.md)

**Independent slices** (disjoint paths; dependencies marked):

| Slice | Owns | Depends on |
|---|---|---|
| `db-and-domain` | `src/db/models.py`, `src/db/session.py`, `src/domain/` | — |
| `auth-gmail` | `src/api/auth.py`, `src/api/session.py`, `src/channels/gmail/` (oauth, client, mutations, normalize), `src/security/crypto.py` | `db-and-domain` |
| `llm-provider` | `src/llm/` (NVIDIA client, Gemini fallback, timeouts, cost accounting) | — |
| `taxonomy-api` | `src/api/taxonomy.py`, `src/db/seed.py` | `db-and-domain` |
| `triage-graph` | `src/graph/` (state, nodes, edges, agent, runner), `src/prompts/classify.md` | `db-and-domain`, `llm-provider` (interfaces only — build against contracts in spec/agent.md, integrate at gate) |
| `runs-undo-api` | `src/api/runs.py`, `src/api/audit.py`, `src/tools/undo.py`, `src/api/events.py` (SSE), `src/events/` | `db-and-domain` |
| `frontend-dashboard` | `frontend/src/app/`, `frontend/src/components/`, `frontend/src/lib/` | — (builds against spec/api.md contracts) |
| `e2e-and-gate-tests` | `tests/unit/`, `tests/integration/`, `tests/e2e/`, `tests/conftest.py` (test-isolation guard) | contracts only |

**Gate (exact, real keys from `.env`, live-Gmail-safe via test-isolation guard):**

```
uv run pytest tests/unit tests/integration -q && pnpm --dir frontend build && pnpm --dir frontend exec playwright test tests/e2e/phase1
```

**How the user tests it:** `uv run python -m src` (backend, port 8001) and
`pnpm --dir frontend dev` (port 3000). Open http://localhost:3000, click "Sign in
with Google", approve Gmail access, watch the mini-audit counts appear, tweak a
category name, press **Clean my inbox**. Expect: a live feed streaming one sentence
per action with expandable reasoning and a cost ticker; on completion a run card with
per-category counts and an **Undo this run** button; clicking it restores Gmail
exactly (verify in Gmail: labels removed, archived threads back in INBOX). The
Ledger, Costs, and Sender profiles panels are visible but labelled "Coming in
Phase 2 — not yet functional" — that is by design, not a bug.

### Phase 2 — Drive to inbox zero, profiles, ledger, costs

**Goal:** Chew through the whole backlog chunk-by-chunk toward inbox zero, with
repeat senders bypassing the LLM, a searchable ledger, deeper per-category rules,
and full cost dashboards — wiring every Phase 1 stub into a real feature.

**Capabilities:** [backlog-drive](capabilities/backlog-drive.md),
[sender-profiles](capabilities/sender-profiles.md),
[ledger-search](capabilities/ledger-search.md),
[category-rules](capabilities/category-rules.md),
[cost-dashboard](capabilities/cost-dashboard.md)

**Independent slices:**

| Slice | Owns | Depends on |
|---|---|---|
| `backlog-and-profiles` | `src/graph/` additions (progress node, profile-match node), `src/tools/profiles.py`, `src/api/profiles.py` | — |
| `ledger-api` | `src/api/ledger.py` | — |
| `rules-depth` | taxonomy rule engine additions in `src/tools/rules.py`, `src/api/taxonomy.py` merge/rename semantics | — |
| `costs-api` | `src/api/costs.py` | — |
| `frontend-phase2` | dashboard panels replacing the labelled stubs (ledger search, cost cards, profiles, inbox-zero progress) | backend slices' API contracts (spec/api.md, fixed up front) |
| `phase2-tests` | `tests/*/phase2`, `tests/e2e/phase2` | contracts only |

**Gate:**

```
uv run pytest tests/unit tests/integration -q && pnpm --dir frontend exec playwright test tests/e2e/phase2
```

**How the user tests it:** press "Clean my inbox" repeatedly; watch the inbox-zero
progress bar fall chunk by chunk (50–100 threads each, newest first), adjust the
taxonomy between chunks and see the agent adapt; search the ledger for a sender and
see decision/reason/undo state per email; open Costs and see per-run cards (calls,
tokens, est. cost, fallback events) plus cumulative totals; confirm a known repeat
sender (e.g. GitHub) is filed with "sender profile — no LLM call" in the feed.

### Phase 3 — Deploy

**Goal:** `Dockerfile` + `docker-compose.yml` + `docs/deploy.md` for a cheap VPS,
including the documented SQLite→Postgres migration path. No new product capability.

**Slices:** `docker` (Dockerfile, compose, .dockerignore), `deploy-docs`
(`docs/deploy.md`, `docs/postgres-migration.md`) — both independent.

**Gate:**

```
docker compose up -d --build && curl -fsS http://localhost:8001/api/health && pnpm --dir frontend exec playwright test tests/e2e/phase1
```

**How the user tests it:** follow `docs/deploy.md` verbatim on a clean machine; the
full Phase 1 journey works inside the container.

# Zero Inbox Agent — Roadmap

## What It Is

Zero Inbox Agent reads a user's Gmail, categorizes every thread, and keeps the inbox at zero by
archiving and labelling the noise and proposing persistent Gmail filter rules — so only genuinely
relevant mail stays visible.

Decisions are made at **thread level** (never per message) and presented in **clusters** (e.g.
"142 threads from Substack newsletters"), so a large inbox becomes ~30 decisions instead of
thousands.

## Who Uses It

A founder-type with a busy personal Gmail. v1 targets one such user, but the system is
**multi-tenant from day one**: per-user authentication, per-user mailbox connection and token
storage, strict per-user data isolation, and per-user taxonomy, rules, memory and profile. Shared
**starter rule-pack templates** ("founder", "engineer", "recruiter") can be adopted and then edited
per user.

## The Overriding Guardrail

**An important email must never be missed.** Correctness on real mail outranks feature count. Any
trade-off between "hide more" and "risk hiding something important" resolves toward keeping the mail
visible. Three mechanisms enforce this and **all three must be live before any phase performs a real
Gmail mutation**:

1. **Second-pass reviewer** — a separate LLM pass auditing everything marked for archive, looking
   only for false negatives.
2. **Confidence floor** — below the floor the agent never archives; the thread stays in the inbox and
   enters a "needs your call" queue.
3. **Reply-history signal** — anyone the user has ever replied to is important unless explicitly
   overridden.

Anything time-sensitive (deadlines, invoices, legal, security alerts) errs heavily toward staying
visible.

## Safety Invariants (apply to every phase, forever)

- **Never delete anything, ever.** Archive + label only. The Gmail `trash` and `delete` operations are
  never called.
- **Nothing acts without approval** until the user explicitly promotes a rule to automatic.
- **Full undo** for every mutation the agent performs.
- **Dry-run mode** — a new rule simulates and shows exactly what *would* be archived.
- **Complete audit trail** — every decision (reasoning + confidence + which rule fired), every mailbox
  mutation (parameters + undo token), and every user correction (as a training signal).
- **Privacy** — by default only headers, subject and a redacted ~200-character snippet leave the
  machine; full-body escalation happens only for unsure threads. Obvious secrets (OTPs, API keys,
  card numbers, passwords) are redacted **before anything leaves the machine**. **Email bodies are
  never persisted to the database** — only headers, identifiers, decisions and reasoning are stored.

## Interaction Modes

| Mode | What the user does |
|------|--------------------|
| **Triage queue** | Sweeps clusters, approving/rejecting individually or bulk-approving a whole cluster |
| **Rules-first** | Reviews *proposed filter rules* instead of individual threads |
| **Chat** | Types plain English ("stop showing me GitHub notifications unless I'm mentioned") and it becomes a rule |
| **Daily digest** | Sees what was hidden, so nothing vanishes silently |

New mail is triaged continuously. The **historical backlog cleanup** is an explicit user-launched job
that proceeds in dated chunks, is cancellable, and resumes without redoing work.

## Capabilities

See [`capabilities/index.md`](capabilities/index.md) for the full list and phase mapping.

## Success Criteria

- [ ] A user connects their own Gmail through a full OAuth web flow and sees their real recent threads.
- [ ] Every triaged thread carries a category, a confidence score, human-readable reasoning, and an
      explicit indicator of **which tier decided it** (deterministic rule / learned sender history /
      LLM / second-pass reviewer).
- [ ] Threads are grouped so that ~200 threads collapse to ≤ 40 cluster decisions.
- [ ] Zero threads whose sender the user has previously replied to are proposed for archive without an
      explicit override.
- [ ] No mailbox mutation occurs without either explicit approval or a user-promoted automatic rule.
- [ ] Every mutation has a recorded undo token and can be undone from the dashboard.
- [ ] No email body text is present in any database table (verified by an automated test).
- [ ] The majority of threads in a typical run are resolved by tiers 1–2 without an LLM call, and the
      dashboard reports the rules-vs-LLM ratio and the run's spend.

## Out of Scope (v1)

- Any destructive operation (delete/trash/spam-report) — permanently out of scope, not deferred.
- Sending mail automatically. Drafts are created and left for the user to send.
- Non-Gmail channels. The core is channel-agnostic and Gmail is the first adapter, but no second
  adapter ships in v1.
- Calendar, contacts, or Drive integration.
- Mobile app / native clients. Web dashboard only.
- Team/shared inboxes and delegated access.
- Public multi-user signup, billing, or org administration (the system is multi-tenant *technically*;
  onboarding is invite/self-OAuth only).
- Attachment content analysis.
- Search over historical mail as a user-facing feature.

> **Assumed:** Authentication for the dashboard is Google OAuth itself — completing the Gmail OAuth
> flow both connects the mailbox and establishes the user session. There is no separate
> username/password system in v1.

> **Assumed:** "Never delete" is interpreted to also forbid marking as spam and moving to trash;
> only `archive` (remove `INBOX` label) and `addLabels` are permitted mutations, plus filter and
> draft creation.

---

## Phases of Development

Four phases: one first-win phase and three requirements phases.

---

### Phase 1 — Connect Gmail + Dry-Run Clustered Triage

**Goal.** The user clicks "Connect Gmail", completes the real Google OAuth flow, launches a triage
run over their most recent ~200 inbox threads, and sees those threads in the dashboard as a
**clustered triage queue** — each cluster and thread carrying category, confidence, reasoning and
which-tier-fired — under an unmissable **"DRY RUN — nothing in your Gmail has been changed"** banner.

**Phase 1 writes NOTHING back to Gmail.** The Gmail adapter in Phase 1 exposes read operations only;
the mutation methods exist but raise `DryRunViolation` if called. Approving/rejecting in the queue
records the user's intent in the database and nothing else.

Capabilities delivered real: [gmail-connection](capabilities/gmail-connection.md),
[thread-ingestion](capabilities/thread-ingestion.md),
[cost-tiered-triage](capabilities/cost-tiered-triage.md),
[thread-clustering](capabilities/thread-clustering.md),
[triage-queue-review](capabilities/triage-queue-review.md),
[decision-audit-trail](capabilities/decision-audit-trail.md) (record-only half).

**Labelled non-functional stubs in Phase 1** (visible, greyed, each carrying a `COMING SOON` chip and
a tooltip naming its phase — none can read as a bug): Rules view, Chat view, Daily digest view,
Backlog cleanup launcher, VIP list editor, Priorities profile editor, Cost panel, Model dropdown,
Undo button, "Create Gmail filter" button, "Draft reply" button, Unsubscribe suggestions, Stale
threads.

#### Slices

All Phase 1 slices are **independent** and own disjoint paths. Cross-slice interfaces (DB schema,
API request/response shapes, function signatures) are fully specified in
[`data.md`](data.md), [`api.md`](api.md), [`architecture.md`](architecture.md) and
[`agent.md`](agent.md) — slices code against the spec contract, not against each other's output, so
all seven can be generated concurrently.

| # | Slice | Owns (disjoint paths) | Depends on |
|---|-------|----------------------|-----------|
| 1 | `db-schema` | `src/db/models.py`, `src/domain/*.py`, `alembic/`, `tests/unit/db/`, `tests/unit/domain/` | none |
| 2 | `llm-provider` | `src/llm/providers/nvidia.py`, `src/llm/providers/base.py`, `src/llm/client.py`, `src/config/settings.py`, `tests/unit/llm/`, `tests/integration/test_llm_provider.py` | none |
| 3 | `gmail-adapter` | `src/channels/**` (`base.py`, `gmail/`), `src/api/auth.py`, `src/security/crypto.py`, `tests/unit/channels/`, `tests/integration/test_gmail_adapter.py` | none |
| 4 | `triage-graph` | `src/graph/**`, `src/tools/rules.py`, `src/tools/redact.py`, `src/tools/clustering.py`, `src/prompts/*.md`, `tests/unit/graph/`, `tests/unit/tools/`, `tests/integration/test_triage_pipeline.py` | none |
| 5 | `api-routes` | `src/api/__init__.py`, `src/api/session.py`, `src/api/connections.py`, `src/api/triage.py`, `src/api/runs.py`, `src/api/_common.py`, `src/__main__.py`, `tests/unit/api/` | none |
| 6 | `frontend` | `frontend/**` | none |
| 7 | `observability-e2e` | `src/observability/**`, `tests/e2e/**`, `playwright.config.ts`, `README.md` | none |

#### Gate (exact commands, run from the repo root, real APIs via `.env`)

```bash
uv run alembic upgrade head && uv run alembic current
uv run pytest tests/unit tests/integration -q
cd frontend && pnpm install && pnpm build && cd ..
uv run python -m src &            # serves http://localhost:8001
npx playwright test tests/e2e/ --reporter=line
```

All must exit 0. `alembic current` must print a revision hash, not blank. The pytest run includes
`tests/integration/test_triage_pipeline.py`, which triages a **fixture of 220 real-shaped threads**
against the **real NVIDIA NIM endpoint** using the key from `.env` — large enough that a sampled
result and a full-data result are observably different (the test asserts every one of the 220 threads
has a persisted decision and that cluster item-counts sum to 220). `tests/integration/test_gmail_adapter.py`
runs against the **real Gmail API** using the stored refresh token and skips (BLOCKED, not passed) if
no mailbox is connected. Also asserted by the gate: no `Item`, `Decision` or log row contains a body
field (`tests/integration/test_no_body_persisted.py`).

#### How the user tests it

1. `uv run alembic upgrade head`, then `cd frontend && pnpm build && cd .. && uv run python -m src`.
2. Open **http://localhost:8001/app/** (note the port, the `/app/`, and the trailing slash).
3. Click **Connect Gmail** → real Google consent screen → approve → returns to the dashboard showing
   the connected address.
4. Click **Run triage (200 threads)** → a live progress bar counts threads as they are decided.
5. Sweep the clustered triage queue: expand a cluster to see its threads, expand a thread to read the
   full reasoning and see the **which-tier-fired** badge (RULE / SENDER HISTORY / LLM). Approve or
   reject a cluster.
6. Confirm the red **"DRY RUN — nothing in your Gmail has been changed"** banner is pinned at the top,
   and confirm in Gmail itself that nothing moved.
7. **Real in Phase 1:** connect, sync, triage, clusters, confidence, reasoning, tier badge, needs-your-call
   bucket, progress bar, approve/reject (recorded only). **Labelled stubs:** every item in the stub list
   above — greyed with a `COMING SOON` chip.

---

### Phase 2 — Never-Miss Safeguards + Real Gmail Actions + Taxonomy Labels + Memory

**Goal.** The user turns dry-run off with confidence: the three never-miss mechanisms are live, the
taxonomy materialises 1:1 as real Gmail labels, approved decisions really archive and label mail, and
every action is undoable in one click. The agent starts learning from every correction.

Capabilities: [never-miss-safeguards](capabilities/never-miss-safeguards.md),
[taxonomy-management](capabilities/taxonomy-management.md),
[gmail-actions-and-undo](capabilities/gmail-actions-and-undo.md),
[user-memory](capabilities/user-memory.md),
[decision-audit-trail](capabilities/decision-audit-trail.md) (completed: mutation log + undo tokens +
corrections as training signal).

**Ordering guarantee:** the never-miss slice is a declared dependency of the mutation slice — no real
Gmail write ships before the reviewer, the floor and the reply-history signal are live and tested.

#### Slices

| # | Slice | Owns (disjoint paths) | Depends on |
|---|-------|----------------------|-----------|
| 1 | `never-miss` | `src/graph/nodes_review.py`, `src/tools/never_miss.py`, `src/prompts/reviewer.md`, `src/graph/agent.py` (rewire), `tests/unit/graph/test_never_miss.py`, `tests/integration/test_never_miss.py` | none |
| 2 | `taxonomy` | `src/tools/taxonomy.py`, `src/api/categories.py`, `src/channels/gmail/labels.py`, `tests/unit/tools/test_taxonomy.py` | none |
| 3 | `memory` | `src/tools/memory.py`, `src/api/memory.py`, `src/db/models.py` (memory tables), `alembic/versions/0002_*.py`, `tests/unit/tools/test_memory.py` | none |
| 4 | `gmail-mutations` | `src/channels/gmail/mutations.py`, `src/tools/actions.py`, `src/api/actions.py`, `tests/integration/test_gmail_mutations.py` | **slice 1** (never-miss must be live), **slice 2** (labels must exist) |
| 5 | `frontend-phase2` | `frontend/src/app/**` (queue actions, undo, VIP editor, profile editor, settings) | none |
| 6 | `e2e-phase2` | `tests/e2e/phase2/**` | none |

#### Gate

```bash
uv run alembic upgrade head && uv run alembic current
uv run pytest tests/unit tests/integration -q
cd frontend && pnpm build && cd .. && uv run python -m src &
npx playwright test tests/e2e/ --reporter=line
```

`tests/integration/test_gmail_mutations.py` runs against the **real Gmail API**: it archives a
single agent-created test thread, asserts the `INBOX` label is gone and the category label is
present, then calls undo and asserts the thread is back in the inbox with the label removed, and
asserts an `ActionLog` row with a non-null undo token exists for each mutation.
`tests/integration/test_never_miss.py` runs the full 220-thread fixture and asserts (a) no thread from
an ever-replied sender is proposed for archive, (b) every archive proposal below the confidence floor
landed in `needs_your_call`, and (c) the reviewer flipped at least the seeded false-negative bait
thread back to keep.

#### How the user tests it

Run a triage pass, open Settings and set the auto-act threshold, then approve a cluster with dry-run
**off**. Check Gmail: those threads are archived and carry the matching label; nothing is in Trash.
Click **Undo** on the action-log row and confirm the threads reappear in the inbox. Un-archive
something yourself, return to the dashboard, and see the correction recorded and the sender's
importance raised. Add a VIP entry and write the priorities profile; re-run triage and see VIP mail
kept. **Stubs remaining:** Rules view, Chat, Digest, Backlog job, Cost panel, Model dropdown,
Unsubscribe suggestions, Stale threads, Draft replies.

---

### Phase 3 — Autopilot & Background Visibility

**Goal.** After connecting Gmail, triage starts automatically. The user sees a compact run summary card and can approve-all in one click. A daily scheduler keeps the inbox at zero with new mail. A live activity feed shows what's happening in the background. The catch-up digest lets the user know what's important without opening Gmail. The taxonomy editor is now real (D10 fix).

Capabilities: [autopilot-and-digest](capabilities/autopilot-and-digest.md), taxonomy editor (D10 fix — included in the same capability file).

#### Slices

| # | Slice | Owns (disjoint paths) | Depends on |
|---|-------|----------------------|-----------|
| 1 | `backend-autopilot` | `src/api/auth.py` (auto-trigger on callback), `src/api/runs.py` (add summary + approve-and-apply endpoints), `src/scheduler.py`, `src/api/digest.py` (`GET /api/digest/latest`), `tests/unit/api/test_autopilot.py`, `tests/integration/test_autopilot.py` | none |
| 2 | `backend-events` | `src/events.py` (in-memory event bus, last-50-per-user ring buffer), `src/api/events.py` (SSE endpoint), `tests/unit/api/test_events.py` | none |
| 3 | `frontend-autopilot` | `frontend/src/components/RunSummary.tsx`, `frontend/src/components/DigestPanel.tsx`, `frontend/src/components/ActivityDrawer.tsx`, `frontend/src/hooks/useEvents.ts`, `frontend/src/app/page.tsx` (wire summary card as landing after run) | none |
| 4 | `frontend-settings-taxonomy` | `frontend/src/components/TaxonomyEditor.tsx`, `frontend/src/components/Settings.tsx` (replace taxonomy `StubPanel` with `TaxonomyEditor`) | none |

Slices 1 and 2 own fully disjoint backend paths. Slices 3 and 4 own fully disjoint frontend paths. All four can run concurrently.

#### Gate

```bash
uv run alembic upgrade head && uv run alembic current
uv run pytest tests/unit tests/integration -q
cd frontend && pnpm build && cd .. && uv run python -m src &
npx playwright test tests/e2e/ --reporter=line
```

`tests/integration/test_autopilot.py`: connects a mailbox, asserts a `Run` row enters `started` state within 5 s of the OAuth callback (auto-trigger), polls until the run is `completed`, calls `GET /api/runs/{run_id}/summary` and asserts all fields are present and `total_threads > 0`, calls `POST /api/runs/{run_id}/approve-and-apply` and asserts `applied + skipped_keep + skipped_needs_your_call == non-needs_your_call decision count` and `applied >= 0` (real Gmail mutation; test marks as SKIPPED if dry_run forced), calls `GET /api/digest/latest` and asserts the response matches the expected schema. `tests/unit/api/test_events.py`: publishes three synthetic events to the in-memory bus for a test user and asserts the SSE stream yields them in insertion order within 1 s.

#### How the user tests it

1. Log out (or open a fresh session) and click **Connect Gmail** — within a few seconds a triage run starts automatically with no button click. The Activity drawer (bell icon) opens and shows a `run_started` event.
2. When the run completes, the **Run Summary card** appears as the primary landing: N threads across K categories, cost, needs-your-call badge, and the top-3 clusters.
3. Click **Approve all & Apply** — Gmail is mutated, a toast shows "Applied N changes · Undo".
4. Open the **Digest** tab — see what was auto-archived and what needs attention (`time_sensitive_kept`, `vip_mail`, `needs_your_call`, `auto_archived` breakdown).
5. Open the **Activity drawer** — see the timestamped event feed for the completed run.
6. Open **Settings → Taxonomy**: rename a category inline, change its default action, drag to reorder. No page refresh needed; changes persist.
7. To verify the daily scheduler: check that a `next_run_at` row is written to the DB on startup. In test mode, set `settings.scheduler_run_at = "now"` to trigger immediately.

**Real in Phase 3:** auto-trigger, run summary card, approve-all + apply, digest tab, activity drawer, taxonomy editor. **Labelled stubs remaining:** Rules view, Chat view, Backlog cleanup, Cost panel, Model dropdown, Unsubscribe suggestions, Stale threads, Draft replies.

---

### Phase 4 — Rules, Chat, Digest, Backlog & Proactive Assistance

**Goal.** The user stops reviewing individual threads: they review *rules*. Plain English becomes a
rule, mined patterns become one-click filters covering hundreds of threads, the backlog is cleaned in
resumable dated chunks, a daily digest guarantees nothing vanishes silently, and the cost panel makes
spend and the rules-vs-LLM ratio visible.

Capabilities: [rule-proposals](capabilities/rule-proposals.md),
[chat-to-rules](capabilities/chat-to-rules.md),
[daily-digest](capabilities/daily-digest.md),
[backlog-cleanup](capabilities/backlog-cleanup.md),
[cost-and-model-controls](capabilities/cost-and-model-controls.md),
[proactive-assistance](capabilities/proactive-assistance.md).

#### Slices

| # | Slice | Owns (disjoint paths) | Depends on |
|---|-------|----------------------|-----------|
| 1 | `rule-mining` | `src/tools/rule_mining.py`, `src/api/rules.py`, `src/channels/gmail/filters.py`, `src/prompts/rule_proposal.md`, `tests/integration/test_rule_mining.py` | none |
| 2 | `chat-to-rules` | `src/graph/chat_graph.py`, `src/api/chat.py`, `src/prompts/chat_rules.md`, `tests/integration/test_chat_to_rules.py` | none |
| 3 | `digest` | `src/tools/digest.py`, `src/api/digest.py`, `src/prompts/digest.md`, `tests/integration/test_digest.py` | none |
| 4 | `backlog-job` | `src/jobs/backlog.py`, `src/api/jobs.py`, `tests/integration/test_backlog_job.py` | none |
| 5 | `cost-and-models` | `src/tools/cost.py`, `src/api/cost.py`, `src/llm/models_catalog.py`, `tests/unit/tools/test_cost.py` | none |
| 6 | `proactive` | `src/tools/proactive.py`, `src/tools/drafts.py`, `src/api/proactive.py`, `src/prompts/draft_reply.md`, `tests/integration/test_proactive.py` | none |
| 7 | `frontend-phase3` | `frontend/src/app/rules/**`, `frontend/src/app/chat/**`, `frontend/src/app/digest/**`, `frontend/src/app/backlog/**`, `frontend/src/app/cost/**` | none |
| 8 | `e2e-phase3` | `tests/e2e/phase3/**` | none |

#### Gate

```bash
uv run alembic upgrade head && uv run alembic current
uv run pytest tests/unit tests/integration -q
cd frontend && pnpm build && cd .. && uv run python -m src &
npx playwright test tests/e2e/ --reporter=line
```

`tests/integration/test_rule_mining.py` runs against the full 220-thread fixture and asserts at least
one mined rule covers ≥ 20 threads and that its dry-run preview enumerates exactly the threads it
would archive. `tests/integration/test_chat_to_rules.py` sends three plain-English instructions to the
**real** NVIDIA NIM endpoint and asserts each produces a valid, schema-conformant rule and that a
follow-up turn ("actually make that only for weekends") correctly amends the rule produced in the
previous turn — proving conversation memory. `tests/integration/test_backlog_job.py` starts a backlog
job over the fixture, cancels it mid-run, restarts it, and asserts no thread is decided twice and all
220 finish decided.

#### How the user tests it

Open **Rules** and approve a mined rule covering hundreds of threads; use **Preview (dry run)** first
to see exactly what it would archive. Type "stop showing me GitHub notifications unless I'm mentioned"
in **Chat**, then follow up with "actually keep the ones about the api repo" and watch the rule amend
rather than restart. Launch the **Backlog cleanup** job, watch chunked progress stream, cancel it,
restart it, confirm nothing is redone. Open **Digest** to see what was hidden. Open **Cost** to see
run spend, monthly total, and the rules-vs-LLM ratio, and switch the model in the dropdown. Everything
is now real — there are no stubs left.

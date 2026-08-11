# Zero Inbox Agent — Roadmap

## What It Is

Zero Inbox Agent reads a user's Gmail, categorizes every thread, and keeps the inbox at zero by
archiving and labelling the noise autonomously — so only genuinely relevant mail stays visible.
Mistakes are corrected by undo. Persistent Gmail filter rules can be promoted from mined patterns.

Decisions are made at **thread level** (never per message) and presented in **clusters** (e.g.
"142 threads from Substack newsletters"), so a large inbox becomes ~30 cluster groups instead of
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
2. **Confidence floor** — below the floor the agent never archives; the thread is auto-kept and
   surfaced in the digest's "auto-kept (low confidence)" section.
3. **Reply-history signal** — anyone the user has ever replied to is important unless explicitly
   overridden.

Anything time-sensitive (deadlines, invoices, legal, security alerts) errs heavily toward staying
visible.

## Safety Invariants (apply to every phase, forever)

- **Never delete anything, ever.** Archive + label only. The Gmail `trash` and `delete` operations are
  never called.
- **Autonomous triage with full undo.** The agent classifies and applies changes immediately. Human
  control is at the taxonomy/label level (what categories exist and their default actions) and via undo
  (reverse a whole run or a single action). There is no per-decision approval step.
- **needs_your_call is auto-kept.** Any thread whose confidence is below the floor is kept in the
  inbox automatically and never archived without a rule explicitly promoted to `automatic` by the user.
- **Full undo for every mutation.** Before each Gmail mutation the pre-triage label snapshot is stored
  in `ActionLog.undo_token`. Any run or individual action can be reversed from the dashboard.
- **Dry-run mode (debug).** When `settings.dry_run=true` the agent classifies but performs no Gmail
  mutations. Off by default in production; only used for development and testing.
- **Complete audit trail** — every decision (reasoning + confidence + which rule fired), every mailbox
  mutation (parameters + undo token), and every user correction (as a training signal).
- **Privacy** — by default only headers, subject and a redacted ~200-character snippet leave the
  machine; full-body escalation happens only for unsure threads. Obvious secrets (OTPs, API keys,
  card numbers, passwords) are redacted **before anything leaves the machine**. **Email bodies are
  never persisted to the database** — only headers, identifiers, decisions and reasoning are stored.

## Interaction Modes

| Mode | What the user does |
|------|--------------------|
| **Triage history** | Reviews what the agent did — cluster groups with applied actions, confidence, and tier badges — and undoes a run or individual action if needed |
| **Rules-first** | Reviews *proposed filter rules* instead of individual threads |
| **Chat** | Types plain English ("stop showing me GitHub notifications unless I'm mentioned") and it becomes a rule |
| **Daily digest** | Sees what was hidden and what was auto-kept, so nothing vanishes silently |

New mail is triaged continuously. The **historical backlog cleanup** is an explicit user-launched job
that proceeds in dated chunks, is cancellable, and resumes without redoing work.

## Capabilities

See [`capabilities/index.md`](capabilities/index.md) for the full list and phase mapping.

## Success Criteria

- [ ] A user connects their own Gmail through a full OAuth web flow and sees their real recent threads.
- [ ] Every triaged thread carries a category, a confidence score, human-readable reasoning, and an
      explicit indicator of **which tier decided it** (deterministic rule / learned sender history /
      LLM / second-pass reviewer).
- [ ] Threads are grouped so that ~200 threads collapse to ≤ 40 cluster groups.
- [ ] Zero threads whose sender the user has previously replied to are archived without an explicit
      override or a user-promoted automatic rule.
- [ ] After a run completes, all non-keep and non-needs_your_call decisions are automatically applied
      in Gmail; the dashboard shows the applied counts and a single "Undo this run" button.
- [ ] Every mutation has a recorded pre-triage label snapshot and can be undone from the dashboard —
      individually (`POST /api/actions/{id}/undo`) or for the whole run
      (`POST /api/runs/{run_id}/undo`).
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

Five phases: one first-win phase and four requirements phases.

---

### Phase 1 — Connect Gmail + Autonomous Clustered Triage

**Goal.** The user clicks "Connect Gmail", completes the real Google OAuth flow, launches a triage
run over their most recent ~200 inbox threads, and sees the applied results in the dashboard as a
**clustered history view** — each cluster and thread carrying category, confidence, reasoning and
which-tier-fired. The run applies non-keep decisions to Gmail automatically on completion.

**Never-miss safeguards are live before the first real mutation.** The second-pass reviewer, the
confidence floor, and the reply-history signal are all wired in Phase 1 — `needs_your_call` threads
are auto-kept, never archived.

> **Note:** The old Phase 1 constraint of forced `dry_run=true` is removed. Triage is live from
> Phase 1. `dry_run` remains a togglable debug setting (default `false`).

Capabilities delivered real: [gmail-connection](capabilities/gmail-connection.md),
[thread-ingestion](capabilities/thread-ingestion.md),
[cost-tiered-triage](capabilities/cost-tiered-triage.md),
[thread-clustering](capabilities/thread-clustering.md),
[triage-history-view](capabilities/triage-history-view.md),
[decision-audit-trail](capabilities/decision-audit-trail.md) (record-only half).

**Labelled non-functional stubs in Phase 1** (visible, greyed, each carrying a `COMING SOON` chip and
a tooltip naming its phase — none can read as a bug): Rules view, Chat view, Daily digest view,
Backlog cleanup launcher, VIP list editor, Priorities profile editor, Cost panel, Model dropdown,
Per-decision Undo button (real in Phase 2), Run-level Undo button (real in Phase 3),
"Create Gmail filter" button, "Draft reply" button, Unsubscribe suggestions, Stale threads.

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
5. When the run completes, the **Triage History** view shows clusters with applied actions. Expand a
   cluster to see its threads, expand a thread to read the full reasoning and see the **which-tier-fired**
   badge (RULE / SENDER HISTORY / LLM). Confirm threads show `archived` or `kept` or `auto-kept —
   low confidence` applied action labels.
6. Check Gmail itself: threads the agent archived are no longer in the inbox and carry their category
   label. Nothing is in Trash.
7. Confirm the amber **DRY RUN** banner is **not** present (dry_run is off by default).
8. **Real in Phase 1:** connect, sync, triage, auto-apply, clusters, confidence, reasoning, tier badge,
   auto-kept bucket (informational), progress bar, history view. **Labelled stubs:** every item in the
   stub list above — greyed with a `COMING SOON` chip.

---

### Phase 2 — Never-Miss Safeguards + Taxonomy Labels + Memory + Per-Action Undo

**Goal.** The three never-miss mechanisms are confirmed live and tested in isolation. The taxonomy
materialises 1:1 as real Gmail labels. Every applied action is undoable in one click via the audit
log. The agent starts learning from every correction.

> **Note:** The never-miss safeguards are introduced here as an isolated, tested capability — they
> were structurally wired in Phase 1's triage graph but are formally gated and verified in Phase 2.

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
| 5 | `frontend-phase2` | `frontend/src/app/**` (per-action undo button in audit log, VIP editor, profile editor, settings) | none |
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
present, then calls `POST /api/actions/{action_log_id}/undo` and asserts the thread is back in the
inbox with the label removed, and asserts an `ActionLog` row with a non-null undo token exists for
each mutation.
`tests/integration/test_never_miss.py` runs the full 220-thread fixture and asserts (a) no thread from
an ever-replied sender is archived, (b) every archive decision below the confidence floor was
auto-kept instead, and (c) the reviewer flipped at least the seeded false-negative bait thread back
to keep.

#### How the user tests it

Run a triage pass. Check Gmail: threads the agent archived carry the matching label; nothing is in
Trash. Click **Undo** on an action-log row and confirm the thread reappears in the inbox. Un-archive
something yourself, return to the dashboard, and see the correction recorded and the sender's
importance raised. Add a VIP entry and write the priorities profile; re-run triage and see VIP mail
kept. **Stubs remaining:** Rules view, Chat, Digest, Backlog job, Cost panel, Model dropdown,
Unsubscribe suggestions, Stale threads, Draft replies, Run-level Undo button.

---

### Phase 3 — Autopilot & Background Visibility

**Goal.** After connecting Gmail, triage starts automatically and applies immediately. The user sees
a compact run summary card showing what was done, with a single "Undo this run" button. A daily
scheduler keeps the inbox at zero with new mail. A live activity feed shows what's happening in the
background. The catch-up digest lets the user know what's important without opening Gmail. The
taxonomy editor is now real (D10 fix).

Capabilities: [autopilot-and-digest](capabilities/autopilot-and-digest.md), taxonomy editor
(D10 fix — included in the same capability file).

#### Slices

| # | Slice | Owns (disjoint paths) | Depends on |
|---|-------|----------------------|-----------|
| 1 | `backend-autopilot` | `src/api/auth.py` (auto-trigger on callback), `src/api/runs.py` (add summary + run-undo endpoints), `src/scheduler.py`, `src/api/digest.py` (`GET /api/digest/latest`), `tests/unit/api/test_autopilot.py`, `tests/integration/test_autopilot.py` | none |
| 2 | `backend-events` | `src/events.py` (in-memory event bus, last-50-per-user ring buffer), `src/api/events.py` (SSE endpoint), `tests/unit/api/test_events.py` | none |
| 3 | `frontend-autopilot` | `frontend/src/components/RunSummary.tsx`, `frontend/src/components/DigestPanel.tsx`, `frontend/src/components/ActivityDrawer.tsx`, `frontend/src/hooks/useEvents.ts`, `frontend/src/app/page.tsx` (wire summary card as landing after run) | none |
| 4 | `frontend-settings-taxonomy` | `frontend/src/components/TaxonomyEditor.tsx`, `frontend/src/components/Settings.tsx` (replace taxonomy `StubPanel` with `TaxonomyEditor`) | none |

Slices 1 and 2 own fully disjoint backend paths. Slices 3 and 4 own fully disjoint frontend paths.
All four can run concurrently.

#### Gate

```bash
uv run alembic upgrade head && uv run alembic current
uv run pytest tests/unit tests/integration -q
cd frontend && pnpm build && cd .. && uv run python -m src &
npx playwright test tests/e2e/ --reporter=line
```

`tests/integration/test_autopilot.py`: connects a mailbox, asserts a `Run` row enters `started`
state within 5 s of the OAuth callback (auto-trigger), polls until the run is `completed`, calls
`GET /api/runs/{run_id}/summary` and asserts all fields are present and `total_threads > 0`, asserts
`applied_count + kept_count + auto_kept_count == total_threads`, calls
`POST /api/runs/{run_id}/undo` and asserts `reversed == applied_count` and every
reversed `ActionLog` row has a non-null `undone_at`, calls `GET /api/digest/latest` and asserts the
response matches the expected schema.
`tests/unit/api/test_events.py`: publishes three synthetic events to the in-memory bus for a test
user and asserts the SSE stream yields them in insertion order within 1 s.

#### How the user tests it

1. Log out (or open a fresh session) and click **Connect Gmail** — within a few seconds a triage run
   starts automatically with no button click. The Activity drawer (bell icon) opens and shows a
   `run_started` event. Gmail mutations are applied as the run completes.
2. When the run completes, the **Run Summary card** appears as the primary landing: "N threads
   archived · M kept · K auto-kept (low confidence)", cost, category breakdown, and top-3 clusters.
3. Click **"Undo this run"** — confirm the dialog — and verify Gmail: archived threads are back in
   the inbox, category labels removed. The card updates to show "Run undone".
4. Open the **Digest** tab — see what was auto-archived and what was auto-kept (`time_sensitive_kept`,
   `vip_mail`, `auto_kept_low_confidence`, `auto_archived` breakdown).
5. Open the **Activity drawer** — see the timestamped event feed for the completed run, including the
   `gmail_mutation_applied` events and the final `run_completed` event with the "Undo run" button.
6. Open **Settings → Taxonomy**: rename a category inline, change its default action, drag to reorder.
   No page refresh needed; changes persist.
7. To verify the daily scheduler: check that a `next_run_at` row is written to the DB on startup. In
   test mode, set `settings.scheduler_run_at = "now"` to trigger immediately.

**Real in Phase 3:** auto-trigger, run summary card with "Undo this run", run-level undo, digest tab,
activity drawer, taxonomy editor. **Labelled stubs remaining:** Rules view, Chat view, Backlog
cleanup, Cost panel, Model dropdown, Unsubscribe suggestions, Stale threads, Draft replies.

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

---

### Phase 5 — Triage Transparency

> **Assumed:** The user requested this as "Phase 4 — Triage Transparency" but the roadmap already has a Phase 4. This phase is numbered 5 to preserve existing phase numbering without renaming any prior phase.

**Goal.** Full per-thread visibility of classification and Gmail mutation decisions in the Activity drawer: every classified thread appears in real time with subject, category, action and which tier decided it; every archived thread shows its Gmail label. A shared `SseContext` eliminates the duplicate `EventSource` in `page.tsx`.

Capability: [triage-transparency](capabilities/triage-transparency.md).

#### Slices

| # | Slice | Owns (disjoint paths) | Depends on |
|---|-------|----------------------|-----------|
| A | `backend-transparency` | `src/graph/nodes.py` (emit `thread_classified` after each tier decision), `src/tools/actions.py` (emit `thread_archived` inside `apply_decision`), `tests/unit/graph/test_triage_transparency.py` | none |
| B | `frontend-transparency` | `frontend/src/lib/SseContext.tsx` (new shared context + single `EventSource`), `frontend/src/lib/types.ts` (extend `SseEventType` union), `frontend/src/components/ActivityDrawer.tsx` (render new event types, use `SseContext.addEvent`), `frontend/src/app/page.tsx` (remove local `EventSource`, read from `SseContext`) | none |

Slices A and B are fully independent — they own disjoint file paths and can be generated concurrently.

#### Gate

```bash
uv run pytest tests/unit/ -q
cd /Users/sai/Workspace/Code/zero-inbox-agent/frontend && pnpm build
```

Both commands must exit 0. `tests/unit/graph/test_triage_transparency.py` asserts:
- `thread_classified` is emitted once per thread with all required fields at the correct tier decision point.
- `thread_archived` is emitted exactly once per successful archive mutation inside `apply_decision`.
- Monkeypatching `bus.publish` to raise does not cause the test run to record a failed triage decision or a failed mutation — the exception is caught and the run proceeds.
- Both event shapes match their documented JSON schemas (field names and types).
`pnpm build` asserts zero TypeScript errors after the `SseEventType` union extension.

#### How the user tests it

1. Start the server (`uv run python -m src`) and the frontend (`cd frontend && pnpm dev`).
2. Open the dashboard and click **Run triage**.
3. Open the **Activity drawer** (bell icon) while the run is in progress.
4. Watch per-thread events arrive in real time: each classified thread shows `"[tier] subject → category (action, confidence%)"`.
5. After the run, scroll the drawer: every thread that was archived shows `"Archived: subject → label_name"`.
6. Open browser DevTools → Network → EventSource: confirm there is **exactly one** SSE connection (no duplicate), confirming the `SseContext` consolidation.

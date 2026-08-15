# Zero Inbox Agent — Roadmap

## What It Is

Zero Inbox Agent reads a user's Gmail, categorizes every thread, and keeps the inbox at zero by
archiving and labelling the noise autonomously — so only genuinely relevant mail stays visible.
Mistakes are corrected by undo. Persistent Gmail filter rules can be promoted from mined patterns.

Decisions are made at **thread level** (never per message) and presented in **clusters** (e.g.
"142 threads from Substack newsletters"), so a large inbox becomes ~30 cluster groups instead of
thousands.

**Inbox zero is the goal, not triage.** A run ends with the inbox actually reduced and reports the
exact remainder by reason. The written definition of "zero" — which threads leave, which stay and why —
lives in
[drive-to-inbox-zero](capabilities/drive-to-inbox-zero.md#the-inbox-zero-definition-written-down-and-stated-in-the-ui)
and is stated verbatim in the UI.

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

> **Phase 9 — what "never miss" means, redefined once, deliberately, by the user.** Until Phase 9 a
> never-miss verdict meant *leave the thread in the inbox*, which put a hard 365-thread floor under a
> product whose whole promise is zero. From Phase 9 a never-miss verdict means **archive the thread
> and attach its never-miss label** — `ZeroInbox/Urgent`, `ZeroInbox/Important`, `ZeroInbox/People` —
> so it is one click away in the Gmail sidebar, marked with the reason it was held, and undoable
> per thread and in bulk. **The safety guarantee is preserved by labelling and recoverability, not by
> inbox residency.** Nothing is ever deleted, and no never-miss thread is ever archived without its
> label. This is the **one** safety rule the user has chosen to redefine, explicitly and in writing,
> after seeing the measured numbers. Every other invariant below is unchanged and absolute, and this
> precedent does not extend to any of them. Full reasoning and evidence:
> [never-miss-safeguards](capabilities/never-miss-safeguards.md#phase-9-the-never-miss-semantic-is-redefined--from-hold-to-label).

## Safety Invariants (apply to every phase, forever)

- **Never delete anything, ever.** Archive + label only. The Gmail `trash` and `delete` operations are
  never called.
- **Autonomous triage with full undo.** The agent classifies and applies changes immediately. Human
  control is at the taxonomy/label level (what categories exist and their default actions) and via undo
  (reverse a whole run or a single action). There is no per-decision approval step.
- **needs_your_call is auto-kept.** Any thread whose confidence is below the floor is kept in the
  inbox automatically and never archived without a rule explicitly promoted to `automatic` by the user.
- **Full undo for every mutation.** Before each Gmail mutation the pre-triage label snapshot is stored
  in `ActionLog.undo_token`. Any run or individual action can be reversed from the dashboard. **From
  Phase 9 a whole re-organisation of the mailbox is reversible as a single bulk-undo operation** —
  a mutation the user cannot reverse in one action is not a shippable mutation.
- **`review_state = "reviewed"` is a claim about a DECISION, not about a run** (Phase 9). Only rows
  the second-pass reviewer actually audited are marked reviewed, and the reviewer audits **every**
  action `apply_decision()` is willing to mutate. `NotReviewedError` must never be able to pass
  vacuously.
- **Dry-run mode (debug).** When `settings.dry_run=true` the agent classifies but performs no Gmail
  mutations. Off by default in production; only used for development and testing.
- **Autonomy goes through the reviewer, never around it.** The agent's decision to act on its own is
  made *before* the never-miss chain (`align_to_category_default`), so every archive it chooses is
  audited by the second-pass reviewer, floored, and vetoable by the VIP and reply-history guards.
  Auto-apply never passes `force=True`, never writes `review_state`, and never archives a
  `keep`-proposed decision. The `force` path stays exactly what it is: a deliberate, user-initiated
  sweep. See [drive-to-inbox-zero](capabilities/drive-to-inbox-zero.md#g-safety-invariants-this-capability-does-not-weaken-binding-restated-because-it-is-easy-to-erode).
- **A run that did not finish its own work says so.** An apply pass that could not run, or that left
  `distance_to_zero > 0`, sets `triage_runs.error_message`, emits `run_apply_failed`, and returns
  `apply_ok = false`. A silent early `return` that looks like a successful run is a defect, not a
  degradation.
- **Durable ≠ final.** Decisions are persisted the moment they are made, but a decision is only
  *final* — visible-as-final and eligible for apply/approve — once the never-miss reviewer has
  upgraded it (`decisions.review_state = "reviewed"`). See
  [durable-resumable-runs](capabilities/durable-resumable-runs.md).
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
- [ ] A completed run leaves `distance_to_zero = 0` — every thread the agent decided should leave the
      inbox has actually left it — and the dashboard states the remainder by reason
      ("N archived · N need your call · N kept by category · N not confident enough · N held by
      VIP/reply history"). A run that archived nothing is impossible to mistake for a success.
- [ ] **(Phase 9)** A completed run leaves `inbox_remaining = 0` **with no human intervention** —
      never-miss mail is archived under its own `ZeroInbox/*` label rather than held in the inbox, is
      one click away, and is undoable per thread and in bulk. Anything that could not be archived is
      named in the ledger with its reason, and "we reached zero" never appears unless the inbox is
      genuinely empty.
- [ ] **(Phase 9)** The user can rebuild the taxonomy from their own mail and re-organise **every**
      past decision — including already-archived threads — resumably, with a live ledger of anything
      skipped and a single bulk undo that reverses the whole re-organisation.

## Out of Scope (v1)

- Any destructive operation (delete/trash/spam-report) — permanently out of scope, not deferred.
- Sending mail automatically. Drafts are created and left for the user to send.
- Non-Gmail channels. The core is channel-agnostic and Gmail is the first adapter, but no second
  adapter ships in v1.
- Calendar, contacts, or Drive integration.
- Mobile app / native clients. Web dashboard only.
- Team/shared inboxes and delegated access.
- Billing, org administration, SSO beyond Google, SCIM, teams/roles/RBAC, an admin console and
  audit-log export. Phase 8 makes self-serve Google sign-up, session management and account lifecycle
  real; everything else on this line stays deferred and is not promised anywhere in the product copy.
  See [account-and-identity](capabilities/account-and-identity.md#what-enterprise-grade-means-here--scoped-honestly).
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

Nine phases: one first-win phase and eight requirements phases.

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

**Goal.** Full per-thread visibility of classification and Gmail mutation decisions **on the main dashboard page, with no drawer opened and nothing clicked** (the Activity drawer holds the full history only): every classified thread appears in real time with subject, category, action and which tier decided it; every archived thread shows its Gmail label. A shared `SseContext` eliminates the duplicate `EventSource` in `page.tsx`.

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

---

### Phase 6 — Durable, Resumable, Transparent Runs

**Goal.** A triage run over a real 2,000+ thread mailbox is never lost. Decisions land in the database
as they are made (in an explicit `provisional`, not-yet-reviewed state), an interrupted run is
resumed in one click without re-classifying a single already-decided thread, the user watches threads
classified one by one with tier and reason while the run proceeds, and a degraded LLM provider is
surfaced and circuit-broken instead of silently stretching a run to 20+ minutes.

**Motivating defect.** Run `fbeed060` decided 2,003 of 2,176 threads in ~23 minutes with 2,774
`llm.retry` events and left **0 rows in `decisions`** — all work lived in LangGraph in-memory state.
Root cause: `persist_decisions` writes items, clusters, decisions and `llm_calls` atomically **once at
the very end of the graph**.

**CRITICAL CONSTRAINT — the never-miss guarantee does not regress.** End-of-graph persistence was
deliberate: nothing is shown as final or actioned before the second-pass reviewer can flip a
false-negative `archive` back to `keep`. Incremental persistence separates **durability** from
**finality** via `decisions.review_state`: rows land `provisional`, the reviewer upgrades them to
`reviewed`, and **only `reviewed` rows are eligible for apply/approve** — enforced in
`apply_decision()` before the mutator is called, not bypassable by `force=True`. See
[never-miss-safeguards](capabilities/never-miss-safeguards.md).

Capabilities: [durable-resumable-runs](capabilities/durable-resumable-runs.md) (new),
[never-miss-safeguards](capabilities/never-miss-safeguards.md) (extended: `review_state` gate),
[triage-transparency](capabilities/triage-transparency.md) (extended: coverage + provisional
labelling + degraded banner), [decision-audit-trail](capabilities/decision-audit-trail.md)
(extended: audit survives interruption).

No new provider, no new keys, **no change to the default/primary model**. The existing 120 s LLM
timeout and the `finish_reason == "length"` re-issue behaviour are unchanged. Slice 3 additionally
adds (a) a **cross-model fallback chain within the Nemotron family** that rotates on persistent
failure of **any** kind including `APIConnectionError`/timeouts — NVIDIA routes per model to separate
backend pools, so one saturated pool does not mean the endpoint is down
([Rule F](capabilities/durable-resumable-runs.md#f-cross-model-fallback-rotate-on-persistent-failure-of-any-kind)),
(b) **process-wide client-side rate limiting** at 350 req/min under the account's 490 req/min ceiling
([Rule G](capabilities/durable-resumable-runs.md#g-global-rate-limiting-process-wide)), and (c) an
**overall never-stuck bound** ([Rule H](capabilities/durable-resumable-runs.md#h-never-get-stuck-hard-requirement)).

#### Slices

Three slices, **fully disjoint file ownership** — all three generate concurrently. Where one slice
calls a function another slice writes, it is a **spec-contract dependency only** (the signature is
pinned below); no slice waits on another's output, and both land in the same gate.

| # | Slice | Owns (disjoint paths) | Depends on |
|---|-------|----------------------|-----------|
| 1 | `durable-resume` | `src/graph/checkpoint.py` (new), `src/graph/nodes.py`, `src/graph/nodes_review.py`, `src/graph/persistence.py`, `src/graph/runner.py`, `src/graph/state.py`, `src/graph/agent.py`, `src/db/models.py`, `alembic/versions/0005_decision_review_state.py`, `src/api/runs.py`, `src/api/__init__.py`, `src/tools/actions.py`, `frontend/src/components/ResumeBanner.tsx`, `frontend/src/app/page.tsx`, `tests/unit/graph/test_checkpoint.py`, `tests/unit/tools/test_review_state_guard.py`, `tests/integration/test_resume.py` | none (spec-contract: calls `events.bus.emit_thread_classified()` from slice 2 and `llm.health.circuit_open()` / `ProviderCircuitOpen` from slice 3; registers slice 3's `provider_health.router`) |
| 2 | `live-transparency` | `src/events/bus.py`, `src/events/__init__.py`, `frontend/src/lib/SseContext.tsx`, `frontend/src/lib/types.ts`, `frontend/src/components/ActivityDrawer.tsx`, `frontend/src/components/ThreadFeedRow.tsx` (new), `tests/unit/events/test_thread_classified.py` | none (spec-contract: emits the payloads slice 1 supplies) |
| 3 | `provider-resilience` | `src/llm/health.py` (new), `src/llm/throttle.py` (new), `src/llm/client.py`, `src/llm/providers/nvidia.py`, `src/llm/providers/base.py`, `src/api/provider_health.py` (new), `tests/unit/llm/test_circuit_breaker.py`, `tests/unit/llm/test_model_fallback.py` (new), `tests/unit/llm/test_throttle.py` (new), `tests/unit/llm/test_never_stuck.py` (new), `tests/unit/api/test_provider_health.py` | none (spec-contract: exposes `llm.health.model_chain()` / `current_model()` / `advance_model()`, which slice 1's `_model_candidates` consumes; emits via slice 2's `emit_model_fallback`) |

**Pinned cross-slice contracts** (each slice codes against these, not against another slice's files):

```python
# slice 2 writes, slice 1 calls — one event per decided thread
events.bus.emit_thread_classified(
    user_id: str, *, run_id: str, item_id: str, subject: str, from_email: str,
    category: str, action: str, decided_by: str, confidence: float,
    reasoning: str, review_state: str,
) -> None                      # never raises; wrapped in try/except internally

events.bus.emit_provider_degraded(user_id, *, run_id, provider, model,
                                  calls, retries, consecutive_failures) -> None
events.bus.emit_run_resumable(user_id, *, run_id, items_total, items_decided, reason) -> None
events.bus.emit_model_fallback(user_id, *, run_id, from_model: str, to_model: str,
                               reason: str) -> None   # mid-run model switch, rendered on the main-page feed

# slice 3 writes, slice 1 calls
llm.health.snapshot(run_id: str | None) -> dict   # {provider, model, model_chain, chain_position,
                                                  #  calls, retries, consecutive_failures,
                                                  #  circuit_open, degraded, throttle}
llm.health.reset(run_id: str) -> None             # called by runner on start/resume; also resets the
                                                  # run to chain position 0
llm.health.model_chain(preferred: str | None) -> list[str]
    # the full ordered chain (preferred first if set, de-duplicated)
llm.health.current_model(run_id: str) -> str
    # the model this run must use NOW (chain[chain_position]) — the advance is PER-RUN and STICKS
llm.health.advance_model(run_id: str, *, reason: str) -> str | None
    # move to the next chain entry, return it; None == chain exhausted. Lock-guarded and idempotent:
    # two concurrent batches failing on the same model advance the run ONE step and emit ONE event.
    # slice 1's `src/graph/nodes.py::_model_candidates` returns the chain sliced from the run's
    # current position (first element == current_model(run_id)).
class llm.health.ProviderCircuitOpen(RuntimeError): ...   # raised by the client, NOT retried
api.provider_health.router                        # FastAPI APIRouter, mounted by slice 1
```

##### Slice 1 — `durable-resume`

- New `src/graph/checkpoint.py`: `record_batch(state, decisions, *, tier) -> None` — in its own short
  transaction, upserts the batch's items, inserts the decisions with `review_state="provisional"`
  (skipping any `(run_id, item_id)` that already exists), appends the batch's `llm_calls` rows, bumps
  `items_decided` atomically, and calls `emit_thread_classified` per decision. Wrapped so a checkpoint
  failure logs at WARNING and never kills the run.
- `nodes.py`: tiers 1–4 (`apply_deterministic_rules`, `apply_sender_history`, `llm_classify_batch`,
  `deep_read_escalation`) call `record_batch` instead of the current inline `bus.emit` blocks;
  `persist_decisions` becomes idempotent finalisation (clusters + `review_state` upgrade + counts),
  no longer the first write. `llm_classify_batch` lets `ProviderCircuitOpen` propagate as
  `state["error"]` rather than degrading the batch.
- `nodes.py::_model_candidates(state)` (slice 1 owns the function; slice 3 owns the chain) becomes a
  one-liner returning `llm.health.model_chain((state.get("settings") or {}).get("llm_model"))`
  **sliced from the run's current chain position**, typed `list[str]`, so its first element is
  `llm.health.current_model(run_id)`. The existing candidate loops at the `llm_classify_batch` and
  `deep_read_escalation` call sites **are** the fallback mechanism — no parallel mechanism. They
  advance on **any** persistent failure (including `APIConnectionError` / `APITimeoutError`) once the
  current model's existing retry budget is exhausted, by calling
  `llm.health.advance_model(run_id, reason=...)` and `emit_model_fallback(...)`, then continuing the
  run on the returned model. `advance_model` returning `None` (chain exhausted) is what feeds the
  never-stuck bound ([Rule H](capabilities/durable-resumable-runs.md#h-never-get-stuck-hard-requirement)).
- `nodes_review.py`: after the reviewer + never-miss floor, upgrade rows to `review_state="reviewed"`
  (or `review_failed`) and re-emit `thread_classified` for every flipped thread.
- `runner.py`: `execute_triage` loads `already_decided_item_ids(run_id)` and seeds it into state;
  `fetch_items` filters them out of every downstream queue. `llm.health.reset(run_id)` on start.
- `persistence.py`: `already_decided_item_ids()`, `insert_provisional_decisions()`,
  `upgrade_review_state()`; `update_run` treats `resumable` like `running` (non-terminal, but never
  overwrites `cancelled`).
- `api/__init__.py`: `_reconcile_orphaned_runs()` marks an orphan `resumable` when it has ≥ 1 persisted
  decision, `failed` otherwise; mounts `provider_health.router`.
- `api/runs.py`: `POST /api/runs/{run_id}/resume`; `resumable` + `remaining` on the run payloads.
- `tools/actions.py`: new `NotReviewedError`; `apply_decision()` raises it for
  `review_state != "reviewed"` **before** touching the mutator, independent of `status`, not bypassed
  by `force=True`.
- Frontend: `ResumeBanner.tsx` + mounting it in `page.tsx` (screen 13 in [ui.md](ui.md)).
- **Tests:** `test_checkpoint.py` (rows exist mid-run; a raising checkpoint does not fail the run);
  `test_review_state_guard.py` (`NotReviewedError` for `provisional` and `review_failed`, with
  `status="approved"` and with `force=True`; mutator never called);
  `test_resume.py` (integration, below).

##### Slice 2 — `live-transparency`

- `events/bus.py`: the four typed emit helpers above (incl. `emit_model_fallback`), each internally `try/except Exception` +
  WARNING log, subject truncated to 60 chars and reasoning to 140 chars **inside the helper** so no
  caller can leak more. Ring buffer unchanged.
- `SseContext.tsx` / `types.ts`: extend `SseEventType` with `provider_degraded`, `run_resumable` and
  `model_fallback` (`{run_id, from_model, to_model, reason}`);
  add `reasoning` + `review_state` to `ThreadClassifiedEvent`.
- `ActivityDrawer.tsx` + `ThreadFeedRow.tsx`: per-thread feed keyed by `item_id` (later event for the
  same id **replaces** the earlier row), tier badge, reasoning, `NOT YET REVIEWED` / `REVIEW FAILED`
  chips, degraded-provider banner pinned to the top of the **main page** (auto-opening a drawer does
  not satisfy this — see ui.md screen 14), `run_resumable` row with a
  Resume button, and a `model_fallback` row *"Switched model: A → B (reason)"* (screen 14 in
  [ui.md](ui.md)) so a mid-run model switch is never an invisible backend action.
- **Tests:** `test_thread_classified.py` asserts each helper's emitted dict matches the documented
  JSON schema exactly (field names + types), that subject/reasoning are truncated, that **no body or
  unredacted content field is present**, and that a raising subscriber does not propagate.

##### Slice 3 — `provider-resilience`

- `src/llm/health.py`: per-run counters (`calls`, `retries`, `consecutive_failures`), thread-safe;
  `degraded` when `retries/max(calls,1) >= 1.0` or `retries >= 50`; circuit opens after **5
  consecutive** fully-failed calls and then raises `ProviderCircuitOpen` immediately instead of
  attempting the call; `reset(run_id)` clears it.
- `providers/nvidia.py`: increments the counters at the existing `llm.retry` site and on
  success/terminal failure; emits `provider_degraded` once per 50 further retries. **The 120 s timeout
  and the `finish_reason == "length"` handling are not touched. The model and provider are not
  changed.**
- `src/llm/health.py` also owns the **cross-model fallback chain and failure classification**
  ([Rule F](capabilities/durable-resumable-runs.md#f-cross-model-fallback-rotate-on-persistent-failure-of-any-kind)):
  - `MODEL_CHAIN = ["nvidia/nemotron-3-nano-30b-a3b", "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/nvidia-nemotron-nano-9b-v2"]` — measured live against the real endpoint at 0.56 s / 0.65 s /
    2.03 s. `meta/llama-3.3-70b-instruct` and `openai/gpt-oss-120b` are **excluded**: both timed out at
    45 s, i.e. worse than the primary. The default model does not change.
  - `model_chain(preferred) -> list[str]` — `preferred` first if set, then the chain, de-duplicated.
  - `current_model(run_id) -> str` / `advance_model(run_id, *, reason) -> str | None` — a **per-run**
    chain position that **sticks** for the rest of the run (not per batch, not per tier). Lock-guarded
    and idempotent under concurrency: two batches failing on the same model advance one step and emit
    one `model_fallback`. `None` = chain exhausted.
  - **Rotate on persistent failure of ANY kind.** There is **no** `is_model_specific()` predicate —
    that contract is deleted. `APIConnectionError`, `APITimeoutError`, DNS/TLS/connect failures, and
    every `APIStatusError` status all advance the chain once the current model's existing retry budget
    is exhausted. Requires an inline comment recording *why*: NVIDIA routes per model to separate
    backend pools, so one model's pool can be saturated (connection errors, timeouts) while the others
    answer in under 2 s on the same key and host — measured live during run `fbeed060`.
  - **No mid-run re-probing** of an abandoned model. `reset(run_id)` (start/resume only) returns the
    run to position 0.
- `src/llm/throttle.py` (new): a **process-wide** token/leaky bucket gating every outbound LLM request
  — first attempts, **retries**, and every tier — before it is issued. Per-batch limiting cannot work
  because the graph's `MAX_CONCURRENCY` runs batches in parallel. Account ceiling is **490 req/min**;
  default target **350 req/min** for headroom, configurable via `AGENT_LLM_MAX_RPM`
  (`.env.example` + [architecture.md](architecture.md)). Continuous refill (never a fixed window);
  waiting callers block, and a wait is never counted as a failure or a retry. `snapshot()` exposes
  `{max_rpm, available, waiting}`.
- **Never-stuck bound** ([Rule H](capabilities/durable-resumable-runs.md#h-never-get-stuck-hard-requirement)):
  a per-run wall-clock ceiling `AGENT_RUN_MAX_SECONDS` (default `3600`) **and** a bound of **3**
  consecutive fully-failed batches after `advance_model` has returned `None`. Either bound ends the
  run `resumable` with a human-readable `error_message` and all partial decisions intact.
- `providers/nvidia.py`: `LLMResult.model` must be the model that **actually served** the call
  (`response.model`, falling back to the requested id only when absent) so `llm_calls.model` — and
  therefore cost attribution — names the real model, not the requested one. Attach the failing model
  id to raised `LLMError`s so the caller can classify. `_RETRY_STATUS`, the 120 s timeout and the
  `finish_reason == "length"` handling are untouched.
- `api/provider_health.py`: `GET /api/provider-health`, reporting the **current model**, the ordered
  `model_chain` + `chain_position`, and the `throttle` state alongside the health counters.
- **Tests:** `test_model_fallback.py` (new) — `model_chain` ordering/de-dup and the absence of any
  out-of-family model; a stub failing model 1 with a 404 and succeeding on model 2 yields exactly one
  `model_fallback` emit and an `llm_calls` row whose `model` is model 2; **a stub raising
  `APIConnectionError` on every model-1 call and succeeding on model 2 also rotates** — the run
  completes on model 2, one `model_fallback` naming `APIConnectionError` is emitted, and model 1 is
  never requested again for the rest of the run (the advance sticks per-run); two concurrent failing
  batches advance exactly one position and emit exactly one event.
  `test_throttle.py` (new) — with `AGENT_LLM_MAX_RPM=60` and 200 calls issued from concurrent tasks
  (retries counted), no 60 s window ever exceeds 60 outbound requests, every call eventually
  completes, and no call is dropped or turned into a failure; the bucket is shared process-wide
  (two independent run ids share one budget).
  `test_never_stuck.py` (new) — with **all three** models stubbed to fail every call the run
  terminates as `resumable` well inside a test timeout (never hangs), `error_message` names the
  exhausted chain and the last failure, and the partial decisions are intact; separately, the
  wall-clock ceiling alone ends a run that would otherwise grind.
  `test_circuit_breaker.py` — a stub provider failing every call opens the circuit after
  exactly 5 consecutive failures and the 6th call raises `ProviderCircuitOpen` **without an HTTP
  attempt**; a success resets `consecutive_failures`; `degraded` flips at the documented thresholds;
  the 120 s timeout constant is asserted unchanged. `test_provider_health.py` — endpoint envelope +
  auth scoping.

#### Gate (exact commands, run from the repo root, real APIs via `.env`, production DB driver)

```bash
uv run alembic upgrade head && uv run alembic current
uv run pytest tests/unit tests/integration -q
cd frontend && pnpm install && pnpm build && cd ..
uv run python -m src &
npx playwright test tests/e2e/ --reporter=line
```

All must exit 0; `alembic current` must print a revision hash including
`0005_decision_review_state`.

`tests/integration/test_resume.py` is the load-bearing gate. It runs the **220-thread fixture against
the real NVIDIA NIM endpoint** using the key from `.env` — large enough that a partial result and a
full result are observably different — and asserts:

1. **Durability:** while the run is in flight, `count(decisions where run_id=:id)` is > 0 and strictly
   increasing before `finalize` runs.
2. **Interrupt:** the run is killed after ~40 % of threads are decided; the persisted count matches
   `triage_runs.items_decided` and is strictly between 0 and 220.
3. **Reconcile:** `_reconcile_orphaned_runs()` marks that run `resumable`, not `failed`.
4. **Resume:** `POST /api/runs/{run_id}/resume` finishes the run with **exactly 220** decision rows,
   **zero** duplicate `(run_id, item_id)` pairs, and **strictly fewer `llm_calls` rows added during
   the resume than a from-zero run makes** — proving already-decided threads were skipped, not
   re-classified. `cost_usd` is additive, never reset.
5. **Never-miss not regressed:** every row ends `review_state="reviewed"`; a variant of the run with
   the reviewer forced to fail leaves every row `review_failed` and applies **zero** Gmail mutations,
   with `apply_decision()` raising `NotReviewedError` (also with `force=True`).
6. **Coverage:** the number of distinct `item_id`s in emitted `thread_classified` events equals 220.
7. **Circuit breaker:** with the provider stubbed to fail every call **for every model in the chain**,
   the run ends `resumable` within seconds rather than after exhausting all batches, a
   `provider_degraded` event was emitted, and every thread decided before the failure is still in
   `decisions`. (With only the current model failing, the circuit opens for that model and the run
   advances instead of stopping — assertion 9.)
8. **Model fallback:** with the primary model stubbed to raise `APIStatusError(status_code=404)` and
   model 2 healthy, the 220-thread run completes, exactly one `model_fallback` event per switch is
   emitted naming `from_model`/`to_model`/`reason`, and
   `SELECT DISTINCT model FROM llm_calls WHERE run_id=:id` contains
   `nvidia/nemotron-3-super-120b-a12b` — the model that actually served, never only the requested
   primary.
9. **Rotation ON connection failure (this is the corrected assertion):** with the primary stubbed to
   raise `APIConnectionError` on **every** call and model 2 healthy, the run **rotates and
   completes** — ≥ 1 `model_fallback` event is emitted whose `reason` names `APIConnectionError`,
   model 2 serves the remaining batches, and after the first advance the primary is **never requested
   again for the rest of the run** (asserted on the recorded request log: the advance is per-run and
   sticks, not re-tried per batch). The run does **not** end `resumable`.
10. **Never stuck:** with **all three** chain models stubbed to fail every call, the run terminates as
    `resumable` inside a test timeout strictly below `AGENT_RUN_MAX_SECONDS` (asserted with
    `pytest.mark.timeout`), `triage_runs.error_message` is a human-readable string naming the
    exhausted chain and the last failure, and every thread decided before the failure is still in
    `decisions`. It never hangs and never grinds indefinitely.
11. **Global throttle:** with `AGENT_LLM_MAX_RPM=60`, the 220-thread run's outbound LLM request
    timestamps (first attempts **and** retries, all tiers) never exceed 60 in any rolling 60 s
    window, and the run still completes — the throttle delays, never drops.
12. **Default unchanged:** the run's first request uses `AGENT_NVIDIA_DEFAULT_MODEL`
    (`nvidia/nemotron-3-nano-30b-a3b`), and `llm.health.model_chain(None)[0]` equals it.

`tests/integration/test_no_body_persisted.py` must still pass unchanged — it pins that no body text
reaches the database, and the new event payloads are additionally asserted body-free in
`tests/unit/events/test_thread_classified.py`.

#### How the user tests it

1. `uv run alembic upgrade head`, then `cd frontend && pnpm build && cd .. && uv run python -m src`.
2. Open **http://localhost:8001/app/** and click **Run triage**.
3. Open the **Activity drawer** (bell icon). Within seconds threads scroll past **one at a time** —
   each showing its tier badge (`RULE` / `SENDER HISTORY` / `LLM` / `DEEP READ`), subject, category,
   action, confidence and the reason. Every one carries an amber **NOT YET REVIEWED** chip.
4. While it is running, kill the server (`Ctrl-C`) after a minute or two.
5. Restart it (`uv run python -m src`) and reload the dashboard. The top of the page shows the amber
   **"Resume run — N of M already done"** banner, with N matching how far it had got — *not* a
   "run failed" message.
6. Click **Resume run**. The progress bar starts at N, not 0, and the drawer resumes streaming — the
   already-done threads are never re-classified (watch the run finish far faster and far cheaper than
   the first attempt; the Cost panel total goes up, never back to zero).
7. When the reviewer pass runs at the end, watch the drawer rows lose their **NOT YET REVIEWED** chip;
   any thread the reviewer flipped re-appears with a `REVIEWER` badge and `keep`.
8. Check Gmail: only reviewed decisions were applied. Nothing archived while it was provisional.
9. To see degraded-provider handling, temporarily point `AGENT_NVIDIA_BASE_URL` in `.env` at an
   unreachable host and start a run. Because **every** model is then unreachable, the drawer shows
   the chain being walked — *"Switched model: … → … (APIConnectionError after 3 retries)"* twice —
   pins the red **"NVIDIA is failing — N retries"** banner, and then the run stops as **resumable**
   within seconds with a message naming the exhausted chain and every already-decided thread
   preserved. It must **never** grind for 20 minutes. Restore `.env` and click **Resume run**.
10. To see **cross-model fallback**, set `AGENT_NVIDIA_DEFAULT_MODEL` in `.env` to a bogus id such as
    `nvidia/does-not-exist` and start a run. The drawer shows a
    *"Switched model: nvidia/does-not-exist → nvidia/nemotron-3-super-120b-a12b (model unavailable:
    404)"* row within seconds and **the run completes normally** on the fallback model. Crucially you
    should see that switch **once** — not once per batch — because the run sticks to the new model.
    Open the Cost panel / `GET /api/provider-health`: `model` is the fallback, `chain_position` is 1,
    and the spend is attributed to the model that actually served it. Restore `.env` afterwards.
11. To see the **global rate limit**, hit `GET /api/provider-health` mid-run: `throttle.max_rpm` is
    350 (or whatever `AGENT_LLM_MAX_RPM` is set to) and `throttle.waiting` is > 0 while batches are
    queued. Set `AGENT_LLM_MAX_RPM=30` and restart: the same run visibly takes longer but still
    completes — no errors, no dropped threads.
12. **Real in Phase 6:** incremental persistence, resume banner + resume, per-thread live feed with
    tier and reason, provisional labelling, review-state apply guard, degraded banner, circuit
    breaker. **Labelled stubs remaining:** unchanged from Phase 4 — none.

---

### Phase 7 — Drive to Inbox Zero

**Goal.** A run ends with the inbox **actually reduced** — the confident archives applied in Gmail —
and the dashboard states, in plain words, exactly what is left and why. The agent stops handing the
user a 56-cluster to-do list and starts finishing the job. **And while it works, the user can see it
working**: the classification feed streams live on the main page with no clicks, and never goes
quiet.

**Motivating defect (all measured, not inferred).** Run `fbeed060` (2,176 threads, `dry_run=0`, status
`completed`) produced 1,348 `proposed/keep`, 615 `proposed/archive`, 213 `needs_your_call/keep` — and
**zero decisions reached `applied`**. Four independent causes:

1. `user_settings.auto_act_threshold` is **dead config** — persisted, defaulted, sliderised and read
   by **no** decision or apply code path. The UI promises a control that does nothing.
2. Its `0.95` default is above the model's entire achievable range: of the 615 archive proposals,
   **0** scored `>= 0.95` (22 at `0.90–0.94`, 522 at `0.80–0.89`, 71 at `0.75–0.79`).
3. `_auto_apply_decisions` aborted **before** its per-decision loop — a bare `return` after a
   log line — and the run still reported itself `completed`.
4. 1,348 threads were decided `keep`. If confident keeps stay in the inbox, zero is unreachable by
   definition, no matter how well apply works.

Phase 6 added a fifth blocker that did not exist during that run: `apply_decision` raises
`NotReviewedError` unless `review_state == "reviewed"`, and `_auto_apply_decisions` never upgrades it.
**Phase 7 goes through that gate, never around it. No bypass is added.**

**Second motivating defect — the run is invisible while it runs (a first-class Phase 7 deliverable,
not a polish item).** The user has asked for this repeatedly and it has not landed: *"when the triage
is running I don't see the classification results. There should be a continuous feed of things. There
should not be one beat without a log being published for the user."* The plumbing is **done** —
Phase 6's `thread_classified` events are emitted, delivered and rendered by `ThreadFeedRow.tsx`. The
**surfacing** is not:

- `frontend/src/components/ActivityDrawer.tsx:212` is `useState(false)` — the feed lives in a drawer
  that is **closed by default**, so the user never sees it. `frontend/src/app/page.tsx` renders no
  feed at all (it reads `useSse()` only for `fetchedSoFar`). Delivered is not shipped; **visible** is
  shipped, and every prior "transparency is done" claim was false from the user's seat.
- A tier-3 batch is **one** LLM call over ~29 threads, so between dispatch and return **nothing** is
  emitted. On a degraded provider that was minutes of dead air — exactly how a working run became
  indistinguishable from a hung one.

Phase 7 fixes visibility and continuity, **not** the pipeline: the feed renders inline on the main
page unprompted, the graph and LLM client log at a granularity that keeps the feed moving, and a
self-arming watchdog guarantees no gap longer than 3 s while a run is active.

Capabilities: [drive-to-inbox-zero](capabilities/drive-to-inbox-zero.md) (new),
[triage-transparency](capabilities/triage-transparency.md) (extended: rules H/I/J — the live visible
feed, the no-silent-beat guarantee, and verified replay-on-connect),
[never-miss-safeguards](capabilities/never-miss-safeguards.md) (unchanged and binding — the autonomy
stage runs *before* the reviewer so every category-driven archive is audited),
[cost-tiered-triage](capabilities/cost-tiered-triage.md) (extended: the `decided_by="error"` tail is
re-classified on resume, not abandoned),
[gmail-actions-and-undo](capabilities/gmail-actions-and-undo.md) (extended: retry-apply).

**No new subsystem, no new agent, no new graph, no new LLM provider, and no new required key in
`.env`.** Phase 7 reuses `_auto_apply_decisions`, the `auto_apply_complete` event, the existing
`auto_act_threshold` column and slider, the taxonomy's per-category `default_action`, and the Phase 6
checkpoint / review-state machinery (`src/graph/checkpoint.py`,
`src/graph/persistence.py::upgrade_review_state`). The visibility work likewise reuses the existing
`ThreadFeedRow.tsx`, `SseContext.tsx`, the `activity_bus_processor` structlog→bus bridge, the
1000-event ring buffer and its replay-on-connect — it is **calibration, wiring and surfacing, not a
new subsystem**. Phase 7 adds two small graph nodes, two nullable columns, one migration, one
read-only ledger query, two endpoints, one card, one inline feed component and one watchdog module.
That is the whole phase.

#### The decisions this phase makes (and their justification)

| Decision | Value | Justification |
|----------|-------|---------------|
| Inbox-zero definition | The inbox holds only what needs a human: `category_keep` + `held_by_never_miss` + `below_threshold` + `needs_your_call`. Everything else is archived + labelled + undoable. | Written in full in the capability file and rendered **verbatim** in the Inbox-Zero card, so the user's meaning of "zero" and the system's are the same. |
| Autonomy instrument | Per-category, with a global fallback: `max(category.auto_act_threshold ?? settings.auto_act_threshold, confidence_floor)` | A Newsletters archive at 0.85 is a different risk from an Outreach archive at 0.85. `default_action` already carries most of the risk decision (People/Urgent/Legal never auto-act at any confidence); the threshold refines the rest. Newsletters/Notifications inherit the global, so the global slider stays load-bearing. |
| Global default | **`0.80`** (was `0.95`) | The elbow of the measured distribution: `0.95` → 0 archives, `0.90` → 22, **`0.80` → 544 of 615 (88.5%)**, `0.75` → identical to `confidence_floor` and therefore redundant. |
| Per-category seeds | `outreach` = `0.85`, `receipts` = `0.85`, rest NULL | Both are **live** bars. Outreach's 0.85 is a recorded **judgement call, not a measurement**: a real business inquiry wrongly archived costs far more than a promo wrongly kept, and that asymmetry justifies the margin above the 0.80 global — do not optimise it down to 0.80. Receipts is `archive` as of this phase, so its 0.85 is live too: Receipts archives only at `>= 0.85`, a notch more conservative than the global. |
| **Receipts `default_action`** | **`keep` → `archive`** (decided; was an open flag) | Measured by category on `fbeed060`: Notifications 703, Newsletters 456, People 354, **Receipts 283**, Outreach 122, Urgent 68, Legal 10. Keeping Receipts puts a **~715-thread floor** under the inbox (Receipts + People + Urgent + Legal) — the product could not reach its own stated definition of inbox zero. Receipts are archival records, not work: you search for an invoice, you don't action it from the inbox. Nothing is lost — labelled `ZeroInbox/Receipts`, searchable, one-click undoable, never trashed. **The safety net still binds and is the right instrument for the exception:** a genuinely time-sensitive receipt is flagged `time_sensitive` and held by `held_by_never_miss`, staying visible regardless of the category default — which is why this flip is safe. It also matches the user's revealed preference ("archive everything except needs_your_call"). **People, Urgent and Legal remain `keep`.** Full reasoning in [drive-to-inbox-zero](capabilities/drive-to-inbox-zero.md#decision--receipts-is-archive-was-an-open-question-now-decided). |
| Migration of the persisted `0.95` | `UPDATE user_settings SET auto_act_threshold = 0.80 WHERE auto_act_threshold > 0.90` | A value above 0.90 was never read by any code path, so it never expressed a preference — and it sits above the model's ceiling. Leaving it would mean the fix silently does nothing for the exact account that reported the problem. The other real account's `0.75` is a deliberate setting and is **left untouched**. |
| The `keep` question | The category default is applied **at decision time, before the reviewer** — the decision *becomes* `archive` — never as an apply-time override of a `keep`. | Converting before the reviewer means the reviewer audits it, the floor binds, and VIP/reply-history can veto. Overriding at apply time would archive mail the reviewer never saw, through the `force` door built for a deliberate user sweep. The "keeps are not force-archived" invariant is preserved exactly. |
| The dead slider | Made real and relabelled, with an above-ceiling warning | Shipping a no-op control is not acceptable. It is not deleted, because it is now the bar governing Newsletters + Notifications — the bulk of the mail. |
| Where the live feed lives | **Inline on the main page**, rendered unprompted while a run is active; the Activity drawer stays as the full-history surface | The drawer is `useState(false)` — closed by default. A feed nobody opens is not transparency. For a triage agent, watching it classify *is* the product, so during a run it belongs in the most prominent place on screen, not one click away. |
| How continuity is achieved | **Log at the right granularity** in the graph + LLM client and let `activity_bus_processor` bridge every line to the bus — not by adding hand-placed `bus.emit` calls | The bridge already forwards *every* structlog line for a bound user. Hand-placed emits are exactly what drifted out of coverage before (Gmail 429s and LLM retries were invisible despite retrying repeatedly). A bridge cannot drift; a call site can. |
| Heartbeat interval | **`HEARTBEAT_INTERVAL_SECONDS = 3.0`** | Comfortably below the ~5 s at which a human reads a static screen as "stuck", and above the noise floor. Worst case it adds 20 events/min — negligible against the 1000-event ring and the hundreds of real log lines a run already emits. It never fires while the graph is publishing faster than 3 s. |
| Max tolerated silence (asserted) | **`< 5.0 s`** max inter-event gap during an active run | The 3.0 s interval plus 2.0 s of thread-scheduling tolerance. The backend gate asserts this against a *simulated 60 s tier-3 batch* — the exact dead-air window that made a working run look hung. |
| Frontend stale threshold | **`FEED_STALE_SECONDS = 8`** | Two missed heartbeats plus margin. At 8 s the amber "no activity for Ns — still waiting on {phase}" line means the **watchdog itself** stopped, which is real signal. A shorter threshold would fire on ordinary jitter and train the user to ignore it. |
| Watchdog lifecycle | **Self-arming from the bus bridge**, self-disarming on `run_completed` / `run_resumable` / `run_failed` / `error` | If the graph had to start and stop it, some path would eventually forget — which is the whole class of bug being closed. Arming off the event stream also means it needs **zero** edits inside `src/graph/nodes.py`, keeping slice ownership disjoint. |

#### Slices

**Five** slices, **fully disjoint file ownership** — all five generate concurrently. Where one slice
calls a function another slice writes, it is a **spec-contract dependency only** (signatures pinned
below); no slice waits on another's output and all five land in the same gate. **No two backend
slices touch `src/graph/nodes.py`, and no two touch `src/events/bus.py`.** Slice 5's continuity work
is deliberately designed to require **no** edit inside slice 2's or slice 3's files: it arms itself
from the event stream. The one exception — the *graph-level* log lines of Rule I2 — is folded into
**slice 2's** responsibilities (stated in its row) rather than duplicating ownership of
`src/graph/nodes.py`.

| # | Slice | Owns (disjoint paths) | Depends on |
|---|-------|----------------------|-----------|
| 1 | `autonomy-policy` | `src/graph/autonomy.py` (new), `src/graph/nodes_autonomy.py` (new), `src/graph/agent.py`, `src/db/models.py`, `src/db/seed.py`, `alembic/versions/0006_autonomy_policy.py` (new), `src/tools/taxonomy.py`, `src/api/session.py`, `src/api/categories.py`, `tests/unit/graph/test_autonomy.py` (new), `tests/unit/graph/test_align_to_category_default.py` (new), `tests/unit/tools/test_taxonomy_autonomy.py` (new), `tests/unit/db/test_autonomy_migration.py` (new), `tests/unit/api/test_settings_validation.py` (new) | none (spec-contract: reads `state["categories"][*]["default_action"]` and `["auto_act_threshold"]`, supplied by slice 2's `_load_context_rows`) |
| 2 | `apply-and-converge` **+ graph log granularity** | `src/graph/nodes.py`, `src/graph/runner.py`, `src/graph/persistence.py`, `tests/unit/graph/test_auto_apply.py` (new), `tests/unit/graph/test_error_tier_resume.py` (new), `tests/unit/graph/test_log_granularity.py` (new), `tests/integration/test_drive_to_zero.py` (new), `tests/integration/test_apply_diagnosis.py` (new) | none (spec-contract: calls slice 1's `graph.autonomy.effective_threshold()`, slice 3's `graph.remainder.remainder_ledger()` and slice 3's `events.bus.emit_apply_progress` / `emit_run_apply_failed` / `emit_inbox_zero_report`) |
| 3 | `remainder-report` | `src/graph/remainder.py` (new), `src/api/runs.py`, `src/events/bus.py`, `tests/unit/graph/test_remainder.py` (new), `tests/unit/api/test_remainder_api.py` (new), `tests/unit/events/test_apply_events.py` (new) | none (spec-contract: calls slice 2's `graph.nodes.apply_run_decisions()` from `POST /api/runs/{id}/apply`; reads `decisions.autonomy_state` written per slice 1's policy) |
| 4 | `frontend-inbox-zero` **+ the visible live feed** | `frontend/src/components/InboxZeroCard.tsx` (new), `frontend/src/components/LiveRunFeed.tsx` (new), `frontend/src/components/RunSummary.tsx`, `frontend/src/components/Settings.tsx`, `frontend/src/components/TaxonomyEditor.tsx`, `frontend/src/components/ActivityDrawer.tsx`, `frontend/src/components/ThreadFeedRow.tsx`, `frontend/src/lib/SseContext.tsx`, `frontend/src/lib/types.ts`, `frontend/src/lib/api.ts`, `frontend/src/app/page.tsx`, `tests/e2e/phase7/inbox-zero.spec.ts` (new), `tests/e2e/phase7/autonomy-settings.spec.ts` (new), `tests/e2e/phase7/live-feed.spec.ts` (new) | none (spec-contract: codes against the response shapes in [api.md](api.md#phase-7--drive-to-inbox-zero) and the `activity_heartbeat` shape in [triage-transparency](capabilities/triage-transparency.md#activity_heartbeat-event-shape), written by slice 5) |
| 5 | `continuous-activity` | `src/events/heartbeat.py` (new), `src/observability/logging.py`, `src/llm/client.py`, `tests/unit/events/test_heartbeat.py` (new), `tests/unit/observability/test_activity_bridge.py` (new), `tests/integration/test_no_silent_beat.py` (new), `tests/integration/test_event_replay.py` (new) | none. **Owns no file any other slice owns**, and requires no edit in `src/graph/nodes.py` (slice 2) or `src/events/bus.py` (slice 3): the watchdog arms itself from the bus bridge, and it calls only `events.bus.emit()`, which already exists at HEAD. Spec-contract: consumes the graph log events slice 2 adds (Rule I2) — absent them the watchdog still guarantees no silence, it just reports a coarser `phase`. |

**Pinned cross-slice contracts** (each slice codes against these, never against another slice's files):

```python
# slice 1 writes, slices 2 + 3 call
graph.autonomy.effective_threshold(category: dict | None, settings: dict) -> float
    # max(category.auto_act_threshold or settings.auto_act_threshold, settings.confidence_floor)
    # category=None -> settings-only. Never returns < confidence_floor. Never raises.
graph.autonomy.AUTONOMY_STATES = ("auto_act", "below_threshold", "held_by_never_miss",
                                  "category_keep", "needs_your_call")
graph.autonomy.DEFAULT_AUTO_ACT_THRESHOLD = 0.80

# slice 2 writes, slice 3 calls (from POST /api/runs/{run_id}/apply)
graph.nodes.apply_run_decisions(*, run_id: str, user_id: str, channel_account_id: str,
                                dry_run: bool) -> dict
    # Applies every decision with autonomy_state="auto_act", review_state="reviewed",
    # proposed_action in ("archive","digest"), status not in
    # ("needs_your_call","applied","undone","rejected").
    # NEVER passes force=True. NEVER writes Decision.review_state.
    # ALWAYS returns a ledger; never raises, never returns early without one:
    #   {"applied": int, "already_applied": int, "not_reviewed": int, "kept": int,
    #    "needs_your_call": int, "below_threshold": int, "failed": int,
    #    "failures": [{"decision_id": str, "error": str}],
    #    "distance_to_zero": int, "apply_failed_reason": str | None, "dry_run": bool}
    # `_auto_apply_decisions(state, counts)` becomes a thin wrapper over this.

# slice 2 writes, slice 1 reads (context contract)
graph.persistence._load_context_rows(...)   # each state["categories"] entry gains
                                            # "default_action": str and
                                            # "auto_act_threshold": float | None

# slice 2 writes, per-decision key persisted by persist_decisions
decision["autonomy_state"]  ->  decisions.autonomy_state   # update-in-place for the whole run

# slice 3 writes, slice 2 calls
graph.remainder.remainder_ledger(session, *, run_id: str, user_id: str) -> dict
    # Live query over `decisions`, never cached counts. Shape in api.md.
    # inbox_remaining == sum(remainder.*) + distance_to_zero, always.
events.bus.emit_apply_progress(user_id, *, run_id, applied, total_to_apply, failed) -> None
events.bus.emit_run_apply_failed(user_id, *, run_id, reason, distance_to_zero) -> None
events.bus.emit_inbox_zero_report(user_id, *, run_id, applied, distance_to_zero,
                                  remainder: dict) -> None
    # each internally try/except + WARNING log; never raises; counts and ids only, no body text

# ── slice 5 writes, nobody calls (self-arming) ────────────────────────────────
events.heartbeat.HEARTBEAT_INTERVAL_SECONDS: float = 3.0
events.heartbeat.note_activity(user_id: str, *, run_id: str | None, phase: str | None,
                               fields: dict) -> None
    # Called ONLY from observability.logging.activity_bus_processor (slice 5's own file).
    # Records "this run published something just now" + the last observed phase/fields.
    # A run_id-carrying event ARMS the watchdog; an event of type run_completed /
    # run_resumable / run_failed / error DISARMS it. Never raises.
events.heartbeat.start_watchdog() -> None        # idempotent; one daemon thread process-wide
events.heartbeat.stop_watchdog() -> None         # for tests
    # While armed, every HEARTBEAT_INTERVAL_SECONDS with no publication for a run it emits
    # bus.emit(user_id, {"type": "activity_heartbeat", ...}) with the shape in
    # spec/capabilities/triage-transparency.md. Uses ONLY bus.emit(), which exists at HEAD —
    # it adds no helper to src/events/bus.py (slice 3's file).

# slice 2 writes, slice 5 consumes off the bus (no import, no call)
# The Rule I2 graph log events: triage.page_fetched / tier_started / tier_finished /
# batch_dispatched / batch_returned / reviewer_started / reviewer_finished / checkpoint /
# apply_progress — each at INFO, each carrying run_id and user_id in structlog contextvars.
# `phase` in the heartbeat is derived from the newest such event name.

# slice 4 codes against, slice 5 emits
SseEventType gains "activity_heartbeat"   # frontend/src/lib/types.ts + SseContext SSE_EVENT_TYPES
```

##### Slice 1 — `autonomy-policy`

- `src/graph/autonomy.py`: `effective_threshold`, `should_align(decision, category, settings,
  sender_stats, vip) -> bool` (Rule C1's exclusion list), `classify_autonomy_state(...) -> str`
  (the fixed precedence documented on `mark_autonomy_state` in [agent.md](agent.md#nodes)),
  `refresh_cluster_actions(clusters, decisions) -> list[dict]`.
  Pure functions, no DB, no I/O — the whole policy is unit-testable without a session.
- `src/graph/nodes_autonomy.py`: `align_to_category_default(state) -> dict` and
  `mark_autonomy_state(state) -> dict`, thin LangGraph wrappers over the pure functions.
- `src/graph/agent.py`: wire both nodes into the guarded chain —
  `cluster_decisions → align_to_category_default → second_pass_reviewer` and
  `apply_never_miss_floor → mark_autonomy_state → persist_decisions`.
- `src/db/models.py`: `Category.auto_act_threshold` (nullable Float),
  `Decision.autonomy_state` (nullable String), `UserSettings.auto_act_threshold` default `0.80`.
- `src/db/seed.py`: seed `outreach` and `receipts` at `0.85`; leave the rest NULL.
- `src/tools/rules.py` — **owned by slice 1 for this one-line change only** (no other Phase 7 slice
  touches this file): in `DEFAULT_TAXONOMY`, the `receipts` entry becomes
  `"default_action": "archive"` (was `"keep"`). This is where the seeded `default_action` values live —
  **not** `src/db/seed.py`. Nothing else in the file changes.
- `alembic/versions/0006_autonomy_policy.py`: the statements in
  [data.md § Phase 7 migration](data.md#phase-7-migration), exactly as written. **This touches real
  user rows — no ad-hoc scripts, no improvisation, and the downgrade documents the one-way step.**
- `src/tools/taxonomy.py`: validate `0 < auto_act_threshold <= 1`; `Urgent` still cannot be `archive`.
- `src/api/session.py`: replace the hardcoded `0.95` fallbacks (`:143`, `:187`) with `0.80`; validate
  the PATCH; return `warning: "above_model_ceiling"` above `0.90`.
- `src/api/categories.py`: return and accept `auto_act_threshold`.
- **Tests:** `test_autonomy.py` (threshold resolution incl. the floor lower bound; the fixed
  precedence of `classify_autonomy_state`); `test_align_to_category_default.py` (every exclusion in
  C1 individually — `needs_your_call`, `error`, `rule`, `time_sensitive`, `unsure`, `ever_replied`,
  VIP; People/Urgent at 0.99 unmoved; Newsletters at 0.88 moved and at 0.79 not; **Receipts at 0.86
  moved and at 0.82 not — the live 0.85 bar; and a `time_sensitive` Receipt at 0.95 unmoved**; cluster
  `suggested_action` refreshed); `test_taxonomy_autonomy.py`; `test_autonomy_migration.py` (upgrade on
  a seeded DB containing a `0.95` row and a `0.75` row: the first becomes `0.80`, the second is
  unchanged, **no row is left above 0.90**, `outreach`/`receipts` are `0.85`, **a seeded `receipts`
  category with `default_action = 'keep'` becomes `'archive'` while one a user already set to `digest`
  is untouched**, pre-existing decisions
  are `autonomy_state IS NULL`; then downgrade + upgrade again); `test_settings_validation.py`.

##### Slice 2 — `apply-and-converge`

- `src/graph/nodes.py`: `apply_run_decisions(...)` per the pinned contract; `_auto_apply_decisions`
  reduced to a wrapper; `_build_mutator_for_user` failure recorded as `apply_failed_reason` instead of
  a bare `return`; `finalize` writes `counts["apply"]` + `counts["remainder"]`, sets
  `triage_runs.error_message` and emits `run_apply_failed` when the pass failed or
  `distance_to_zero > 0`, and emits `inbox_zero_report`.
- `src/graph/runner.py` + `src/graph/persistence.py`: `already_decided_item_ids` excludes
  `decided_by="error"` and `review_state="review_failed"`;
  `insert_provisional_decisions` **overwrites in place** when the existing row is `decided_by="error"`
  (and only then), never double-counting `items_decided`; `_load_context_rows` adds `default_action`
  and `auto_act_threshold` to `state["categories"]`; `persist_decisions` writes
  `decisions.autonomy_state` for every row of the run.
- **Tests:** `test_auto_apply.py` — the review gate binds (a `provisional` and a `review_failed` row
  are counted in `not_reviewed`, mutator never called); **`force` is asserted `False` on every
  `apply_decision` call and `review_state` is asserted never written**; both silent-abort paths
  produce a populated `apply_failed_reason` (patched `_build_mutator_for_user`, patched session
  factory); `dry_run=True` performs zero mutations; a per-decision Gmail failure isolates and the loop
  continues. `test_error_tier_resume.py` — the F1/F2 pair, including the trap that F1 without F2
  silently discards the re-classification.
- `tests/integration/test_apply_diagnosis.py` — **the silent-abort cause proof.** Read-only: loads the real
  connected account's refresh token, calls `_build_mutator_for_user` and one `labels().list()`, and
  asserts it succeeds. **Zero writes, zero mutations, reuses the real-Gmail fixture pattern from
  `tests/integration/test_gmail_mutations.py`, and SKIPs (BLOCKED, not passed) when no mailbox is
  connected.** If it fails, that failure *is* the proven root cause; the implementer records it in the
  commit message and an inline comment at the failure site. **The cause is proven, not assumed.**
- `tests/integration/test_drive_to_zero.py` — the load-bearing gate (assertions below).
- **Log granularity (Rule I2 in [triage-transparency](capabilities/triage-transparency.md#rules--i-not-one-beat-without-a-log)).**
  This slice owns `src/graph/nodes.py`, so the *graph-level* log lines are its responsibility — no
  other slice may edit that file. Add, at INFO, with `run_id` **and** `user_id` bound in structlog
  contextvars (the bridge drops events without a `user_id`): `triage.page_fetched`,
  `triage.tier_started` / `tier_finished`, `triage.batch_dispatched` (with `batch_n`, `batch_total`,
  `batch_size`, `model`), `triage.batch_returned`, `triage.reviewer_started` / `reviewer_finished`,
  `triage.checkpoint`, `triage.apply_progress`. **Add no new `bus.emit(...)` call sites for these** —
  `activity_bus_processor` forwards them automatically, which is the whole point (hand-placed emits
  are what drifted before). `tests/unit/graph/test_log_granularity.py` captures structlog output over
  the fixture and asserts each event name appears with its documented fields and a bound `user_id`.

##### Slice 3 — `remainder-report`

- `src/graph/remainder.py`: one pure read function, `remainder_ledger(session, *, run_id, user_id)`.
  Grouped `SELECT` over `decisions` by `autonomy_state` and `status`. NULL `autonomy_state` lands in
  `unclassified` — never folded into a healthy bucket.
- `src/api/runs.py`: `GET /api/runs/{run_id}/remainder`; `POST /api/runs/{run_id}/apply`
  (background task, `409 not_appliable` unless `completed`, idempotent); `distance_to_zero` +
  `apply_ok` on `run_payload`; `applied_count` + `distance_to_zero` + `remainder` on `/summary`.
- `src/events/bus.py`: the three typed emit helpers, each internally `try/except` + WARNING, carrying
  counts/ids/reasons only.
- **Tests:** `test_remainder.py` — `inbox_remaining == sum(buckets) + distance_to_zero` over a seeded
  run with every bucket non-empty **and** a run with NULL `autonomy_state` rows (asserting they land in
  `unclassified`); `test_remainder_api.py` — envelope, auth scoping to the session user, `404` for
  another user's run, `409 not_appliable` on a `running` run, and `POST .../apply` twice in a row is a
  no-op the second time; `test_apply_events.py` — payload schemas exact, **no body/content field
  present**, a raising subscriber does not propagate.

##### Slice 4 — `frontend-inbox-zero`

- `InboxZeroCard.tsx` (screen 16 in [ui.md](ui.md)): headline, the verbatim inbox-zero definition,
  the five-row remainder ledger (zero-count rows rendered greyed, never hidden), the red
  `apply_ok === false` bar with **Retry archiving** → `POST /api/runs/{run_id}/apply`, and the dry-run
  chip. Mounted in `page.tsx` under `InboxSummary`; refetched on the three new SSE events, not polled.
- `Settings.tsx` (screen 17): relabel the slider, sub-label naming the floor as the hard lower bound,
  the always-visible calibration note, and the red above-0.90 warning.
- `TaxonomyEditor.tsx`: per-category `auto_act_threshold` input, disabled with an explanatory note on
  `keep` categories.
- `RunSummary.tsx`: `applied_count` + `distance_to_zero`. `ActivityDrawer.tsx` + `types.ts`: render
  `apply_progress`, `run_apply_failed`, `inbox_zero_report`.
- **`LiveRunFeed.tsx` (screen 18 in [ui.md](ui.md#18-live-run-feed-on-the-main-page-phase-7)) — the
  headline deliverable of this slice.** Mounted **inline** in `page.tsx` under the progress bar and
  above the cluster list, rendered **whenever a run is active, with no click and no toggle**. It
  reads `useSse()` and renders the newest 12 `feed` rows with the **existing**
  `ThreadFeedRow.tsx` — no second `EventSource`, no second row renderer, no forked feed model. Header
  line with thread count / tier / model and a live "last update {x}s ago"; pinned bottom line showing
  the newest `activity_heartbeat` verbatim state; amber *"No activity for Ns — still waiting on
  {phase}"* line after `FEED_STALE_SECONDS = 8`; a red `provider_degraded` line above the feed; the
  idle one-liner when no run is active; **"See all activity →"** opening the drawer.
- `ActivityDrawer.tsx`: **unchanged in behaviour** — it stays the full-history surface and keeps
  `useState(false)`. It is no longer the *only* way to see the feed, which is the actual fix. It
  additionally renders `apply_progress`, `run_apply_failed`, `inbox_zero_report`.
- `SseContext.tsx` + `types.ts`: add `"activity_heartbeat"` to `SseEventType` and
  `SSE_EVENT_TYPES`, and expose the newest heartbeat plus a `lastEventAt` timestamp on the context
  value so the stale line and the "last update" stamp are derived, not polled. `buildFeed` must
  **not** turn heartbeats into scrolling rows (they are the pinned line, like `provider_degraded`).
- **Tests:** `tests/e2e/phase7/` — `inbox-zero.spec.ts`: the card renders the definition text and all
  five buckets including zero-count ones; an `apply_ok === false` run renders the red bar and a
  working Retry button and **never** the neutral state. `autonomy-settings.spec.ts`: the slider
  warning appears above 0.90. **`live-feed.spec.ts` (the one that closes the reported defect):**
  against a real active run it loads `/app/`, **clicks nothing and opens no drawer**, asserts
  classification rows are present on the main page within 2 s of first paint, then polls the on-page
  row count three times ≥ 3 s apart and asserts it **strictly increases**; it captures a screenshot of
  the mid-run main page as an artifact; and it reloads the page mid-run and asserts the feed is
  populated on first paint from the replay buffer (row count > 0 within 2 s), not empty.

##### Slice 5 — `continuous-activity`

Owns only files no other slice owns. **The guarantee: while a run is active, no gap longer than
`HEARTBEAT_INTERVAL_SECONDS = 3.0` seconds passes with nothing published.**

- `src/events/heartbeat.py` (new): the watchdog, per the pinned contract. One daemon thread
  process-wide, started lazily on first `note_activity`. Per `(user_id, run_id)` it holds the last
  publication time and the last observed `phase` + fields. Every 0.5 s it sweeps armed runs and, for
  any whose last publication is older than the interval, emits `activity_heartbeat` via the existing
  `bus.emit()` with `phase`, `detail`, `batch_n`, `batch_total`, `batch_size`, `model`, `elapsed_s`
  (since the phase began) and `silent_for_s`. **Derived from observed state — never a placeholder,
  never a bare spinner.** Disarms on `run_completed` / `run_resumable` / `run_failed` / `error`, and
  after a 10-minute idle ceiling as a belt-and-braces leak guard. Wrapped so it can never raise into
  a caller, and it must never emit its own heartbeat as activity (no self-feeding loop).
- `src/observability/logging.py`: `activity_bus_processor` gains a single call to
  `events.heartbeat.note_activity(...)` after the successful `bus.emit`, inside the same
  `try/except`. **This is the only integration point** — nothing else in the codebase starts, stops
  or feeds the watchdog, so it cannot drift out of coverage.
- `src/llm/client.py`: LLM-level granularity — `llm.call_started` / `llm.call_finished` (with
  `model`, `latency_ms`), `llm.retry` (`attempt`, `error`, `backoff_ms`) and `llm.model_fallback`
  alongside the existing `model_fallback` event. These are the lines that make a slow provider legible
  rather than silent. No behaviour change to the client, no new provider, no new env key.
- **Tests:**
  - `test_heartbeat.py` — arms on a `run_id` event and disarms on `run_completed`; publishes nothing
    when no run is armed; heartbeat carries the `phase`/`batch_n`/`model` from the last observed log
    line, with monotonically increasing `elapsed_s`; never emits while activity is faster than the
    interval; a raising `bus.emit` does not propagate.
  - `test_activity_bridge.py` — a log line with a bound `user_id` reaches the bus **and** notes
    activity; one without a `user_id` does neither; secrets/body fields are still redacted before
    both (the bridge stays after `redact_processor`).
  - **`test_no_silent_beat.py` — the load-bearing backend assertion.** Simulates a tier-3 batch taking
    **60 s** (a stubbed slow LLM call; real bus, real watchdog, `_isolated_db`), records every event
    published for the run with timestamps, and asserts the **maximum inter-event gap is < 5.0 s**,
    that ≥ 15 `activity_heartbeat` events were published, and that every heartbeat payload contains
    no `subject` / `from_email` / `body` / `snippet` / `content` key.
  - `test_event_replay.py` — with ≥ 200 events in the ring, a fresh `GET /api/events` connection
    delivers the full replay **before** any live event, in order, oldest first. Replay already exists
    in `src/api/events.py`; **this slice verifies it end-to-end and does not modify that file.**

#### Gate (exact commands, run from the repo root, real APIs via `.env`, production DB driver)

```bash
uv run alembic upgrade head && uv run alembic current
uv run pytest tests/unit tests/integration -q
cd frontend && pnpm install && pnpm build && cd ..
npx playwright test tests/e2e/ --reporter=line
```

`alembic current` must print a revision hash including `0006_autonomy_policy`.
`pnpm build` must exit 0 with zero TypeScript errors.

**Playwright runs against the already-running supervised server on `http://localhost:8001`. Do not
start a second server and do not kill or restart the supervised one.**

`tests/e2e/phase7/live-feed.spec.ts` needs a run that is **actually running** while it observes. It
starts one itself via the UI/API over the **220-thread fixture** (never a full-inbox run, per the
production-safety rules below) and observes it live. If no run can be started it must **fail loudly,
not skip** — a silently skipped visibility test is how this defect survived to Phase 7.

**Judging the gate — known pre-existing failures, out of scope for Phase 7.** These were proven red
before this phase's first commit (A/B verified at HEAD) and Phase 7 is judged on **its own tests plus
no new regressions**:

- **5 integration failures:** model-shape drift, a live-mailbox flake, `RefreshError` mapping, and
  `GmailMutator` method drift.
- **24 Playwright failures:** Phase-2 stub specs asserting UI that no longer exists.

A Phase 7 test failing is a blocker. One of the above failing is not. Any failure **outside** both
sets is a new regression and blocks the phase.

`tests/integration/test_drive_to_zero.py` is the load-bearing gate. It runs the **220-thread fixture
against the real NVIDIA NIM endpoint** using the key from `.env`, plus a **615-row confidence fixture
replaying the measured `fbeed060` distribution** (0 / 22 / 522 / 71 across the `>=0.95` / `0.90–0.94` /
`0.80–0.89` / `0.75–0.79` bands). The 615-row fixture is deliberately large enough that a sampled
answer and a full-data answer differ: at `0.80` the correct answer is **544 `auto_act` / 71
`below_threshold`**, and any sampling or short-circuit produces a different number. It asserts:

1. **Calibration, full-data:** the 615-row fixture at the new default yields exactly `544` `auto_act`
   and `71` `below_threshold`; the same fixture at `0.95` yields exactly `0` `auto_act` — reproducing
   today's production behaviour and proving the threshold is genuinely enforced.
2. **A realistic archive reaches `applied` end-to-end through the real review gate:** a decision at
   confidence **0.83** (the modal band) ends `status="applied"`, was `review_state="reviewed"` at
   apply time, was applied with `force=False`, has an `ActionLog` row with a non-null `undo_token`,
   and the thread is out of the Gmail inbox with its category label attached.
3. **`distance_to_zero == 0`** on the healthy completed run, and
   `ledger.applied == count(autonomy_state='auto_act')`.
4. **silent-abort regression, mutator path:** with `_build_mutator_for_user` patched to raise, the run still
   completes classification but `ledger.apply_failed_reason` names the exception,
   `triage_runs.error_message` is a non-null human-readable sentence, a `run_apply_failed` event was
   emitted, `GET /api/runs/{id}` returns `apply_ok=false`, and
   `distance_to_zero == count(autonomy_state='auto_act')`. **A silent abort can never again look like
   a successful run.**
5. **silent-abort regression, outer path:** the same assertions with the DB session factory patched to raise
   inside `apply_run_decisions`.
6. **The Phase 6 review gate is not weakened:** a run whose reviewer pass is forced to fail applies
   **zero** mutations, every row is counted in `ledger.not_reviewed`, `apply_decision` raised
   `NotReviewedError` before the mutator was touched, and `distance_to_zero` reflects them.
7. **No side door:** across the whole run, `apply_decision` is never called with `force=True` and
   `Decision.review_state` is never written by any code path under `apply_run_decisions` (asserted by
   spying on the call and on the ORM attribute).
8. **Keeps are not force-archived:** every applied decision had `proposed_action in ("archive",
   "digest")` at apply time; zero `keep`-proposed decisions were mutated.
9. **Never-miss still binds after realignment:** over the 220-thread fixture, zero `ever_replied`
   senders, zero VIP senders and zero `time_sensitive` threads are applied, and the seeded
   false-negative bait thread is still flipped back to `keep` by the reviewer — *after*
   `align_to_category_default` ran on it.
10. **Urgent/People are untouchable:** an Urgent thread and a People thread at confidence 0.99 both
    end `proposed_action="keep"` with `autonomy_state="category_keep"`.
11. **User rules win:** a `decided_by="rule"` keep in an archive category is never realigned.
12. **`dry_run` is absolute:** the same run with `dry_run=true` performs zero Gmail mutations,
    `ledger.dry_run` is true, `ledger.applied == 0`, and no `run_apply_failed` event is emitted.
13. **Convergence on the tail:** 20 `decided_by="error"` rows seeded into an interrupted run are
    all re-classified on resume (none ends `decided_by="error"`), with zero duplicate
    `(run_id, item_id)` pairs and **no already-`reviewed` thread re-sent to the LLM** (asserted on the
    `llm_calls` delta).
14. **Retry-apply is idempotent:** `POST /api/runs/{run_id}/apply` on a fully-applied run returns
    `already_applied == n`, `applied == 0`, and makes zero Gmail calls; called twice it is a no-op the
    second time.
15. **Ledger arithmetic:** `inbox_remaining == sum(remainder buckets) + distance_to_zero`, and
    `remainder.unclassified == 0` for a Phase-7 run.
16. **Nothing destructive:** every `ActionLog.operation` is in `{archive, add_label, remove_label}` and
    every row has a non-null `undo_token`.

**The visibility bar — how this is judged (read this before reporting the feed done).**

> **It is NOT done because events reach the browser.** That is exactly the mistake already made once:
> Phase 6's events were delivered and rendered, into a drawer nobody opened, and the work was reported
> shipped while the user saw nothing. The only acceptable evidence is: **start a real run, load the
> dashboard the way a user would with NO EXTRA CLICKS, and see classification results visibly
> streaming on the main page within a second or two — and the feed never goes visually static for
> more than a couple of seconds while the run is active.**

`tests/e2e/phase7/live-feed.spec.ts` and `tests/integration/test_no_silent_beat.py` are load-bearing
alongside `test_drive_to_zero.py`. They additionally assert:

17. **REQUIRED GATE ASSERTION — visible with zero clicks:** `tests/e2e/phase7/live-feed.spec.ts` loads
    `/app/` exactly as a user does — **no clicks, no drawer interaction** — starts/observes a **real**
    run, and asserts classification rows are **present on the main page** within **2 s** of first
    paint. This assertion is **required**, not optional and not nice-to-have; it must never be
    `test.skip`ped or conditionally bypassed. **If the test cannot start or observe a real run it
    must FAIL LOUDLY** with the reason — a skipped or vacuously-passing live-feed test counts as a
    Phase 7 gate failure.
18. **REQUIRED GATE ASSERTION — visibly increasing / never static:** the same test polls the on-page
    feed row count three times, ≥ 3 s apart, during the active run and asserts the count is
    **strictly increasing** across the polls, and that the feed never sits visually static for more
    than the `FEED_STALE_SECONDS = 8` window without the amber *"No activity for Ns — still waiting
    on {phase}"* line appearing. A screenshot of the mid-run main page is captured as evidence.
    Equally required; equally must fail loudly rather than skip.
19. **No silent beat:** during a simulated 60 s tier-3 batch the **maximum inter-event gap is
    < 5.0 s** and ≥ 15 `activity_heartbeat` events are published, each carrying a real `phase`,
    `batch_n`/`batch_total`/`batch_size`/`model` from the preceding `triage.batch_dispatched` line,
    and a monotonically increasing `elapsed_s`.
20. **Late join / reload:** a page loaded mid-run paints a **populated** feed from the replay buffer
    (row count > 0 within 2 s), and a fresh `GET /api/events` delivers the ring replay in order before
    any live event.
21. **No hand-placed emit regression:** the Phase 7 diff adds **zero** new `bus.emit(` call sites in
    `src/graph/` for the Rule I2 events — they arrive via `activity_bus_processor`. (Checked by the
    auditor on the diff; the corresponding coverage is asserted by
    `tests/unit/graph/test_log_granularity.py`.)
22. **Privacy holds on the new surface:** no `activity_heartbeat` or `log` event carries `subject`,
    `from_email`, `body`, `snippet` or `content` beyond the documented, truncated fields.

`tests/integration/test_no_body_persisted.py` and `tests/integration/test_resume.py` must still pass
unchanged — Phase 7 must not regress the Phase 6 durability or privacy guarantees.

#### Production safety (binding on every implementer and the auditor)

`zero_inbox.db` holds **11,449 real decisions for 2 real accounts**.

- **Never write an ad-hoc script against the real DB.** `get_settings()` is cached and does **not**
  honour an env override from a standalone script — a previous agent polluted the real DB exactly that
  way. Every test uses the existing `_isolated_db` fixture pattern.
- **Do not kill or restart the supervised server on `:8001`**, and do not start a second one.
- **Do not launch a full-inbox run** while building or gating. The 220-thread fixture and the 615-row
  confidence fixture are the test surface.
- Migration `0006` is the only sanctioned write to real user rows, and only via
  `uv run alembic upgrade head` — never by hand, never by script.

#### How the user tests it

1. `uv run alembic upgrade head`, then `cd frontend && pnpm build && cd .. && uv run python -m src`.
2. Open **http://localhost:8001/app/** and go to **Settings**. The autonomy slider now reads
   **"Act on its own above this confidence"** and sits at **0.80** — it was silently 0.95 and did
   nothing. Drag it to 0.95: a red warning appears — *"At this setting the agent will archive almost
   nothing — your inbox will not reach zero."* Drag it back to 0.80.
3. Open **Settings → Taxonomy**. Newsletters, Notifications, Outreach **and Receipts** show `archive`;
   People, Urgent (and Legal, if you have it) show `keep` with their threshold input disabled and the
   note *"kept by default — never archived automatically"*. Outreach **and Receipts** each show
   `0.85` — a notch stricter than the 0.80 global. **Receipts changed from `keep` to `archive` in this
   phase**: receipts are records you search for, not work you action, and keeping them put a ~700-thread
   floor under your inbox. They are archived and labelled `ZeroInbox/Receipts`, never deleted, and a
   time-sensitive one (invoice due, payment failed) is still held in the inbox by the never-miss layer.
   If you disagree, flip it back here — **this is the control that decides what "zero" means for you.**
4. Go back to the dashboard and click **Run triage (200 threads)**. **Do not open the Activity
   drawer. Do not click anything.** Within a second or two the **live feed appears by itself on the
   main page** and starts scrolling: one row per thread — tier badge · subject · → category · action ·
   confidence · reasoning — newest at the top, provisional rows carrying the amber
   `NOT YET REVIEWED` chip and being replaced in place when the reviewer flips them. **This is the
   thing that has been asked for repeatedly and never landed: the feed you can see without hunting
   for it.**
5. **Watch for dead air.** Under the feed a line always reports real state — e.g. *"classifying batch
   12/75 — 29 threads, 18s elapsed, nvidia/nemotron-3-nano-30b-a3b"* — updating at least every 3
   seconds even during a slow single LLM call over a whole batch. **At no point should the screen sit
   still with nothing changing.** If the backend genuinely stalls you get an explicit amber
   *"No activity for 12s — still waiting on tier3_classify"* — never a frozen, unexplained feed.
   Working-but-slow and stuck must always look different.
6. **Reload the page in the middle of the run.** The feed comes back **populated immediately** with
   the recent history (replayed from the server's 1000-event buffer), not empty.
7. Click **"See all activity →"**: the Activity drawer still opens with the complete scrollback,
   including the **apply phase** (`apply_progress` rows) — a phase that did not exist before.
8. When the run completes, the **Inbox-Zero card** is at the top of the page. It states the definition
   in plain words and shows the ledger, e.g. *"142 archived this run · 58 still in your inbox ·
   distance to zero: 0"*, with a row per reason: need your call / kept by category / not confident
   enough / held by VIP or reply history. The live feed collapses to a one-line summary — it never
   leaves an empty box or a progress bar for work that is not running.
9. **Check Gmail.** The archived threads are genuinely out of the inbox and carry their `ZeroInbox/…`
   label. **Nothing is in Trash.** People, Urgent (and Legal) threads are still in the inbox — **and so
   are any time-sensitive receipts**, held by the never-miss layer. Ordinary Receipts threads are
   **gone from the inbox**, findable under the `ZeroInbox/Receipts` label and restored by Undo.
10. Click **Undo this run** on the Run Summary card and confirm everything comes back — then re-run.
    The archives are real mutations with real undo tokens, not a report.
11. **Test the loud-failure path deliberately:** temporarily rename your Gmail connection's refresh
    token (or disconnect the mailbox) and run triage again. The card must show the **red bar** —
    *"The agent decided N threads should leave your inbox but could not archive them"* — with the
    reason and a **Retry archiving** button. It must **not** show a green/neutral "run complete".
    Reconnect and click **Retry archiving**: the archives complete without re-classifying anything
    (the Cost panel total does not move).
12. **Test the tail:** if a previous run left threads showing the `ERROR` tier badge, click
    **Resume run** — those threads are re-classified rather than treated as done, and already-decided
    threads are not re-sent to the LLM (the cost barely moves).
13. **Real in Phase 7:** enforced per-category autonomy, category-default realignment before the
    reviewer, real Gmail archiving on completion, loud apply failures with retry, the error-tier
    tail finished on resume, the Inbox-Zero card with the stated definition and the remainder ledger,
    honest autonomy controls, **and the live classification feed visible on the main page with no
    clicks, never silent for more than a few seconds.** **Labelled stubs remaining:** none.

> **If step 4 or step 5 fails — if you have to open the drawer to see anything, or the screen sits
> still — Phase 7 is not done, regardless of what the other steps show.**

---

### Phase 8 — The Product Front Door, Real Accounts & Review Recovery

**Goal.** Zero Inbox stops being a tool that opens straight into an operator console and becomes a
product someone can meet for the first time. A signed-out visitor to `/app/` gets a homepage that
explains what it does, states the promise — *an inbox that holds only what needs a human* — and sells
the thing that actually differentiates it: **when it can't be sure, it stops and says so**. Signing in
becomes a first-class flow that asks Google for a name and an email only; mailbox access is a separate
consent the user grants at step 1 of a three-step first run that ends in a deliberately designed
**moment of trust** — the first thread the agent *keeps* for a never-miss reason, shown before it has
archived anything. The already-multi-tenant data model finally gets the product around it: visible,
revocable sessions with sign-out-everywhere, connected-mailbox management, globally unique mailbox
ownership so two accounts can never point at one inbox, account deletion that names its exact
consequences, and session hardening. A design system — tokens, type scale, component states,
responsive rules, accessibility — replaces per-component styling, and the codebase's existing
state-is-never-colour-alone discipline is promoted to a token-level requirement. Finally, the known
recovery gap closes: a completed run holding `review_failed` decisions gets a **Retry review** action
that re-enters the never-miss gate instead of forcing a whole new run.

**Scoped honestly.** This phase does **not** build SSO/SAML, SCIM, organisations, roles, RBAC, an
admin console, audit-log export, billing or delegated mailboxes, and the product copy promises none of
them. Five things done properly beats a long list half-done —
see [account-and-identity](capabilities/account-and-identity.md#what-enterprise-grade-means-here--scoped-honestly).

Capabilities: [product-front-door](capabilities/product-front-door.md) (new),
[account-and-identity](capabilities/account-and-identity.md) (new),
[review-recovery](capabilities/review-recovery.md) (new),
[never-miss-safeguards](capabilities/never-miss-safeguards.md) (unchanged and binding).

#### Invariants — this phase adds surfaces, it does not add a path around anything

Every one of these is built, tested and load-bearing today. **No new surface — sign-up, sign-in,
onboarding, account settings, session revocation, mailbox disconnect, account deletion or Retry
review — may create a path around any of them.** A generator that weakens one has failed the phase
regardless of what else it delivered.

- **Never delete, trash or spam-report.** No such method exists on the Gmail mutator and the absence
  is asserted structurally. Disconnecting a mailbox and deleting an account perform **zero** Gmail
  operations of any kind.
- **`apply_decision()` raises `NotReviewedError` before touching the mutator**, independent of
  `status`, and is not bypassable by `force=True`. `retry-review` re-enters this gate; it never
  bypasses it and never writes `review_state` outside the reviewer node.
- **A `keep`-proposed decision is never archived** except via the explicit, user-initiated `force`
  path. No Phase 8 route uses that path.
- **People / Urgent / Legal can never be set to `default_action=archive`** (`NEVER_ARCHIVE_KEYS`,
  enforced on create **and** update). No account, onboarding or settings surface exposes a control
  that could.
- **VIP, reply-history, time-sensitive and the confidence floor all bind ahead of the reviewer**, in
  a retry exactly as in a first pass.
- **Every mutation carries an undo token; `dry_run` is absolute.** The onboarding "start in dry-run
  instead" control sets the real setting and changes nothing else.
- **No message bodies are persisted or sent to the LLM** beyond headers, subject and the redacted
  200-char snippet. Phase 8 adds no content-bearing column and no content-bearing event; the only new
  columns are session metadata and `last_synced_at`.
- **`require_user_id` stays the single user-scope chokepoint.** Phase 8 adds revocation *inside* it
  and introduces no second auth path, no bearer token and no API key. Every new route is user-scoped
  and returns `404` for another user's row.
- **No raw IP and no raw user-agent string is stored, logged or returned**; `refresh_token_enc` is
  never returned by any route.

#### Slices

Five slices, **fully disjoint file ownership** — all five generate concurrently. There is **no true
cross-slice build dependency**: where one slice consumes something another writes it is a
**spec-contract dependency only** (the endpoint shapes are pinned in [api.md](api.md#phase-8--identity-account-and-review-recovery),
the token names in [ui.md](ui.md#colour-tokens)), and all five land in the same gate.

| # | Slice | Owns (disjoint paths) | Depends on |
|---|-------|----------------------|-----------|
| 1 | `identity-backend` | `src/api/auth.py`, `src/api/session.py`, `src/api/account.py` (new), `src/api/__init__.py`, `src/db/models.py`, `alembic/versions/0007_sessions_and_mailbox_ownership.py`, `src/channels/gmail/oauth.py`, `src/channels/gmail/store.py`, `.env.example`, `tests/unit/api/test_account.py` (new), `tests/unit/api/test_session_revocation.py` (new), `tests/unit/api/test_oauth_intent.py` (new), `tests/integration/test_identity.py` (new) | none |
| 2 | `review-recovery` | `src/graph/review_retry.py` (new), `src/api/runs.py`, `frontend/src/components/InboxZeroCard.tsx`, `tests/unit/graph/test_review_retry.py` (new), `tests/integration/test_review_recovery.py` (new) | none |
| 3 | `design-system-and-front-door` | `frontend/src/app/globals.css`, `frontend/src/app/page.tsx`, `frontend/src/lib/api.ts`, `frontend/src/lib/tokens.ts` (new), `frontend/src/components/Chrome.tsx`, `frontend/src/components/ConnectCard.tsx`, `frontend/src/components/AccountMenu.tsx` (new), `frontend/src/components/Onboarding.tsx` (new), `frontend/src/components/home/**` (new: `Homepage.tsx`, `Hero.tsx`, `HonestyBand.tsx`, `HowItWorks.tsx`, `SafetyModel.tsx`, `SignInCard.tsx`, `SiteFooter.tsx`) | none |
| 4 | `account-ui` | `frontend/src/components/Settings.tsx`, `frontend/src/components/AccountSection.tsx` (new) | none |
| 5 | `e2e-phase8` | `tests/e2e/phase8/**` (new) | none |

**Path-disjointness notes (read before writing a line of code).**

- Slice 3 owns `frontend/src/lib/api.ts` and `frontend/src/app/globals.css`. **Slices 2 and 4 must not
  edit either.** Slice 4 declares its `/api/account*` fetch paths locally inside `AccountSection.tsx`;
  slice 2 declares its `retry-review` path locally inside `InboxZeroCard.tsx`. Both consume the design
  tokens **by class name only** — the names are pinned in [ui.md](ui.md#colour-tokens) and are a
  contract, not a shared file.
- Slice 1 is the only slice that edits `src/api/__init__.py`, `src/db/models.py` or `alembic/`.
  Slice 2 needs no mount (the `runs` router is already mounted) and adds **no** model or migration.
- Slice 2 *imports* the reviewer node and `apply_decision()`; it **must not edit**
  `src/graph/nodes_review.py`, `src/graph/nodes.py`, `src/graph/state.py` or `src/tools/actions.py`.
  If the retry appears to need a change in one of those files, that is a spec question — raise it,
  do not edit across the seam.
- No two slices own the same file. Verified file by file: the four backend/frontend source sets and
  the e2e set intersect nowhere.

##### Slice 1 — `identity-backend`

- `oauth.py`: `build_authorization_url(config, *, state, scopes)` — `SIGNIN_SCOPES =
  ("openid", "email", "profile")` and the existing Gmail set as `CONNECT_SCOPES`. The Gmail set is
  copied, not rewritten.
- `auth.py`: `intent` on `/auth/google/start`, carried inside the signed `zi_oauth_state` payload
  (`{state, code_verifier, intent}`) and read **only** from there on the callback. `intent=signin`
  upserts the `users` row, issues a session and returns — **no `channel_accounts` write, no refresh
  token requirement, no auto-triage task**. `intent=connect` (and a missing `intent`) keeps today's
  behaviour byte for byte, including `_auto_triage_task`. The `"insecure-dev-key"` fallback in
  `_serializer` is deleted; a missing `AGENT_SECRET_KEY` fails at startup.
- `store.py`: the ownership guard — a `connect` for an `account_email` already owned by a different
  `user_id` raises, and the route maps it to `409 mailbox_already_connected` having written nothing.
- `session.py`: cookie payload `{uid, sid}`; `require_user_id` rejects a revoked/unknown `sid`;
  legacy `uid`-only cookies authenticate, get a `user_sessions` row and a re-issued cookie;
  `last_seen_at` throttled to once per 60 s; `secure=` set from `_is_https(request)`, which tries
  `request.url.scheme` and **falls back to the raw ASGI `scope["scheme"]`** (a scope with no server
  and no Host header drops the URL scheme, which would ship the cookie without `secure` over https).
- `account.py` (new): the five routes in [api.md](api.md#routes). Deletion does **not** use FK
  cascade — SQLite does not enforce it without `PRAGMA foreign_keys=ON`; the route deletes every
  `user_id`-carrying table explicitly, metadata-driven and user-scoped
  ([data.md](data.md#deletion-semantics)) — and makes **zero** Gmail calls. Disconnect revokes at
  Google best-effort (WARNING on failure) and deletes the local ciphertext regardless.
- `models.py` + `0007`: `user_sessions`, the guarded global unique index on
  `(channel, account_email)`, and `channel_accounts.last_synced_at`. Per
  [data.md](data.md#phase-8-migration), the ownership guard **raises naming the offending addresses**
  rather than resolving a conflict.
- **Tests:** `test_oauth_intent.py` — the two scope sets, asserted exactly, and that `intent` is read
  from the signed cookie and ignored from the query string on the callback.
  `test_session_revocation.py` — revoke → `401` on replay; revoke-all; legacy `uid`-only cookie is
  upgraded in place; `last_seen_at` throttling.
  `test_account.py` — envelope shapes; cross-user `404` on every route; no `refresh_token_enc`, raw IP
  or raw UA in any response; `confirm_email` mismatch is `422` and deletes nothing.
  `test_identity.py` (integration, `_isolated_db`) — a full `signin` creates a user and zero
  connections and zero runs; a `connect` for an address owned by another user is `409` and writes zero
  rows; account deletion removes rows from **every** user-scoped table (asserted table by table) with
  a spy proving **zero Gmail calls**; the `0007` guard raises on a seeded duplicate and creates the
  index when there is none.

##### Slice 2 — `review-recovery`

- `src/graph/review_retry.py` (new): `retry_review(*, run_id, user_id) -> dict`. Loads the run's
  `review_state IN ('provisional','review_failed')` decisions, feeds them through the **existing**
  reviewer node in the existing batch shape, then calls the **existing** apply pass. It never writes
  `review_state` itself and never passes `force=True`. Emits `retry_review_started` /
  `retry_review_finished` via the existing bus so the live feed shows it working.
- `src/api/runs.py`: `POST /api/runs/{run_id}/retry-review` — background task, `409 not_retryable`
  unless `completed` with ≥ 1 non-`reviewed` decision, idempotent, user-scoped (`404` cross-user).
- `InboxZeroCard.tsx`: the amber unreviewed bar (screen 24), rendered **below** the red apply-failure
  bar and never instead of it, with the **Retry review** button, its loading label, and the ledger
  refetch on success.
- **Tests:** `test_review_retry.py` — with the reviewer stubbed to succeed, rows upgrade and
  archive-eligible ones apply; with it stubbed to fail, rows stay `review_failed` and **zero** Gmail
  mutations occur; `apply_decision` is never called with `force=True` (spy) and `Decision.review_state`
  is never written outside the reviewer node (ORM attribute spy); a `keep`-proposed row is never
  mutated; a seeded VIP sender and a seeded ever-replied sender are still held.
  `test_review_recovery.py` (integration, `_isolated_db`, real NIM via `.env`) — a completed run
  seeded with **120** `review_failed` decisions over the 220-thread fixture (large enough that a
  sampled retry and a full retry give different counts): the retry reviews all 120, the ledger's
  `not_reviewed` falls by exactly the number passed, every applied row has a non-null `undo_token`,
  `409` on a `running` run, `404` cross-user, a second immediate call performs zero LLM and zero Gmail
  calls, and the same run under `dry_run=true` reviews and mutates nothing.

##### Slice 3 — `design-system-and-front-door`

- `globals.css`: the token set from [ui.md](ui.md#design-system) under `@theme` — colour, type scale,
  spacing, radius, elevation, motion, plus the `prefers-reduced-motion` block. **The Tailwind v4 lines
  (`@source "../";` first, `postcss.config.mjs` untouched) are preserved exactly.**
- `components/home/**`: screen 19 and screen 20. The five safety promises verbatim, the honesty band
  as **static copy labelled as the author's own inbox** (never wired to an API), exactly one CTA.
- `Onboarding.tsx`: screen 21's three steps, including the corrected step-1 promise, the
  "start in dry-run instead" control wired to `PATCH /api/settings`, the pre-first-archive pinned
  line, and the never-miss **keep** callout (rendered only when one genuinely occurs in the first 50
  decisions — never fabricated).
- `ConnectCard.tsx`: the false Phase-1 copy is removed — *"Nothing is changed until you say so"* and
  *"never in Phase 1"* are gone; the replacement promise is the true one.
- `page.tsx`: the front-door gate — **`/api/me` 401 ⇒ render `<Homepage/>` and nothing else**, no
  console skeleton, no flash of console chrome; no connection ⇒ onboarding; otherwise the screen-25
  column order (Inbox-Zero card → live feed → clusters). A mid-session `unauthenticated` from any
  `/api/*` returns the user to the homepage with *"You were signed out."* This is a surgical guard
  around the existing console, **not** a rewrite of it.
- `Chrome.tsx` + `AccountMenu.tsx`: the top-bar account menu (screen 23) replacing the bare address,
  keyboard-operable, with **Account & security**, **Settings** and **Sign out** →
  `POST /auth/logout` then a hard navigation to `/app/`.
- `lib/api.ts`: `SIGNIN_URL` / `CONNECT_URL` for the two intents.
- **Tests:** covered by slice 5's Playwright suite plus `pnpm build` (zero TypeScript errors). This
  slice writes no unit test framework of its own.

##### Slice 4 — `account-ui`

- `AccountSection.tsx` (new): screen 22 — identity, connected mailboxes with Reconnect/Disconnect and
  their exact-consequence confirm modals, the signed-in device list with per-row **Sign out** and
  **Sign out everywhere**, and **Delete account** requiring the user to type their email and naming
  the real counts from `GET /api/account`. Skeleton loading, envelope error + Retry, and an empty
  state for "no mailbox connected". Declares its own fetch paths locally — it does **not** edit
  `lib/api.ts`.
- `Settings.tsx`: mounts `AccountSection` as the first group, above the autonomy controls. No change
  to any existing control's behaviour — the Phase 7 slider, its calibration note and its
  above-ceiling warning are untouched.
- Every control implements all six component states from
  [ui.md](ui.md#component-states-required-for-every-interactive-component); every disabled control
  carries a reason.

##### Slice 5 — `e2e-phase8`

`tests/e2e/phase8/`, run against the already-running supervised server on `:8001`:

- `front-door.spec.ts` — a **signed-out** context loading `/app/` sees the headline, the five safety
  promises and exactly one CTA, and sees **no** run-status pill, **no** left rail and **no** cluster
  list. It asserts the string *"Nothing is changed until you say so"* is absent from the page.
- `auth-flow.spec.ts` — the sign-in card states the minimal-scope promise; the account menu opens by
  keyboard (`Enter`, arrows, `Escape` returns focus); **Sign out** returns the user to the homepage
  and a reload stays on the homepage.
- `account.spec.ts` — a signed-in context reaches **Settings → Account**, sees its mailbox row and at
  least one device row marked **This device**, and sees the delete confirm requiring the typed email.
  It **does not** execute the delete against the live account.
- `retry-review.spec.ts` — on a run whose ledger reports `not_reviewed > 0`, the amber bar and a
  working **Retry review** button are present, and the bar coexists with the red apply-failure bar
  when both apply. If no such run exists it **fails loudly with the reason** rather than skipping.
- `design-system.spec.ts` — no horizontal overflow at 375px / 768px / 1440px; every element carrying
  an `ok`/`warn`/`danger`/`info` state token resolves to non-empty accessible text (the
  state-is-never-colour-alone rule); the Inbox-Zero card and the live feed are both present at every
  width.

#### Gate (exact commands, run from the repo root, real APIs via `.env`, production DB driver)

```bash
uv run alembic upgrade head && uv run alembic current
uv run pytest tests/unit tests/integration -q
cd frontend && pnpm install && pnpm build && cd ..
npx playwright test tests/e2e/ --reporter=line
```

`alembic current` must print a revision hash including `0007_sessions_and_mailbox_ownership`.
`pnpm build` must exit 0 with zero TypeScript errors.

**Playwright runs against the already-running supervised server on `http://localhost:8001`. Do not
start a second server and do not kill or restart the supervised one.**

`tests/integration/test_review_recovery.py` and `tests/integration/test_identity.py` are the
load-bearing gates, alongside the Phase 8 Playwright suite. `tests/integration/test_no_body_persisted.py`,
`tests/integration/test_drive_to_zero.py` and `tests/unit/graph/test_error_channel_reducer.py` must
still pass unchanged — Phase 8 must not regress the Phase 6 durability, the Phase 7 inbox-zero
guarantees or the error-channel fix.

#### Known issues carried, not inherited

These are logged so no generator "fixes" them by weakening something. Phase 8 is judged on **its own
tests plus no new regressions**.

- **(a) `tests/integration/test_resume.py::TestFullResume` — `AttributeError` at line 103.** A fixture
  **visibility** bug, **out of scope for Phase 8**. **No generator may make this pass by weakening,
  loosening, deleting or `xfail`-ing any assertion in that test.** Leave it exactly as it is.
- **(b) The `review_failed` recovery gap** — this is the gap slice 2 closes. Until slice 2 lands,
  `POST /api/runs/{id}/apply` correctly refuses those rows; that refusal is the never-miss gate
  working, not a bug to route around.
- **(c) Pre-existing red, proven before this phase's first commit:** **5 integration failures**
  (model-shape drift, a live-mailbox flake, `RefreshError` mapping, `GmailMutator` method drift) and
  **~6 Playwright stub specs asserting Phase-2 UI that no longer exists**. One of these failing is not
  a blocker. A Phase 8 test failing is. Any failure **outside** both sets is a new regression and
  blocks the phase.
  > Slice 3 rewrites `page.tsx`'s signed-out branch and `Chrome.tsx`, which may change *which*
  > obsolete Phase-2 specs fail. Deleting or rewriting those obsolete specs to go green is **not**
  > permitted in this phase — record the before/after set instead.
- **(d) The `error` channel fix landed as commit `3893bb7`** (`keep_first_error` reducer in
  `src/graph/state.py`, covered by `tests/unit/graph/test_error_channel_reducer.py`). **Build on top
  of it. Never revert it, never re-annotate that channel, never edit that reducer or its test.** Slice
  2 does not own `src/graph/state.py` for exactly this reason.

#### Accepted deviations and follow-ups (Phase 8)

Deliberate, reviewed departures from the letter of this spec. **None of these is a defect** — each was
accepted at the Phase 8 gate. They are recorded here so a later phase neither "restores" the original
wording nor re-litigates the decision.

- **(1) FK cascade → explicit user-scoped delete.** The spec described account deletion as an FK
  cascade. SQLite does not enforce `ON DELETE CASCADE` without `PRAGMA foreign_keys=ON` per
  connection, so the cascade would have orphaned every child row on the production SQLite file while
  passing on Postgres. `src/api/account.py` deletes every `user_id`-carrying table explicitly,
  metadata-driven and user-scoped. **The spec has been corrected** ([data.md](data.md#deletion-semantics),
  [api.md](api.md#routes), [account-and-identity](capabilities/account-and-identity.md)) — the code is
  the reference, not the original wording.
- **(2) `secure` cookie flag → scheme with an ASGI-scope fallback.** The spec described deriving the
  flag from `request.url.scheme`. A scope with no `server` and no `Host` header drops the URL scheme,
  which would have shipped the session cookie without `secure` over https. `src/api/session.py:_is_https`
  falls back to the raw ASGI `scope["scheme"]`. Legacy `uid`-only cookies remain valid and are upgraded
  in place — **this phase signs nobody out**. **The spec has been corrected**
  ([api.md](api.md#session-hardening-behaviour-change-no-new-route)).
- **(3) `not_reviewed` is computed in `src/api/runs.py`, not `src/graph/remainder.py`.**
  [ui.md](ui.md) screen 24 requires the figure, and `remainder.py` does not emit it; the review-recovery
  slice computed it in the runs API instead, as a live user- and run-scoped
  `COUNT(decisions WHERE review_state IN ('provisional','review_failed'))` that is **never cached**.
  That is the correct behaviour under concurrency — a retry in flight must not be read from a stale
  ledger snapshot — and it was accepted on that basis. **Follow-up, not a defect:** move the count into
  `remainder.py` when screen 24 is next revised, keeping it a live query rather than a cached ledger
  field. Until then `remainder.py`'s documented invariant at line 45,
  `inbox_remaining == sum(remainder.values()) + distance_to_zero`, is unaffected: `not_reviewed` is an
  additional live figure, not a remainder bucket.

**Phase 9 candidate (do not fix in Phase 8, do not spec a fix yet).** `second_pass_reviewer` audits
only rows whose `proposed_action == "archive"` (`src/graph/nodes_review.py:171-175`), so a pending
`digest`-proposed row can be finalised as `reviewed` without a reviewer verdict, and `digest` is a
mutable action (`src/tools/actions.py:140`). This matches normal-run behaviour exactly and is
pre-existing — `retry-review` did not introduce it and must not diverge from the normal run to paper
over it. Recorded for a Phase 9 decision on whether the reviewer's audit scope should widen to every
mutable proposed action.

#### Production safety (binding on every implementer and the auditor)

`zero_inbox.db` holds **~12,500 real decisions for 2 real accounts**, and **a real 903-thread resumed
run is applying against the live account right now.**

- **Never write an ad-hoc script against the real DB.** `get_settings()` is cached and does **not**
  honour an env override from a standalone script — a previous agent polluted the real DB exactly that
  way. Every test uses the existing `_isolated_db` fixture pattern.
- **Do not kill or restart the supervised server on `:8001`**, and do not start a second one.
- **Do not launch a full-inbox run** while building or gating. The 220-thread fixture is the test
  surface.
- **Never execute `DELETE /api/account`, `DELETE /api/account/connections/{id}` or
  `POST /api/account/sessions/revoke-all` against a real account.** Every test of those routes runs
  against `_isolated_db` with a synthetic user. Playwright asserts the delete **confirm dialog** and
  stops there.
- **Do not start a `retry-review` against the live run in flight.** Slice 2's integration test seeds
  its own completed run in `_isolated_db`.
- Migration `0007` is the only sanctioned write to real user rows, and only via
  `uv run alembic upgrade head` — never by hand, never by script. Its duplicate-mailbox guard must
  **raise**, never resolve a conflict.

#### How the user tests it

1. `uv run alembic upgrade head`, then `cd frontend && pnpm build && cd .. && uv run python -m src`.
2. Open **http://localhost:8001/app/ in a private window** (no session). You get a **homepage**, not
   an empty console: the headline *"An inbox that holds only what needs a human"*, the honesty band
   describing the real run that stopped and told the truth rather than archiving 350 unread threads,
   the three-step explanation, the five safety promises, and **one** button — **Sign in with Google**.
3. Click it. The sign-in card says signing in asks Google for **your name and email only**, and that
   mail access is a separate step. Complete it. Check the Google consent screen: it asks for
   **no Gmail scope**. You now have an account with no mailbox attached.
4. You land on **onboarding step 1 — Connect your mailbox**, with the scopes in plain English and the
   *true* promise (it archives on its own once confident; it never deletes; everything is undoable).
   The old sentence *"Nothing is changed until you say so"* is gone from the whole app.
5. Connect. **Step 2** tells you what is about to happen before it happens, and offers
   **Start in dry-run instead** if you'd rather watch a full pass change nothing.
6. **Step 3 — the moment of trust.** The live feed fills the screen and rows arrive with **no click**.
   Above it: *"Nothing has been archived yet — the reviewer checks every decision first."* When the
   agent keeps its first thread for a never-miss reason, a callout names it —
   *"Kept: '…' — you've replied to this sender before."* **The product proves it protects before it
   proves it cleans.** On the first real archive the line becomes *"Archiving now — N so far. Undo any
   of it."* with a live **Undo this run**.
7. When the run finishes, the **Inbox-Zero card** and the definition are where they have always been,
   and the feed collapses to its one-line summary. Reload: you are a returning user now, and you land
   straight on the daily loop — card, feed, clusters — with onboarding never shown again.
8. **Open the account menu** in the top bar (keyboard works: `Enter`, arrows, `Escape`). Go to
   **Account & security**. You see your identity, your connected mailbox with its status and last
   sync, and **your signed-in devices** — with **This device** marked. Sign in from a second browser
   and watch a second row appear.
9. Click **Sign out** on the *other* device's row. Reload that browser: it is signed out and back on
   the homepage. Then try **Sign out everywhere** from the second browser — every session including
   the current one drops to the homepage. **A session you can see is a session you can revoke.**
10. **Try to connect the same Gmail address from a second account.** You get a clear
    *"{address} is already connected to another Zero Inbox account"* — not a silent second agent
    mutating one inbox.
11. Read the **Disconnect** and **Delete account** confirm dialogs (do **not** confirm them on your
    real account). Each names the exact consequence and the exact counts, and each states the honest
    part: **your Gmail is untouched — archived mail stays archived and nothing is deleted.**
12. **Retry review.** If a run left threads that never got past the reviewer, the Inbox-Zero card now
    shows an amber bar — *"N threads never got past the reviewer, so they were left in your inbox
    rather than archived unseen"* — with **Retry review**. Click it and watch the reviewer rows arrive
    in the live feed. Threads the reviewer passes are archived with undo tokens; threads it flips to
    keep stay in your inbox. If the reviewer fails again the bar returns with the new reason — it
    never quietly marks anything reviewed.
13. **Check Gmail.** Nothing in Trash. People, Urgent and Legal still in the inbox. Nothing about
    signing in, signing out, disconnecting or deleting changed a single message.
14. Resize the window to phone width and back. Nothing overflows; the Inbox-Zero card and the live
    feed are still the two things you can see.
15. **Real in Phase 8:** the signed-out homepage, minimal-scope sign-in separate from mailbox consent,
    the three-step onboarding with the never-miss moment of trust, the steady-state daily loop, the
    account menu, Account & security with mailbox and device management and account deletion,
    globally unique mailbox ownership, session hardening, the design system, and **Retry review**.
    **Labelled stubs remaining:** none.

> **If step 2 shows a console instead of a homepage, or step 3 requires a click to see anything, or
> any step 11 dialog fails to state that Gmail is untouched — Phase 8 is not done, regardless of what
> the other steps show.**

---

### Phase 9 — Inbox Zero Without a Human

**Goal.** The inbox reaches **actual zero with no human intervention**, and the user owns the
taxonomy that gets it there. Three things make that true, and one thing has to be fixed before any of
them is safe to ship.

1. **The review gate stops passing vacuously** (item zero — the safety foundation, below).
2. **A never-miss verdict becomes a label, not a hold.** The 227 threads the agent is holding in the
   inbox are archived into `ZeroInbox/Urgent` / `ZeroInbox/Important` / `ZeroInbox/People` — one
   click away, marked with the reason, undoable. The user's instruction, verbatim: *"I have not
   replied to anything in my inbox that I can see right now. We should move everything into relevant
   labels."*
3. **The taxonomy is derived from the user's actual mail**, not from a fixed default list — and the
   user can redo it whenever they like.
4. **When the taxonomy changes, the tool re-organises everything.** The user's instruction, verbatim:
   *"Re-organise everything."* All ~10,336 past decisions, including relabelling threads that are
   already archived — not just the current inbox, and never a silent sample.

**The measured state this phase eliminates** (the user's real inbox after the last run):
**227 `held_by_never_miss` · 76 `category_keep` · 46 `below_threshold` · 16 `needs_your_call` ·
0 `unclassified` = 365.** Target: **0**.

#### Sender concentration — the measured evidence, and why the taxonomy must be derived

Measured on the live account. **Use these numbers directly; do not re-derive them, and do not query
the live DB to confirm them.** The current taxonomy is 8 generic categories (newsletters, legal,
notifications, receipts, outreach, people, urgent — plus one junk category, see *test pollution*
below). The user's mail is extremely concentrated, and none of that concentration is served:

| Cluster | Threads | Senders |
|---------|--------:|---------|
| Facebook | ~1,586 | `notification@facebookmail.com` (503), `notification+kr4knbaqrsga@facebookmail.com` (489), `reminders@facebookmail.com` (250), `friendsuggestion@facebookmail.com` (184), `notification@priority.facebookmail.com` (130) |
| BookMyShow | ~635 | `no-reply@entertainment.bookmyshow.com` (423), `no-reply@updates.bookmyshow.com` (212) |
| Jagriti Theatre | 334 | `contact@jagrititheatre.com` |
| Apple | ~176 | `no_reply@email.apple.com`, `noreply@email.apple.com` |
| PayPal | 78 | |
| Twitter | 40 | |

All of it currently collapses into **Notifications**. That is precisely why confidence lands below
the floor and why 16 threads found no fitting category. A taxonomy derived from *this* inbox
obviously wants a **Social/Facebook** category, an **Events/Tickets** category (BookMyShow + Jagriti
≈ 970 threads, about **one fifth of his mail**) and a **Billing/Subscriptions** category — each
classifiable **from the sender alone**, at very high confidence, with **no LLM judgement**.

**The design insight this phase builds on — and the reason the success measure is reachable rather
than aspirational.** A handful of senders account for most of the volume. Discovery therefore exists
to **convert sender concentration into deterministic tier-1 rules**, so the dominant senders stop
needing judgement at all: they classify deterministically at confidence well above the floor, and the
LLM is reserved for the genuine long tail. `needs_your_call` and `below_threshold` go to zero because
the mail that produced them is no longer being asked of the model.

**This runs through the existing [cost-tiered-triage](capabilities/cost-tiered-triage.md) tier-1 /
tier-2 machinery. Phase 9 adds no second classification path.** Discovery emits `Rule` rows
(`kind=deterministic`, `source=mined`, `status=active`) that the existing `apply_deterministic_rules`
node consumes unchanged; sender-history evidence flows through the existing tier-2 path. A parallel
classifier would be a "plumbed but never wired" seam and would also make the success measure
unreachable, because the run's own `counts.by_tier` is the proof the concentration was exploited.

#### Test pollution on the live account — `e2e-actions-test`

A category `key='e2e-actions-test'`, `name='E2EActionsTest'`, `default_action=archive` exists on the
user's live account (`6b4ab0f4…`), left behind by a test that ran against production data. Nothing is
filed under it, so it is harmless — but the user will see it the moment he opens the taxonomy editor,
which undermines confidence in exactly the surface this phase is about. Three requirements, all
binding:

1. **Removal goes through the normal taxonomy path**, in a gated slice — `GET
   /api/categories/{id}/usage` then `DELETE /api/categories/{id}` against the running server. **An
   ad-hoc script against `zero_inbox.db` is forbidden** and must not be specced, written or run.
2. **Verify, do not assume.** The deletion is permitted only after the usage endpoint reports
   **zero** `decisions` (and zero `rules`) referencing the category. A non-zero count stops the
   operation and is reported — it is never deleted "because it looked empty".
3. **Root-cause it.** A structural guard makes it impossible for a test to create a category on a
   real user's account again — the same defect class as the cached-`get_settings()` incident that
   polluted the real DB earlier. **Slice 8 (`test-isolation-guard`) owns it.** Row deletion alone is
   not an acceptable close-out.

The 227 have two distinct causes, already diagnosed — do not re-derive them:

- **A real bug — self-addressed mail (18 threads, `decided_by="sender_history"`).** All 18 are from
  `psykrsna@gmail.com`, **the user's own address**, held with *"You have replied to
  psykrsna@gmail.com before (24 replies of 0 received)"*. `_accumulate_recipients`
  (`src/channels/gmail/adapter.py:458`) harvests `To`/`Cc` from the user's own `SENT` mail, so the
  user is his own most-replied-to correspondent. `24 replies of 0 received` is the signature.
- **The time-sensitive guard working as told (the rest, `decided_by="llm"` 186 / `"reviewer"` 23).**
  Top held senders: `no_reply@email.apple.com` (44), `noreply@email.apple.com` (39),
  `no-reply@accounts.google.com` (39), `reminders@facebookmail.com` (27), `security@facebookmail.com`
  (11) — automated no-reply senders correctly judged time-sensitive. The verdict is right; only its
  *expression* was wrong. These archive under their label from Phase 9. **A `no-reply` sender can
  never be a genuine correspondent** — a strong, free taxonomy signal this phase makes first-class.

Capabilities: [never-miss-safeguards](capabilities/never-miss-safeguards.md) (**redefined** —
label, not hold — plus the review-gate and correspondent-truth integrity fixes),
[inbox-derived-taxonomy](capabilities/inbox-derived-taxonomy.md) (new),
[drive-to-inbox-zero](capabilities/drive-to-inbox-zero.md) (extended: the ledger must reach 0),
[taxonomy-management](capabilities/taxonomy-management.md) (extended: `Important`, the reconciled
guard), [gmail-actions-and-undo](capabilities/gmail-actions-and-undo.md) (extended: never-miss
labelled archive, bulk relabel, bulk undo).

#### Item zero — a live safety gap. Slice 1. Specced first because nothing else is safe until it lands

`digest` decisions reach the same `mutator.archive_and_label(remove_label_ids=[INBOX])` as archives
(`src/tools/actions.py:140` permits `proposed_action in ("archive", "digest")`), but
`second_pass_reviewer` audits **only** `proposed_action == "archive"`
(`src/graph/nodes_review.py:171-174`). `finalise_review` then upgrades `review_state` across the
run's **whole** decision set, so a `digest` row reads `reviewed` having never been reviewed and the
`NotReviewedError` gate passes **vacuously**. **Live: 171 digest decisions, 44 already applied
unreviewed.**

The approved fix is the deeper one, and it closes the class rather than the instance:

- **`finalise_review` marks only rows that were ACTUALLY audited.** It receives the explicit set of
  audited item ids and upgrades exactly those to `reviewed`; every other row stays `provisional`.
  `review_state="reviewed"` becomes a claim about the **decision**, not about the run.
- **The reviewer covers every mutating action**, not the `archive` label specifically. Scope is
  defined by mutability: `nodes_review.REVIEWABLE_ACTIONS` and `actions.MUTABLE_ACTIONS` are one
  contract, asserted equal by a test, so a future third mutating action cannot silently reopen the
  hole.
- **This matters more after the reframe, not less** — far more mail is mutated once never-miss
  archives.
- **Historic rows:** migration `0008` downgrades to `provisional` every `reviewed`-but-never-audited
  row that is **not yet applied**. The 44 already-applied rows are not rewritten — they are counted in
  a new `unreviewed_applied` ledger figure and stated in the UI. The honesty rule outranks a tidy
  migration.

#### Invariants — this phase redefines exactly one rule and adds no path around any other

- **NEVER delete, trash or spam-report.** No such method on the mutator; asserted structurally.
- **No never-miss thread is ever archived without its label.** A bare archive of a
  `NEVER_ARCHIVE_KEYS` category is not a permitted operation. If no never-miss label resolves, the
  thread **stays in the inbox** and the ledger names it under `no_never_miss_label`.
- **Every mutation carries an undo token**, and the whole re-organisation is reversible as **one**
  bulk operation.
- **`dry_run` is absolute** — for triage and for the re-organisation job.
- **`NotReviewedError` still fires before the mutator and is not bypassable by `force=True`.** No
  Phase 9 path (auto-apply, re-organisation, bulk relabel) passes `force=True` or writes
  `review_state` outside `finalise_review` / `upgrade_review_state`.
- **No message bodies persisted or sent to the LLM** beyond headers, subject and the redacted 200-char
  snippet. The taxonomy census carries counts and addresses — **no subjects in the census
  aggregates**, and gap-set subjects are truncated to 60 chars exactly as the live feed already is.
- **`NEVER_ARCHIVE_KEYS` (`urgent`, `people`, `legal`, + `important`) can never be set to
  `default_action=archive`** — the guard is **kept and extended, not deleted**. Reconciliation with
  the reframe is written down at
  [never-miss-safeguards § NEVER_ARCHIVE_KEYS reconciled](capabilities/never-miss-safeguards.md#never-archive-keys-reconciled):
  a category-wide archive default is a bulk silent sweep of the mail a human must see; a never-miss
  archive is per thread, caused by the signal firing, always labelled and always undoable. Those are
  different operations. The guard blocks the first and never blocked the second.
- **Honesty:** *"we reached zero"* never appears unless the inbox is genuinely empty. A run or job
  that could not finish sets `error_message`, emits its failure event and returns `ok=false`.

#### Slices

Eight slices, **fully disjoint file ownership** — no two slices own the same file, so all eight can be
generated concurrently. Where one slice consumes something another writes it is a **spec-contract
dependency only** (signatures pinned below); all seven land in the same gate. **Slice 1 is the safety
foundation: no other slice may weaken, loosen, `xfail` or delete any assertion it adds, and no other
slice may edit `src/graph/nodes_review.py` or `src/graph/persistence.py`.**

| # | Slice | Owns (disjoint paths) | Depends on |
|---|-------|----------------------|-----------|
| 1 | `review-gate-integrity` | `src/graph/nodes_review.py`, `src/graph/persistence.py`, `alembic/versions/0008_review_state_integrity.py` (new), `tests/unit/graph/test_review_scope.py` (new), `tests/unit/graph/test_finalise_review_scope.py` (new), `tests/integration/test_review_gate_integrity.py` (new) | none |
| 2 | `correspondent-truth` | `src/channels/gmail/adapter.py`, `src/channels/base.py`, `src/tools/correspondents.py` (new), `src/tools/never_miss.py`, `tests/unit/tools/test_correspondents.py` (new), `tests/unit/channels/test_sender_history_self.py` (new) | none |
| 3 | `never-miss-as-label` | `src/graph/autonomy.py`, `src/graph/nodes_autonomy.py`, `src/graph/remainder.py`, `src/tools/actions.py`, `tests/unit/graph/test_never_miss_label.py` (new), `tests/unit/tools/test_actions_never_miss.py` (new), `tests/integration/test_inbox_zero_reframe.py` (new), `tests/integration/test_drive_to_zero.py` (**one assertion only** — the old never-miss semantic), `tests/fixtures/phase9/remainder_365.py` (new) | none (spec-contract: consumes slice 1's `finalise_review` signature and slice 2's `is_no_reply` / self-address exclusion) |
| 4 | `taxonomy-discovery` | `src/tools/taxonomy_discovery.py` (new), `src/tools/taxonomy.py`, `src/tools/rules.py`, `src/prompts/taxonomy_discovery.md` (new), `src/api/taxonomy_discovery.py` (new), `src/api/categories_usage.py` (new), `src/api/__init__.py`, `tests/unit/tools/test_taxonomy_discovery.py` (new), `tests/unit/tools/test_mined_sender_rules.py` (new), `tests/integration/test_taxonomy_discovery.py` (new) | none |
| 5 | `reorganisation-job` | `src/jobs/reorganise.py` (new), `src/api/reorganise.py` (new), `src/db/models.py`, `alembic/versions/0009_reorg_jobs.py` (new), `tests/unit/jobs/test_reorganise.py` (new), `tests/integration/test_reorganise.py` (new), `tests/fixtures/phase9/decisions_10k.py` (new) | none (spec-contract: calls slice 3's `archive_to_never_miss_label` / `relabel_decision` and slice 4's discovery result shape) |
| 6 | `frontend-phase9` | `frontend/src/components/TaxonomyDiscovery.tsx` (new), `frontend/src/components/ReorganiseCard.tsx` (new), `frontend/src/components/Settings.tsx`, `frontend/src/components/InboxZeroCard.tsx`, `frontend/src/components/TaxonomyEditor.tsx`, `frontend/src/lib/types.ts`, `frontend/src/app/page.tsx` | none |
| 7 | `e2e-phase9-and-spec-hygiene` | `tests/e2e/phase9/**` (new), `tests/e2e/phase1/**`, `tests/e2e/phase2/**`, `tests/e2e/phase3/**` | none |
| 8 | `test-isolation-guard` | `tests/conftest.py`, `tests/isolation.py` (new), `tests/unit/test_isolation_guard.py` (new), `scripts/README_cleanup.md` (new, procedure only — **no executable script**) | none (spec-contract: the `e2e-actions-test` cleanup step calls slice 4's `GET /api/categories/{id}/usage`) |

**Path-disjointness notes (read before writing a line of code).**

- Slice 1 is the **only** slice that edits `src/graph/nodes_review.py` or `src/graph/persistence.py`.
  Slice 3 and slice 5 *call* `finalise_review` / `upgrade_review_state`; if either appears to need a
  change inside those files, that is a spec question — raise it, do not edit across the seam.
- Slice 3 is the **only** slice that edits `src/tools/actions.py`. Slice 5 imports
  `apply_decision` / `archive_to_never_miss_label` / `relabel_decision` and adds no mutation path of
  its own.
- Slice 5 is the **only** slice that edits `src/db/models.py` or adds migration `0009`. Slice 1 owns
  `0008` and adds **no** model change (the `review_state` column already exists). Pinned chain:
  `0008.down_revision = "0007_sessions_and_mailbox_ownership"`, `0009.down_revision = "0008_review_state_integrity"`.
- Slice 4 is the **only** slice that edits `src/api/__init__.py` (it mounts both new routers:
  `taxonomy_discovery.router` **and** slice 5's `reorganise.router` — slice 5 exports the router and
  does not mount it).
- Slice 6 is the only slice touching `frontend/`. Slice 7 is the only slice touching `tests/e2e/`.
- Slice 4 is the **only** slice that edits `src/tools/rules.py` (it adds the mined-sender-rule
  materialiser and the `important` seed entry). It **must not** change the tier-1 matcher semantics
  in `apply_rules` — discovery produces rows the existing matcher already understands.
- Slice 8 is the **only** slice that edits `tests/conftest.py`. No other slice may add, relax or
  monkeypatch around the isolation guard it installs; a test that needs a real account is a spec
  question, not a local override.
- **Fixtures are owned, not shared.** Slice 3 owns `tests/fixtures/phase9/remainder_365.py` (the
  227/76/46/16/0 distribution) and slice 5 owns `tests/fixtures/phase9/decisions_10k.py`. Slice 4's
  integration test **imports** `remainder_365` and does not edit it. The existing 220-thread fixture
  is unchanged and unowned by this phase.
- **`tests/integration/test_drive_to_zero.py` is edited by slice 3 only, and only to update the single
  assertion that encodes the OLD never-miss semantic** (a `held_by_never_miss` thread remains in the
  inbox). Every other assertion in that file is untouched, and the change is named explicitly in the
  slice's report. No other slice may edit that file.

**Pinned cross-slice contracts** (each slice codes against these, not another slice's files):

```python
# slice 1 writes; slices 3 and 5 call
graph.persistence.finalise_review(
    session, *, run_id: str, user_id: str, decisions: list[dict],
    audited_item_ids: list[str],              # NEW, REQUIRED — only these become "reviewed"
    review_failed_item_ids: list[str] | None = None,
) -> dict            # {"reviewed": n, "review_failed": n, "not_audited": n, "flipped": [...]}
graph.nodes_review.REVIEWABLE_ACTIONS: frozenset[str]   # == tools.actions.MUTABLE_ACTIONS

# slice 2 writes; slices 3 and 4 call
tools.correspondents.is_no_reply(email: str) -> bool
tools.correspondents.is_self_address(email: str, *, account_email: str, aliases: list[str]) -> bool
tools.correspondents.NO_REPLY_PATTERNS: tuple[str, ...]

# slice 3 writes; slice 5 calls
tools.actions.MUTABLE_ACTIONS: frozenset[str]            # {"archive", "digest"}
tools.actions.archive_to_never_miss_label(session, user_id, decision_id, *, mutator,
                                          label_lookup, dry_run) -> ActionLog
tools.actions.relabel_decision(session, user_id, decision_id, *, mutator, label_lookup,
                               dry_run, keep_archived: bool) -> ActionLog
graph.autonomy.never_miss_category_key(decision, item, *, vip, sender_stats) -> str | None

# slice 4 writes; slices 5 and 6 call
tools.taxonomy_discovery.build_census(session, *, user_id) -> list[dict]
tools.taxonomy_discovery.propose_taxonomy(session, *, user_id, census, gap_set) -> dict
    # {"proposal": [{key, name, description, default_action, rationale, evidence_senders,
    #                covered_threads}], "coverage": {...}, "partial": bool, "partial_reason": str|None}
tools.taxonomy_discovery.mine_sender_rules(census, proposal) -> list[dict]
    # tier-1 Rule rows: {matcher: {from_email|from_domain|list_id}, action, category_key,
    #                    confidence, kind:"deterministic", source:"mined", status:"active"}
tools.taxonomy_discovery.materialise_rules(session, *, user_id, rules) -> dict   # {created, updated}

# slice 5 writes; slice 6 calls
jobs.reorganise.start(session, *, user_id, dry_run: bool) -> str        # reorg_job id
jobs.reorganise.ledger(session, *, job_id) -> dict
    # {"total", "done", "skipped": {reason: n}, "status", "undoable"}
```

##### Slice 1 — `review-gate-integrity` (item zero)

- `nodes_review.py`: `REVIEWABLE_ACTIONS = frozenset({"archive", "digest"})`, imported from / asserted
  equal to `tools.actions.MUTABLE_ACTIONS`. `second_pass_reviewer` selects
  `proposed_action in REVIEWABLE_ACTIONS`, and returns the **set of item ids it actually audited**
  (successfully-reviewed batches only) in state as `audited_item_ids`.
- `_upgrade_review_state` passes `audited_item_ids` through; `finalise_review` marks only those rows
  `reviewed`, marks `review_failed_item_ids` `review_failed`, and **leaves every other row untouched**
  — reporting `not_audited` so the run ledger can state it.
- `persistence.upgrade_review_state` keeps its signature but gains a hard rule: calling it with
  `item_ids=None` **and** `state="reviewed"` raises — the run-wide upgrade that caused this defect is
  no longer expressible.
- `0008_review_state_integrity.py`: set `review_state='provisional'` where
  `review_state='reviewed' AND proposed_action NOT IN ('archive') AND status <> 'applied'` for
  pre-Phase-9 runs. Already-applied rows are **not** rewritten; the migration logs their exact count
  (expected 44) so it can be reported. Idempotent and re-runnable.
- **Tests:** `test_review_scope.py` — the two sets are equal (fails if either is changed alone); a
  `digest` proposal is included in the reviewer's batch. `test_finalise_review_scope.py` — a run where
  the reviewer audits 3 of 10 rows leaves exactly 3 `reviewed` and 7 `provisional`; a run-wide upgrade
  raises. **Both fail against pre-Phase-9 code.**
  `test_review_gate_integrity.py` (integration, `_isolated_db`) — a run seeded with 171 `digest` +
  archive decisions and the reviewer stubbed to fail: **zero** mutations, every row `review_failed`,
  `apply_decision` raised `NotReviewedError` before the mutator (spy proves the mutator was never
  called), including with `force=True`.

##### Slice 2 — `correspondent-truth`

- `tools/correspondents.py` (new): `is_no_reply` (matches `no-reply`/`noreply`/`no_reply`/
  `donotreply`/`do-not-reply`, case-, dot-, hyphen- and underscore-insensitive on the local part) and
  `is_self_address` (normalises Gmail dots and `+tags`; compares against `account_email` and the
  alias list).
- `adapter.py`: `sender_history()` passes `self._account_email` and the `sendAs` alias list into
  `_accumulate_recipients`, which **skips** any self address entirely — no `SenderSignal`, no
  `replied_count`, no `ever_replied`. Aliases are fetched once via
  `users.settings.sendAs.list`, best-effort: on failure fall back to `account_email` alone and log at
  WARNING (never fail the run).
- `channels/base.py`: `SenderSignal` gains `is_no_reply: bool` so the flag reaches the census and the
  guards without a second parse.
- `tools/never_miss.py`: defence in depth — `apply_reply_history_guard` ignores an `ever_replied`
  claim for a self address or a no-reply sender even if a stale `sender_profiles` row still asserts
  one. Both layers tested independently.
- **Tests:** `test_sender_history_self.py` — a `SENT` fixture addressed to `psykrsna@gmail.com`,
  `psy.krsna@gmail.com` and `psykrsna+news@gmail.com` produces **zero** reply signals for all three,
  and a genuine correspondent in the same fixture still produces one. `test_correspondents.py` — the
  full no-reply pattern matrix including the five measured live addresses, plus normal addresses that
  must not match (`reply@`, `noreplyneeded@example.com` is documented as matching by design).
  **Both fail against pre-Phase-9 code.**

##### Slice 3 — `never-miss-as-label` (the reframe)

- `autonomy.py`: `never_miss_category_key(...)` — the deterministic first-match resolution
  (VIP/`ever_replied` → `people`; `time_sensitive` → `urgent`; other reviewer hold → `important`;
  else `None`). `classify_autonomy_state` is unchanged: `held_by_never_miss` still names the reason —
  it now describes *why it was labelled*, not *why it stayed*.
- `nodes_autonomy.py`: after the never-miss chain, a `held_by_never_miss` decision whose never-miss
  category resolves is set to `proposed_action="archive"` with that `category_id`, its reasoning
  appended with one plain sentence naming the label. Unresolvable → stays `keep`, ledger reason
  `no_never_miss_label`. **`needs_your_call` and below-floor rows are untouched.**
- `remainder.py`: `held_by_never_miss` no longer counts toward `inbox_remaining` when it was archived
  to its label; new buckets `no_never_miss_label` and `unreviewed_applied` (the honest count of the 44
  historic rows). The documented invariant
  `inbox_remaining == sum(remainder buckets) + distance_to_zero` still holds.
- `actions.py`: `MUTABLE_ACTIONS`; `archive_to_never_miss_label` (refuses to run without a resolved
  category label — that refusal is the guarantee, not an edge case); `relabel_decision(...,
  keep_archived: bool)` for the re-organiser, which removes the old `ZeroInbox/*` label, adds the new
  one, and **never re-adds `INBOX`** when `keep_archived=True`. Both go through the same
  `NotReviewedError` / `dry_run` / undo-token path as `apply_decision` — no second mutation path
  exists.
- **Tests:** `test_never_miss_label.py` — the resolution table, including the unresolvable case;
  `NEVER_ARCHIVE_KEYS` still rejects `default_action="archive"` while
  `archive_to_never_miss_label` succeeds into the same category.
  `test_actions_never_miss.py` — a never-miss archive without a resolvable label raises and calls the
  mutator zero times; every mutation writes a non-null undo token; `dry_run` blocks both new
  functions; `force=True` does not bypass `NotReviewedError`.
  `test_inbox_zero_reframe.py` (integration, `_isolated_db`, real NIM via `.env`) — the load-bearing
  gate, below.

##### Slice 4 — `taxonomy-discovery`

- `build_census` — deterministic, no LLM: per sender/domain/`List-Id`, `thread_count`,
  `unread_count`, `ever_replied` (post slice 2), `is_no_reply`, `has_unsubscribe`, `in_gap_set`.
  **No subjects in the aggregate rows.**
- `propose_taxonomy` — census + gap set → a proposal with per-category evidence and coverage.
  Chunked at ≤ 200 senders per call; uses the existing model-fallback chain and throttle. On total LLM
  failure returns the deterministic-signal proposal with `partial=true` and a stated reason — and
  **never** the seed six presented as derived. Validation rejects any evidence-free category.
- **`mine_sender_rules` — the load-bearing step, and it runs through the EXISTING tier-1/tier-2
  machinery. There is no second classification path in Phase 9, and no generator may introduce one.**
  Approving a proposal materialises, for every sender / domain / `List-Id` in a category's evidence
  list that clears the concentration bar (**≥ 10 threads** and a single dominant category), a
  `Rule` row using the **existing** `src/db/models.py:Rule` schema and the **existing** enums —
  `kind=RuleKind.DETERMINISTIC`, `source=RuleSource.MINED`, `status=RuleStatus.ACTIVE` — matched by
  the **unchanged** `tools.rules.apply_rules` matcher in the **unchanged**
  `graph.nodes.apply_deterministic_rules` node. These decisions land `decided_by="rule"` with
  `rule_id` set, at confidence **well above `confidence_floor`**, at zero token cost. Sender-history
  evidence continues to flow through the existing **tier-2** path; discovery writes no classifier of
  its own, calls no LLM at triage time, and adds no node to the graph.
  - Why this is what makes the success measure reachable: the five Facebook addresses (~1,586), the
    two BookMyShow addresses (~635) and `contact@jagrititheatre.com` (334) — roughly **half the
    inbox** — stop being asked of the model at all. `needs_your_call` and `below_threshold` are
    produced by the LLM being asked to squeeze concentrated automated mail into an ill-fitting
    generic category; remove the question and both buckets go to zero deterministically instead of
    hopefully. The LLM is reserved for the genuine long tail.
  - A mined rule is never silently destructive: it may not target a `NEVER_ARCHIVE_KEYS` category
    with `action="archive"` (`_validate_action` applies unchanged), and every mined rule records its
    evidence (`thread_count`, sender) so the user can see why it exists and disable it.
  - Re-running discovery **updates** the matching mined rule in place rather than accumulating
    duplicates; a `source=user` rule is never overwritten by a mined one.
- `api/categories_usage.py` — `GET /api/categories/{id}/usage` → `{decisions: n, rules: n, items: n}`,
  read-only, user-scoped. This is the **verification** step that must precede any category deletion,
  including the `e2e-actions-test` cleanup. Deletion with a non-zero count is refused.
- `api/taxonomy_discovery.py` — `POST /api/taxonomy/discover` (proposal only, mutates nothing),
  `POST /api/taxonomy/apply` (creates/renames/retires categories and their labels, returns the diff
  and whether a re-organisation is recommended). Both user-scoped, standard envelope.
- `tools/taxonomy.py` — adds `important` to the seed set and to `NEVER_ARCHIVE_KEYS`; the
  `_validate_action` guard is otherwise **unchanged**.
- **Tests:** `test_taxonomy_discovery.py` — census correctness on the sender fixture; evidence-free
  category rejected; LLM-failure fallback marked partial.
  `test_mined_sender_rules.py` — the measured concentration fixture (5 Facebook addresses, 2
  BookMyShow addresses, Jagriti, Apple, PayPal, Twitter, at the measured counts) yields a mined rule
  for every sender ≥ 10 threads; each mined rule is matched by the **unchanged**
  `tools.rules.apply_rules` (asserted by calling that function directly — the seam is tested, not
  assumed); a mined `archive` rule targeting a `NEVER_ARCHIVE_KEYS` category is rejected; re-running
  discovery updates in place and creates zero duplicates and overwrites no `source=user` rule.
  `test_taxonomy_discovery.py` (integration, real NIM) — over the **365-row remainder fixture**
  (227/76/46/16/0, the full set, not a sample) the discovered taxonomy re-triages to
  `needs_your_call == 0` and `below_threshold == 0`, against 16 and 46 under the seed taxonomy —
  **and `counts.by_tier` shows the majority resolved by tier 1/2 with `decided_by="rule"`, proving
  the concentration was exploited deterministically rather than re-asked of the model.** A run that
  reaches zero with the concentrated senders still `decided_by="llm"` **fails this test.**
  `GET /api/categories/{id}/usage` returns the true counts for a category with and without
  referencing decisions.

##### Slice 5 — `reorganisation-job`

- `models.py` + `0009`: `reorg_jobs` (`id`, `user_id`, `status`, `total`, `done`, `skipped` JSON,
  `cursor`, `dry_run`, `started_at`, `finished_at`, `error_message`) and
  `action_log.reorg_job_id` (nullable) so bulk undo is a single indexed query.
- `jobs/reorganise.py`: re-classifies **every** decision for the user under the new taxonomy through
  the existing graph and the existing reviewer, then mutates through slice 3's functions. Resumable
  from `cursor`; runs under the existing process-wide throttle; `429`/`quotaExceeded` backs off and
  retries rather than dropping a thread. **Never caps, never samples.** Every non-mutated thread is
  counted under exactly one reason in `skipped`, and `done + sum(skipped.values()) == total` is an
  asserted invariant. Emits `reorg_progress` at least every 3 s (Phase 7 no-silent-beat rule).
- `api/reorganise.py`: `POST /api/reorg` (409 if a reorg or an applying run is in flight),
  `GET /api/reorg/{job_id}`, `POST /api/reorg/{job_id}/cancel`, `POST /api/reorg/{job_id}/undo`
  (**bulk undo, one operation, idempotent**). Exports `router`; slice 4 mounts it.
- **Tests:** `test_reorganise.py` (unit) — ledger arithmetic; the `409` paths; bulk undo idempotency.
  `test_reorganise.py` (integration, `_isolated_db`) — a **10,000-decision** fixture (large enough
  that a sampled and a full run differ observably): every row accounted for; already-archived threads
  are relabelled and **not** returned to the inbox; kill at ~40 % and resume completes with zero
  duplicate mutations and strictly fewer LLM calls on the resume; bulk undo restores every pre-job
  label set and a second call makes zero Gmail calls; `dry_run=true` mutates nothing; a
  `review_failed` row is counted `not_reviewed` and never mutated.

##### Slice 6 — `frontend-phase9`

- `TaxonomyDiscovery.tsx` — **Rebuild my categories from my mail**: the proposal as a diff
  (keep/rename/merge/add/retire), each row showing its evidence senders and thread count, the coverage
  figure, and the gap-threads-resolved figure. Editable before approval. Approving offers
  **Re-organise everything** with a plain statement of what it will do and how long it will take.
- `ReorganiseCard.tsx` — live progress (`done / total`, current phase), the skipped-by-reason table
  rendered **while it runs, not only at the end**, Cancel, and **Undo the whole re-organisation** as
  one button. A partial job renders amber with its reasons, never green.
- `InboxZeroCard.tsx` — the ledger reaches `0`, with the new `no_never_miss_label` and
  `unreviewed_applied` rows shown when non-zero. The card must state, in plain words, that never-miss
  mail was **archived under its label, not deleted, and is one click away** — with the label names as
  Gmail links. "We reached zero" renders **only** when `inbox_remaining == 0`.
- `TaxonomyEditor.tsx` / `Settings.tsx` — the `NEVER_ARCHIVE_KEYS` categories keep their disabled
  archive control **and now carry the explanatory note**: *"kept for you — archived under its own
  label, never swept as a category."*
- All six component states from [ui.md](ui.md#component-states-required-for-every-interactive-component);
  state is never colour alone.

##### Slice 7 — `e2e-phase9-and-spec-hygiene`

- `tests/e2e/phase9/` against the already-running supervised server on `:8001`:
  `inbox-zero.spec.ts` (the card reaches 0 and names the labels; "we reached zero" is absent when the
  ledger is non-zero), `taxonomy-discovery.spec.ts` (discover → proposal with evidence → edit →
  approve → re-organise offered), `reorganise.spec.ts` (progress visible with **no clicks**, skipped
  reasons rendered mid-run, bulk-undo button present and enabled — it asserts the **confirm dialog**
  and does not execute a re-organisation against the live account).
- **Obsolete-spec cleanup, taken on explicitly.** The **13 Playwright specs asserting Phase-1/2/3
  console chrome for an anonymous visitor** are **genuinely obsolete** — Phase 8 deliberately made an
  anonymous visitor land on the homepage, which is correct behaviour. Phase 8 forbade deleting them so
  that phase could not go green by deletion; **Phase 9 takes them on and deletes or rewrites them**,
  and the slice's report must list each file with `deleted` or `rewritten` and one line of why. That
  is the whole permitted change to `tests/e2e/phase1|2|3`.

##### Slice 8 — `test-isolation-guard` (root-cause for the `e2e-actions-test` pollution)

The row deletion is the symptom. The defect is that a test was **able** to bind to a real user's
account at all — the same class as the cached-`get_settings()` incident. This slice makes it
structurally impossible and makes the failure loud.

- `tests/isolation.py` (new) — the guard, as plain assertions:
  - `assert_isolated_db(engine)`: the bound SQLAlchemy URL must resolve to a path under `tmp_path`,
    and must **not** be `zero_inbox.db` or any file under `data/`. Raises `RealDatabaseError` with the
    offending URL in the message.
  - `assert_test_user(user_id)` / `assert_test_mailbox(email)`: test data must use the reserved
    `test-` id prefix and an `@example.com`/`@test.invalid` address. The known real ids and addresses
    (`6b4ab0f4…`, `psykrsna@gmail.com`) are a hard **deny-list**; using one raises immediately.
- `tests/conftest.py` — `_isolated_db` calls `assert_isolated_db(engine)` after binding, and a new
  **autouse** `_no_real_account` fixture wraps the session factory so that **committing** a
  `Category`, `Decision`, `Rule` or `ChannelAccount` whose `user_id` is not a `test-` id **raises**.
  The guard fires on write, not on teardown, so the offending test fails with a message naming the
  test and the row it tried to create. It cannot be disabled by a marker or an env var.
- `tests/unit/test_isolation_guard.py` (new) — the guard itself is tested: a deliberate attempt to
  write a `Category` for the real user id raises `RealUserError`; an attempt to point the engine at
  `zero_inbox.db` raises `RealDatabaseError`; a normal `test-` write succeeds. **These tests fail if
  the guard is removed or weakened**, which is the point.
- `scripts/README_cleanup.md` (new) — the **procedure, not a script**, for removing
  `e2e-actions-test` from the live account, executed by a human against the running server on `:8001`
  after the gate is green:
  1. `GET /api/categories/{id}/usage` (slice 4). Proceed **only** if `decisions == 0`, `rules == 0`,
     `items == 0`. A non-zero count **stops** the operation and is reported to the user.
  2. `DELETE /api/categories/{id}` — the normal taxonomy path, which leaves the Gmail label and all
     mail intact.
  3. Re-`GET` the category list to confirm it is gone.
  **Writing or running an ad-hoc script against `zero_inbox.db` is forbidden.** This file must
  contain no executable code.

#### Explicitly NOT taken on in Phase 9 (carried, not inherited)

- **`tests/integration/test_resume.py::TestFullResume`** — a fixture **visibility** bug
  (`AttributeError` at line 103). **Still out of scope. No generator may make it pass by weakening,
  loosening, deleting or `xfail`-ing any assertion in it.** Leave it exactly as it is.
- **The 5 pre-existing integration failures** (model-shape drift, a live-mailbox flake,
  `RefreshError` mapping, `GmailMutator` method drift). One of these failing is not a blocker. A
  Phase 9 test failing is. Any failure **outside** this set and the resume fixture is a new regression
  and blocks the phase.

#### Assumptions made in this phase (confirm or correct before slice 1 starts)

> **Assumed:** the never-miss label set is `People` / `Urgent` / `Important`, resolved
> deterministically in that precedence, and **`Important` is added to the seed taxonomy and to
> `NEVER_ARCHIVE_KEYS`**. The user said *"we have labels called Urgent and Important or whatever so
> they can go there"* — `Important` is created if it does not already exist rather than reusing an
> ill-fitting category.

> **Assumed:** account aliases are read from Gmail `users.settings.sendAs.list`, best-effort: on
> failure the fix still applies to `channel_accounts.account_email` alone and logs at WARNING. The
> primary bug (18 threads from the account's own address) is closed either way.

> **Assumed:** "re-organise everything" means **every decision row for that user** (~10,336),
> including already-archived threads, in one resumable job — not the current inbox, and not a sample.
> This is a direct restatement of the user's *"Re-organise everything."*

> **Assumed:** the historic **44 already-applied unreviewed** rows are **not** rewritten by migration
> `0008`. They are counted and surfaced as `unreviewed_applied`. Rewriting them would make the
> database assert a review that never happened.

> **Assumed:** the gate's migration check runs against a throwaway
> `data/phase9_migration_check.db` via a command-line `AGENT_DATABASE_URL` override, never against
> `zero_inbox.db`, and the scratch file is deleted afterwards.

> **Assumed:** the sender-concentration bar for minting a deterministic tier-1 rule is **≥ 10 threads
> from one sender/domain/`List-Id` with a single dominant category**. Ten is the same threshold the
> discovery success criterion already uses for "must be named in some category's evidence", so the
> two cannot drift apart. It is a `user_settings`-free constant in `tools/taxonomy_discovery.py`.

> **Assumed:** test data is identified by a reserved `test-` `user_id` prefix and
> `@example.com` / `@test.invalid` addresses, with the measured real id and address as an explicit
> deny-list. This is a convention the guard enforces; existing fixtures that violate it are updated
> by slice 8 within its own files only.

> **Assumed:** the `e2e-actions-test` deletion is a **post-gate operational step performed by a
> human** against the running server, not an automated build step — no generator deletes a row on a
> real account. If the usage check is non-zero, nothing is deleted and the user is told.

> **Assumed:** re-organisation and triage-apply are mutually exclusive (`409 run_in_progress`). Two
> writers against one mailbox is not a state this system is specified to support.

#### Gate (exact commands, run from the repo root, real APIs via `.env`)

```bash
AGENT_DATABASE_URL="sqlite:///./data/phase9_migration_check.db" uv run alembic upgrade head
AGENT_DATABASE_URL="sqlite:///./data/phase9_migration_check.db" uv run alembic heads
uv run pytest tests/unit tests/integration -q
cd frontend && pnpm install && pnpm build && cd ..
npx playwright test tests/e2e/ --reporter=line
```

All must exit 0. `alembic heads` must print a **single** head, `0009_reorg_jobs`.

- **The migration check runs against a throwaway `data/phase9_migration_check.db`, never against
  `zero_inbox.db`.** Alembic runs in its own process and reads `AGENT_DATABASE_URL` fresh, so the
  cached `get_settings()` hazard does not apply — but the override must be on the command line, and
  the scratch file must be deleted afterwards.
- **Every test uses the `_isolated_db` fixture, and slice 8's guard now enforces it structurally** —
  a test that binds `zero_inbox.db` or writes a row for a real `user_id` fails loudly with a named
  error rather than silently polluting production. No test runs alembic against the real DB, and no
  test launches a full-inbox triage run — the 220-thread fixture, the 365-row remainder fixture and
  the 10,000-decision re-organisation fixture are the entire test surface.
- **Playwright runs against the already-running supervised server on `http://localhost:8001`. Do not
  start a second server and do not kill or restart the supervised one.**

`tests/integration/test_inbox_zero_reframe.py` is the load-bearing gate. It runs the **365-row
remainder fixture replaying the measured live distribution (227 `held_by_never_miss` / 76
`category_keep` / 46 `below_threshold` / 16 `needs_your_call` / 0 `unclassified`)** plus the
220-thread fixture against the **real NVIDIA NIM endpoint** via `.env`. The 365 rows are the full set
— a sampled answer and a full answer are observably different (227 is not reachable from a sample).
It asserts:

1. **Zero is real:** the run ends `inbox_remaining == 0` and `distance_to_zero == 0`, with **no human
   intervention** — no approval step, no `force=True`, no manual sweep.
2. **All 227 are labelled, not lost:** every formerly-`held_by_never_miss` thread is `status="applied"`
   with a `ZeroInbox/{Urgent,Important,People}` label attached, a non-null `undo_token`, and `INBOX`
   removed. **Zero threads in Trash; zero `ActionLog.operation` outside `{archive, add_label,
   remove_label}`.**
3. **Nothing is archived unlabelled:** a seeded never-miss thread whose category has been deleted is
   **not** archived — it stays in the inbox and is reported under `no_never_miss_label`.
4. **The self-address bug is dead:** the 18 self-addressed threads produce **zero** reply-history
   holds, and `SenderProfile.ever_replied` is false for the account's own address and its aliases.
   *(Fails against pre-Phase-9 code.)*
5. **No-reply senders never claim correspondence:** the five measured live no-reply addresses produce
   zero reply-history signals, while still being eligible for a time-sensitive never-miss label.
6. **The review gate is not vacuous:** with the reviewer stubbed to fail, **zero** mutations occur,
   every affected row is `review_failed`, and `apply_decision` raised `NotReviewedError` before the
   mutator (spy). With the reviewer auditing a strict subset, only the audited rows are `reviewed`.
   *(Fails against pre-Phase-9 code.)*
7. **`digest` is audited:** every `digest` proposal in the run has a reviewer verdict; none reaches
   the mutator as `provisional`.
8. **The floor still binds:** a below-floor thread is `needs_your_call`, stays in the inbox, and is
   **not** archived by the reframe.
9. **`NEVER_ARCHIVE_KEYS` still holds:** setting `people`/`urgent`/`legal`/`important` to
   `default_action="archive"` raises on create and on update, in the same test run in which those
   categories successfully receive never-miss archives.
10. **Bulk undo:** run-level undo returns all 227 threads to the inbox with their exact pre-triage
    label sets; a second call makes zero Gmail calls.
11. **`dry_run` is absolute:** the same run under `dry_run=true` performs zero Gmail mutations and
    still produces the complete ledger.
12. **No side door:** across the whole run `apply_decision` is never called with `force=True`, and
    `Decision.review_state` is never written outside `finalise_review` / `upgrade_review_state`
    (ORM attribute spy).
13. **Honesty:** with the mutator patched to raise, the run reports `apply_ok=false`, a non-null
    `error_message`, a `run_apply_failed` event, and **"we reached zero" is absent** from the ledger
    payload.
14. **Ledger arithmetic:** `inbox_remaining == sum(remainder buckets) + distance_to_zero`, and the
    `unreviewed_applied` figure equals the count the `0008` migration reported.

`tests/integration/test_taxonomy_discovery.py`, `tests/integration/test_reorganise.py` and
`tests/integration/test_review_gate_integrity.py` are load-bearing alongside it.
`tests/integration/test_no_body_persisted.py` and `tests/integration/test_drive_to_zero.py` must still
pass — Phase 9 must not regress the privacy or Phase 7 guarantees. Where `test_drive_to_zero.py`
asserts the **old** never-miss semantic (a `held_by_never_miss` thread remains in the inbox), slice 3
**updates that specific assertion to the new semantic and says so in its report**; every other
assertion in that file stays untouched.

#### Production safety (binding on every implementer and the auditor)

`zero_inbox.db` holds **~12,500 real decisions for 2 real accounts**, and the server on `:8001` is
**in use**.

- **Never write an ad-hoc script against the real DB.** `get_settings()` is cached and does **not**
  honour an env override from a standalone script. Every test uses `_isolated_db`.
- **Do not kill or restart the supervised server on `:8001`**, and do not start a second one.
- **Do not launch a full-inbox triage run and do not launch a re-organisation against the live
  account** while building or gating. Fixtures only.
- Migrations `0008` and `0009` are the only sanctioned writes to real user rows, and only via
  `uv run alembic upgrade head` — never by hand, never by script. `0008` must never rewrite an
  already-`applied` row.

#### How the user tests it

1. `uv run alembic upgrade head`, then `cd frontend && pnpm build && cd .. && uv run python -m src`.
2. Open **http://localhost:8001/app/** → **Settings → Taxonomy** → **Rebuild my categories from my
   mail**. You get a proposal derived from *your* senders — Apple, Google, Facebook, PayPal,
   BookMyShow appear by name, with thread counts, not a generic list. Every proposed category shows
   the senders it will absorb and how many of your **16 no-fit** and **46 low-confidence** threads it
   resolves. Edit anything you disagree with, then **Approve**.
3. You are offered **Re-organise everything**. Accept. Watch the progress card on the main page —
   **with no clicks** — count through your ~10,336 past decisions, including relabelling mail that is
   already archived. Anything it cannot do is listed **while it runs**, by reason and count, not
   hidden until the end.
4. When it finishes, click **Undo the whole re-organisation** if you want your old state back: one
   button, one operation, everything returns. Then re-run it.
5. Run triage. When it completes the **Inbox-Zero card** reads **0 still in your inbox**. Not 365.
6. **Check Gmail.** Your inbox is empty. The 227 threads that used to sit there are now under
   `ZeroInbox/Urgent`, `ZeroInbox/Important` and `ZeroInbox/People` — **one click in the sidebar**,
   each labelled with why it was held. The Apple / Google / Facebook security and sign-in notices are
   under Urgent. **Nothing is in Trash. Nothing was deleted.**
7. Open **Settings → Taxonomy** and try to set **People** or **Urgent** to archive-by-default. It is
   still refused, with the note *"kept for you — archived under its own label, never swept as a
   category."* That is the distinction this phase is built on: your Urgent mail leaves the inbox
   **into Urgent**; the Urgent category is never treated as noise.
8. Click **Undo this run** and confirm all 227 come back to the inbox exactly as they were. Then
   re-run.
9. Mail yourself something from your own address. It is **not** treated as "a sender you reply to"
   any more — the *"24 replies of 0 received"* nonsense is gone.
10. **Real in Phase 9:** inbox-derived taxonomy with evidence, taxonomy redo, full re-organisation of
    all past decisions with live progress and one-button bulk undo, never-miss-as-label reaching
    actual zero, the review gate made real, and the self-address fix. **Labelled stubs remaining:**
    none.

> **If step 5 shows anything other than 0, or step 6 finds a never-miss thread archived without a
> label, or step 7 lets you set People to archive — Phase 9 is not done, regardless of what the other
> steps show.**

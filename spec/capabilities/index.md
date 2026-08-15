# Capabilities

Every capability maps to exactly one phase. See [`../roadmap.md`](../roadmap.md#phases-of-development)
for the phase plan and slices.

## Phase 1 — Connect Gmail + Autonomous Clustered Triage

| Capability | What it does |
|------------|--------------|
| [gmail-connection](gmail-connection.md) | Full Google OAuth web flow; per-user encrypted refresh-token storage; multi-tenant isolation |
| [thread-ingestion](thread-ingestion.md) | Pulls recent inbox threads into the channel-agnostic `Item` shape; redacts secrets; never persists bodies |
| [cost-tiered-triage](cost-tiered-triage.md) | Four-tier cascade: rules → sender history → batched LLM (20–50/call) → deep read; applies all non-keep decisions automatically on completion |
| [thread-clustering](thread-clustering.md) | Collapses hundreds of threads into ~30 cluster groups |
| [triage-history-view](triage-history-view.md) | Read-only dashboard view of applied decisions: reasoning, confidence, tier badge, applied action per thread; no approve/reject |
| [decision-audit-trail](decision-audit-trail.md) | Decision half: reasoning, confidence, which-rule-fired, structured logs + LangSmith traces |

## Phase 2 — Never-Miss Safeguards + Real Gmail Actions + Taxonomy Labels + Memory

| Capability | What it does |
|------------|--------------|
| [never-miss-safeguards](never-miss-safeguards.md) | Second-pass reviewer, confidence floor, reply-history signal — **all live before any mutation** |
| [taxonomy-management](taxonomy-management.md) | Default + custom + agent-proposed categories, each 1:1 with a real Gmail label |
| [gmail-actions-and-undo](gmail-actions-and-undo.md) | Real label + archive mutations, never delete, full undo, complete mutation log |
| [user-memory](user-memory.md) | Corrections as training signal, VIP list, priorities profile, per-sender stats |
| [decision-audit-trail](decision-audit-trail.md) | Completed: mutation log with undo tokens, corrections |

## Phase 3 — Autopilot & Background Visibility

| Capability | What it does |
|------------|--------------|
| [autopilot-and-digest](autopilot-and-digest.md) | Auto-trigger on connect (applies on completion), run summary card with "Undo this run" button, run-level undo (`POST /api/runs/{run_id}/undo`), daily scheduler, catch-up digest, SSE activity feed, real taxonomy editor (D10 fix) |

## Phase 4 — Rules, Chat, Digest, Backlog & Proactive Assistance

| Capability | What it does |
|------------|--------------|
| [rule-proposals](rule-proposals.md) | Pattern mining, dry-run preview, promotion to automatic, real persistent Gmail filters, starter rule packs |
| [chat-to-rules](chat-to-rules.md) | Plain English → rules, with full conversation memory |
| [daily-digest](daily-digest.md) | Exhaustive daily summary of what was hidden, one click to un-hide |
| [backlog-cleanup](backlog-cleanup.md) | User-launched chunked historical cleanup — streaming, cancellable, resumable |
| [cost-and-model-controls](cost-and-model-controls.md) | Run + monthly spend, rules-vs-LLM ratio, per-user model dropdown |
| [proactive-assistance](proactive-assistance.md) | Missed-important flags, unsubscribe candidates, stale threads, draft replies |

## Phase 5 — Triage Transparency

| Capability | What it does |
|------------|--------------|
| [triage-transparency](triage-transparency.md) | Per-thread SSE events (`thread_classified`, `thread_archived`) surfaced on the main dashboard page (drawer = history only); shared `SseContext` eliminates duplicate EventSource |

## Phase 6 — Durable, Resumable, Transparent Runs

| Capability | What it does |
|------------|--------------|
| [durable-resumable-runs](durable-resumable-runs.md) | Incremental per-batch persistence in a `provisional` review state; one-click resume of an interrupted run without re-classifying; live per-thread feed with tier + reason + not-yet-reviewed labelling; provider-degraded banner and a circuit breaker that aborts fast with partial work preserved |
| [never-miss-safeguards](never-miss-safeguards.md) | Extended: `review_state` gate — only `reviewed` decisions are appliable, enforced in `apply_decision()` |
| [triage-transparency](triage-transparency.md) | Extended: per-thread coverage guarantee, `reasoning` + `review_state` on every event, provisional labelling, degraded-provider banner |
| [decision-audit-trail](decision-audit-trail.md) | Extended: decisions and `llm_calls` survive an interrupted run |

## Phase 7 — Drive to Inbox Zero

| Capability | What it does |
|------------|--------------|
| [drive-to-inbox-zero](drive-to-inbox-zero.md) | Makes `auto_act_threshold` a real, calibrated, per-category autonomy control; converts confident archive-category keeps at decision time so the reviewer still audits them; applies them through the Phase 6 review gate (never around it, never with `force`); makes a failed apply loud instead of a silent `return`; finishes the `decided_by="error"` tail on resume; and reports `distance_to_zero` plus a remainder ledger explaining every thread still in the inbox |
| [never-miss-safeguards](never-miss-safeguards.md) | Unchanged and binding: `align_to_category_default` runs **before** the reviewer, so every category-driven archive is audited, floored and VIP/reply-history-guarded |
| [cost-tiered-triage](cost-tiered-triage.md) | Extended: `decided_by="error"` rows are not treated as decided — a resume re-classifies the tail |
| [gmail-actions-and-undo](gmail-actions-and-undo.md) | Extended: retry-apply (`POST /api/runs/{run_id}/apply`) recovers a failed apply pass without re-classifying |
| [triage-transparency](triage-transparency.md) | Extended (rules H/I/J): the live classification feed renders **inline on the main page with no clicks** while a run is active (the drawer stays as full history); continuity is guaranteed by logging at the right granularity through the structlog→bus bridge plus a self-arming watchdog, so no gap exceeds 3 s — including inside a single 29-thread tier-3 LLM call; and replay-on-connect is verified so a mid-run load or reload paints a populated feed |

## Phase 8 — Product Front Door, Account & Review Recovery

| Capability | What it does |
|------------|--------------|
| [product-front-door](product-front-door.md) | A signed-out homepage that explains the product and sells its honesty, a first-class sign-in, a three-step first-run flow ending in the moment of trust, the steady-state daily loop, and the design system (tokens, component states, responsive, a11y) every surface is built from |
| [account-and-identity](account-and-identity.md) | Sign-in as its own OAuth intent (`openid email profile`) separate from mailbox consent; revocable server-side sessions with a device list and sign-out-everywhere; globally unique mailbox ownership; session hardening; disconnect a mailbox and delete an account, neither of which touches Gmail |
| [review-recovery](review-recovery.md) | **Retry review** — re-runs the real never-miss reviewer over a completed run's `provisional` / `review_failed` decisions and applies whatever it passes, so an outage during the reviewer no longer costs a whole run. It re-enters the gate; it never bypasses it |
| [never-miss-safeguards](never-miss-safeguards.md) | Unchanged and binding: retry-review calls the same reviewer and the same `apply_decision()`, never writes `review_state` directly and never uses `force=True` |

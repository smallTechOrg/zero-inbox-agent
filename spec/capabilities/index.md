# Capabilities

Every capability maps to exactly one phase. See [`../roadmap.md`](../roadmap.md#phases-of-development)
for the phase plan and slices.

## Phase 1 — Connect Gmail + Dry-Run Clustered Triage

| Capability | What it does |
|------------|--------------|
| [gmail-connection](gmail-connection.md) | Full Google OAuth web flow; per-user encrypted refresh-token storage; multi-tenant isolation |
| [thread-ingestion](thread-ingestion.md) | Pulls recent inbox threads into the channel-agnostic `Item` shape; redacts secrets; never persists bodies |
| [cost-tiered-triage](cost-tiered-triage.md) | Four-tier cascade: rules → sender history → batched LLM (20–50/call) → deep read |
| [thread-clustering](thread-clustering.md) | Collapses hundreds of threads into ~30 cluster decisions |
| [triage-queue-review](triage-queue-review.md) | The dashboard sweep: reasoning, confidence, tier badge, approve/reject, bulk approve — **pure dry-run** |
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
| [autopilot-and-digest](autopilot-and-digest.md) | Auto-trigger on connect, run summary card, one-click approve-all + apply, daily scheduler, catch-up digest, SSE activity feed, real taxonomy editor (D10 fix) |

## Phase 4 — Rules, Chat, Digest, Backlog & Proactive Assistance

| Capability | What it does |
|------------|--------------|
| [rule-proposals](rule-proposals.md) | Pattern mining, dry-run preview, promotion to automatic, real persistent Gmail filters, starter rule packs |
| [chat-to-rules](chat-to-rules.md) | Plain English → rules, with full conversation memory |
| [daily-digest](daily-digest.md) | Exhaustive daily summary of what was hidden, one click to un-hide |
| [backlog-cleanup](backlog-cleanup.md) | User-launched chunked historical cleanup — streaming, cancellable, resumable |
| [cost-and-model-controls](cost-and-model-controls.md) | Run + monthly spend, rules-vs-LLM ratio, per-user model dropdown |
| [proactive-assistance](proactive-assistance.md) | Missed-important flags, unsubscribe candidates, stale threads, draft replies |

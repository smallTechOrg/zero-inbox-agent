# Capability: Triage Queue Review

## What It Does
Presents the clustered decisions in the dashboard so the user can sweep through them — expanding a
cluster or thread to see reasoning, confidence and which tier fired, then approving or rejecting
individually or a whole cluster at once.

## Inputs
| Input | Type | Source | Required |
|-------|------|--------|----------|
| Run | entity | `triage_runs` | Yes |
| Clusters + decisions | entities | `clusters`, `decisions` | Yes |
| User approve/reject | interaction | dashboard | Yes |

## Outputs
| Output | Type | Destination |
|--------|------|-------------|
| Decision status | enum | `decisions.status` |
| Live run progress | JSON | `GET /api/runs/{id}` |
| Correction signal | entity | `corrections` (Phase 2) |

## External Calls
None in Phase 1 — approving records intent only. Phase 2 hands approved decisions to
[gmail-actions-and-undo](gmail-actions-and-undo.md).

## Business Rules
- **Phase 1 is pure dry-run.** Approving or rejecting changes only `decisions.status`; the mailbox is
  never touched, and any attempted mutation raises `dry_run_violation`.
- A permanent, unmissable dry-run banner is displayed while `dry_run` is true.
- Every thread row shows the tier badge (`RULE` / `SENDER HISTORY` / `LLM` / `DEEP READ` / `REVIEWER`)
  and, where a rule fired, the rule's name.
- Expanding a thread shows the **full** reasoning verbatim — never truncated.
- The "Needs your call" group is pinned at the top of the queue and can never be bulk-approved as
  archive; each of its members requires an individual decision.
- Results stream in as they are decided; the queue is usable before the run finishes.
- A run is cancellable from the queue; cancelling leaves already-persisted decisions intact.
- Keyboard sweep: `j`/`k`, `a` approve, `x` reject, `A` approve cluster, `enter` expand.

## Success Criteria
- [ ] After a real run, the queue shows real clusters with real subjects from the user's mailbox.
- [ ] The dry-run banner is present on every screen while dry-run is on.
- [ ] Every thread row displays a tier badge matching its `decided_by` value.
- [ ] Expanding a thread reveals the full reasoning string stored in the database.
- [ ] Approving a cluster sets every member decision to `approved` in one request.
- [ ] In Phase 1, after approving a cluster, nothing in the user's actual Gmail has changed.
- [ ] The progress bar advances while a run is in flight and reaches `total/total` on completion.

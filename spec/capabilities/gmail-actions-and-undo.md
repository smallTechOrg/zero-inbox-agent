# Capability: Mailbox Actions & Undo

## What It Does
Applies approved decisions to the real mailbox — labelling and archiving threads — and records an undo
token for every mutation so any action can be fully reversed in one click.

## Inputs
| Input | Type | Source | Required |
|-------|------|--------|----------|
| Approved decisions | `Decision[]` | triage queue review | Yes |
| Dry-run flag | bool | `user_settings` | Yes |
| Category labels | label ids | taxonomy management | Yes |
| Undo request | action log id | Audit screen | No |

## Outputs
| Output | Type | Destination |
|--------|------|-------------|
| Mailbox mutation | label added / `INBOX` removed | the user's Gmail |
| Action record | entity with undo token | `action_logs` |
| Decision status | `applied` / `undone` | `decisions` |

## External Calls
| System | Operation | On Failure |
|--------|-----------|------------|
| Mailbox channel | `archive_and_label(threadId, add=[category_label], remove_inbox=True)` — one atomic `modify()` call | Retry 3× with backoff; on failure the decision stays `approved`, nothing is logged as applied, and the UI shows the error with Retry |
| Mailbox channel | `undo_archive_and_label` — inverse atomic `modify()` call | Retry 3×; on failure surface the failure explicitly — never mark it undone optimistically |

## Business Rules
- **Archive means exactly one thing:** removing the `INBOX` label, nothing else. It is applied in the
  **same atomic `modify(threadId, addLabelIds=[category_label], removeLabelIds=['INBOX'])` call** as
  adding the matching category label — a thread is never left labelled-but-still-in-inbox or
  archived-but-uncategorized by a partial write.
- **The Gmail label state is the sole source of truth for what is archived and how it is categorized.**
  There is no separate "archive" table or system of record; `action_logs` and `decisions` record what
  the agent *did* and *why* (per [decision-audit-trail](decision-audit-trail.md)), but they are history,
  not the live state — the live state is always read from Gmail's own label list.
- **Never delete.** Only `addLabelIds` and removing `INBOX` are permitted. `trash`, `delete` and
  `spam` are not implementable operations in the adapter — the `ChannelAdapter` interface
  (`src/channels/base.py`, see [architecture.md](../architecture.md)) has no `trash()`, `delete()`, or
  `report_spam()` method at all, on any implementation. This is a structural guarantee, not a promise
  enforced only by business logic: the methods do not exist to be called.
- **Reject performs zero mutation, ever, in any phase.** Rejecting a decision only sets
  `decisions.status = rejected`; it is never passed to `actions.apply_decision()` and never reaches the
  adapter. The thread stays exactly as it was — still in the inbox, no label change, no Gmail API call.
  Only `approved` decisions are eligible for `POST /api/actions/apply`.
- **Nothing acts without approval** unless a rule has been explicitly promoted to `automatic` by the
  user, and even then only above `auto_act_threshold`.
- No mutation runs while `dry_run` is true — the attempt raises `dry_run_violation` (409).
- No mutation runs for a decision that did not pass all three never-miss safeguards.
- Every mutation writes an `action_logs` row **before** the decision is marked `applied`, containing the
  exact request parameters and an undo token describing the precise inverse operation.
- Undo restores the prior label state exactly (re-adds `INBOX`, removes the added category label), sets
  `action_logs.undone_at`, and marks the decision `undone`. Undo is idempotent.
- Mutations are applied thread-level, in batches, with per-thread success recorded individually — a
  partial batch failure never loses the record of what did succeed.

## Success Criteria
- [ ] Approving a cluster with dry-run off archives those threads in the real mailbox and applies the
      matching `ZeroInbox/<Category>` label, verified by reading the threads back.
- [ ] Nothing appears in Trash after any agent operation, ever.
- [ ] Every mutation has an `action_logs` row with a non-null undo token.
- [ ] Clicking Undo returns the thread to the inbox with the added label removed, verified by reading it
      back from Gmail.
- [ ] Calling undo twice on the same action leaves the mailbox in the same state (idempotent).
- [ ] Attempting to apply a decision while dry-run is on returns 409 and mutates nothing.
- [ ] Attempting to apply a decision that is in `needs_your_call` is rejected.

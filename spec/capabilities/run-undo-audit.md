# Capability: Run Cards, Whole-Run Undo & Audit Trail

## What It Does
Every run appears as a timeline card; one click reverts every Gmail mutation that run made; every mutation is audited (timestamp, reason, run ID) so Gmail is never corruptible.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Undo trigger | `POST /api/runs/{id}/undo` | run card button (with confirm) | yes |
| mutations rows | audit records | triage run | yes |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Inverse Gmail mutations | label ops | user's Gmail |
| Updated rows | `mutations.undone_at`, `thread_decisions.undone`, `runs.status=undone` | [data.md](../data.md) |
| undo_* feed events | events | [live-activity-feed](live-activity-feed.md) |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| Gmail API | inverse label ops | undo is itself resumable: re-trigger continues from first non-undone mutation |

## Business Rules
- Undo granularity is the WHOLE RUN only; inverse ops applied newest-first per run.
- Audit row is written before its Gmail mutation executes (never an unaudited mutation).
- Inverse map: add_label↔remove_label, remove_inbox↔restore_inbox; only these four ops exist in the mutation executor.
- Undone threads become re-decidable by future runs.
- 409 if the run is active or already undone.

## Success Criteria
- [ ] After undo, every affected thread's labels and INBOX presence match its pre-run state exactly (integration test via the audited mutation log).
- [ ] An interrupted undo, re-triggered, completes without double-inverting any mutation.
- [ ] Every mutation row has run_id, reason, and timestamp (schema + write-path test).

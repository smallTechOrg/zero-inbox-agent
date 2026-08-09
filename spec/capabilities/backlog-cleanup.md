# Capability: Backlog Cleanup Job

## What It Does
Runs an explicitly user-launched job that works through the historical inbox backlog in dated chunks,
streaming results as they are decided, with a live progress bar, and cancellable and resumable without
redoing completed work.

## Inputs
| Input | Type | Source | Required |
|-------|------|--------|----------|
| Date range | start + end date | Backlog screen | Yes |
| Chunk size | days (default 30) | Backlog screen | No |
| Cancel / resume | interaction | Backlog screen | No |

## Outputs
| Output | Type | Destination |
|--------|------|-------------|
| Job progress | counts + cursor | `triage_runs` (`kind=backlog`) |
| Decisions + clusters | entities | `decisions`, `clusters` |
| Streamed results | polled JSON | Backlog screen |

## External Calls
| System | Operation | On Failure |
|--------|-----------|------------|
| Mailbox channel | list threads within a dated chunk | Retry 3×; on persistent failure the chunk is marked failed, the cursor is preserved, and the job can resume from it |
| LLM provider | batch classification | Same degradation as triage — failures become `needs_your_call`, never archive |

## Business Rules
- The backlog job is **never automatic** — it only runs when the user launches it.
- Work proceeds oldest-chunk-first (or newest-first, user choice), one dated chunk at a time, with the
  cursor persisted after each chunk.
- Results stream in as they are decided; the user can start sweeping before the job finishes.
- Cancelling stops after the in-flight chunk and preserves every decision already made.
- Resuming continues from the persisted cursor. Decisions are idempotent on `(run_id, item_id)`, so
  **no thread is ever decided twice**.
- All the never-miss safeguards apply identically to backlog decisions — the backlog is not a
  lower-standard path.
- The job reports its running token spend so a large backlog cannot silently burn budget.

## Success Criteria
- [ ] A backlog job over the 220-thread fixture completes with all 220 decided exactly once.
- [ ] Cancelling mid-run and resuming produces zero duplicate decisions and still reaches 220 decided.
- [ ] The progress bar shows per-chunk counts advancing while the job runs.
- [ ] Cancelling preserves every decision already persisted.
- [ ] A failing chunk does not fail the whole job; the cursor allows resuming from it.
- [ ] The job's `cost_usd` is non-zero and matches the sum of its `llm_calls` rows.

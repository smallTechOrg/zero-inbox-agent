# Capability: Backlog Drive to Inbox Zero

## What It Does
Repeated manual chunks (50–100 threads, newest first) chew through the whole backlog with a visible progress-to-zero indicator, letting the user adjust taxonomy between chunks.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Chunk trigger | `POST /api/runs {chunk_limit≤100}` | dashboard button | yes |
| Remaining count | `GET /api/progress` | decision index vs INBOX total | yes |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Progress (remaining/total, per-chunk delta) | JSON | command-strip progress bar |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| Gmail API | INBOX total estimate | show last known value with "stale" marker |

## Business Rules
- Still strictly manual — each chunk is a button press; "Clean my inbox" relabels to "Clean next 100" while backlog remains.
- Newest-first ordering always; undecided = in INBOX and no non-undone decision row.
- Reaching zero undecided shows a celebratory "Inbox chunk clean" state.

## Success Criteria
- [ ] Three consecutive chunks on a 250-thread test dataset reduce the remaining count by exactly the decided amounts with no overlap between chunks.
- [ ] Taxonomy edits between chunks change subsequent filing (real-LLM test).

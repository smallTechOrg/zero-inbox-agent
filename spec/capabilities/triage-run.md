# Capability: Triage Run (Clean My Inbox)

## What It Does
On manual trigger, classifies one chunk of up-to-50 (Phase 2: 100) undecided INBOX threads, newest first, in cheap batched header-only LLM calls, applies each category's rule to Gmail, and is resumable with a never-redo guarantee.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Trigger | `POST /api/runs` | dashboard button only (no schedules) | yes |
| ClassifierViews | privacy-bounded metadata | Gmail API | yes |
| Current taxonomy | categories | [data.md](../data.md) | yes |

## Outputs
| Output | Type | Destination |
|---|---|---|
| thread_decisions rows | records | decision index / ledger |
| Gmail mutations + audit rows | label ops | [run-undo-audit](run-undo-audit.md) |
| Feed events + cost rows | events | [live-activity-feed](live-activity-feed.md) |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| NVIDIA NIM | batched classification | 30s hard timeout / 429 / error → automatic Gemini fallback for the batch; retry NVIDIA next batch |
| Gemini | fallback classification | run interrupts with plain-English reason; resumable |
| Gmail API | thread list (metadata), label add/remove | reconnect banner or interrupt-with-reason; resumable |

## Business Rules
- Email bodies are never fetched into the classifier path and never sent to any LLM (allowed fields: architecture.md privacy boundary). No deep-read escalation exists.
- One decision per thread (thread-aware); batches of ≤25 threads per LLM call.
- Confidence < 0.7 → best-guess label **plus** "Needs review" label, flagged in feed and ledger.
- One active run per user; re-trigger resumes an interrupted run; decided threads are never re-decided (unless their run was undone).
- Decision row persists before its mutations; mutations are idempotent on resume.
- Graph contract: [agent.md](../agent.md).

## Success Criteria
- [ ] A 50-thread chunk on a real inbox completes with every thread labelled per its category's rule, first try.
- [ ] Killing the process mid-run and re-triggering resumes with zero duplicate decisions and zero duplicate mutations (integration test).
- [ ] With NVIDIA forced to fail (bad key), the run completes on Gemini and each batch logs a fallback event; with NVIDIA healthy, Gemini is never called.
- [ ] A test asserts the serialized LLM payload contains no body field for a fixture thread whose body holds a sentinel string.
- [ ] No LLM call can exceed the hard timeout (test with a stalling mock transport at the HTTP layer; classification content tests use the real API).

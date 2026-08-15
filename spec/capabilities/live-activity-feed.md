# Capability: Live Activity Feed

## What It Does
While a run (or undo) executes, the dashboard streams every action as a plain-English sentence with expandable reasoning, progress, per-category counts, and a live cost/token ticker including fallback events.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| run_events | persisted + live events | triage run / undo | yes |

## Outputs
| Output | Type | Destination |
|---|---|---|
| SSE stream | `GET /api/runs/{id}/events` | dashboard feed |
| run_events rows | records | replay + run-card drill-down |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| — (in-process) | SSE over event bus | client auto-reconnects with `?after_seq`; nothing lost (events persisted) |

## Business Rules
- One sentence per action, present tense, human-readable ("Filed 'ACME invoice' → Finance — archived").
- Each decision event carries the reasoning + confidence in `detail_json`, rendered behind an expander.
- Cost ticker updates per LLM call; fallback events rendered inline ("NVIDIA timed out — switched to Gemini for this batch").
- Events are persisted before emission; a page reload mid-run replays the full feed.

## Success Criteria
- [ ] Reloading mid-run shows the complete feed so far and continues live.
- [ ] Every mutation and decision of a run has exactly one feed event (count equality test).
- [ ] Playwright sees sentences streaming during a real Phase 1 run.

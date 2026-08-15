# Capability: Cost Dashboard

## What It Does
Per-run cost cards (LLM calls, tokens in/out, estimated cost, fallback events) and per-user cumulative totals, replacing the Phase 1 stub.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| llm_calls rows | records | LLM client accounting | yes |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Per-run cards + cumulative aggregates (by provider, incl. fallback counts and profile-bypass savings) | JSON | `GET /api/costs` → costs panel |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| — | local aggregates | — |

## Business Rules
- Estimated cost computed from a per-model price table in config (env-overridable), labelled "estimated".
- Fallback events counted per run and cumulatively; profile-bypass shows "N threads filed with zero LLM cost".

## Success Criteria
- [ ] A completed run's card totals equal the sum of its llm_calls rows exactly.
- [ ] Cumulative totals match the aggregate across all runs for the user (and only that user).
- [ ] Phase 1 stub badge gone (Playwright).

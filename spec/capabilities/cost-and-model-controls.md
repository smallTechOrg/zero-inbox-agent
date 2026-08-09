# Capability: Cost Visibility & Model Controls

## What It Does
Shows the user what the agent is spending — per run, month to date, and the ratio of threads handled
by rules versus the LLM — and lets them choose which model to use from a dropdown of available free
models, persisted per user.

## Inputs
| Input | Type | Source | Required |
|-------|------|--------|----------|
| LLM call records | tokens, model, latency | `llm_calls` | Yes |
| Run counts by tier | JSON | `triage_runs.counts` | Yes |
| Model selection | model id | Cost/Settings screen | No |

## Outputs
| Output | Type | Destination |
|--------|------|-------------|
| Cost panel | run spend, month-to-date, per-model breakdown | Cost screen |
| Rules-vs-LLM ratio | percentage | Cost screen |
| Per-user model preference | model id | `user_settings.llm_model` |

## External Calls
| System | Operation | On Failure |
|--------|-----------|------------|
| LLM provider | list available models for the dropdown | Fall back to a static catalogue containing the configured default — the dropdown is never empty |

## Business Rules
- Every LLM call writes an `llm_calls` row with model, token counts, latency and computed cost, tagged
  with its purpose (classify / deep read / review / chat / rule proposal / digest / draft).
- Cost is computed from a per-model rate table; a model with no known rate reports tokens and shows
  cost as "n/a" rather than a wrong number.
- The rules-vs-LLM ratio is `(tier-1 + tier-2 decisions) / total decisions`, reported per run and for
  the month.
- The model is a **swappable id** passed per call — never hardcoded. Changing the dropdown takes effect
  on the next run with no restart.
- Selecting a model that the provider rejects surfaces a clear error and reverts to the previous
  selection; it never leaves the user unable to run triage.

## Success Criteria
- [ ] After a real run, the cost panel shows a non-zero spend matching the sum of that run's
      `llm_calls` rows.
- [ ] The rules-vs-LLM ratio matches the run's per-tier counts.
- [ ] The model dropdown lists at least the configured default and persists the selection across a
      page reload and a process restart.
- [ ] Switching the model changes the `model` recorded on the next run's `llm_calls` rows.
- [ ] Selecting an invalid model produces a clear error and leaves the previous model in effect.
- [ ] Month-to-date total equals the sum of that user's `llm_calls` for the month, and excludes other
      users' calls.

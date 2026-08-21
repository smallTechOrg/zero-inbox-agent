# Capability: Sender Profiles

## What It Does
Repeat senders with a consistent history ("GitHub notifications → always Notifications") are filed instantly without any LLM call, cutting cost and latency.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Decision history per sender | thread_decisions | decision index | yes |
| Manual profile edits | JSON | dashboard profiles panel | no |

## Outputs
| Output | Type | Destination |
|---|---|---|
| sender_profiles rows | records | [data.md](../data.md) |
| Profile-sourced decisions (`source=profile`, confidence 1.0) | records | ledger + feed |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| — | none (that's the point) | — |

## Business Rules
- Auto-create after 3 consecutive high-confidence (≥0.8) same-category decisions for a sender; never from needs_review decisions.
- The `match_profiles` graph node decides profiled threads before any LLM batch; feed sentence says "sender profile — no LLM call".
- User can add/delete profiles; deleting a category deletes its profiles.
- Profile decisions still write decision + audit rows and are undoable like any other.

## Success Criteria
- [ ] A sender with 3 consistent decisions is filed on the 4th thread with zero LLM calls for that thread (llm_calls count assertion).
- [ ] Undoing a run containing profile decisions restores those threads too.

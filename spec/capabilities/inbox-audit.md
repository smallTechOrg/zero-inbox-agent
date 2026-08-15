# Capability: Inbox Mini-Audit

## What It Does
A fast (<10s), read-only, observable scan of the INBOX that shows the user what they're dealing with before any cleaning happens.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| INBOX thread metadata | headers/counts | Gmail API | yes |

## Outputs
| Output | Type | Destination |
|---|---|---|
| inbox_snapshot row | record | [data.md](../data.md) |
| Counts (total, unread, oldest, top-10 senders, category-tab mix) | JSON | dashboard command strip |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| Gmail API | list threads/labels (metadata only) | reconnect banner or "audit failed — try again" with retry button |

## Business Rules
- Strictly read-only; zero mutations.
- INBOX label only; archived mail is never counted or fetched.
- Auto-runs on first signed-in visit; re-runnable on demand; no LLM calls.

## Success Criteria
- [ ] Snapshot appears within 10s for a 5,000-thread inbox (uses Gmail estimates + top-slice sampling for top senders, labelled "approximate").
- [ ] Gmail write API is provably never called during audit (isolation-guard log assertion).

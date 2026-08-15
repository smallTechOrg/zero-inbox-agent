# Capability: Ledger Search

## What It Does
A searchable email-by-email ledger on the dashboard: for every triaged email — what was decided, why (one line), and its undo state.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Query (text, category, needs_review, page) | params | ledger panel | no |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Paged rows (sender, subject, snippet, category, confidence, reason, source, undone, run link) | JSON | `GET /api/ledger` → ledger panel |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| — | local query over thread_decisions | — |

## Business Rules
- Text search matches sender + subject + snippet (SQLite LIKE; Postgres path uses ILIKE — no FTS dependency).
- Needs-review rows visually flagged; clicking a row expands reason + its mutations.
- Paged 50/page, newest first.

## Success Criteria
- [ ] Searching a known sender over a 250-decision dataset returns exactly that sender's rows.
- [ ] Undone rows display as undone immediately after a run undo.
- [ ] Phase 1 stub badge is gone; panel is live (Playwright).

# Capability: Proactive Assistance

## What It Does
Surfaces the things the user should act on without being asked: possible missed-important mail as the
top priority, unsubscribe candidates, stale threads awaiting a reply, and drafted replies for the
keepers.

## Inputs
| Input | Type | Source | Required |
|-------|------|--------|----------|
| Decisions + reviewer flips | entities | `decisions` | Yes |
| Sender history | stats | `sender_profiles` | Yes |
| Threads with `List-Unsubscribe` | `Item[]` | `items` | Yes |
| Sent-mail history | reply timestamps | mailbox channel | Yes |
| Priorities profile | text | `priority_profiles` | No |

## Outputs
| Output | Type | Destination |
|--------|------|-------------|
| Possible missed-important list | ranked threads | Proactive panel (top of the dashboard) |
| Unsubscribe candidates | threads + unsubscribe URL | Proactive panel |
| Stale threads awaiting reply | ranked threads | Proactive panel |
| Draft replies | mailbox drafts | the user's Gmail drafts |

## External Calls
| System | Operation | On Failure |
|--------|-----------|------------|
| Mailbox channel | create draft (`gmail.compose`) | Retry 3×; on failure show the drafted text in-app so the work is not lost |
| Mailbox channel | read sent mail for reply/staleness detection | Retry 3×; on failure the staleness list is shown as unavailable, not as empty |
| LLM provider | draft a reply in the user's voice | Retry once; then show a clear failure and no draft |

## Business Rules
- **Possible missed-important is the top-priority surface** — anything the reviewer flipped, anything
  in `needs_your_call`, and anything time-sensitive appears here above everything else.
- Unsubscribe candidates are proposed only, never acted on: the agent surfaces the `List-Unsubscribe`
  link and the user clicks it. The agent never sends an unsubscribe request itself.
- A stale thread is one the user received, has previously replied to that sender, and has not replied
  to for more than 3 days.
- Drafts are **created, never sent** — `gmail.compose` is used to place a draft; no send scope is
  requested and no send operation exists in the adapter.
- Drafts are generated only for threads the agent decided to keep.
- Every proactive suggestion states why it was surfaced, in the same expandable-reasoning style as
  triage decisions.

## Success Criteria
- [ ] Any reviewer-flipped or `needs_your_call` thread appears in the possible-missed-important list.
- [ ] Unsubscribe candidates all have a real `List-Unsubscribe` URL, and no unsubscribe request is ever
      issued by the agent.
- [ ] Stale threads are exactly those matching the 3-day, previously-replied criterion.
- [ ] Requesting a draft creates a real Gmail draft, verified by reading the drafts list back, and no
      message is ever sent.
- [ ] A draft-generation failure shows an error and creates nothing — never a blank draft.
- [ ] Every proactive item shows an expandable reason for being surfaced.

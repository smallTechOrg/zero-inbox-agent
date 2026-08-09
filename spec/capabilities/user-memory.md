# Capability: User Memory & Learning

## What It Does
Gives the agent persistent per-user memory: every correction the user makes, an explicit VIP /
never-hide list, a plain-English priorities profile written once, and per-sender history statistics —
all fed back into triage as evidence so the agent gets more accurate with use.

## Inputs
| Input | Type | Source | Required |
|-------|------|--------|----------|
| Corrections | approve/reject/un-archive events | dashboard + mailbox re-scan | Yes |
| VIP entries | email / domain / keyword | Settings screen | No |
| Priorities profile | free text | Settings screen | No |
| Sender events | received / opened / replied / archived | mailbox scan | Yes |

## Outputs
| Output | Type | Destination |
|--------|------|-------------|
| Sender importance | float per sender | `sender_profiles.importance_score` |
| `ever_replied` flag | bool | `sender_profiles` |
| VIP list | entities | `vip_entries` |
| Priorities profile | text injected into the classifier prompt | LLM context |
| Rule candidates | proposals | `rules` (status `proposed`) |

## External Calls
| System | Operation | On Failure |
|--------|-----------|------------|
| Mailbox channel | scan sent mail to establish reply history | Retry 3×; on failure `ever_replied` stays unknown, which is treated as **important** (fail-safe, never fail-open) |

## Business Rules
- Un-archiving a thread the agent hid is the strongest correction signal: it raises the sender's
  importance score and creates a candidate rule proposal to stop hiding that sender or topic.
- `ever_replied` is derived from the user's sent mail and is never cleared by the agent — only an
  explicit user override can demote a replied-to sender.
- The VIP list holds emails, domains and keywords; a match can never be archived automatically.
- The priorities profile is plain English, written once, and is injected verbatim into the classifier
  and reviewer prompts. It is the user's own words, never rewritten by the agent.
- All memory is strictly per user. Starter rule packs are copied in on adoption, never shared live.
- Memory is additive and auditable: corrections are appended, never overwritten, so any behaviour
  change can be traced to the correction that caused it.
- Unknown evidence always resolves toward "important".

## Success Criteria
- [ ] Un-archiving an agent-hidden thread creates a `corrections` row and increases that sender's
      `importance_score`.
- [ ] After three corrections against the same sender, a rule proposal covering that sender appears.
- [ ] A sender added to VIP is never proposed for archive on the next run, at any confidence.
- [ ] Writing "I care about anything from investors and about our fundraise" in the priorities profile
      changes the classification of at least one matching thread on the next run.
- [ ] `ever_replied` is true for every sender present in the user's sent mail within the scanned window.
- [ ] A second user's corrections have zero effect on the first user's sender profiles.

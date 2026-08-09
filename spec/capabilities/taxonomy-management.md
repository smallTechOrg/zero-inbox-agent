# Capability: Taxonomy Management

## What It Does
Manages the user's categories — shipping a sensible editable default set, letting the user define
their own in plain English, letting the agent propose a taxonomy fitted to their actual mail, and
materialising every category **1:1 as a real Gmail label**.

## Inputs
| Input | Type | Source | Required |
|-------|------|--------|----------|
| Default taxonomy | seed | application seed data | Yes |
| User edits | plain-English name + description | Settings screen | No |
| Mail sample | `Item[]` | recent runs | No (for proposal) |

## Outputs
| Output | Type | Destination |
|--------|------|-------------|
| Categories | entities | `categories` |
| Real mailbox labels | label ids | channel + `categories.channel_label_id` |
| Proposed taxonomy | list of categories with rationale | Settings screen for approval |

## External Calls
| System | Operation | On Failure |
|--------|-----------|------------|
| Mailbox channel | create / rename label | Retry 3×; on failure keep the category but mark it unsynced and show a "Sync labels" action — triage still works |
| LLM provider | propose a taxonomy from a sample of the user's mail | Fall back to the default taxonomy; never block the user |

## Business Rules
- Default taxonomy: **Newsletters, Notifications, Receipts, Outreach, People, Urgent** — all editable
  and deletable by the user.
- Each category's plain-English `description` is used verbatim in the classifier prompt, so editing the
  description immediately changes classification behaviour.
- Every category maps to exactly one mailbox label, namespaced `ZeroInbox/<Name>`. Creating a category
  creates the label; renaming renames it; **deleting a category never deletes the label or any mail** —
  the label is left in place.
- A proposed taxonomy is only a proposal: nothing is created until the user approves it.
- Categories are per user; two users' taxonomies never interact.
- The `Urgent` category can never carry `default_action = archive`.

## Success Criteria
- [ ] A new user is seeded with the six default categories.
- [ ] `POST /api/categories/sync-labels` results in a real `ZeroInbox/<Name>` label existing in Gmail for
      every category, verified by reading the label list back.
- [ ] Renaming a category renames the Gmail label, and no mail moves.
- [ ] Deleting a category leaves the Gmail label and all mail intact.
- [ ] Editing a category description changes at least one thread's classification on the next run.
- [ ] Setting `Urgent` to archive is rejected with a validation error.

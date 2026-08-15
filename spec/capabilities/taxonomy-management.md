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
- **Phase 9 — the seed set is a starting point, not the taxonomy.** The category set that a user
  actually runs on is **derived from their own inbox** and re-derivable on demand. See
  [inbox-derived-taxonomy](inbox-derived-taxonomy.md), which owns discovery, the redo flow and the
  re-organisation of past decisions. This file continues to own seeding, label materialisation and
  the validation rules below.
- Seed taxonomy: **Newsletters, Notifications, Receipts, Outreach, People, Urgent** and — from
  Phase 9 — **Important**, all editable and deletable by the user. Seeded `default_action`: `archive`
  for Newsletters, Notifications, Outreach **and Receipts** (Receipts changed from `keep` to `archive`
  in Phase 7 — see
  [drive-to-inbox-zero](drive-to-inbox-zero.md#decision--receipts-is-archive-was-an-open-question-now-decided));
  `keep` for People, Urgent and Important. A user-added category such as **Legal** typically stays
  `keep`.
- **`Important` (Phase 9)** is the destination for a never-miss verdict that is neither a person nor
  time-critical — *"the reviewer said this matters"*. Like People/Urgent/Legal it is a
  `NEVER_ARCHIVE_KEYS` member: it can never carry `default_action = archive`.
- Each category's plain-English `description` is used verbatim in the classifier prompt, so editing the
  description immediately changes classification behaviour.
- Every category maps to exactly one mailbox label, namespaced `ZeroInbox/<Name>`. Creating a category
  creates the label; renaming renames it; **deleting a category never deletes the label or any mail** —
  the label is left in place.
- The Gmail label sidebar is the master list of what is archived and how it is categorized — there is no
  separate archive table; see [gmail-actions-and-undo](gmail-actions-and-undo.md) for the atomic
  archive+label write and the audit-trail-vs-source-of-truth distinction.
- **A derived taxonomy is only half the win; the other half is that it classifies for free.** The
  user's mail is heavily concentrated in a handful of senders (Facebook ~1,586 across five addresses,
  BookMyShow ~635 across two, Jagriti Theatre 334, Apple ~176, PayPal 78, Twitter 40 — all currently
  collapsing into `Notifications`). A category derived from that evidence is decidable **from the
  sender alone**, so approving one materialises deterministic tier-1 rules rather than more work for
  the model. See
  [inbox-derived-taxonomy § A6](inbox-derived-taxonomy.md#a6-concentration-becomes-tier-1-rules--through-the-existing-machinery)
  and [cost-tiered-triage](cost-tiered-triage.md).
- **Deletion verifies, it never assumes.** `GET /api/categories/{id}/usage` returns
  `{decisions, rules, items}`; deleting a category is permitted only when all three are **zero**. A
  non-zero count refuses the deletion and names the counts — a category is never removed "because it
  looked empty". (The Gmail label and all mail survive deletion regardless.)
- **A test must never be able to create a category on a real account.** `e2e-actions-test` /
  `E2EActionsTest` (`default_action=archive`) exists on the live account as leftover test pollution.
  It is removed **through this capability's own API** — usage check, then `DELETE` — and **never by an
  ad-hoc script against `zero_inbox.db`**. The structural fix is the test-isolation guard owned by
  Phase 9 slice 8 (`test-isolation-guard`), which makes a real-account write raise loudly; row
  deletion alone is not an acceptable close-out. See [roadmap](../roadmap.md).
- A proposed taxonomy is only a proposal: nothing is created until the user approves it.
- Categories are per user; two users' taxonomies never interact.
- **`NEVER_ARCHIVE_KEYS = {urgent, people, legal, important}` can never carry
  `default_action = archive`**, on create and on update. This guard survives the Phase 9 never-miss
  reframe deliberately and unchanged: a *category-wide* archive default is a bulk, silent sweep of the
  one kind of mail a human must see, which is a different operation from a **per-thread never-miss
  archive into that category's own label**. The reasoning, and the distinction, are stated in
  [never-miss-safeguards](never-miss-safeguards.md#never-archive-keys-reconciled).
- **A never-miss category is archived *to its label*, never bare.** `archive_to_never_miss_label` is
  the only path that may archive a `NEVER_ARCHIVE_KEYS` thread, it always attaches the category label,
  and it always writes an undo token.

## Success Criteria
- [ ] A new user is seeded with the seven default categories (six pre-Phase-9 plus `Important`).
- [ ] `POST /api/categories/sync-labels` results in a real `ZeroInbox/<Name>` label existing in Gmail for
      every category, verified by reading the label list back.
- [ ] Renaming a category renames the Gmail label, and no mail moves.
- [ ] Deleting a category leaves the Gmail label and all mail intact.
- [ ] Editing a category description changes at least one thread's classification on the next run.
- [ ] Setting `Urgent`, `People`, `Legal` or `Important` to `default_action=archive` is rejected with a
      validation error, on create and on update.
- [ ] `GET /api/categories/{id}/usage` returns the true `{decisions, rules, items}` counts, and
      `DELETE /api/categories/{id}` is **refused** when any count is non-zero and succeeds when all
      are zero — proving deletion verifies rather than assumes.
- [ ] A test that attempts to create a `Category` for a non-`test-` `user_id`, or to bind
      `zero_inbox.db`, **raises** and fails loudly (slice 8's guard); removing the guard makes
      `tests/unit/test_isolation_guard.py` fail.
- [ ] A never-miss archive into `ZeroInbox/Urgent` succeeds while the same category still rejects
      `default_action=archive` — proving the two operations are distinct and both guards hold.

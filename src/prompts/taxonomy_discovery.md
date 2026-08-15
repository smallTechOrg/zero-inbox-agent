# Taxonomy discovery — name and group what is already there

You are deriving an email category set for ONE specific person from a census of
their ACTUAL senders. You are not designing a generic inbox taxonomy.

## What you are given

- **Census** — one row per sender / domain / mailing list, with the number of
  threads it accounts for and a few free signals. There are no subjects and no
  message bodies here, by design. Fields:
  - `value` — the sender address, domain or `List-Id`
  - `kind` — `sender` | `domain` | `list_id`
  - `thread_count`, `unread_count`
  - `ever_replied` — the user has genuinely replied to this address
  - `is_no_reply` — the address cannot receive a reply, so it is structurally
    **not** a correspondent
  - `has_unsubscribe` — carries an unsubscribe link
  - `in_gap_set` — this sender produced at least one thread the current taxonomy
    could not place
- **Gap set** — the threads the current taxonomy failed on: the ones the
  classifier marked *no category fits* or landed under the confidence bar.
  Subjects are truncated. **These are the holes you exist to close.**
- **Current taxonomy** — what the user has today.

## The job

**The census decides what exists; you name and group it.** Every category you
propose must absorb real, named senders from the census. Concentration is the
signal: if five addresses at one domain account for a sixth of the mailbox,
that is a category, and calling it "Notifications" along with everything else is
the defect you are fixing.

## Hard rules

1. **Evidence or nothing.** Every proposed category MUST list at least one real
   `value` from the census in `evidence_senders`, copied verbatim. A category
   with no evidence is rejected before the user ever sees it.
2. **Never invent a sender.** Only values present in the census.
3. **Cover the concentration.** Every census row with `thread_count >= 10` must
   appear in exactly ONE category's `evidence_senders`. One sender, one
   category — a sender claimed twice cannot become a deterministic rule.
4. **Close the gaps.** Every sender with `in_gap_set = true` must be covered.
5. **`is_no_reply` is decisive.** An address that cannot receive a reply is
   automated mail. It never belongs in a person-to-person category.
6. **`ever_replied` is decisive the other way.** An address the user has replied
   to is a correspondent and belongs in a person-to-person category.
7. **`default_action`** is `archive`, `keep` or `digest`. Prefer `archive` for
   high-volume automated mail — that is what reaching inbox zero means. But
   `people`, `urgent`, `legal` and `important` MUST be `keep`; they are the mail
   a human has to see.
8. **`description` is used verbatim as the classifier's definition of the
   category.** Write one concrete sentence that names the kind of mail, not a
   vague label.
9. Prefer **8–14 categories**. Fewer and the concentration collapses again; more
   and the user cannot hold it in their head.
10. Reuse an existing `key` when you mean the same thing as an existing
    category. A new meaning gets a new `snake_case` key.

## Output

Return ONLY a JSON array. Each element:

```json
{
  "key": "snake_case_key",
  "name": "Display Name",
  "description": "One concrete sentence defining what belongs here.",
  "default_action": "archive",
  "rationale": "Why this user's mail needs this category, citing the volume.",
  "evidence_senders": ["notification@facebookmail.com", "reminders@facebookmail.com"]
}
```

No prose, no markdown fence, no explanation outside the JSON.

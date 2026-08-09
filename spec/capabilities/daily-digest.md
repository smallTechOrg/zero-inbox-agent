# Capability: Daily Digest

## What It Does
Produces a daily summary of everything that was hidden, grouped by category, so nothing ever vanishes
silently and the user can un-hide anything in one click.

## Inputs
| Input | Type | Source | Required |
|-------|------|--------|----------|
| Applied decisions for the day | `Decision[]` | `decisions` | Yes |
| Categories | entities | `categories` | Yes |
| Digest hour + timezone | settings | `user_settings` | Yes |

## Outputs
| Output | Type | Destination |
|--------|------|-------------|
| Digest | markdown summary + counts | `digests` + Digest screen |
| Un-hide action | undo of the underlying action | `action_logs` |

## External Calls
| System | Operation | On Failure |
|--------|-----------|------------|
| LLM provider | summarize the day's hidden mail in plain English | Fall back to the mechanical grouped list — the digest is never skipped |

## Business Rules
- Every thread hidden that day appears in the digest — the digest is exhaustive, not a sample. A hidden
  thread missing from the digest is a correctness defect.
- Grouped by category, ordered by count descending, each group listing sender and subject.
- Threads marked `time_sensitive` are listed first under a distinct heading even if they were kept.
- Each row links straight to the un-hide action, which reuses the undo path.
- The digest is generated for the user's local `digest_hour_local` in their timezone.
- The digest is rendered in-app in v1; no email is sent.

## Success Criteria
- [ ] The digest for a day lists exactly the set of threads whose decisions were `applied` as archive
      that day — count matches the database exactly.
- [ ] Un-hiding from the digest returns the thread to the inbox, verified against the real mailbox.
- [ ] A day with no hidden mail renders a clear empty state, not an error.
- [ ] With the LLM deliberately unavailable, the digest still renders the mechanical grouped list.
- [ ] Time-sensitive threads appear in their own section at the top.

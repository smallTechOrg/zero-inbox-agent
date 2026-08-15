# Capability: Taxonomy Management

## What It Does
Each user owns an editable set of categories with per-category rules; the agent classifies against the current taxonomy every run, so edits between chunks change behaviour immediately.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Category name/description/rule edits | JSON | dashboard taxonomy panel | yes |

## Outputs
| Output | Type | Destination |
|---|---|---|
| categories rows | records | [data.md](../data.md) |
| Gmail labels | labels | user's Gmail (created lazily on first use, prefixed `ZI/`) |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| Gmail API | create/rename label | run pauses that action, feed reports it, run continues |

## Business Rules
- Seed defaults on first sign-in: Finance, Newsletters, Notifications, Personal, Shopping, Travel, Needs review.
- Rules: `label_only` or `label_and_archive` (e.g. Newsletters → auto-archive default; Finance → label only default).
- "Needs review" is reserved: not deletable, rule fixed to `label_only`.
- Phase 1: add, rename, edit rule, delete-if-unused. Phase 2 ([category-rules](category-rules.md)): merge/delete-with-reassignment.
- Descriptions are classifier guidance and are included in the prompt.

## Success Criteria
- [ ] Renaming a category between chunks makes the next chunk file into the new name (integration test, real LLM).
- [ ] Deleting an in-use category in Phase 1 returns a clear 409 message.
- [ ] Gmail labels are only ever created under the `ZI/` prefix and are fully removable.

> **Assumed:** agent-created Gmail labels are namespaced `ZI/<Category>` so all agent state in Gmail is identifiable and removable.

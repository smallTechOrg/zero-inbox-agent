# Capability: Category Rules Depth (Merge / Delete / Reassign)

## What It Does
Completes taxonomy management: merge categories, delete-with-reassignment, and reorder — with the historical relabeling applied to Gmail as an audited, undoable operation.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| Merge/delete request | `DELETE /api/taxonomy/{id}?merge_into=<id>` | taxonomy panel | yes |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Repointed thread_decisions + sender_profiles | records | [data.md](../data.md) |
| Gmail relabel mutations grouped as a system run (`trigger=taxonomy_merge`) | audit rows | run timeline (undoable card) |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| Gmail API | swap `ZI/Old` → `ZI/New` labels on affected threads | resumable like any run |

## Business Rules
- A merge is executed as a run: it appears on the timeline with its own card and one-click undo (undo restores the old category, labels, and profile pointers).
- "Needs review" can be neither merged away nor deleted.
- No LLM calls involved.

## Success Criteria
- [ ] Merging A into B relabels every affected thread in Gmail and repoints all rows; undo restores the exact prior state.
- [ ] The merge appears as a timeline card with counts and reason.

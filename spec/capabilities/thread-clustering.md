# Capability: Thread Clustering

## What It Does
Groups the run's decisions into a small number of clusters ("142 threads from Substack newsletters")
so a large inbox collapses into roughly thirty decisions instead of thousands.

## Inputs
| Input | Type | Source | Required |
|-------|------|--------|----------|
| Decisions | `Decision[]` | cost-tiered triage | Yes |
| Threads | `Item[]` | `items` | Yes |

## Outputs
| Output | Type | Destination |
|--------|------|-------------|
| Clusters | `Cluster[]` | `clusters` |
| Cluster assignment | FK per decision | `decisions.cluster_id` |
| Sample subjects | 3 per cluster | API response |

## External Calls
None — clustering is deterministic and local.

## Business Rules
- Grouping key precedence: `list_id` → `from_email` → `from_domain` → `category`. The first key that
  gathers ≥ 3 threads wins; anything left over falls into a per-category cluster.
- A cluster's `suggested_action` is the majority proposed action of its members; a cluster whose
  members disagree on action is split by action so a bulk approval is never ambiguous.
- A cluster records both `min_confidence` and `avg_confidence`; the UI surfaces the minimum, because
  the weakest member is what the user is risking.
- A cluster containing any `time_sensitive` or VIP thread is never given a bulk archive suggestion —
  those threads are pulled out into their own keep cluster.
- Clusters are per run and never span users.
- Target: ~200 threads collapse to ≤ 40 clusters.

## Success Criteria
- [ ] A run over 220 threads produces ≤ 40 clusters, and the cluster item-counts sum to exactly 220.
- [ ] Every decision has a non-null `cluster_id`.
- [ ] No cluster contains members with differing `proposed_action`.
- [ ] A cluster containing a time-sensitive thread does not suggest bulk archive.
- [ ] Each cluster returns exactly three real sample subjects from its members.

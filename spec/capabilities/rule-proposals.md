# Capability: Rule Proposals & Persistent Filters

## What It Does
Mines the user's decision history for repeating patterns and proposes a single rule covering hundreds
of threads, previews exactly what it would archive in dry-run, and — once approved — creates a real
persistent Gmail filter so the pattern is handled forever.

## Inputs
| Input | Type | Source | Required |
|-------|------|--------|----------|
| Decision history | `Decision[]` | `decisions` | Yes |
| Threads | `Item[]` | `items` | Yes |
| Corrections | `Correction[]` | `corrections` | Yes |
| Starter rule pack | template | `rule_pack_templates` | No |
| User approval | interaction | Rules screen | Yes |

## Outputs
| Output | Type | Destination |
|--------|------|-------------|
| Proposed rules ranked by coverage | `Rule[]` (status `proposed`) | `rules` |
| Dry-run preview | list of threads that would be archived | Rules screen |
| Persistent mailbox filter | filter id | channel + `rules.channel_filter_id` |

## External Calls
| System | Operation | On Failure |
|--------|-----------|------------|
| Mailbox channel | create filter (`gmail.settings.basic`) | Retry 3×; the rule stays active in-app and is marked "filter not synced" with a retry action — behaviour is unaffected |
| LLM provider | name and explain a mined pattern in plain English | Fall back to a mechanically generated name |

## Business Rules
- Mining is deterministic (grouping by `List-Id`, sender, domain, subject shape); the LLM only names
  and explains the pattern, it does not invent the matcher.
- Proposals are ranked by coverage — the rule that resolves the most threads is shown first.
- **Every proposal must be previewable in dry-run** before it can be activated, and the preview
  enumerates the exact threads affected.
- Rule states: `proposed` → `active` (proposes decisions, still needs approval) → `automatic` (acts
  without per-decision approval). Promoting to `automatic` requires an explicit confirmation naming the
  consequence, and is the **only** path to unattended action.
- A rule can never be proposed that would archive a VIP sender or a sender with `ever_replied = true`.
- A created Gmail filter never carries a delete/trash action — label and skip-inbox only.
- Adopting a starter pack ("founder", "engineer", "recruiter") **copies** its rules into the user's
  account as `proposed`; the shared template is never referenced at runtime.

## Success Criteria
- [ ] Mining over the 220-thread fixture proposes at least one rule covering ≥ 20 threads.
- [ ] A proposal's dry-run preview enumerates exactly the threads it would archive, and running the rule
      afterwards affects exactly that set.
- [ ] A rule cannot be activated without a preview having been generated.
- [ ] Approving a rule creates a real Gmail filter, verified by reading the filter list back.
- [ ] No created filter has a delete or trash action.
- [ ] No proposed rule matches a VIP sender or an `ever_replied` sender.
- [ ] Adopting the "founder" pack creates that pack's rules in the user's account as `proposed` and
      leaves the template unchanged.

# Capability: Chat to Rules

## What It Does
Turns plain English typed in a chat ("stop showing me GitHub notifications unless I'm mentioned") into
a concrete, previewable rule — with **full conversation memory**, so a follow-up amends the rule from
the previous turn rather than starting over.

## Inputs
| Input | Type | Source | Required |
|-------|------|--------|----------|
| User message | text | Chat screen | Yes |
| Conversation history | prior turns | `chat_messages` | Yes |
| Taxonomy + existing rules | entities | `categories`, `rules` | Yes |

## Outputs
| Output | Type | Destination |
|--------|------|-------------|
| Assistant reply | text | Chat screen + `chat_messages` |
| Drafted rule | `Rule` (status `proposed`) | `rules` |
| Dry-run preview | affected threads | Chat screen |

## External Calls
| System | Operation | On Failure |
|--------|-----------|------------|
| LLM provider | interpret intent + draft a rule as strict JSON | Retry once; then reply "I couldn't turn that into a rule — can you rephrase?" and create nothing |

## Business Rules
- **Conversation memory is required, not optional.** The full prior turn history for that user is
  loaded on every turn. A turn that amends ("actually make that only for weekends") must modify the
  rule produced by the referenced earlier turn, recorded via `chat_messages.rule_id`.
- The drafted rule is validated against the rule schema; an invalid draft is never persisted.
- Every drafted rule is automatically simulated in dry-run and the preview is shown **before** the
  Apply button is offered.
- Chat can create and amend rules and answer questions about the user's mail statistics. It can never
  perform a mailbox mutation directly — it produces rules the user applies.
- Chat can never draft a rule that archives a VIP or `ever_replied` sender.
- Chat history is per user and never crosses tenants.

## Success Criteria
- [ ] "Stop showing me GitHub notifications unless I'm mentioned" produces a schema-valid rule matching
      GitHub notification mail with a mention exception.
- [ ] A follow-up turn "actually keep the ones about the api repo" **amends** the previous rule (same
      `rule_id`) rather than creating a second unrelated rule.
- [ ] Every drafted rule is accompanied by a dry-run preview before Apply is enabled.
- [ ] An uninterpretable message creates no rule and returns a clear explanatory reply.
- [ ] Reloading the page shows the full prior conversation.
- [ ] A request to "delete all my newsletters" is refused with an explanation that the agent never
      deletes, and offers archive instead.

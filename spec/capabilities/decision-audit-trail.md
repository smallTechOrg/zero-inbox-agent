# Capability: Decision & Action Audit Trail

## What It Does
Records a complete, inspectable history of everything the agent decided and did: every decision with
its reasoning, confidence and which rule fired; every mailbox mutation with its parameters and an undo
token; and every user correction as a training signal.

*Phase 1 delivers the decision half. Phase 2 completes it with the mutation log and corrections.*

## Inputs
| Input | Type | Source | Required |
|-------|------|--------|----------|
| Decisions | entities | triage graph | Yes |
| Mutations | operation + params | action layer (Phase 2) | Yes |
| User corrections | interaction / observed un-archive | dashboard, mailbox re-scan | Yes |
| LLM call metrics | tokens, latency, model | LLM client | Yes |

## Outputs
| Output | Type | Destination |
|--------|------|-------------|
| Decision record | entity | `decisions` |
| Mutation record + undo token | entity | `action_logs` |
| Correction record | entity | `corrections` |
| Cost record | entity | `llm_calls` |
| Structured event | JSON log + LangSmith trace | stdout / LangSmith |

## External Calls
None directly — this capability observes the others.

## Business Rules
- Every decision persists: category, proposed action, confidence, full reasoning, `decided_by` tier,
  `rule_id` when a rule fired, and `time_sensitive`.
- Every mutation persists: operation, exact request parameters, provider response, and an **undo
  token** sufficient to fully reverse it. A mutation without an undo token is a defect.
- `action_logs.operation` has no delete/trash value — destructive operations are not representable.
- Every correction persists the from/to action and its source, updates the sender's importance score,
  and becomes a candidate signal for a proposed rule.
- Nothing in the audit trail is ever deleted or edited; corrections are appended, not overwritten.
- Structured logging (structlog JSON to stdout) is unconditional; LangSmith tracing is enabled when
  `LANGCHAIN_API_KEY` is present. Observability is wired from Phase 1, never deferred.
- No secret, token, or email body ever appears in a log line or an audit row.

## Success Criteria
- [ ] Every decision row from a real run has non-empty reasoning and a valid `decided_by` value.
- [ ] Every `action_logs` row (Phase 2) has a non-null `undo_token`, and applying it reverses the action.
- [ ] `action_logs.operation` never takes a delete or trash value — enforced by a check on the enum.
- [ ] Un-archiving a thread the agent hid produces a `corrections` row and raises that sender's
      importance score.
- [ ] A Phase 1 end-to-end run emits a structlog summary event containing `run_id`, per-tier counts and
      cost, visible on stdout.
- [ ] A log scan of a full run finds no token, secret or body text.

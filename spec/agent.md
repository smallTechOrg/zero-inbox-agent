# Agent Graph

Framework: **LangGraph** (`StateGraph`). Two graphs exist: the **triage graph** (Phase 1, extended in
Phase 2) and the **chat-to-rules graph** (Phase 3).

## Patterns Used

From [`harness/patterns/agentic-ai.md`](../harness/patterns/agentic-ai.md):

| Pattern | Where |
|---------|-------|
| #16 Resource-Aware Optimization | The cost-tiered cascade: tiers 1–2 are free, the LLM sees only the remainder, batched 20–50 items/call |
| #2 Routing | `route_after_history` sends each item to `resolved`, `llm_batch`, or `deep_read` |
| #3 Parallelization | LLM batches fan out via `Send` at max concurrency 4 |
| #4 Reflection | `second_pass_reviewer` audits every archive proposal for false negatives (Phase 2) |
| #13 Human-in-the-Loop | Confidence floor → `needs_your_call`; no mutation without approval or a promoted rule |
| #18 Guardrails | Redaction before egress; JSON-schema validation of every LLM response; `DryRunViolation` on any mutation while dry-run is on |
| #12 Exception Handling | Retries/backoff on Gmail + NIM; a failed batch degrades to `needs_your_call`, never to "archive" |
| #8 Memory Management | Per-user sender history, corrections, VIP list, priorities profile injected as evidence (Phase 2); chat turn history (Phase 3) |
| #9 Learning and Adaptation | Corrections update sender importance and seed rule proposals |
| #19 Evaluation and Monitoring | structlog + LangSmith traces + per-call token/cost rows |

**Not used:** multi-agent collaboration, planning, RAG, MCP, tree-of-thought. The task is a
classification cascade, not open-ended exploration — adding them would be gold-plating.

---

## State

```python
# src/graph/state.py
from typing import Annotated, TypedDict
import operator

class TriageState(TypedDict, total=False):
    # identity / scope
    run_id: str
    user_id: str
    channel_account_id: str
    limit: int                      # threads to pull (Phase 1: 200)
    dry_run: bool                   # Phase 1: always True

    # per-user context loaded once
    categories: list[dict]          # taxonomy
    rules: list[dict]               # active deterministic rules
    sender_stats: dict[str, dict]   # sender_email -> {received, replied, archived, ever_replied}
    vip: dict                       # {emails: [], domains: [], keywords: []}   (Phase 2)
    priorities_profile: str         # plain-English profile               (Phase 2)
    settings: dict                  # {auto_act_threshold, confidence_floor, model}

    # working set
    items: list[dict]               # normalized Item dicts (redacted snippet, no body)
    resolved: Annotated[list[dict], operator.add]    # decisions from tiers 1-2
    llm_queue: list[dict]           # items needing an LLM verdict
    deep_queue: list[dict]          # borderline items needing full-thread read
    llm_decisions: Annotated[list[dict], operator.add]   # reducer — batches fan in here

    # outputs
    decisions: list[dict]
    clusters: list[dict]
    counts: dict                    # {total, by_tier, by_category, needs_your_call}
    cost: dict                      # {tokens_in, tokens_out, usd, llm_calls}

    # control
    error: str | None
    status: str                     # running | completed | failed | cancelled | resumable

    # Phase 6 — durable, resumable runs
    already_decided: set[str]       # item_ids with a decision row for this run_id; excluded
                                    # from every tier queue so a resume re-classifies nothing
```

`llm_decisions` and `resolved` use `operator.add` reducers so parallel `Send` branches merge without
clobbering each other. Everything else is last-write-wins (written by exactly one node).

**Phase 7 adds no new `TriageState` key.** It adds one key *inside* each decision dict —
`autonomy_state` — written by `mark_autonomy_state` and persisted to `decisions.autonomy_state` by
`persist_decisions`. It also requires that each entry of `state["categories"]` carries
`default_action` and `auto_act_threshold` (a pinned cross-slice contract on
`graph.persistence._load_context_rows`), and that `state["settings"]["auto_act_threshold"]` is the
user's real persisted value — which, from Phase 7, is finally read by something.

---

## Nodes

| Node | Phase | Does |
|------|-------|------|
| `load_context` | 1 | Loads the user's categories, active rules, sender stats, settings (Phase 2: VIP list + priorities profile) from the DB into state |
| `fetch_items` | 1 | `ChannelAdapter.list_threads(limit)` → normalized `Item` dicts; headers + subject + ≤200-char snippet only |
| `redact_items` | 1 | Runs `tools.redact()` over every subject/snippet **in place**. Single egress chokepoint — nothing reaches an LLM unredacted |
| `apply_deterministic_rules` | 1 | Tier 1. Matches each item against active rules (sender, domain, `List-Id`, subject regex, has-attachment). Match → decision with `decided_by="rule"`, `confidence=rule.confidence`, `rule_id` set. Appends to `resolved` |
| `apply_sender_history` | 1 | Tier 2. Unresolved items only. Strong prior from `sender_stats`: ever-replied → keep at high confidence; never-opened bulk sender with ≥N archived → archive proposal. `decided_by="sender_history"` |
| `prepare_llm_batches` | 1 | Chunks `llm_queue` into batches of 20–50 by token budget; emits one `Send("llm_classify_batch", …)` per batch |
| `llm_classify_batch` | 1 | Tier 3. One LLM call per batch. Prompt: `prompts/classify.md` + taxonomy + (Phase 2) priorities profile. Returns strict JSON array `[{item_id, category, action, confidence, reasoning, time_sensitive}]`, schema-validated. `decided_by="llm"`. Items it marks `unsure` go to `deep_queue` |
| `deep_read_escalation` | 1 | Tier 4. For each borderline item, fetches the full thread + the user's past replies to that sender **in memory**, redacts, and makes a single-item LLM call. `decided_by="llm_deep"` |
| `second_pass_reviewer` | **2** | Reflection. Takes every decision proposing archive and asks a reviewer prompt (`prompts/reviewer.md`) one question only: *is this a false negative — something the user would be upset to miss?* Any flip becomes `keep` with `decided_by="reviewer"` and the reviewer's reasoning appended. Batched 20–50 |
| `apply_never_miss_floor` | **2** | Enforces: (a) confidence < `confidence_floor` → `needs_your_call`; (b) sender in VIP or `ever_replied` and no explicit override → force `keep`; (c) `time_sensitive` true → force `keep` unless a user-promoted rule explicitly says otherwise |
| `cluster_decisions` | 1 | `tools.clustering.cluster()` groups decisions by mailing-list id → sender → domain → category, emitting `Cluster` rows with counts, a suggested bulk action and the minimum confidence in the cluster |
| `align_to_category_default` | **7** | `graph/nodes_autonomy.py`. **The only stage in the system permitted to move a decision from `keep` toward `archive`**, and it runs *before* every never-miss safeguard so the reviewer, the floor and the VIP/reply-history guards still audit and can veto the result. Sets `proposed_action` to the category's `default_action` when the category is `archive`/`digest`, `confidence >= effective_threshold(category, settings)`, and none of the exclusions apply (`needs_your_call`, `decided_by="error"`, `decided_by="rule"`, `time_sensitive`, `unsure`, `ever_replied`, VIP). Also refreshes each cluster's `suggested_action` in place, since `cluster_decisions` ran before it. Never lowers confidence, never rewrites `decided_by`, never touches `status` or `review_state`. Rules C1–C5 in [drive-to-inbox-zero](capabilities/drive-to-inbox-zero.md#c-category-default-decides-the-action--align_to_category_default) |
| `mark_autonomy_state` | **7** | `graph/nodes_autonomy.py`. Runs at the *end* of the never-miss chain, when every verdict is final. Stamps each decision with exactly one `autonomy_state`: `needs_your_call` (status is `needs_your_call`) → `held_by_never_miss` (`decided_by="reviewer"`, VIP, `ever_replied`, or `time_sensitive`) → `category_keep` (category `default_action` is `keep`, or the keep came from a user rule) → `auto_act` (action is `archive`/`digest` and `confidence >= effective_threshold`) → `below_threshold` (otherwise). Evaluated in that fixed precedence, so the value always names the *first* rule that stopped the agent acting. This is the grouping key of the remainder ledger and the sole basis of `distance_to_zero` |
| `persist_decisions` | 1 | Writes `Decision`, `Cluster`, `LlmCall` rows; updates `TriageRun` counts/cost. Idempotent on `(run_id, item_id)` so a resumed run never double-decides. **Phase 6:** no longer the first write — it finalises clusters, upgrades `review_state`, and reconciles counts over rows already checkpointed |
| `handle_error` | 1 | Sets `status="failed"`, records the error on the run, and marks any undecided item `needs_your_call` — degradation always keeps mail visible. **Phase 6:** closes the run `resumable` instead of `failed` when ≥ 1 decision is already persisted, or when the cause is `ProviderCircuitOpen` |
| `finalize` | 1 | Sets `status="completed"`, writes final counts + cost, emits the structlog summary event. **Phase 7:** calls `apply_run_decisions(...)` (unless `dry_run`), writes the apply + remainder ledgers into `triage_runs.counts`, sets `error_message` and emits `run_apply_failed` when the apply pass failed or `distance_to_zero > 0`, and emits `inbox_zero_report` |

`second_pass_reviewer` and `apply_never_miss_floor` are wired in Phase 2. Phase 1 applied a simple
floor inside `persist_decisions` (confidence < 0.75 → `needs_your_call`) which Phase 2 replaces with
the full reviewer + floor + VIP-guard cascade.

`align_to_category_default` and `mark_autonomy_state` are wired in Phase 7 and bracket the never-miss
chain: the first runs immediately before `second_pass_reviewer`, the second immediately after
`apply_never_miss_floor`. That ordering is the safety argument — nothing the autonomy policy proposes
escapes the reviewer, and nothing is stamped `auto_act` before the reviewer has had its say.

---

## Edges

```
START → load_context
load_context        → fetch_items                    | error → handle_error
fetch_items         → redact_items                   | error → handle_error
redact_items        → apply_deterministic_rules
apply_deterministic_rules → apply_sender_history
apply_sender_history → route_after_history:
        "llm"       → prepare_llm_batches
        "skip_llm"  → cluster_decisions              (everything resolved by tiers 1-2)
prepare_llm_batches → Send(*) → llm_classify_batch   (fan-out, max concurrency 4)
llm_classify_batch  → route_after_llm:
        "deep"      → deep_read_escalation
deep_read_escalation → cluster_decisions
cluster_decisions   → align_to_category_default      (Phase 7; Phase 2-6: → second_pass_reviewer)
align_to_category_default → second_pass_reviewer     (Phase 7)
second_pass_reviewer → apply_never_miss_floor        (Phase 2+)
apply_never_miss_floor → mark_autonomy_state         (Phase 7; Phase 2-6: → persist_decisions)
mark_autonomy_state → persist_decisions              (Phase 7)
persist_decisions   → finalize
finalize            → END
handle_error        → END
```

Every edge from `cluster_decisions` onward is `edges.guard(...)`-wrapped, so an `error` in state at any
point short-circuits to `handle_error`. The two Phase 7 nodes are inserted **inside** that guarded
chain, not around it.

Routing functions (`src/graph/edges.py`):

- `route_after_history(state) -> "llm" | "skip_llm"` — `"llm"` iff `state["llm_queue"]` is non-empty.
- `route_after_llm(state) -> "deep"` — always routes to `deep_read_escalation`; when `deep_queue` is
  empty, `deep_read_escalation` becomes a passthrough (no LLM calls, items pass through unchanged).
  This keeps the LangGraph fan-in depth uniform across all branches, preventing Send/join shape
  mismatches when some batches produce no deep items.
- Every node-level edge is guarded: if `state.get("error")` is set, route to `handle_error`.

---

## Concurrency

- LLM batch fan-out via `Send`, **max 4 concurrent batches** (`config={"max_concurrency": 4}`).
- Batch size 20–50 items, chosen by an estimated-token budget of ~12k input tokens per call.
- `deep_read_escalation` is capped at **25 items per run**; overflow goes straight to
  `needs_your_call` rather than blowing the budget or silently archiving.
- **Phase 6 revises the write model.** Every tier node now calls
  `graph.checkpoint.record_batch(state, decisions, tier=…)` as its batch lands: a **short, independent
  transaction per batch** writing decisions as `review_state="provisional"`, appending that batch's
  `llm_calls`, bumping `items_decided` atomically, and emitting one `thread_classified` event per
  decision. Parallel batch branches do not contend — each owns a disjoint set of `(run_id, item_id)`
  keys and the unique constraint makes a collision a skip, not an error. A checkpoint failure is
  logged at WARNING and never fails the run; the un-checkpointed threads are simply re-decided on
  resume. See [capabilities/durable-resumable-runs.md](capabilities/durable-resumable-runs.md).
- **Resume:** `runner.execute_triage` loads `already_decided_item_ids(run_id)` into state and
  `fetch_items` removes them from every tier queue, so a resumed run re-classifies nothing.
- **Circuit breaker:** `llm_classify_batch` and `deep_read_escalation` let `llm.health.ProviderCircuitOpen`
  propagate into `state["error"]` (they do **not** swallow it into a degraded batch), which routes to
  `handle_error` and closes the run `resumable` with all provisional work preserved.

## Error Handler

`handle_error` never fails the user's mail. It: records `TriageRun.status="failed"` and the error
message, marks every item without a decision as `needs_your_call`, emits a structlog `error` event
with `run_id`, and ends. A partial run is always resumable — every write is idempotent on
`(run_id, item_id)`. **Phase 6:** when the run already has ≥ 1 persisted decision (or the error is
`ProviderCircuitOpen`), the run is closed `resumable` rather than `failed`, and a `run_resumable`
event is emitted so the dashboard offers a one-click resume. `cancelled` remains terminal and is
never converted.

## Finalize

`finalize` writes `status="completed"`, the per-tier counts, the token/cost totals, and emits a single
structlog event: `run_id`, `total`, `by_tier`, `needs_your_call`, `llm_calls`, `usd`, `duration_ms`.
That event is the source for the Phase 3 cost panel.

**Phase 7 — finalize also finishes the job.** Unless `dry_run` is on it calls
`graph.nodes.apply_run_decisions(...)`, which applies every `autonomy_state="auto_act"`,
`review_state="reviewed"` archive through the *unmodified* `tools.actions.apply_decision` gate
(never `force=True`, never writing `review_state`). It then writes the apply and remainder ledgers into
`triage_runs.counts`, and emits `triage.inbox_zero_report`. **`apply_run_decisions` never returns
early and silently** — every failure path populates `ledger.apply_failed_reason`; `finalize` turns a
non-null reason, or any `distance_to_zero > 0`, into a human-readable `triage_runs.error_message`, a
`run_apply_failed` SSE event, and `apply_ok=false` on the run payload. A run that archived nothing can
never render as a clean success. See
[drive-to-inbox-zero](capabilities/drive-to-inbox-zero.md#d-apply--through-the-review-gate-never-around-it-never-silent).

## Graph Assembly (pseudocode)

```python
# src/graph/agent.py
from langgraph.graph import StateGraph, START, END
from langgraph.types import Send
from graph.state import TriageState
from graph import nodes, edges

def _build_triage_graph():
    g = StateGraph(TriageState)
    for name in ("load_context", "fetch_items", "redact_items",
                 "apply_deterministic_rules", "apply_sender_history",
                 "prepare_llm_batches", "llm_classify_batch",
                 "deep_read_escalation", "cluster_decisions",
                 "persist_decisions", "handle_error", "finalize"):
        g.add_node(name, getattr(nodes, name))
    # Phase 2 adds:
    # g.add_node("second_pass_reviewer", nodes_review.second_pass_reviewer)
    # g.add_node("apply_never_miss_floor", nodes_review.apply_never_miss_floor)

    # Phase 7 adds (src/graph/agent.py, owned by the autonomy-policy slice):
    # g.add_node("align_to_category_default", nodes_autonomy.align_to_category_default)
    # g.add_node("mark_autonomy_state", nodes_autonomy.mark_autonomy_state)
    # cluster_decisions -> align_to_category_default -> second_pass_reviewer
    # apply_never_miss_floor -> mark_autonomy_state -> persist_decisions
    # Both edges are edges.guard(...)-wrapped, exactly like the nodes they sit between.

    g.add_edge(START, "load_context")
    g.add_conditional_edges("load_context", edges.guard("fetch_items"),
                            {"fetch_items": "fetch_items", "handle_error": "handle_error"})
    g.add_conditional_edges("fetch_items", edges.guard("redact_items"),
                            {"redact_items": "redact_items", "handle_error": "handle_error"})
    g.add_edge("redact_items", "apply_deterministic_rules")
    g.add_edge("apply_deterministic_rules", "apply_sender_history")
    g.add_conditional_edges("apply_sender_history", edges.route_after_history,
                            {"llm": "prepare_llm_batches", "skip_llm": "cluster_decisions"})
    g.add_conditional_edges("prepare_llm_batches",
                            lambda s: [Send("llm_classify_batch", {**s, "batch": b})
                                       for b in s["batches"]],
                            ["llm_classify_batch"])
    g.add_conditional_edges("llm_classify_batch", edges.route_after_llm,
                            {"deep": "deep_read_escalation", "done": "cluster_decisions"})
    g.add_edge("deep_read_escalation", "cluster_decisions")
    g.add_conditional_edges("cluster_decisions", edges.guard("persist_decisions"),
                            {"persist_decisions": "persist_decisions", "handle_error": "handle_error"})
    g.add_edge("persist_decisions", "finalize")
    g.add_edge("finalize", END)
    g.add_edge("handle_error", END)
    return g.compile()

triage_graph = _build_triage_graph()
```

`src/graph/runner.py` exposes the entry point used by the API and by tests:

```python
def run_triage(*, user_id: str, channel_account_id: str, limit: int = 200,
               dry_run: bool = True, run_id: str | None = None) -> str:
    """Creates (or resumes) a TriageRun, invokes triage_graph, returns run_id."""
```

## Chat-to-Rules Graph (Phase 3)

A small separate graph in `src/graph/chat_graph.py`:

```
START → load_chat_history → interpret_intent → (route)
            "rule"    → draft_rule → simulate_dry_run → respond → END
            "question"→ answer_from_state → respond → END
            "amend"   → load_prior_rule → draft_rule → simulate_dry_run → respond → END
```

`ChatState` carries `messages: Annotated[list, operator.add]` — the full prior turn history for that
user is loaded from the `chat_messages` table on every turn, so "actually make that only for weekends"
correctly amends the rule from the previous turn. Conversation memory is a **required** part of this
capability, not an enhancement. Every drafted rule is simulated in dry-run before the user is shown
the "Apply" button.

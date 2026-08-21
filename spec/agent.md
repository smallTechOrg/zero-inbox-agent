# Agent Graph — Triage Run

Framework: **LangGraph**. Pattern: linear pipeline with a batch loop and an
error-handler edge (no deep-read escalation, no planner — a single cheap batched
classification pass; see `harness/patterns/agentic-ai.md` "pipeline" pattern).

## State

```python
class RunState(TypedDict):
    run_id: str
    user_id: str
    chunk_limit: int                 # 50 (Phase 1) … 100 (Phase 2)
    threads: list[ClassifierView]    # undecided INBOX threads, newest first
    batches: list[list[ClassifierView]]
    batch_index: int
    decisions: list[Decision]        # this run's decisions so far
    counts: dict[str, int]           # per-category
    cost: CostTotals                 # calls, tokens, est_cost, fallback_events
    error: str | None                # human-readable; set only by error handler
```

`ClassifierView` holds ONLY the privacy-allowed fields (see architecture.md).
`Decision` = thread_id, category_id, confidence (0–1), one-line reason, needs_review.

## Nodes

| Node | Does |
|---|---|
| `load_chunk` | Fetch up to `chunk_limit` INBOX threads newest-first; drop any `gmail_thread_id` already in `thread_decisions` for this user (never-redo). Emit `chunk_loaded`. Empty chunk → straight to `finalize` ("inbox chunk clean"). |
| `match_profiles` | (Phase 2; Phase 1 pass-through) Decide threads whose sender has a `sender_profiles` row — no LLM. Emit one feed event each. |
| `classify_batch` | One `classify_batch()` LLM call for the next ≤25 undecided threads against the user's current taxonomy (fetched fresh each run — the agent adapts to edits). Confidence < 0.7 ⇒ keep best-guess category AND set `needs_review`. Persist each decision row immediately. Emit per-thread feed events with reasoning + cost ticker update. |
| `apply_actions` | For each new decision: apply the category's rule — always add the category label; `auto_archive` rule also removes INBOX; `needs_review` additionally adds the "Needs review" label. Audit row written before each Gmail call. Serial per run. |
| `error_handler` | Any node exception: persist run status `interrupted` with a human-readable reason (rate limit / reconnect Gmail / provider down), emit `run_interrupted`, go to `finalize`. Work already persisted stays; the next `POST /runs` resumes. |
| `finalize` | Write run totals + status (`completed`/`interrupted`), emit `run_finished`. Always runs. |

## Edges

```
START → load_chunk → match_profiles → classify_batch → apply_actions
apply_actions → classify_batch        # while batch_index < len(batches)
apply_actions → finalize              # when all batches done
load_chunk → finalize                 # empty chunk
(any node exception) → error_handler → finalize → END
```

## Concurrency & Resumability

- One active run per user (enforced at `POST /runs`; a second trigger while running
  returns the active run_id).
- Batches are sequential; Gmail mutations are serial. Within `classify_batch` the
  single LLM call covers the whole batch (that IS the batching).
- Resumability is **data-driven, not checkpoint-driven**: `thread_decisions` is the
  source of truth. A resumed run re-enters `load_chunk`, which skips decided
  threads; decided-but-unapplied decisions (decision row exists, no audit row) are
  re-applied idempotently. No LangGraph checkpointer needed.

## LLM Call Contract (inside `classify_batch`)

- Prompt: `src/prompts/classify.md` + taxonomy (names, descriptions) + batch of
  `ClassifierView`s. Output: strict JSON list of `{thread_id, category, confidence,
  reason}`; unparseable entries → `needs_review` with reason "classifier output
  invalid", never a crash.
- Provider: NVIDIA; on timeout (30s hard)/429/error → Gemini for this batch, emit
  `fallback` event; next batch tries NVIDIA first again.

## Assembly

```python
g = StateGraph(RunState)
for name in ("load_chunk", "match_profiles", "classify_batch", "apply_actions",
             "error_handler", "finalize"):
    g.add_node(name, wrap_with_error_edge(node_fns[name]))  # exceptions → error_handler
g.set_entry_point("load_chunk")
g.add_conditional_edges("load_chunk", has_threads, {True: "match_profiles", False: "finalize"})
g.add_edge("match_profiles", "classify_batch")
g.add_edge("classify_batch", "apply_actions")
g.add_conditional_edges("apply_actions", more_batches, {True: "classify_batch", False: "finalize"})
g.add_edge("error_handler", "finalize")
g.add_edge("finalize", END)
agent = g.compile()
```

`src/graph/runner.py` exposes `run_triage(user_id, run_id)`; the API starts it as a
FastAPI background task and streams its events over SSE.

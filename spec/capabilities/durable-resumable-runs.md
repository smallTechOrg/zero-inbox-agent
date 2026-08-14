# Capability: Durable, Resumable, Transparent Triage Runs

## What It Does
Persists every triage decision to the database **as it is made** — in an explicit not-yet-reviewed
state — so an interrupted run can be resumed from exactly where it stopped, streamed thread-by-thread
while it runs, and abandoned safely when the LLM provider is degraded, without ever letting an
un-reviewed archive become visible-as-final or actionable.

## Why (the defect this closes)
Decisions were written to the database **once, at the very end of the graph**, after the never-miss
reviewer. On a real mailbox, run `fbeed060` decided 2,003 of 2,176 threads over ~23 minutes against a
flaky provider (2,774 `llm.retry` events) and left **0 rows in `decisions`**. All of that work lived
only in LangGraph in-memory state, so there was (a) nothing to resume from and (b) nothing to show
per-thread while the run was in flight.

End-of-graph persistence was **deliberate** — see
[never-miss-safeguards](never-miss-safeguards.md). Nothing may be shown as final or actioned before
the second-pass reviewer has had the chance to flip a false-negative `archive` back to `keep`. This
capability keeps that guarantee by separating **durability** from **finality**: a decision is durable
the moment it is made, and only becomes *final* (visible-as-final, eligible for apply/approve) when
the reviewer has upgraded it.

## Inputs
| Input | Type | Source | Required |
|-------|------|--------|----------|
| Per-tier decision batch | `Decision[]` (in-memory, one tier's output) | tiers 1–4 of [cost-tiered-triage](cost-tiered-triage.md) | Yes |
| Existing run id | UUID string | `triage_runs.id` of the interrupted run | On resume |
| Already-decided item ids for the run | `str[]` | `decisions` where `run_id = :run_id` | On resume |
| Reviewer verdicts | `Decision[]` with `decided_by="reviewer"` | [never-miss-safeguards](never-miss-safeguards.md) | Yes |
| Provider health snapshot | `{consecutive_failures, retries_in_run, calls_in_run, circuit_open, current_model, chain_position, throttle}` | LLM client | Yes |

## Outputs
| Output | Type | Destination |
|--------|------|-------------|
| Provisional decision rows | `decisions` with `review_state="provisional"` | `decisions` |
| Reviewed decision rows | `decisions` with `review_state="reviewed"` | `decisions` |
| Run status `resumable` | enum value | `triage_runs.status` |
| Resume summary | `{run_id, items_total, items_decided, remaining}` | `GET /api/runs/{run_id}` |
| `thread_classified` SSE event (per thread, incl. `review_state`) | JSON | SSE bus → Activity drawer |
| `provider_degraded` / `run_resumable` / `model_fallback` SSE events | JSON | SSE bus → Activity drawer |
| `llm_calls.model` = the model that actually served the call | string | `llm_calls` |

## External Calls
| System | Operation | On Failure |
|--------|-----------|------------|
| Database | short-transaction batch insert of provisional decisions after each tier batch | Logged at WARNING; the batch is retried once on the next checkpoint. A checkpoint failure never kills the run — the run continues in memory and the un-checkpointed threads are simply re-decided on resume (idempotency makes that safe). |
| Database | upgrade `review_state` to `reviewed` after the reviewer pass | The run fails to `resumable`; nothing is applied, because only `reviewed` decisions are appliable. |
| LLM provider | classification / deep read / review | Retries are counted. When the circuit opens (see rules below) the run aborts to `resumable` with all provisional work preserved. |

## Business Rules

### A. Durable ≠ final
- Every decision is written to `decisions` with `review_state = "provisional"` **within the tier node
  that produced it**, in its own short transaction, not at the end of the graph.
- `review_state` takes exactly one of `provisional` | `reviewed` | `review_failed`.
- The second-pass reviewer + never-miss floor upgrade rows to `review_state = "reviewed"` (writing any
  flipped `proposed_action`, `decided_by="reviewer"` and appended reasoning at the same time).
- If the reviewer pass cannot complete for a batch, those rows become `review_state="review_failed"`
  and are treated exactly like un-reviewed rows: never applied.
- **Only `review_state="reviewed"` decisions are eligible for apply/approve.** `apply_decision()` and
  every approve path raise `NotReviewedError` for anything else, **before** calling the mutator —
  defense in depth, mirroring the existing `NotArchivableError` keep-proposed guard in
  `src/tools/actions.py`. This is checked independently of `status`, so a row cannot be forced through
  by flipping `status` to `approved`.
- `force=True` does **not** bypass the review guard. `force` only ever bypassed the
  keep-proposed guard; an un-reviewed archive is never mutable by any code path.
- Read APIs (`/api/triage/items`, `/api/triage/clusters`) return `review_state` on every row so no
  surface can render a provisional decision as final.

### B. Resume
- Relaunching triage for a connection whose most recent run is `resumable` **reuses that run's
  `run_id`** (`_ensure_run` in `src/graph/runner.py` already accepts an existing id and sets it back
  to `running`). No new run row is created, so counts and cost stay on one row.
- On resume the graph loads the set of `item_id`s that already have a decision for that `run_id` and
  **excludes them from every tier queue** after `fetch_items`. Threads already decided are never
  re-sent to the LLM.
- Idempotency is enforced by the existing `UniqueConstraint("run_id", "item_id")` on `decisions`; a
  double-decide attempt is skipped, never inserted twice, never double-counted in `items_decided` or
  `cost_usd`. (Precedent test: `tests/unit/graph/test_graph.py::test_a_resumed_run_never_decides_a_thread_twice`.)
- `llm_calls` rows are appended per checkpoint, so cost accrues incrementally and a resume adds to —
  never resets — `triage_runs.tokens_in/tokens_out/cost_usd`.
- On resume, threads persisted as `provisional` in the interrupted run are carried into the reviewer
  pass along with the newly decided ones, so the reviewer sees the whole run's archive proposals
  exactly once.
- A resumed run finalises and auto-applies exactly as a fresh run does — after the reviewer has
  upgraded every row to `reviewed`.

### C. Interrupted runs are `resumable`, not `failed`
- `triage_runs.status` gains the value `resumable`.
- `_reconcile_orphaned_runs()` (`src/api/__init__.py`) at startup marks an orphaned `running` run
  `resumable` **when it has ≥ 1 persisted decision**, with the message
  *"Interrupted at N of M threads — nothing was left half-applied. Resume to continue."* A run with
  zero decisions is still marked `failed` (there is nothing to resume).
- `resumable` is not terminal: `POST /api/runs/{run_id}/resume` returns it to `running`.
- `cancelled` remains terminal and is never converted to `resumable`.

### D. Live transparency
- Every decided thread emits exactly one `thread_classified` event at the moment its tier resolves it,
  carrying `review_state` and the deciding tier (`rule` | `sender_history` | `llm` | `llm_deep` |
  `reviewer` | `error`). Event shape and rendering are owned by
  [triage-transparency](triage-transparency.md); this capability guarantees **coverage** — one event
  per decided thread, no tier omitted.
- A thread whose decision the reviewer later flips emits a **second** event with
  `decided_by="reviewer"` and `review_state="reviewed"`, which replaces the earlier row in the feed
  (keyed by `item_id`), so the user watches provisional become final.
- Privacy is unchanged and non-negotiable: events carry headers/subject (≤ 60 chars) and the already
  redacted snippet only. No body ever leaves the machine or reaches the database
  (`tests/integration/test_no_body_persisted.py`).

### E. Flaky-provider resilience
- The LLM client maintains a per-run health counter: `calls`, `retries`, `consecutive_failures`.
- **Degraded** (surface, do not stop): when `retries / max(calls, 1) >= 1.0` **or**
  `retries >= 50`, emit a `provider_degraded` event once per 50 further retries with
  `{provider, model, retries, calls, consecutive_failures}`. The Activity drawer pins a banner:
  *"NVIDIA is failing — N retries. This run is degraded and may take much longer than usual."*
- **Circuit open** (stop fast): after **5 consecutive** fully-failed LLM calls (every retry exhausted),
  the circuit opens. The client raises `ProviderCircuitOpen` on the next call instead of attempting it.
- The circuit is tracked **per model**. On `ProviderCircuitOpen` for the run's current model, the run
  first tries to `advance_model(...)` (Rule F) and continues on the next model with a fresh circuit.
  Only when the chain is exhausted does the graph stop issuing LLM work, route to the error handler,
  and close the run as **`resumable`** — not `failed` — because Rule A guarantees all decided threads
  are already durable. A `run_resumable` event carrying the reason is emitted. Nothing is applied.
- The circuit resets when a run is (re)started, so a resume always gets a fresh attempt.
- The existing **120 s hard LLM timeout** and the `finish_reason == "length"` re-issue behaviour are
  unchanged. The provider and the **default model** are unchanged.

### F. Cross-model fallback (rotate on persistent failure of ANY kind)

A single retired, overloaded, saturated or schema-hostile model must not take a 2,000-thread run down.

**Rotation applies to connection errors and timeouts too.** NVIDIA routes each model to its own
backend pool, so a saturated or degraded pool for **one** model produces `APIConnectionError` and
timeouts for that model while the other models stay healthy. This is measured, not inferred: minutes
after run `fbeed060` was drowning in retries against `nvidia/nemotron-3-nano-30b-a3b`, a live probe
got sub-2 s answers from `nemotron-3-nano-30b-a3b`, `nemotron-3-super-120b-a12b` **and**
`nvidia-nemotron-nano-9b-v2` on the same key and host. Treating transport failures as
"endpoint-level, do not rotate" was wrong and is **removed**: there is no failure class that is
exempt from rotation.

**The chain** (fixed, in-family, ordered by measured health against the real endpoint):

| Order | Model | Measured | Role |
|-------|-------|----------|------|
| 1 | `nvidia/nemotron-3-nano-30b-a3b` | 0.56 s OK | primary — the unchanged default |
| 2 | `nvidia/nemotron-3-super-120b-a12b` | 0.65 s OK | first fallback |
| 3 | `nvidia/nvidia-nemotron-nano-9b-v2` | 2.03 s OK | last resort (reasoning model; `content` may be null with the text in `reasoning_content` — the provider already handles this) |

- Out-of-family models are **excluded deliberately**: `meta/llama-3.3-70b-instruct` and
  `openai/gpt-oss-120b` both **timed out at 45 s** on the same key/endpoint — strictly worse than the
  primary, so adding them would make failure slower, not rarer.
- The chain is exposed by slice 3 as `llm.health.model_chain(preferred: str | None) -> list[str]`:
  when the user has set `settings.llm_model` it is placed **first** and the standard chain follows
  (de-duplicated); otherwise the standard chain is returned as-is.

**Rotation policy — this is the load-bearing rule.**

- **Try the current model up to its EXISTING retry budget.** The retry counts, backoff, the 120 s
  hard timeout and the `finish_reason == "length"` retry-with-double-budget behaviour are all
  unchanged.
- **If it still fails, advance to the next model in the chain and CONTINUE THE RUN on that model** —
  the batch is not failed. This applies to **every** failure class: `APIStatusError` of any status
  (400/404/422/429/500/503), `APIConnectionError`, `APITimeoutError`, DNS/TLS/connect failures, and
  repeated schema/parse failures. There is no exempt class and no `is_model_specific()` predicate —
  **that contract is removed**.
- **The advance is per-RUN and it STICKS.** `llm.health` holds a per-`run_id` **current model** (an
  index into the chain). Advancing moves that index for the rest of the run, so subsequent batches
  start on the new model and never re-try a known-bad model from scratch. It is **not** reset per
  batch, per node or per tier. Contract:
  - `llm.health.current_model(run_id: str) -> str` — the model calls for this run must use now.
  - `llm.health.advance_model(run_id: str, *, reason: str) -> str | None` — moves to the next chain
    entry and returns it; returns `None` when the chain is exhausted. Idempotent under concurrency
    (a lock; two batches failing on the same model advance the run **one** step, not two).
  - `llm.health.reset(run_id)` puts the run back at chain position 0 — so a resume, and only a
    resume, gets a fresh attempt at the primary.
- **There is no mid-run re-probing of the primary.** Once a run has advanced past a model it does not
  return to it within that run. Simple and predictable beats clever; re-probing is explicitly
  out of scope.
- On every advance, emit `llm.model_fallback` (structured log **and** SSE) naming `from_model`,
  `to_model` and `reason` (the concrete exception class + status/message, e.g.
  `"APIConnectionError after 3 retries"`).
- `_model_candidates(state)` in `src/graph/nodes.py` returns
  `llm.health.model_chain((state.get("settings") or {}).get("llm_model"))` **starting at the run's
  current position** — i.e. the run's current model is its first element. The existing per-call-site
  loops in `llm_classify_batch` and `deep_read_escalation` remain the mechanism; on exhausting the
  current model's retries they call `advance_model(...)`. **No parallel retry/rotation mechanism is
  introduced.**
- **Cost attribution:** an `llm_calls` row records the model that **actually served** the call
  (`LLMResult.model`, i.e. the response's model id, falling back to the requested id only when the
  response omits it) — never the originally requested one. Otherwise spend from a fallback model is
  silently attributed to the primary and the cost panel lies.

### G. Global rate limiting (process-wide)

The NVIDIA account limit is **490 requests/minute** (authoritative). Per-batch limiting cannot enforce
this — the graph's `MAX_CONCURRENCY` runs several batches at once, and retries are invisible to any
per-batch counter. Evidence: 2,774 retries in ~23 minutes is ~120 req/min of *pure retry churn* on top
of normal batch traffic, and concurrent batches burst far above the average.

- A **process-wide token bucket** (`src/llm/throttle.py`, owned by slice 3) gates **every** outbound
  LLM HTTP request before it is issued — first attempts, **retries**, and every tier's calls
  (classify, deep read, reviewer). Nothing bypasses it.
- Default target **350 req/min** — real headroom under the 490 ceiling. Configurable via
  `AGENT_LLM_MAX_RPM` (documented in `.env.example` and
  [architecture.md](../architecture.md#settings-srcconfigsettingspy-env-prefix-agent_)).
- The bucket refills continuously (leaky/token bucket, not a fixed window), so a burst is smoothed
  rather than allowed-then-starved. A waiting caller blocks on the bucket; waiting is bounded by the
  run's overall bound (Rule H) and never counts as a failure or a retry.
- The bucket is a single process-wide instance shared by all runs and all users; its state
  (`capacity`, `available`, `max_rpm`, `waiting`) is reported by `GET /api/provider-health`.

### H. Never get stuck (hard requirement)

A run must **always** either make forward progress or terminate with a clear reason and its partial
work preserved. Rule A's incremental persistence makes termination safe at any moment. No
configuration of failures — chain exhausted, throttle saturated, provider dead — may leave a run
grinding indefinitely.

- **Per-run wall-clock ceiling:** `AGENT_RUN_MAX_SECONDS` (default `3600`). When exceeded, the run
  ends `resumable` immediately.
- **Consecutive-failure bound after chain exhaustion:** once `advance_model` has returned `None`
  (chain exhausted), **3** further consecutive fully-failed batches end the run `resumable`. The
  circuit breaker (Rule E, 5 consecutive failed calls) still applies within a model.
- On any of these, the run is closed as `resumable` with a human-readable `triage_runs.error_message`
  naming the bound that was hit and the last failure — e.g. *"All 3 models failed
  (last: APIConnectionError). Stopped after 1,204 of 2,176 threads; nothing was left half-applied.
  Resume to continue."* — a `run_resumable` SSE event is emitted, and **every already-decided thread
  stays in `decisions`**. Nothing is applied.

## Success Criteria
- [ ] While a run is in flight, `SELECT count(*) FROM decisions WHERE run_id = :id` is strictly
      increasing and non-zero long before the run finishes.
- [ ] Killing the process mid-run and restarting the server marks the run `resumable` (not `failed`)
      with `items_decided` equal to the number of persisted decisions.
- [ ] `POST /api/runs/{run_id}/resume` over the 220-thread fixture, interrupted at 40 % decided,
      completes the run with exactly 220 decision rows, zero duplicate `(run_id, item_id)` pairs, and
      **fewer LLM calls than a run from zero** (proving the already-decided threads were skipped, not
      re-classified).
- [ ] `apply_decision()` raises `NotReviewedError` for a decision with
      `review_state in ("provisional", "review_failed")` — with `status="approved"` **and** with
      `force=True` — and the mutator is never called.
- [ ] After the reviewer pass, every decision row for the run has `review_state = "reviewed"`, and any
      thread the reviewer flipped has `proposed_action="keep"` and `decided_by="reviewer"`.
- [ ] No `archive` decision is applied to Gmail while its row is `provisional` — asserted by a test
      that runs auto-apply against a run whose reviewer pass was forced to fail: zero mutations.
- [ ] Every one of the 220 fixture threads produces exactly one `thread_classified` event, and every
      provisional event carries `review_state="provisional"`.
- [ ] Forcing 5 consecutive LLM failures opens the circuit **for that model** and the run advances to
      the next chain model; with every model failing, the run ends `resumable` within seconds (not
      after exhausting all batches), a `provider_degraded` event was emitted, and every thread decided
      before the failure is still in `decisions`.
- [ ] `llm.health.model_chain(None) == ["nvidia/nemotron-3-nano-30b-a3b",
      "nvidia/nemotron-3-super-120b-a12b", "nvidia/nvidia-nemotron-nano-9b-v2"]`, and
      `model_chain("x/y")[0] == "x/y"` with the standard chain following, de-duplicated. No
      out-of-family model appears in either result.
- [ ] `_model_candidates(state)` returns `llm.health.model_chain(settings.llm_model)` sliced from the
      run's current position — its first element equals `llm.health.current_model(run_id)`.
- [ ] A stub provider that raises `APIStatusError(status_code=404)` for model 1 and succeeds on
      model 2 completes the batch on model 2 and emits exactly one `model_fallback` event with
      `from_model`/`to_model`/`reason`.
- [ ] A stub provider that raises `APIConnectionError` for model 1 on every call and succeeds on
      model 2 **rotates**: the run completes on model 2, exactly one `model_fallback` event is
      emitted with a reason naming `APIConnectionError`, and model 1 is never requested again for the
      rest of the run (proving the advance is per-run and sticks, not per-batch).
- [ ] `advance_model` called concurrently from two batches failing on the same model advances the run
      exactly **one** chain position and emits exactly **one** `model_fallback` event.
- [ ] With all three models stubbed to fail every call, the run terminates within a bounded time as
      `resumable` (never hangs), `error_message` names the exhausted chain and the last failure, and
      every thread decided before the failure is still in `decisions`. Asserted with a test timeout
      strictly less than `AGENT_RUN_MAX_SECONDS`.
- [ ] With `AGENT_LLM_MAX_RPM=60` and 200 queued calls issued across concurrent batches (retries
      included), the measured outbound request rate over any 60 s window never exceeds 60, and the
      calls all eventually complete — the throttle delays, never drops or fails, a request.
- [ ] After a fallback, the `llm_calls` row for the served batch has
      `model == "nvidia/nemotron-3-super-120b-a12b"` — not the requested primary — so `cost_usd`
      groups by the model that actually did the work.
- [ ] No email body text appears in any table or any SSE event
      (`tests/integration/test_no_body_persisted.py` still passes).

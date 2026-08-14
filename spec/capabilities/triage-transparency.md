# Capability: Triage Transparency

## What It Does

Emits fine-grained SSE events for every per-thread classification decision and every successful Gmail archive mutation, so the Activity drawer shows the full decision trail of a triage run in real time.

## Inputs

| Input | Type | Source | Required |
|-------|------|---------|----------|
| `run_id` | UUID string | current triage run context | Yes |
| `item_id` | string | `Item.id` | Yes |
| `subject` | string | `Item.subject` (first 60 chars) | Yes |
| `from_email` | string | `Item.from_email` | Yes |
| `category` | string | `Category.name` | Yes |
| `action` | string | decided action (`archive`, `keep`, `needs_your_call`) | Yes |
| `decided_by` | string | tier that resolved the thread (`rule`, `sender_history`, `llm`, `llm_deep`, `reviewer`, `error`) | Yes |
| `confidence` | float | decision confidence score 0–1 | Yes |
| `reasoning` | string | decision reasoning, truncated to 140 chars | Yes |
| `review_state` | string | `provisional` \| `reviewed` \| `review_failed` | Yes |
| `label_name` | string | `Category.channel_label_name` (archive events only) | Conditional |

## Outputs

| Output | Type | Destination |
|--------|------|-------------|
| `thread_classified` SSE event | JSON object | SSE event bus → connected clients |
| `thread_archived` SSE event | JSON object | SSE event bus → connected clients |
| Updated `SseContext` shared state | React context value | `ActivityDrawer` and any consumer of `SseContext` |

### `thread_classified` event shape

```json
{
  "type": "thread_classified",
  "run_id": "<uuid>",
  "item_id": "<string>",
  "subject": "<first 60 chars of subject>",
  "from_email": "<sender address>",
  "category": "<category name>",
  "action": "<archive|keep|needs_your_call>",
  "decided_by": "<rule|sender_history|llm|llm_deep|reviewer|error>",
  "confidence": 0.92,
  "reasoning": "<first 140 chars of reasoning>",
  "review_state": "<provisional|reviewed|review_failed>"
}
```

### `provider_degraded` event shape

```json
{
  "type": "provider_degraded",
  "run_id": "<uuid>",
  "provider": "nvidia",
  "model": "<model id>",
  "calls": 412,
  "retries": 2774,
  "consecutive_failures": 2
}
```

### `run_resumable` event shape

```json
{
  "type": "run_resumable",
  "run_id": "<uuid>",
  "items_total": 2176,
  "items_decided": 2003,
  "reason": "<provider_circuit_open|interrupted>"
}
```

### `model_fallback` event shape

```json
{
  "type": "model_fallback",
  "run_id": "<uuid>",
  "from_model": "nvidia/nemotron-3-nano-30b-a3b",
  "to_model": "nvidia/nemotron-3-super-120b-a12b",
  "reason": "model unavailable: 404"
}
```

Emitted on **every** chain advance, whatever the failure class — including transport failures, where
`reason` reads e.g. `"APIConnectionError after 3 retries"` (see
[durable-resumable-runs Rule F](durable-resumable-runs.md#f-cross-model-fallback-rotate-on-persistent-failure-of-any-kind)).
Because the advance is per-run and sticks, at most one event is emitted per chain step per run.

### `thread_archived` event shape

```json
{
  "type": "thread_archived",
  "run_id": "<uuid>",
  "item_id": "<string>",
  "subject": "<first 60 chars of subject>",
  "category": "<category name>",
  "label_name": "<gmail label string>"
}
```

## External Calls

| System | Operation | On Failure |
|--------|-----------|------------|
| In-memory SSE event bus | `bus.publish(user_id, event_dict)` | Wrapped in `try/except`; exception is logged at WARNING level and the triage run continues — a bus error never fails a run or a Gmail mutation |

## Business Rules

- `thread_classified` is emitted once per thread, immediately after the tier that resolves it writes its decision — before `apply_decision` is called.
- `thread_archived` is emitted inside `apply_decision()`, after a successful `archive_and_label` Gmail API call returns without error. It is NOT emitted for `keep` or `needs_your_call` actions.
- Both emits are wrapped in `try/except Exception` so any event-bus error is isolated from the triage and mutation code paths.
- `subject` is always truncated to 60 characters (`item.subject[:60]`) before emission — no full subject leaves the in-process bus if longer.
- `run_id` for `thread_archived` is sourced from `decision.run_id`, not re-derived from context.
- Frontend: a single `SseContext` (new `frontend/src/lib/SseContext.tsx`) owns the one shared `EventSource` connection and exposes the event list to all consumers. `page.tsx` removes its local `EventSource` and reads from `SseContext` instead. `ActivityDrawer` calls `SseContext.addEvent` rather than maintaining its own connection.
- **Coverage guarantee:** exactly one `thread_classified` event is emitted for **every** decided
  thread, from **every** tier — including tier 4 deep reads, degraded (`decided_by="error"`) results,
  and reviewer flips. A decided thread with no event is a defect. Coverage is owned by
  [durable-resumable-runs](durable-resumable-runs.md) rule D.
- `ActivityDrawer` renders `thread_classified` events as:
  `"[decided_by] subject → category (action, confidence%) — reasoning"`, keyed by `item_id`: a later
  event for the same `item_id` (a reviewer flip) **replaces** the earlier row rather than appending.
- **Provisional labelling is mandatory.** An event with `review_state="provisional"` renders with a
  visible amber `NOT YET REVIEWED` chip and muted styling; `review_failed` renders a red
  `REVIEW FAILED — kept` chip. Only `reviewed` renders as a final decision. A provisional
  classification must never read as final.
- `provider_degraded` renders as a **pinned banner at the top of the drawer** (not a scrolling row),
  auto-opening the drawer: *"NVIDIA is failing — {retries} retries. This run is degraded and may take
  much longer than usual."* It clears on the next `run_completed` or `run_resumable`.
- `run_resumable` renders a row *"Run interrupted at {items_decided} of {items_total} — resumable"*.
- `model_fallback` renders an amber system row *"Switched model: {from_model} → {to_model} —
  {reason}"* ([ui.md](../ui.md) screen 14), so a mid-run model change is visible, never silent.
- `ActivityDrawer` renders `thread_archived` events as: `"Archived: subject → label_name"`.
- `frontend/src/lib/types.ts` extends the `SseEventType` union with `"thread_classified"` and `"thread_archived"` and exports the corresponding typed interfaces.

## Success Criteria

- [ ] Running a triage pass against ≥ 10 threads causes exactly one `thread_classified` SSE event per thread to appear in the Activity drawer, each carrying correct `subject`, `category`, `action`, `decided_by`, and `confidence`.
- [ ] Every thread for which `apply_decision` performs a successful Gmail archive emits exactly one `thread_archived` SSE event visible in the Activity drawer with `subject`, `category`, and `label_name`.
- [ ] Injecting a bus error (monkeypatching `bus.publish` to raise) does not cause any `pytest` test to record a failed triage run or a failed Gmail mutation — the run completes and the error appears only as a WARNING log line.
- [ ] `pnpm build` in `frontend/` exits 0 with no TypeScript errors after adding the two new event types to `SseEventType`.
- [ ] `page.tsx` contains no local `new EventSource(...)` instantiation — it reads from `SseContext`.
- [ ] `uv run pytest tests/unit/ -q` passes with a new unit test asserting both event shapes match their documented JSON schemas (field names, types, required fields).
- [ ] Over the 220-thread fixture, the count of distinct `item_id`s in `thread_classified` events
      equals the count of rows in `decisions` for that run — no tier is silently unreported.
- [ ] An event with `review_state="provisional"` renders the `NOT YET REVIEWED` chip; after the
      reviewer runs, the same `item_id`'s row is replaced by a `reviewed` row without the chip.

# Capability: Triage Transparency

## What It Does

Emits fine-grained SSE events for every per-thread classification decision and every successful Gmail
archive mutation, and **surfaces them, unprompted, on the main page while a run is active**, so the
user watches the agent think in real time and can always tell working-but-slow from stuck.

> **Phase 6 shipped the plumbing; Phase 7 ships the visibility.** Everything below the
> [Phase 7 section](#phase-7--the-live-visible-feed-and-the-no-silent-beat-guarantee) is the Phase 7
> extension. Phase 6's contribution — the events, the bus, the ring buffer, `ThreadFeedRow`,
> `SseContext` — is unchanged and reused as-is.

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
- **Main-page visibility is the bar.** Every rule below is satisfied only when the state is visible
  on the **main dashboard page without the user opening any drawer or taking any action**. The
  Activity drawer is the full-history/archive surface and may *additionally* render anything here;
  rendering only into the drawer satisfies nothing.
- The live feed row renderer (`ThreadFeedRow.tsx`) is shared by both surfaces; `thread_classified`
  events render as:
  `"[decided_by] subject → category (action, confidence%) — reasoning"`, keyed by `item_id`: a later
  event for the same `item_id` (a reviewer flip) **replaces** the earlier row rather than appending.
- **Provisional labelling is mandatory.** An event with `review_state="provisional"` renders with a
  visible amber `NOT YET REVIEWED` chip and muted styling; `review_failed` renders a red
  `REVIEW FAILED — kept` chip. Only `reviewed` renders as a final decision. A provisional
  classification must never read as final.
- `provider_degraded` renders as a **banner pinned to the top of the main dashboard page** (not a
  scrolling row), visible without the user opening any drawer or taking any action: *"NVIDIA is
  failing — {retries} retries. This run is degraded and may take much longer than usual."*
  Auto-opening a drawer is **not** an acceptable substitute for on-page visibility. It clears on the
  next `run_completed` or `run_resumable`.
- `run_resumable` renders a row *"Run interrupted at {items_decided} of {items_total} — resumable"*.
- `model_fallback` renders an amber system row *"Switched model: {from_model} → {to_model} —
  {reason}"* ([ui.md](../ui.md) screen 14), so a mid-run model change is visible, never silent.
- `thread_archived` events render as `"Archived: subject → label_name"` on the main-page feed (and in
  the drawer's scrollback).
- `frontend/src/lib/types.ts` extends the `SseEventType` union with `"thread_classified"` and `"thread_archived"` and exports the corresponding typed interfaces.

---

## Phase 7 — the live visible feed, and the no-silent-beat guarantee

### The defect this closes (measured, not inferred)

The events reach the browser. The user never sees them.

1. **The feed is hidden by default.** `frontend/src/components/ActivityDrawer.tsx:212` is
   `const [open, setOpen] = useState(false)`. The drawer is closed on load and opens only on a click.
2. **The main page never renders the feed at all.** `frontend/src/app/page.tsx` consumes `useSse()`
   only for `fetchedSoFar`; it renders no feed rows.
3. So Phase 6's `thread_classified` events are genuinely emitted, genuinely delivered, and genuinely
   rendered by `ThreadFeedRow.tsx` — **into a panel nobody opens**. Every prior "transparency is
   shipped" claim was, from the user's seat, false. *Delivered is not shipped; visible is shipped.*
4. **There are real dead-air windows.** A tier-3 batch is **one** LLM call covering ~29 threads.
   Between dispatch and return, nothing is emitted at all. On a degraded provider that was minutes of
   silence — which is exactly how a working run became indistinguishable from a hung one.

### Rules — H. The feed is visible without a click

- **H1.** While a run is active, the live classification feed renders **inline on the main page**
  ([ui.md screen 18](../ui.md#18-live-run-feed-on-the-main-page-phase-7)) — not behind a drawer, not
  behind a toggle, not requiring any user action. It appears automatically when a run starts and is
  one of the most prominent elements on screen for the duration of the run. Watching the agent
  classify **is** the product.
- **H2.** It **reuses** `frontend/src/components/ThreadFeedRow.tsx` and the `useSse()` /
  `buildFeed()` surface of `frontend/src/lib/SseContext.tsx` verbatim. No second `EventSource`, no
  second feed model, no duplicated row renderer.
- **H3.** The Activity drawer **remains**, unchanged in behaviour, as the **full-history / archive**
  surface only (it keeps its own scrollback and a copy of the degraded banner). It is never the live
  surface, and **nothing user-critical may live only inside it** — anything visible only in the
  drawer is, from the user's seat, not visible. The main page carries every state the user must
  notice; the drawer merely holds more history.
- **H4.** When no run is active the inline feed collapses to a single line naming the last run's
  outcome — it never renders an empty box, and it never renders a progress bar for work that is not
  running.

### Rules — I. Not one beat without a log

The bar is literal: **there is never a beat of an active run with nothing published.**

- **I1. Granularity comes from logging, not from hand-placed emits.**
  `src/observability/logging.py::activity_bus_processor` already forwards **every** structlog line to
  the per-user bus as a `log` event. Continuous coverage is therefore achieved by **logging at the
  right granularity inside the graph and the LLM client**, not by scattering `bus.emit(...)` calls.
  Hand-placed emits are precisely what drifted out of coverage before; the bridge cannot drift.
  Every log line below must carry `run_id` and `user_id` (bound via structlog contextvars) or the
  bridge drops it.
- **I2. The events that must exist during a run**, each as a structured log line at INFO:

  | Log event | Fields | Emitted by |
  |-----------|--------|------------|
  | `triage.page_fetched` | `page_n`, `fetched_so_far`, `latency_ms` | graph |
  | `triage.tier_started` / `triage.tier_finished` | `tier`, `candidates`, `decided`, `latency_ms` | graph |
  | `triage.batch_dispatched` | `tier`, `batch_n`, `batch_total`, `batch_size`, `model` | graph |
  | `triage.batch_returned` | `tier`, `batch_n`, `decided`, `latency_ms`, `model` | graph |
  | `llm.call_started` / `llm.call_finished` | `model`, `prompt_tokens`, `latency_ms` | LLM client |
  | `llm.retry` | `model`, `attempt`, `error`, `backoff_ms` | LLM client |
  | `llm.model_fallback` | `from_model`, `to_model`, `reason` | LLM client (alongside the existing `model_fallback` event) |
  | `triage.reviewer_started` / `triage.reviewer_finished` | `candidates`, `flips`, `latency_ms` | graph |
  | `triage.checkpoint` | `items_decided`, `items_total` | graph |
  | `triage.apply_progress` | `applied`, `total_to_apply`, `failed` | graph (Phase 7 apply pass) |

  Per-thread `thread_classified` events continue to land as each tier resolves its threads
  (coverage guarantee above, unchanged).
- **I3. In-flight progress for long single calls.** A single LLM call covering a whole batch must not
  be silent while it runs. A watchdog publishes an `activity_heartbeat` carrying **real state** —
  the current phase, the batch index, the batch size, the model, and elapsed seconds — e.g.
  *"classifying batch 12/75 — 29 threads, 18s elapsed, model nvidia/nemotron-3-nano-30b-a3b"*.
  It is **not** a spinner and **not** a content-free tick: a heartbeat that says nothing is the same
  as silence.
- **I4. Silence is impossible by construction.** The watchdog runs independently of the graph:
  whenever a run is active and **nothing** has been published for
  `HEARTBEAT_INTERVAL_SECONDS = 3.0`, it publishes an `activity_heartbeat` describing the last known
  phase and how long it has been in it. It arms itself from the bus bridge (any event carrying a
  `run_id` arms it) and disarms on `run_completed`, `run_resumable`, `run_failed` or `error` — so
  **no graph code has to remember to start or stop it**, which is the only way it cannot drift.
- **I5. The UI states staleness rather than showing nothing.** If the browser has received no event
  for `FEED_STALE_SECONDS = 8` while a run is active, the inline feed renders an explicit amber line
  — *"No activity for 12s — still waiting on {last_phase}."* — rather than sitting visually static.
  Eight seconds is two missed heartbeats plus margin, so this line means *the backend watchdog itself
  stopped*, which is a genuine signal, not noise.
- **I6. Cost of the guarantee.** Worst case the watchdog adds 20 events/minute — negligible against
  the 1000-event ring and against the hundreds of real log lines a run already produces. It is never
  emitted when the graph is already publishing faster than every 3s.
- **I7.** `activity_heartbeat` carries counts, ids, phase names, model ids and elapsed times **only**.
  No subject, no sender, no body — `tests/integration/test_no_body_persisted.py` still passes.

### Rules — J. Reload and late join replay immediately

- **J1.** `GET /api/events` already replays `bus.replay_buffer(user_id)` (1000 events) before
  streaming live (`src/api/events.py:34`). This is **kept and verified end-to-end**, not rebuilt.
- **J2.** A user who loads the dashboard **mid-run** sees the recent history immediately — the inline
  feed is populated on first paint from the replay, never empty-then-slowly-filling.
- **J3.** A reload during a run is indistinguishable from having watched from the start, apart from
  events older than the ring.

### `activity_heartbeat` event shape

```json
{
  "type": "activity_heartbeat",
  "run_id": "<uuid>",
  "phase": "tier3_classify",
  "detail": "batch 12/75 — 29 threads",
  "batch_n": 12,
  "batch_total": 75,
  "batch_size": 29,
  "model": "nvidia/nemotron-3-nano-30b-a3b",
  "elapsed_s": 18.4,
  "silent_for_s": 3.1
}
```

`phase` and `detail` are derived from the most recent log line the bridge saw for this run — the
heartbeat reports **observed** state, never a guess.

## Success Criteria

> **Retro-correction (applies to the Phase 6 criteria below too).** A criterion that a *closed panel*
> can satisfy is not a criterion — that is exactly how Phase 6 shipped "verified" transparency the
> user could not see. Every criterion phrased below as *visible on the main dashboard page without
> the user opening any drawer or taking any action* is judged on the main page, not the drawer.

- [ ] Running a triage pass against ≥ 10 threads causes exactly one `thread_classified` SSE event per thread to render as a row **visible on the main dashboard page without the user opening any drawer or taking any action**, each carrying correct `subject`, `category`, `action`, `decided_by`, and `confidence`.
- [ ] Every thread for which `apply_decision` performs a successful Gmail archive emits exactly one `thread_archived` SSE event rendered **visible on the main dashboard page without the user opening any drawer or taking any action**, with `subject`, `category`, and `label_name`.
- [ ] A `provider_degraded` event renders its red banner **pinned to the top of the main dashboard page, visible without the user opening any drawer or taking any action** — an auto-opened drawer does not satisfy this criterion.
- [ ] Injecting a bus error (monkeypatching `bus.publish` to raise) does not cause any `pytest` test to record a failed triage run or a failed Gmail mutation — the run completes and the error appears only as a WARNING log line.
- [ ] `pnpm build` in `frontend/` exits 0 with no TypeScript errors after adding the two new event types to `SseEventType`.
- [ ] `page.tsx` contains no local `new EventSource(...)` instantiation — it reads from `SseContext`.
- [ ] `uv run pytest tests/unit/ -q` passes with a new unit test asserting both event shapes match their documented JSON schemas (field names, types, required fields).
- [ ] Over the 220-thread fixture, the count of distinct `item_id`s in `thread_classified` events
      equals the count of rows in `decisions` for that run — no tier is silently unreported.
- [ ] An event with `review_state="provisional"` renders the `NOT YET REVIEWED` chip; after the
      reviewer runs, the same `item_id`'s row is replaced by a `reviewed` row without the chip.

### Phase 7 — visibility and continuity

- [ ] **REQUIRED GATE ASSERTION — visible with zero clicks.** `tests/e2e/phase7/live-feed.spec.ts`
      loads `/app/` exactly as a user does, opens **no drawer and clicks nothing beyond starting the
      run**, starts/observes a **real** run, and finds classification rows rendered **visible on the
      main dashboard page** within 2 seconds of first paint. This is a required gate assertion — not
      optional, not nice-to-have, and never `test.skip`. If the test cannot start or observe a real
      run it must **FAIL LOUDLY** (explicit failure with the reason), never skip or pass vacuously.
      Asserting only that events reached the browser is **not** acceptable evidence.
- [ ] **REQUIRED GATE ASSERTION — visibly increasing.** The same test polls the on-page feed row
      count three times, ≥ 3 s apart, and asserts it is **strictly increasing** across the polls —
      the feed moves, it does not merely exist — and that the feed does not go visually static for
      more than **8 s** (`FEED_STALE_SECONDS`) while the run is active without the amber staleness
      line appearing. A screenshot artifact of the mid-run main page is captured as evidence when
      the harness supports it. Also required, also never skipped.
- [ ] **No silent beat, backend.** A test that simulates a tier-3 batch taking 60 s (a stubbed slow
      LLM call, real bus) records every event published for the run and asserts the **maximum
      inter-event gap is < 5.0 s** (`HEARTBEAT_INTERVAL_SECONDS` 3.0 + scheduling tolerance), and
      that at least 15 `activity_heartbeat` events were published, each carrying a non-empty `phase`
      and a monotonically increasing `elapsed_s`.
- [ ] **The heartbeat carries real state.** During the simulated slow batch, the heartbeats report
      `phase="tier3_classify"` with the correct `batch_n`/`batch_total`/`batch_size`/`model` taken
      from the preceding `triage.batch_dispatched` log line — not a constant placeholder.
- [ ] **The watchdog self-arms and self-disarms.** No `run_id`-carrying event for 3 s with no active
      run publishes **zero** heartbeats; after a `run_completed` event the heartbeat stream stops
      within one interval. The graph contains **no** explicit start/stop call.
- [ ] **Granularity comes from logging.** Over the 220-thread fixture, every event class in Rule I2
      appears at least once on the bus as a `log` event, and the diff for the graph adds **no** new
      `bus.emit(` call sites for these — they arrive via `activity_bus_processor`.
- [ ] **Replay on late join.** With ≥ 200 events already in a run's ring buffer, a fresh
      `GET /api/events` connection delivers the replayed history **before** any live event, and a
      Playwright test that loads the page mid-run finds a populated feed on first paint (row count
      > 0 within 2 s), not an empty one.
- [ ] **Stale line, not a blank feed.** With the backend stopped mid-run, the inline feed renders the
      amber *"No activity for Ns — still waiting on {phase}"* line within 10 s; it never renders a
      static feed with no explanation.
- [ ] **The drawer still works.** The Activity drawer opens on click and shows the full history
      including rows scrolled out of the inline feed; `ThreadFeedRow.tsx` is reused unmodified in
      behaviour by both surfaces.
- [ ] `activity_heartbeat` payloads contain no `subject`, `from_email`, `body`, `snippet` or
      `content` key (asserted on the emitted dicts).

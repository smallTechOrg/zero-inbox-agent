# Capability: Autopilot & Digest

## What It Does

Automatically starts a triage run on first Gmail connect (applying immediately on completion), surfaces
a compact run-summary card showing what was done with a single "Undo this run" button, runs a daily
background refresh, streams live activity events, and exposes a catch-up digest that shows what matters
without opening Gmail. Also replaces the taxonomy settings stub with a real inline editor.

## Inputs

| Input | Type | Source | Required |
|-------|------|--------|----------|
| OAuth callback completion | event | `/auth/google/callback` | Yes |
| `run_id` | UUID string | URL path | Yes (summary / undo endpoints) |
| `connection_id` | UUID string | DB (scheduler) | Yes (scheduled run) |
| `settings.timezone` | string (IANA tz) | `UserSettings` | Yes (scheduler fire time) |
| `settings.confidence_floor` | float 0–1 | `UserSettings` | Yes (auto-kept threshold) |
| `settings.scheduler_run_at` | time string / `"now"` | `UserSettings` | Yes (daily trigger) |
| SSE subscriber (client) | HTTP connection | `GET /api/events` | No |
| Category rename / reorder payload | JSON | `PATCH /api/categories/{id}` | Yes (taxonomy editor) |

## Outputs

| Output | Type | Destination |
|--------|------|-------------|
| Auto-triggered run | background task | triage pipeline (identical to `POST /api/connections/{id}/triage limit=200 only_new=false`); applies on completion |
| Run summary card | JSON | `GET /api/runs/{run_id}/summary` — `{run_id, status, total_threads, applied_count, kept_count, auto_kept_count, categories, top_clusters, cost_usd, completed_at}` |
| Run undo result | JSON | `POST /api/runs/{run_id}/undo` — `{reversed, skipped, errors}` |
| Daily triage run | background task | triage pipeline (`only_new=true`); applies on completion |
| Catch-up digest | JSON | `GET /api/digest/latest` |
| SSE event stream | `text/event-stream` | `GET /api/events` |
| Updated category | JSON | `PATCH /api/categories/{id}` (existing endpoint, no new backend) |

## External Calls

| System | Operation | On Failure |
|--------|-----------|-----------|
| Gmail API | archive + label (via existing mutations layer); undo reverses via `threads.modify` with `original_label_ids` snapshot | log error per thread; return in `errors` array; partial undo reported |
| LLM provider | triage decisions (delegated to triage pipeline) | propagate existing error handling |
| APScheduler / threading.Timer | daily fire at `settings.scheduler_run_at` | log error, persist `next_run_at` retry in DB, alert via SSE `error` event |

## Business Rules

- The auto-trigger fires **once per connect event** as a `BackgroundTask` — never on token refresh or
  reconnect. Parameters: `limit=200, only_new=false`. The run applies on completion.
- The scheduler fires **once per day per connected user** at 06:00 local time per `settings.timezone`.
  Each scheduled run uses `only_new=true`. The run applies on completion; no separate approval step.
- `POST /api/runs/{run_id}/undo` reverses **all** `ActionLog` rows for the run in reverse
  chronological order. Each thread is restored to `original_label_ids` stored in `undo_token`.
  Idempotent: a second call on a fully-undone run returns `{reversed: 0, skipped: N, errors: []}`.
  Only `completed` runs can be undone; calling on a `running` or `cancelled` run returns 409.
- `GET /api/digest/latest` returns the digest for the **most recent completed run** (any trigger).
  If no completed run exists, returns `404`. The `auto_kept_low_confidence` section lists threads
  below the confidence floor that were kept automatically.
- SSE events are kept **in memory only** — last 50 per user, not persisted to DB. On server restart
  the feed is empty; clients reconnect gracefully.
- The taxonomy editor (D10 fix) calls only the existing `GET /api/categories` and
  `PATCH /api/categories/{id}` endpoints. No new backend routes. `sort_order` is persisted via PATCH.
  Drag-to-reorder is client-side only.

## Success Criteria

- [ ] Connecting Gmail via OAuth results in a triage `Run` row in `started` state within 5 seconds of
      the callback completing — without any manual button press.
- [ ] `GET /api/runs/{run_id}/summary` returns a JSON object with all fields: `run_id`, `status`,
      `total_threads`, `applied_count`, `kept_count`, `auto_kept_count`, `categories` (array),
      `top_clusters` (≤ 3 items), `cost_usd`, `completed_at`. The sum
      `applied_count + kept_count + auto_kept_count` equals `total_threads`.
- [ ] `POST /api/runs/{run_id}/undo` on a completed run with at least one applied archive action
      returns `{reversed ≥ 1, skipped, errors: []}` and each reversed `ActionLog` row has a
      non-null `undone_at`.
- [ ] A second call to `POST /api/runs/{run_id}/undo` on an already-undone run returns
      `{reversed: 0, skipped: N, errors: []}` without making any Gmail API call.
- [ ] The daily scheduler writes a `next_run_at` timestamp to the DB on startup and creates a new
      `Run` row at the scheduled time.
- [ ] `GET /api/digest/latest` returns a JSON object with all fields: `run_id`, `generated_at`,
      `time_sensitive_kept`, `vip_mail`, `auto_kept_low_confidence`, `auto_archived`.
- [ ] `GET /api/events` with `Accept: text/event-stream` returns a valid SSE stream; a synthetic
      `run_started` event published to the in-memory bus is received by the client within 1 second.
- [ ] The SSE stream delivers events in the order they were published.
- [ ] The frontend renders the Run Summary card as the primary landing view after a completed run,
      showing applied/kept/auto-kept counts, with the "Undo this run" button present and functional.
- [ ] Clicking "Undo this run" shows a confirmation dialog naming the thread count, and on confirm
      calls `POST /api/runs/{run_id}/undo`; on success a toast shows "{reversed} threads restored".
- [ ] After undo, the "Undo this run" button is replaced with "Run undone" (disabled).
- [ ] The Digest tab renders data from `GET /api/digest/latest` with `time_sensitive_kept`,
      `vip_mail`, `auto_kept_low_confidence`, and `auto_archived` sections.
- [ ] The app subscribes to `GET /api/events` on page load and renders each event with an icon and
      timestamp; run activity is **visible on the main dashboard page without the user opening any
      drawer or taking any action** (the Activity drawer holds the full history), and the
      `run_completed` row has an "Undo run" button.
- [ ] Settings → Taxonomy renders a real inline editor (not a stub): each category row supports
      rename, default-action dropdown, and drag-to-reorder; changes persist via `PATCH /api/categories/{id}`.

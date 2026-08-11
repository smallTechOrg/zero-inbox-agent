# Capability: Autopilot & Digest

## What It Does

Automatically starts a triage run on first Gmail connect, surfaces a compact run-summary card so the user can approve everything in one click, runs a daily background refresh, streams live activity events, and exposes a catch-up digest that shows what matters without opening Gmail. Also replaces the taxonomy settings stub with a real inline editor.

## Inputs

| Input | Type | Source | Required |
|-------|------|--------|----------|
| OAuth callback completion | event | `/auth/google/callback` | Yes |
| `run_id` | UUID string | URL path | Yes (summary / approve-and-apply) |
| `connection_id` | UUID string | DB (scheduler) | Yes (scheduled run) |
| `settings.timezone` | string (IANA tz) | `UserSettings` | Yes (scheduler fire time) |
| `settings.auto_act_threshold` | float 0–1 | `UserSettings` | Yes (auto-apply gate) |
| `settings.scheduler_run_at` | time string / `"now"` | `UserSettings` | Yes (daily trigger) |
| SSE subscriber (client) | HTTP connection | `GET /api/events` | No |
| Category rename / reorder payload | JSON | `PATCH /api/categories/{id}` | Yes (taxonomy editor) |

## Outputs

| Output | Type | Destination |
|--------|------|-------------|
| Auto-triggered run | background task | triage pipeline (identical to `POST /api/connections/{id}/triage limit=200 only_new=false`) |
| Run summary card | JSON | `GET /api/runs/{run_id}/summary` |
| Approve-and-apply result | JSON | `POST /api/runs/{run_id}/approve-and-apply` |
| Daily triage run | background task | triage pipeline (`only_new=true`) + auto-apply |
| Catch-up digest | JSON | `GET /api/digest/latest` |
| SSE event stream | `text/event-stream` | `GET /api/events` |
| Updated category | JSON | `PATCH /api/categories/{id}` (existing endpoint, no new backend) |

## External Calls

| System | Operation | On Failure |
|--------|-----------|-----------|
| Gmail API | archive + label (via existing mutations layer) | roll back applied actions; return `undo_tokens` for partial success |
| LLM provider | triage decisions (delegated to triage pipeline) | propagate existing error handling |
| APScheduler / threading.Timer | daily fire at `settings.scheduler_run_at` | log error, persist `next_run_at` retry in DB, alert via SSE `error` event |

## Business Rules

- The auto-trigger fires **once per connect event** as a `BackgroundTask` — never on token refresh or reconnect. Parameters: `limit=200, only_new=false`.
- `POST /api/runs/{run_id}/approve-and-apply` bulk-approves **all non-`needs_your_call`** decisions then applies only `archive` and `digest` actions. `keep` decisions are skipped (counted in `skipped_keep`). `needs_your_call` decisions are silently skipped (counted in `skipped_needs_your_call`). No mutation is performed for skipped rows.
- The scheduler fires **once per day per connected user** at 06:00 local time per `settings.timezone`. Each scheduled run uses `only_new=true`. After the run, any decision with `confidence ≥ settings.auto_act_threshold` is auto-applied without further user interaction.
- `GET /api/digest/latest` returns the digest for the **most recent completed run** (any trigger). If no completed run exists, returns `404`.
- SSE events are kept **in memory only** — last 50 per user, not persisted to DB. On server restart the feed is empty; clients reconnect gracefully.
- The taxonomy editor (D10 fix) calls only the existing `GET /api/categories` and `PATCH /api/categories/{id}` endpoints. No new backend routes. `sort_order` is a client-side integer persisted via PATCH. Drag-to-reorder is client-side only.

## Success Criteria

- [ ] Connecting Gmail via OAuth results in a triage `Run` row in `started` state within 5 seconds of the callback completing — without any manual button press.
- [ ] `GET /api/runs/{run_id}/summary` returns a JSON object with all fields: `run_id`, `status`, `total_threads`, `categories` (array), `top_clusters` (≤ 3 items), `needs_your_call_count`, `cost_usd`, `completed_at`.
- [ ] `POST /api/runs/{run_id}/approve-and-apply` returns `{applied, skipped_keep, skipped_needs_your_call, undo_tokens}` and the sum `applied + skipped_keep + skipped_needs_your_call` equals the count of non-`needs_your_call` decisions in the run.
- [ ] Calling `POST /api/runs/{run_id}/approve-and-apply` on a completed run with at least one approvable archive decision results in `applied ≥ 1` and a corresponding `ActionLog` row with a non-null undo token.
- [ ] The daily scheduler writes a `next_run_at` timestamp to the DB on startup and creates a new `Run` row at the scheduled time.
- [ ] `GET /api/digest/latest` returns a JSON object with all fields: `run_id`, `generated_at`, `time_sensitive_kept`, `vip_mail`, `needs_your_call`, `auto_archived`.
- [ ] `GET /api/events` with `Accept: text/event-stream` returns a valid SSE stream; a synthetic `run_started` event published to the in-memory bus is received by the client within 1 second.
- [ ] The SSE stream delivers events in the order they were published.
- [ ] The frontend renders the Run Summary card as the primary landing view after a completed run, with both CTAs present and functional.
- [ ] The Digest tab renders data from `GET /api/digest/latest` with `time_sensitive_kept`, `vip_mail`, `needs_your_call`, and `auto_archived` sections.
- [ ] The Activity drawer subscribes to `GET /api/events` on page load and renders each event with an icon and timestamp.
- [ ] Settings → Taxonomy renders a real inline editor (not a stub): each category row supports rename, default-action dropdown, and drag-to-reorder; changes persist via `PATCH /api/categories/{id}`.

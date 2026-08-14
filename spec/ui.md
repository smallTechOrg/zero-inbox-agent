# UI

Web dashboard, single origin: **http://localhost:8001/app/** (static export mounted by the backend).

Design intent: dense, calm, informative. The user's core motion is *see what happened → undo if wrong*.
Nothing about the interface should make the autonomous triage feel opaque or risky — the history view
and the single "Undo this run" button are the primary trust surfaces.

## Global chrome

- **Left rail:** History · Rules · Chat · Digest · Backlog · Cost · Settings.
- **Top bar:** connected Gmail address, run status pill, "Run triage" button.
- **Dry-run banner (debug only):** a full-width amber bar shown only when `settings.dry_run=true` —
  **"DRY RUN — Gmail mutations are suppressed."** Hidden by default (dry_run defaults to false in
  production). Toggled from Settings.
- **Stub convention:** every not-yet-built surface renders its real layout, greyed at ~50% opacity,
  with a `COMING SOON · Phase N` chip and a tooltip. Stub controls are `disabled` and never fire a
  request. A stub must never be mistakable for a broken feature.

## Screens

### 1. Connect (empty state)
Single card: "Connect Gmail" → the real Google consent flow. Lists the scopes requested in plain
English and states the two guarantees: *we never delete anything* and *you can always undo*. After
the callback it shows the connected address and a **Run triage (200 threads)** button.

### 2. Triage History *(the Phase 1 primary screen)*
- **Progress bar** while a run is active: `decided / total`, live counts by tier, cancel button.
  Results stream in as they are decided — the list is usable before the run finishes.
- **Read-only history view.** After the run completes, shows applied decisions grouped by cluster.
  Each cluster row: label ("142 threads from Substack newsletters"), thread count, applied action
  (`archived` / `kept` / `auto-kept — low confidence`), confidence range, three sample subjects.
- **Expand a cluster** → its threads: subject, sender, date, redacted snippet, category chip,
  confidence bar, applied action, and a **tier badge** — `RULE` / `SENDER HISTORY` / `LLM` /
  `DEEP READ` / `REVIEWER` — colour-coded, with the rule name shown when a rule fired.
- **Expand a thread** → the full reasoning text, verbatim.
- **Auto-kept section** (informational, not actionable): threads below the confidence floor that were
  kept automatically. Styled neutrally — these are expected outcomes, not errors.
- No approve/reject buttons or keyboard sweep actions. The triage decision is already applied.
- Per-decision **Undo** button — Phase 1 stub, real in Phase 2 (calls `POST /api/actions/{id}/undo`).

### 3. Rules *(stub in Phase 1, real in Phase 3)*
Proposed rules ranked by coverage ("one filter, 312 threads"), each with **Preview (dry run)** showing
exactly which threads it would archive, and Activate / Promote to automatic / Dismiss. Promoting to
automatic requires an explicit confirm modal naming the consequence.

### 4. Chat *(stub in Phase 1, real in Phase 3)*
Plain message thread. The user types "stop showing me GitHub notifications unless I'm mentioned"; the
assistant replies with a drafted rule card + a dry-run preview + Apply. **Prior turns are visible and
in context** — a follow-up amends the previous rule rather than starting over.

### 5. Digest *(stub in Phase 1, real in Phase 3)*
Date picker + the day's summary of what was hidden, grouped by category, each row one click from
un-hiding. This is the "nothing vanishes silently" guarantee made visible.

### 6. Backlog cleanup *(stub in Phase 1, real in Phase 3)*
Date-range picker and chunk size. A live progress bar with per-chunk counts, results streaming in,
**Cancel** and **Resume** — resuming never redoes completed chunks.

### 7. Cost *(stub in Phase 1, real in Phase 3)*
Spend for the current run, month-to-date total, the **rules-vs-LLM handling ratio** as a bar, cost per
model, and the **model dropdown** listing NVIDIA free models (persisted per user).

### 8. Settings *(partly stub in Phase 1)*
Auto-act threshold slider, confidence floor slider, dry-run toggle (off by default; shows amber banner
when enabled), timezone, digest hour, taxonomy editor, **VIP / never-hide list** editor, and the
plain-English **priorities profile** textarea.

---

## Phase 3 screens

### 9. Run Summary card *(primary post-connect / post-run landing — Phase 3)*

Primary landing view shown after a run completes (auto-triggered on connect, or user-triggered).
Loaded from `GET /api/runs/{run_id}/summary` (polled every 2 s while status is `running`; stops on
`completed` or `failed`).

Contents:
- Header: run status pill, `total_threads` count, `cost_usd`, `completed_at` timestamp.
- What was done: `{applied_count} threads archived · {kept_count} kept · {auto_kept_count} auto-kept
  (low confidence)`.
- Category breakdown table: name · count · applied action.
- Top-3 clusters by size (label · count · applied action).
- Single CTA: **"Undo this run"** — calls `POST /api/runs/{run_id}/undo` after a confirmation dialog:
  *"This will restore {applied_count} threads to their pre-triage state in Gmail. Continue?"*.
  On success, a toast shows "{reversed} threads restored". On partial failure, shows
  "{reversed} restored, {errors} failed — see action log".
- The "Undo this run" button is disabled if the run has already been undone (shows "Run undone" label).
- If the run is still in progress, a skeleton card with a live progress bar is shown instead; the CTA
  is disabled until `status === "completed"`.

### 10. Catch-up Digest tab *(Phase 3)*

Accessible from the left rail as **Digest** (replaces the Phase 1–2 stub). Loaded from
`GET /api/digest/latest`.

Sections (collapsible):
1. **Time-sensitive kept** — subject, from, reason chip.
2. **VIP mail** — same shape.
3. **Auto-kept (low confidence)** — threads below the confidence floor that were kept automatically.
   Each row links to the thread in the history view.
4. **Auto-archived** — count + breakdown by category; each category is expandable to list subjects.

Empty state: "No digest yet — run triage first." Error state: standard envelope error + Retry.

### 11. Activity drawer *(Phase 3)*

A slide-in panel anchored to the right edge (or a persistent bottom bar on narrow viewports). Toggled
by a bell icon in the top bar; auto-opens and highlights when a background run is active.

Subscribes to `GET /api/events` (SSE) on page load. Each received event is prepended to a scrollable
feed:
- `run_started` → green "Triage started" row with trigger badge (`user` / `scheduler` / `connect`).
- `run_progress` → progress bar row updating in place (keyed by `run_id`).
- `gmail_mutation_applied` → "Archived N threads · {category}" row.
- `run_completed` → "Run complete · N threads archived · $cost" row with an **"Undo run"** button
  that opens the confirmation dialog and calls `POST /api/runs/{run_id}/undo`.
- `error` → red "Error" row with message.

Icons differentiate event types. Timestamps are relative ("2 s ago") updating in real time. Max 50
events shown (mirrors server-side cap). On SSE disconnect the drawer shows a subtle "Reconnecting…"
chip and retries with exponential back-off.

### 12. Real taxonomy editor *(D10 fix — Phase 3)*

Replaces the `StubPanel` in **Settings → Taxonomy** with a live inline editor backed by the existing
`/api/categories` endpoints.

Layout — a flat list of category rows. Each row:
- **Name**: click to enter inline-edit mode; `Enter` / blur confirms; calls `PATCH /api/categories/{id}`
  with `{name}`.
- **Default action** dropdown: `archive` / `keep` / `digest` / `needs_your_call`; on change calls
  `PATCH /api/categories/{id}` with `{default_action}`.
- **Drag handle** (six-dot icon): drag to reorder; on drop calls `PATCH /api/categories/{id}` with
  `{sort_order: <new_integer>}` for each row whose order changed. Order is client-side only.
- **Delete** icon (only for user-created categories, not system defaults): confirm modal, then
  `DELETE /api/categories/{id}` if that endpoint exists; otherwise deactivated.

**Add category** button at bottom: inline new row, name required, default action defaults to `keep`.

Loading: skeleton rows. Error: toast with message. No page refresh required for any action.

## Phase 6 screens

### 13. Resume banner *(Phase 6 — top of the primary screen)*

`frontend/src/components/ResumeBanner.tsx`, mounted in `page.tsx` above the progress bar. Rendered
whenever `GET /api/runs/latest` returns `status === "resumable"`.

- Full-width amber bar: **"Run interrupted — 2,003 of 2,176 threads already triaged."**
- Primary button: **"Resume run — 2,003 of 2,176 already done"** → `POST /api/runs/{run_id}/resume`.
  Disabled with a spinner while the request is in flight; on success the banner is replaced by the
  live progress bar starting at 2,003, not 0.
- Secondary text link: "Start a fresh run instead" → the normal `POST .../triage`, with a confirm
  dialog naming the consequence: *"This re-classifies all 2,176 threads and costs more."*
- On `409 not_resumable` the banner clears and shows the standard envelope error with Retry.
- Never shown for `running`, `completed`, `cancelled` or `failed` runs.

### 14. Live classification feed *(Phase 6 — inside the Activity drawer)*

The drawer becomes the live classification surface, not just a progress counter.

- One row per decided thread, keyed by `item_id`, newest first, arriving as the run proceeds:
  **tier badge** (`RULE` / `SENDER HISTORY` / `LLM` / `DEEP READ` / `REVIEWER` / `ERROR`) ·
  subject · `→` category · action · confidence % · reasoning (truncated, expandable).
- **Provisional labelling:** a row whose `review_state` is `provisional` carries an amber
  `NOT YET REVIEWED` chip and muted text. `review_failed` carries a red `REVIEW FAILED — kept` chip.
  Only `reviewed` rows render at full contrast with no chip. A later event for the same `item_id`
  (a reviewer flip) **replaces** the earlier row in place, so the user watches provisional become
  final. A provisional row must never read as a final decision.
- **Degraded-provider banner:** a `provider_degraded` event pins a red banner to the **top** of the
  drawer (above the feed, not scrolling with it) and auto-opens the drawer:
  **"NVIDIA is failing — 2,774 retries. This run is degraded and may take much longer than usual."**
  It clears on `run_completed` or `run_resumable`.
- **Model-fallback row:** a `model_fallback` event renders a distinct amber system row (not a thread
  row, never keyed by `item_id`) — *"Switched model: `nvidia/nemotron-3-nano-30b-a3b` →
  `nvidia/nemotron-3-super-120b-a12b` — model unavailable: 404"* — showing `from_model`, `to_model`
  and the reason verbatim. A mid-run model change is a backend action the user must be able to see;
  it is never silent. Subsequent thread rows continue below it, so the boundary is visually obvious.
  Model ids render as text, never as an icon or colour alone. The reason is shown as given, including
  transport reasons such as *"APIConnectionError after 3 retries"* — a connection failure **does**
  rotate the model, because NVIDIA serves each model from its own backend pool. Because the switch is
  per-run and sticks, at most one row per chain step appears — repeated identical switch rows would
  indicate a bug.
- A `run_resumable` event renders a row *"Run interrupted at N of M — resumable"* followed by the
  event's `reason` verbatim (e.g. *"All 3 models failed (last: APIConnectionError)"* or
  *"Run exceeded its 60-minute ceiling"*), with a **Resume** button that performs the same call as the
  Resume banner. A run that stops must always say **why**, in plain words — it never just stalls.
- Tier badges and review chips carry text, never colour alone.

### 15. Review state in the history view *(Phase 6)*

The Triage History thread rows render `review_state` alongside the existing tier badge. A
`provisional` or `review_failed` thread shows the same chip as the drawer and its per-decision Undo /
apply affordances are **disabled** with the tooltip *"Not yet reviewed — the never-miss reviewer has
not seen this decision, so it was never applied."*

## States (required for every list surface)

| State | Treatment |
|-------|-----------|
| Loading | skeleton rows, never a bare spinner on a full page |
| Empty | explanatory copy + the next action ("Connect Gmail" / "Run triage") |
| Error | the error code and message from the envelope, plus a Retry button; `reauth_required` renders a "Reconnect Gmail" button |
| Partial/failed run | the banner "This run failed partway — undecided threads were kept in your inbox and counted as auto-kept (low confidence)" |
| Resumable run | the Resume banner (screen 13) — never the failed-run banner |

## Accessibility & build constraints

- Tier badges carry text, not colour alone.
- All primary flows are keyboard-reachable.
- Tailwind v4: `postcss.config.mjs` (`@tailwindcss/postcss`) and `@source "../";` as the first lines of
  `globals.css` — never removed or overwritten.
- `NODE_OPTIONS=--no-experimental-webstorage` on the `dev`/`build`/`start` scripts.
- Playwright smoke (`tests/e2e/`) asserts, against the live app: the page renders **styled**, the
  triage history view shows real clusters with tier badges after a run, a cluster expands to real
  reasoning text, and the "Undo this run" button is present on a completed run's summary card.

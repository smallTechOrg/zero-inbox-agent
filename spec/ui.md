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
plain-English **priorities profile** textarea. The autonomy slider is made honest in Phase 7
(screen 17) — until then it rendered a control that no code path read.

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

### 14. Classification history *(the Activity drawer — full-history / archive surface only)*

The drawer is the **complete scrollback**: every event of the run, retained beyond what fits on the
main page. It is **not** the live surface. The live surface is
[screen 18](#18-live-run-feed-on-the-main-page-phase-7), rendered inline on the main page.

> **Binding rule: nothing user-critical may live only inside the drawer.** The drawer is
> `useState(false)` — closed by default — so anything visible only there is, from the user's seat,
> not visible at all. Every state a user must notice (classification rows arriving, degraded
> provider, model fallback, run interrupted, staleness) renders on the **main page** without any
> click; the drawer merely *also* holds it, with deeper history.
>
> The per-row, badge, chip and system-row detail below applies to **both** surfaces — screen 18
> renders the same rows from the same `ThreadFeedRow.tsx`. It is specified once here.

- One row per decided thread, keyed by `item_id`, newest first, arriving as the run proceeds:
  **tier badge** (`RULE` / `SENDER HISTORY` / `LLM` / `DEEP READ` / `REVIEWER` / `ERROR`) ·
  subject · `→` category · action · confidence % · reasoning (truncated, expandable).
- **Provisional labelling:** a row whose `review_state` is `provisional` carries an amber
  `NOT YET REVIEWED` chip and muted text. `review_failed` carries a red `REVIEW FAILED — kept` chip.
  Only `reviewed` rows render at full contrast with no chip. A later event for the same `item_id`
  (a reviewer flip) **replaces** the earlier row in place, so the user watches provisional become
  final. A provisional row must never read as a final decision.
- **Degraded-provider banner:** a `provider_degraded` event pins a red banner to the **top of the
  main dashboard page** (above the live feed, not scrolling with it), visible without the user
  opening any drawer or taking any action:
  **"NVIDIA is failing — 2,774 retries. This run is degraded and may take much longer than usual."**
  The drawer *also* pins a copy at the top of its scrollback, but the on-page banner is the binding
  one — auto-opening a drawer is **not** an acceptable substitute for on-page visibility.
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
`provisional` or `review_failed` thread shows the same chip as the live feed and its per-decision Undo /
apply affordances are **disabled** with the tooltip *"Not yet reviewed — the never-miss reviewer has
not seen this decision, so it was never applied."*

## Phase 7 screens

### 16. Inbox-Zero card *(Phase 7 — top of the primary screen, above the cluster list)*

`frontend/src/components/InboxZeroCard.tsx`, mounted in `page.tsx` directly under `InboxSummary`.
Loaded from `GET /api/runs/{run_id}/remainder` for the latest run (refetched on `apply_progress`,
`inbox_zero_report` and `run_apply_failed` SSE events; no polling).

**Headline row** — three numbers, largest first:
`{applied} archived this run` · `{inbox_remaining} still in your inbox` · `distance to zero: {n}`.

**The definition, stated plainly and always visible** (not behind a tooltip, not in a modal — a user
who thinks "zero" means one thing and gets another has been misled):

> **Inbox zero means your inbox holds only what needs a human.**
> Newsletters, Notifications, Outreach and Receipts are archived — never deleted, always undoable —
> when the agent is confident enough to act alone. **People, Urgent and Legal always stay**, along with
> anyone you've replied to, anyone on your VIP list, anything time-sensitive, anything it wasn't
> confident about, and anything one of your own rules kept.
> *Change which categories leave →* (link to **Settings → Taxonomy**)

Both category name lists in that text are **interpolated from the live taxonomy, never hardcoded**: the
first list is every category with `default_action = archive`, the second every category with
`default_action = keep`. The wording above shows them at the seeded defaults (Receipts is `archive` as
of Phase 7; Legal is a user-added keep category and appears only if it exists).

**The remainder ledger** — one row per bucket, count + plain-English reason, in this fixed order:

| Row | Copy |
|-----|------|
| `needs_your_call` | "**213** need your call — below the confidence floor, or the agent couldn't decide" |
| `category_keep` | "**1,031** kept by category — People, Urgent, Legal" (category names read from the live taxonomy, never hardcoded — this row lists exactly the categories whose `default_action` is `keep`; Receipts is **not** among them as of Phase 7) |
| `below_threshold` | "**71** not confident enough to archive on its own" — with an inline hint naming the current bar, e.g. *"the bar is 0.80; these scored 0.75–0.79"* |
| `held_by_never_miss` | "**34** held by VIP or reply history" |
| `unclassified` | "**N** decided before this policy existed" — **only rendered when `> 0`** |

Buckets at zero are rendered greyed with the count `0`, never hidden — a disappearing row reads as a
bug and hides the shape of the remainder.

**The failure state is loud, not a toast.** When `apply_ok === false`, a red bar sits at the top of the
card, above the headline:

> **The agent decided {distance_to_zero} threads should leave your inbox but could not archive them.**
> {apply_failed_reason}
> **[ Retry archiving ]**

The button calls `POST /api/runs/{run_id}/apply`, disables with a spinner while in flight, and on
success refetches the ledger. When `distance_to_zero > 0` with a null `apply_failed_reason`, the same
bar shows *"…{n} archives did not complete — see the action log."* A run that archived nothing must
**never** render in the green/neutral state.

**Dry-run state:** when `dry_run` is true the card shows the ledger with an amber
*"DRY RUN — nothing was archived"* chip and the Retry button is hidden, not disabled-with-a-tooltip
(there is nothing to retry).

Loading: skeleton rows. Empty (no run yet): "Run triage to see how far you are from zero."
Error: standard envelope error + Retry.

### 17. Honest autonomy controls *(Phase 7 — Settings)*

The existing auto-act threshold slider becomes real. It is relabelled and annotated:

- **Label:** "Act on its own above this confidence" (was an unlabelled "auto act threshold").
- **Sub-label:** "Below this the agent proposes but doesn't archive. Your never-miss floor
  ({confidence_floor}) always wins — this slider can only raise the bar, never lower it."
- **Calibration note, always visible:** *"Your model's real confidence tops out around 0.94.
  Above 0.90 the agent will archive almost nothing."*
- **Above-ceiling warning:** when the slider is above `0.90`, or when `PATCH /api/settings` returns
  `warning: "above_model_ceiling"`, a red inline warning appears: **"At this setting the agent will
  archive almost nothing — your inbox will not reach zero."** Saving is still permitted; it is the
  user's call, made with the consequence named.
- The slider's minimum is the user's `confidence_floor` (it can never be dragged below it) and `0` is
  rejected by the API.

**Per-category bars** are shown in the existing **Settings → Taxonomy** editor
(`TaxonomyEditor.tsx`, real since Phase 3). Each row whose `default_action` is `archive` or `digest`
gains an `auto_act_threshold` number input (step 0.01, blank = "inherit global"), saved via
`PATCH /api/categories/{id}`. Rows whose `default_action` is `keep` show the input **disabled** with
the note *"kept by default — never archived automatically"*, so the inertness is visible rather than
confusing. The `Urgent` row's `default_action` dropdown continues to omit `archive` entirely.

### 18. Live run feed on the main page *(Phase 7)*

*Visible without a click — that is the whole point of this screen.*

Rendered by `frontend/src/app/page.tsx` **inline**, directly under the progress bar and above the
cluster list. Not a drawer, not a modal, not behind a toggle. It appears **by itself** the moment a
run becomes active and is the most prominent thing on screen for the duration of the run — for a
triage agent, watching it think is the product.

- **Source:** `useSse()` from `frontend/src/lib/SseContext.tsx`, rendering `feed` rows with the
  existing `frontend/src/components/ThreadFeedRow.tsx`. No second `EventSource`, no second renderer.
- **Header row:** `Watching {n} threads classify · tier {t} · {model}` plus a live "last update
  {x}s ago" stamp, so movement is legible even when rows are identical in shape.
- **Body:** the newest **12** feed rows, newest first, each row identical to screen 14's — tier badge
  · subject · `→` category · action · confidence % · reasoning (truncated), with the same
  `NOT YET REVIEWED` / `REVIEW FAILED — kept` chips. New rows animate in at the top; a reviewer flip
  replaces its row in place. **"See all activity →"** opens the Activity drawer (screen 11/14) for
  the complete scrollback.
- **Heartbeat line, pinned at the bottom of the feed:** the newest `activity_heartbeat` rendered as
  plain text — *"classifying batch 12/75 — 29 threads, 18s elapsed, nvidia/nemotron-3-nano-30b-a3b"*.
  It always names real state; it is never a bare spinner.
- **Stale state (the anti-silence rule):** if no event has arrived for **8 seconds** while a run is
  active, an amber line replaces the heartbeat line — *"No activity for 12s — still waiting on
  {last phase}."* A user must always be able to tell working-but-slow from stuck. A visually static
  feed with no explanation is a defect.
- **Mid-run load / reload:** the feed is populated on first paint from the SSE replay buffer
  (`GET /api/events` replays up to 1000 events before streaming live), so a user joining late sees
  recent history immediately rather than an empty box that slowly fills.
- **Degraded provider:** the `provider_degraded` banner renders as a red line pinned above the inline
  feed on the **main page**, visible with no drawer opened and nothing clicked. The drawer keeps a
  copy in its scrollback (screen 14); the on-page line is the one the criterion is judged on.
- **Idle state:** with no active run the feed collapses to one line — *"Last run: 142 archived ·
  2m ago"* with the **See all activity** link. It never renders an empty box, and never a progress
  bar for work that is not running.
- Rows and chips carry text, never colour alone; the feed is a landmark region and is keyboard
  reachable.

---

# Phase 8 — Design system, front door and account

Everything below is Phase 8. It does not change any Phase 1–7 screen's behaviour; where it revises an
existing screen that is stated explicitly.

## Design system

The console is dense and calm and stays that way. The front door is the opposite register: spacious,
confident, few words. One token set serves both — the difference is spacing and type scale, never a
second palette.

### Colour tokens

Declared once as CSS custom properties in `frontend/src/app/globals.css` under `@theme`, consumed via
Tailwind utility classes. **No component may introduce a raw hex value.** Token names are a pinned
cross-slice contract — other slices consume these names and never redefine them.

| Token | Value | Used for |
|-------|-------|----------|
| `--zi-bg` | `#ffffff` | page background |
| `--zi-bg-subtle` | `#f7f8f8` | rails, cards on white, marketing section bands |
| `--zi-bg-inverse` | `#111827` | the homepage hero band, primary buttons |
| `--zi-fg` | `#111827` | primary text |
| `--zi-fg-muted` | `#4b5563` | secondary text, reasoning, timestamps |
| `--zi-fg-faint` | `#9ca3af` | disabled text, zero-count ledger rows |
| `--zi-border` | `#e5e7eb` | hairlines, card borders |
| `--zi-border-strong` | `#d1d5db` | inputs, dividers that must read as structural |
| `--zi-accent` | `#111827` | the single primary action colour — deliberately neutral, so *state* colour is never competing with brand colour |
| `--zi-ok` | `#047857` | live / applied / connected |
| `--zi-warn` | `#b45309` | provisional, dry run, stale, resumable |
| `--zi-danger` | `#b91c1c` | apply failure, degraded provider, destructive confirms |
| `--zi-info` | `#1d4ed8` | running, in-flight |
| `--zi-focus` | `#2563eb` | focus ring, on every interactive element without exception |

**Binding token-level rule — state is never colour alone.** Every token in the `ok` / `warn` /
`danger` / `info` family may only be used on an element that *also* carries a text label or an
`aria-label` naming the state. A bare coloured dot, bar, chip or border that conveys meaning with no
text is a defect. This codifies the rule the codebase already follows (tier badges, review chips) and
extends it to every new surface. Playwright asserts it on the new surfaces: each state element
resolves to non-empty accessible text.

### Type scale

Single family stack (`ui-sans-serif, system-ui, …`); no webfont is added — a font that has to load is
a blank front door for people on slow connections.

| Token | Size / line-height / weight | Used for |
|-------|------------------------------|----------|
| `zi-display` | 44px / 1.1 / 700 (28px below `md`) | the homepage headline, once per page |
| `zi-h1` | 28px / 1.2 / 600 | page titles, marketing section heads |
| `zi-h2` | 20px / 1.3 / 600 | card headers |
| `zi-h3` | 15px / 1.4 / 600 | row headers, settings group labels |
| `zi-body` | 14px / 1.5 / 400 | default console text |
| `zi-body-lg` | 17px / 1.6 / 400 | marketing prose only |
| `zi-mono` | 12px / 1.4 / 500, tabular-nums | counts, model ids, confidences, timestamps |
| `zi-caption` | 12px / 1.4 / 500 | chips, badges, helper text |

Counts and confidences always use `tabular-nums` so a live-updating number does not reflow its row.

### Spacing, radius, elevation, motion

- **Spacing scale:** `4 · 8 · 12 · 16 · 24 · 32 · 48 · 64` px only. Console density uses 4–16;
  marketing uses 24–64.
- **Radius:** `zi-r-sm` 4px (chips, inputs), `zi-r-md` 8px (cards, buttons), `zi-r-lg` 12px
  (marketing panels). Nothing is fully rounded except avatars.
- **Elevation:** exactly three levels — `flat` (border only, the console default), `raised`
  (`0 1px 2px rgba(17,24,39,.06)`, cards), `overlay` (`0 8px 24px rgba(17,24,39,.12)`, menus, modals,
  the drawer). No other shadow exists.
- **Motion:** 120 ms ease-out for hover/focus, 180 ms for a row entering the live feed, 0 ms for
  anything that would delay reading a number. Everything is wrapped in
  `@media (prefers-reduced-motion: reduce)` → no transform/opacity animation, content still updates.

### Component states (required for every interactive component)

Every button, input, select, link and row must define **all six**: `default`, `hover`, `focus-visible`
(2px `--zi-focus` ring, 2px offset), `active`, `disabled` (60% opacity **plus** a `title`/tooltip
saying *why*, never opacity alone), and `loading` (in-place spinner **plus** the control's label
changed to the present participle — "Archiving…" — never a label that disappears). A disabled control
with no explanation is a defect: this is the same failure mode as the Phase 7 autonomy slider.

### Responsive layout

Three breakpoints: `< 768px` (single column, left rail becomes a horizontal scrolling tab strip
pinned under the top bar, the Activity drawer becomes a bottom sheet), `768–1279px` (rail + content),
`≥ 1280px` (rail + content + max content width 1120px, centred). The live run feed and the
Inbox-Zero card are **never** the surfaces that get dropped on narrow viewports — they are the two
things users watch; anything gets collapsed before they do.

### Accessibility (extends the existing rules, does not replace them)

- All new state elements carry text (see the binding token rule above).
- Landmarks: `banner` (top bar), `navigation` (rail), `main`, `complementary` (drawer),
  `contentinfo` (marketing footer). The live feed is a labelled `region` with `aria-live="polite"`
  and `aria-relevant="additions"` — announcing arrivals, never re-announcing the whole list.
- Colour contrast ≥ 4.5:1 for body text, ≥ 3:1 for borders and large text, at every token pairing.
- The account menu is a real menu: `Escape` closes, focus returns to the trigger, arrow keys move.
- Every modal traps focus and is dismissible with `Escape`; destructive confirms require typing
  nothing but do name the consequence and count.

---

## Phase 8 screens

### 19. Homepage *(signed out — the front door, Phase 8)*

`http://localhost:8001/app/` for a visitor with no valid session. **This is a full replacement for
today's behaviour, where an unauthenticated visitor lands on an empty operator console.** The console
is never rendered, even skeletally, to a signed-out visitor.

- **Top bar:** wordmark "Zero Inbox" on the left; **Sign in** on the right. Nothing else — no rail,
  no run pill, no dry-run banner.
- **Hero:** headline *"An inbox that holds only what needs a human."* Sub-line: *"Zero Inbox reads
  your Gmail, decides what is noise, and archives it — never deletes it, always undoably, and never
  when it isn't sure."* One primary CTA: **Sign in with Google**. One secondary text link:
  *"How the safety model works ↓"* (anchors to the safety section). **There is exactly one CTA on
  this page**; a second competing button is a spec violation.
- **The honesty band — the differentiator, above the fold on desktop.** A short, factual panel
  titled *"What it does when it can't be sure"*:
  > On a real 2,122-thread inbox it archived 1,219 threads and took the inbox from 2,177 to 589.
  > Mid-run the model provider went down. It retried, switched models, and when every model failed
  > it stopped and left 350 threads unread **and told the user so** — rather than archiving mail it
  > had never looked at.
  This is prose, not a testimonial card, and it is not decorated with an illustration. The numbers
  are **static copy** in this phase — they describe a measured result, not the visitor's account —
  and are labelled as such (*"measured on the author's own inbox"*). Presenting them as live stats
  would be a lie; a generator must not wire them to an API.
- **How it works — three steps, no icons-only:** *1. Connect Gmail (read + archive + label; never
  delete).* *2. It classifies every thread in clusters — "142 threads from Substack newsletters" —
  with the reason it decided.* *3. You watch it work live and undo anything, per action or per run.*
- **The safety model, stated as promises** (each one a real, tested guarantee — a generator may not
  add a promise that is not in this list):
  - *It never deletes. There is no delete, no trash, no spam-report anywhere in the system.*
  - *Every change is undoable — per action or the whole run — from a snapshot taken before the change.*
  - *It never archives mail it wasn't confident about, and never mail from anyone you've replied to,
    anyone on your VIP list, or anything time-sensitive.*
  - *People, Urgent and Legal mail can never be set to auto-archive. The setting does not exist.*
  - *Only headers, subjects and a redacted 200-character snippet ever leave your machine. Message
    bodies are never stored.*
- **Footer:** links to the safety section, `/health`, and a plain-text privacy statement. No newsletter
  signup, no social icons, no cookie banner (the only cookie is the session cookie, stated in the
  footer text).
- Fully responsive; renders and is readable with JavaScript disabled for the hero and safety copy.

### 20. Sign in / sign up *(Phase 8)*

One screen, not two — **there is no separate sign-up**. First successful Google sign-in creates the
account; a returning user signs into the existing one. The copy says so: *"New here? Signing in
creates your account."*

- Single card: **Continue with Google**.
- Explicit scope honesty, the thing the old ConnectCard conflated: *"Signing in asks Google for your
  name and email address only. Access to your mail is a separate, later step you approve
  individually."* This is real — see [api.md Phase 8](api.md#phase-8--identity-account-and-review-recovery):
  `intent=signin` requests `openid email profile` only.
- Error states rendered inline from the envelope: `auth_declined` (*"You cancelled sign-in."* +
  Retry), `provider_error`, `validation_error`.
- On success → screen 21 if the user has no connected mailbox, otherwise screen 25.

### 21. First run — onboarding *(Phase 8)*

A three-step flow with a persistent step indicator (`1 · 2 · 3`, current step named in text, never a
bare dot row). It is not skippable, but every step is reversible and the user can sign out from any
step.

**Step 1 — Connect your mailbox.** The existing `ConnectCard` content, corrected: the Phase-1 copy
still claims *"nothing is changed until you say so"* and *"never in Phase 1"*, which is no longer
true — the agent archives autonomously. Replacement promise, which **is** true: *"It archives on its
own once it's confident, and it labels everything it touches. It never deletes, and everything is
undoable."* Scope list stays, with each scope in plain English and why it is needed. CTA:
**Connect Gmail** → `/auth/google/start?intent=connect`.

> A first-run flow that overstates the safety promise is worse than no flow. The corrected copy is
> load-bearing, and Playwright asserts the old sentence is gone.

**Step 2 — What happens next, stated before it happens.** Shown for the few seconds between the
callback and the first classification row. Plain list, no spinner-only state:
*"We're about to read your inbox headers · classify every thread in clusters · archive only what
we're confident about · leave People, Urgent, Legal and anyone you've replied to alone. You'll watch
every decision as it's made. Nothing is hidden from you and nothing is permanent."*
Includes a **"Start in dry-run instead"** secondary control which sets `dry_run=true` before the run
— for a user who wants to watch a full pass change nothing. It is a real control wired to
`PATCH /api/settings`, not a stub.

**Step 3 — The moment of trust (design this one deliberately).** The live run feed (screen 18) takes
the whole viewport for the first run, with no rail and no other card competing. As rows arrive the
user reads real subjects from their own mailbox with real reasons. Three things must be true within
the first ten seconds, and Playwright asserts the first:
1. **Rows are visibly arriving** with no click — the Phase 7 guarantee, now the centrepiece of
   onboarding.
2. A pinned line above the feed reads *"Nothing has been archived yet — the reviewer checks every
   decision first."* It is replaced, on the first real archive, by
   *"Archiving now — {n} so far. Undo any of it."* with an **Undo this run** button that is live from
   the first mutation, not after the run ends.
3. The first thread the agent **keeps** for a never-miss reason is surfaced as a callout above the
   feed — *"Kept: '{subject}' — you've replied to this sender before."* **This is the moment of
   trust**: the product proves it protects before it proves it cleans. If no such thread occurs in
   the first 50 decisions the callout is not faked; it is simply absent.

The run completes → the Inbox-Zero card (screen 16) with the definition and the remainder ledger, and
a one-time onboarding completion line: *"That's the whole loop. From now on it runs on its own —
you'll find everything here."* Onboarding is then never shown again for this user.

### 22. Account & security *(Phase 8 — Settings → Account)*

A new section in the existing Settings panel, above the autonomy controls. Read from
`GET /api/account`.

- **You:** display name, email, avatar-less initial, account created date. Identity is the Google
  account; there is no editable password and the screen says so.
- **Connected mailboxes:** one row per `channel_accounts` row — address, channel, status
  (`connected` / `reauth_required`, as text), connected date, and last successful sync. Each row has
  **Reconnect** (when `reauth_required`) and **Disconnect**. Disconnect opens a confirm modal naming
  the consequence exactly: *"Disconnect {address}? We revoke our access at Google and delete the
  stored token. Your triage history stays, and nothing in Gmail changes — nothing is un-archived and
  nothing is deleted."* This is the honest statement: disconnecting is not an undo.
- **Signed-in devices:** one row per active `user_sessions` row — device/browser summary from the
  user agent, first seen, last seen, and **This device** on the current one. **Sign out** per row,
  and **Sign out everywhere** for all of them (the current session included, which then redirects to
  screen 19). This is the enterprise capability that actually matters here: a session you can see is
  a session you can revoke.
- **Delete account:** a confirm modal naming the exact counts read from the API
  (*"This permanently deletes your account, {n} decisions, {m} action logs and {k} connected
  mailboxes. Your Gmail is untouched — archived mail stays archived and labelled. This cannot be
  undone."*), requiring the user to type their email address. Danger styling **plus** the word
  "Delete" — never red alone.
- Everything on this screen has a loading skeleton, an envelope error + Retry, and an empty state
  (*"No mailbox connected yet"* with the Connect CTA).

### 23. Account menu *(Phase 8 — top bar, revises screen "Global chrome")*

Replaces the bare connected-address text in the console top bar. A button showing the user's initial
and email, opening an `overlay`-elevation menu: the signed-in email (not a link), **Account &
security** (→ screen 22), **Settings**, a divider, **Sign out**. Keyboard-operable per the a11y rules
above. On sign-out: `POST /auth/logout`, then a hard navigation to `/app/` which now renders screen 19.

### 24. Recover a stuck run — "Retry review" *(Phase 8 — Inbox-Zero card)*

Closes a real gap: a `completed` run holding `review_failed` decisions can never be applied, because
the never-miss gate correctly refuses them — and `POST /api/runs/{id}/apply` cannot clear them. Today
the only recovery is a whole new run.

On the Inbox-Zero card (screen 16), when the remainder ledger reports `not_reviewed > 0`, an amber bar
sits **below** the red apply-failure bar (never replacing it — both can be true):

> **{n} threads never got past the reviewer**, so they were left in your inbox rather than archived
> unseen. The reviewer failed or was unavailable during this run.
> **[ Retry review ]**

- The button calls `POST /api/runs/{run_id}/retry-review`, disables with the label **Retrying
  review…**, and on success refetches the ledger; the `not_reviewed` count falls and the archived
  count rises only for threads the reviewer actually passed.
- **What it must not become:** it re-runs the *real* reviewer. It never sets `review_state` directly,
  never applies with `force=True`, and a thread the reviewer flips to keep stays kept. If the reviewer
  fails again, the bar returns with the new reason — it never silently marks anything reviewed.
- While a retry is in flight the live feed (screen 18) shows the reviewer rows arriving, so this is
  not an opaque button.
- `409 not_retryable` (run not `completed`, or nothing to retry) clears the bar and shows the envelope
  error.

### 25. The steady-state daily loop *(Phase 8 — revises the console landing)*

For a returning user with a connected mailbox and at least one completed run, `/app/` renders, top to
bottom, in this fixed order:

1. **Inbox-Zero card** (screen 16) — where you are relative to zero, and why the remainder is there.
2. **Live run feed** (screen 18) — active run streaming, or its one-line idle summary
   (*"Last run: 142 archived · 2m ago"*).
3. Cluster list (screen 2).

The two primary surfaces are 1 and 2 and they stay above the fold on a 900px-tall viewport. The
"Run triage" button remains in the top bar. Nothing about the daily loop requires opening the drawer.

Anything a returning user needs to *notice* — a resumable run, a degraded provider, a failed apply, an
unreviewed remainder — renders in this column, never only in the drawer. That rule is unchanged from
screen 14 and is restated here because Phase 8 adds a new bar (screen 24) to the same column.

---

## States (required for every list surface)

| State | Treatment |
|-------|-----------|
| Loading | skeleton rows, never a bare spinner on a full page |
| Empty | explanatory copy + the next action ("Connect Gmail" / "Run triage") |
| Error | the error code and message from the envelope, plus a Retry button; `reauth_required` renders a "Reconnect Gmail" button |
| Partial/failed run | the banner "This run failed partway — undecided threads were kept in your inbox and counted as auto-kept (low confidence)" |
| Resumable run | the Resume banner (screen 13) — never the failed-run banner |
| Completed run that archived nothing | the red apply-failure bar on the Inbox-Zero card (screen 16) with the reason and **Retry archiving** — never the neutral "run complete" state |
| Signed out (Phase 8) | the homepage (screen 19) — **never** an empty console, never a console skeleton, never a spinner that resolves to nothing |
| Signed in, no mailbox (Phase 8) | onboarding step 1 (screen 21) — not the cluster list with an empty state |
| Completed run with `not_reviewed > 0` (Phase 8) | the amber unreviewed bar with **Retry review** (screen 24), in addition to any apply-failure bar |
| Session revoked elsewhere (Phase 8) | any `/api/*` returning `unauthenticated` mid-session redirects to screen 19 with the line *"You were signed out."* — never a wall of failed panels |

## Accessibility & build constraints

- Tier badges carry text, not colour alone.
- All primary flows are keyboard-reachable.
- Tailwind v4: `postcss.config.mjs` (`@tailwindcss/postcss`) and `@source "../";` as the first lines of
  `globals.css` — never removed or overwritten.
- `NODE_OPTIONS=--no-experimental-webstorage` on the `dev`/`build`/`start` scripts.
- Playwright smoke (`tests/e2e/`) asserts, against the live app: the page renders **styled**, the
  triage history view shows real clusters with tier badges after a run, a cluster expands to real
  reasoning text, and the "Undo this run" button is present on a completed run's summary card.
- Phase 7 Playwright (`tests/e2e/phase7/`) additionally asserts, against the live app: the Inbox-Zero
  card renders the definition text and every remainder bucket including zero-count ones; a run with
  `apply_ok === false` renders the red bar with a working **Retry archiving** button and never the
  neutral state; and the Settings slider shows the above-ceiling warning when dragged above 0.90.
- Phase 7 Playwright also asserts the **live feed** (screen 18) against a real active run, **with no
  drawer opened and nothing clicked**: classification rows are present on the main page within 2 s of
  first paint, and the row count **strictly increases** across three polls ≥ 3 s apart. A screenshot
  of the mid-run main page is captured as an artifact. Asserting only that SSE events arrived is not
  acceptable evidence.
- Phase 8 Playwright (`tests/e2e/phase8/`) additionally asserts, against the live app: a signed-out
  visit to `/app/` renders the homepage headline, the safety promises and exactly one CTA, and renders
  **no** console element (no run pill, no rail, no cluster list); the corrected onboarding copy is
  present and the stale sentence *"Nothing is changed until you say so"* is **absent** anywhere in the
  app; the account menu opens by keyboard and Sign out returns the user to the homepage; the
  Account & security screen lists connected mailboxes and signed-in devices; every element carrying an
  `ok`/`warn`/`danger`/`info` token resolves to non-empty accessible text (the state-is-never-colour-
  alone rule); and the layout has no horizontal overflow at 375px, 768px and 1440px.
- Design tokens live in `frontend/src/app/globals.css` under `@theme` and are consumed by class name.
  The Tailwind v4 lines (`postcss.config.mjs`, `@source "../";` first in `globals.css`) are never
  removed or reordered when the tokens are added.

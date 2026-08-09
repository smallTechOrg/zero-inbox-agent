# UI

Web dashboard, single origin: **http://localhost:8001/app/** (static export mounted by the backend).

Design intent: dense, calm, keyboard-sweepable. The user's core motion is *scan a cluster → one key
press → next cluster*. Nothing about the interface should make hiding mail feel risky.

## Global chrome

- **Left rail:** Triage · Rules · Chat · Digest · Backlog · Cost · Settings.
- **Top bar:** connected Gmail address, run status pill, "Run triage" button.
- **Dry-run banner (Phase 1: permanent, unmissable):** a full-width red/amber bar pinned above all
  content — **"DRY RUN — nothing in your Gmail has been changed."** In Phase 2+ it is shown whenever
  `settings.dry_run` is true and replaced by a green "LIVE — actions will modify your Gmail" bar when
  it is off.
- **Stub convention:** every not-yet-built surface renders its real layout, greyed at ~50% opacity,
  with a `COMING SOON · Phase N` chip and a tooltip. Stub controls are `disabled` and never fire a
  request. A stub must never be mistakable for a broken feature.

## Screens

### 1. Connect (empty state)
Single card: "Connect Gmail" → the real Google consent flow. Lists the scopes requested in plain
English and states the two guarantees: *we never delete anything* and *nothing changes without your
approval*. After the callback it shows the connected address and a **Run triage (200 threads)** button.

### 2. Triage queue *(the Phase 1 primary screen)*
- **Progress bar** while a run is active: `decided / total`, live counts by tier, cancel button.
  Results stream in as they are decided — the list is usable before the run finishes.
- **Cluster list.** Each row: label ("142 threads from Substack newsletters"), thread count, suggested
  action, confidence range, three sample subjects, and **Approve all / Reject all** buttons.
- **Expand a cluster** → its threads: subject, sender, date, redacted snippet, category chip,
  confidence bar, and a **tier badge** — `RULE` / `SENDER HISTORY` / `LLM` / `DEEP READ` / `REVIEWER`
  — colour-coded, with the rule name shown when a rule fired.
- **Expand a thread** → the full reasoning text, verbatim.
- **"Needs your call" section**, pinned at the top: everything below the confidence floor. Styled as
  attention-positive, not as an error.
- Keyboard: `j`/`k` move, `a` approve, `x` reject, `enter` expand, `A` approve whole cluster.
- Per-decision **Undo** button — Phase 1 stub, real in Phase 2.

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
Auto-act threshold slider, confidence floor slider, dry-run toggle (locked on in Phase 1 with an
explanatory tooltip), timezone, digest hour, taxonomy editor, **VIP / never-hide list** editor, and
the plain-English **priorities profile** textarea.

## States (required for every list surface)

| State | Treatment |
|-------|-----------|
| Loading | skeleton rows, never a bare spinner on a full page |
| Empty | explanatory copy + the next action ("Connect Gmail" / "Run triage") |
| Error | the error code and message from the envelope, plus a Retry button; `reauth_required` renders a "Reconnect Gmail" button |
| Partial/failed run | the banner "This run failed partway — undecided threads were kept in your inbox and moved to Needs your call" |

## Accessibility & build constraints

- Tier badges carry text, not colour alone.
- All primary flows are keyboard-reachable; the triage sweep is keyboard-first.
- Tailwind v4: `postcss.config.mjs` (`@tailwindcss/postcss`) and `@source "../";` as the first lines of
  `globals.css` — never removed or overwritten.
- `NODE_OPTIONS=--no-experimental-webstorage` on the `dev`/`build`/`start` scripts.
- Playwright smoke (`tests/e2e/`) asserts, against the live app: the page renders **styled**, the
  dry-run banner is present, the triage queue shows real clusters with tier badges after a run, and a
  cluster expands to real reasoning text.

# UI — The Single Dashboard

One page (`/`). No other routes except the signed-out front door on the same page.
Screen sprawl was the old design's core pain: every feature is a **section of one
dashboard**, not a screen.

## Signed-out state

Hero + "Sign in with Google" button + a short honesty band: what the agent reads
(headers + snippet, never bodies), that everything is undoable, INBOX-only.

## Signed-in dashboard (top to bottom)

1. **Header** — product name, user avatar/menu (sign out, disconnect Gmail). If
   `needs_reconnect`: a persistent banner "Reconnect Gmail to continue" with the
   OAuth button — the only error surface for token problems.
2. **Command strip** — the big **"Clean my inbox"** button (disabled with reason
   while a run is active; reads "Resume cleaning" if last run was interrupted) +
   inbox-state counts from the latest audit (total in INBOX, unread, oldest,
   remaining-to-decide). First visit auto-triggers the mini-audit with a visible
   "auditing your inbox…" progress state.
3. **Live activity feed** (visible while a run is active, collapses to the run card
   after) — streaming one-sentence actions ("Filed 'ACME invoice' → Finance —
   archived"), each expandable to show the model's reasoning + confidence;
   progress bar (n/50) and per-category running counts; cost ticker (calls, tokens,
   est. $) that also surfaces fallback events ("NVIDIA timed out — switched to
   Gemini for this batch"). Reconnect-safe: reload replays the feed via
   `?after_seq`.
4. **Run timeline** — past runs as cards: when, threads, per-category counts, cost,
   status badge (`completed`/`interrupted`/`undone`), and a one-click **Undo this
   run** button with confirm; undo progress streams into the card. Interrupted
   cards say why in plain English.
5. **Taxonomy panel** — category chips with per-category rule selector
   ("Label only" / "Label + archive"), inline rename, add, delete; "Needs review"
   chip is pinned and highlighted; low-confidence flag explained here. Edits apply
   to the next chunk ("the agent adapts").
6. **Ledger section** — *Phase 1: labelled stub.* A designed panel with search box
   and empty result table under a badge: **"Coming in Phase 2 — not yet
   functional."** Phase 2 wires it: search by sender/subject/category, rows show
   decision, one-line reason, needs-review flag, undo state.
7. **Costs section** — *Phase 1: the live ticker is real; the historical dashboard
   is a labelled stub* with the same "Coming in Phase 2" badge. Phase 2: per-run
   cost cards + cumulative totals + fallback-event counts.
8. **Sender profiles** — *Phase 1: labelled stub.* Phase 2: list of learned
   profiles ("GitHub → Notifications, 41 hits"), delete/add.

## Stub convention (binding)

Every non-functional surface carries the same visual badge component ("Coming in
Phase 2 — not yet functional") and disabled inputs — a stub must never be mistakable
for a bug. Playwright asserts the badges exist in Phase 1 and are gone in Phase 2.

## States & polish (first-time-right bar)

Loading, empty ("Inbox chunk clean — nothing to do"), interrupted, and
gmail_reconnect states are designed, not defaulted. No raw JSON, no spinners
without text, dark-friendly neutral styling.

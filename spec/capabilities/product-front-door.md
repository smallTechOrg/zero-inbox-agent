# Capability: Product Front Door

## What It Does
Gives the product a signed-out homepage, a first-class sign-in, and a guided first run, so someone who
has never seen Zero Inbox understands what it does and reaches their first trustworthy result without
being dropped into an operator console.

## Inputs
| Input | Type | Source | Required |
|-------|------|--------|----------|
| session cookie | signed token | browser | no — its absence is what selects the homepage |
| `GET /api/me` | envelope | backend | yes (signed-in branch) |
| onboarding stage | derived (`no session` / `no connection` / `no completed run` / `steady`) | `/api/me` + `/api/runs/latest` | yes |
| live run events | SSE | `GET /api/events` | yes (onboarding step 3) |

## Outputs
| Output | Type | Destination |
|--------|------|-------------|
| Homepage (screen 19) | rendered page | signed-out visitor |
| Sign-in card (screen 20) | rendered page | signed-out visitor |
| Onboarding steps 1–3 (screen 21) | rendered flow | signed-in user with no connection / no completed run |
| Steady-state console (screen 25) | rendered page | returning user |
| Design tokens | CSS custom properties in `globals.css` `@theme` | every component in the app |

## External Calls
| System | Operation | On Failure |
|--------|-----------|------------|
| own backend | `GET /api/me` | `401` → render the homepage (this is the normal signed-out path, not an error); any other error → envelope error + Retry, never a blank page |
| own backend | `GET /api/runs/latest` | envelope error + Retry inside the console column; the homepage never depends on it |
| Google OAuth | via `/auth/google/start?intent=…` | `auth_declined` / `provider_error` rendered inline on screen 20 |

## Business Rules
- A signed-out visitor **never** sees the console — not skeletally, not for a frame. The homepage is
  the default render, and the console mounts only after `/api/me` succeeds.
- Exactly **one** call to action on the homepage.
- Every safety promise on the homepage must correspond to a guarantee that is built and tested. The
  five promises in [ui.md screen 19](../ui.md#19-homepage-signed-out--the-front-door-phase-8) are the
  complete permitted set; adding a sixth is a spec violation.
- The measured inbox numbers in the honesty band are **static copy labelled as the author's own
  inbox**, never wired to an API and never presented as the visitor's own.
- The Phase-1 onboarding sentences *"Nothing is changed until you say so"* and *"never in Phase 1"*
  are removed everywhere — they are no longer true and an overstated promise is worse than none.
- Onboarding step 3 shows the first never-miss **keep** as a callout when one occurs in the first 50
  decisions, and shows nothing when one does not. It is never fabricated.
- Design tokens are the only source of colour, type and spacing. No component introduces a raw hex.
- State is never conveyed by colour alone — every `ok`/`warn`/`danger`/`info` element carries text or
  an `aria-label`.

## Success Criteria
- [ ] A request to `/app/` with no session cookie renders the homepage headline, the five safety
      promises and exactly one CTA, and renders no run-status pill, no left rail and no cluster list.
- [ ] The string "Nothing is changed until you say so" appears nowhere in the built frontend.
- [ ] A signed-in user with zero `channel_accounts` rows lands on onboarding step 1, not on an empty
      cluster list.
- [ ] Onboarding step 3 shows ≥ 1 classification row on the main page within 2 s of first paint, with
      no click (the Phase 7 visibility bar, re-asserted in the onboarding context).
- [ ] At viewport widths 375px, 768px and 1440px the page has no horizontal overflow and the
      Inbox-Zero card and live feed are both present.
- [ ] Every element rendered with a state token resolves to non-empty accessible text.
- [ ] `prefers-reduced-motion: reduce` disables all transform/opacity animation while content still
      updates.

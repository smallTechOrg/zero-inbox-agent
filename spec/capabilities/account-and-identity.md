# Capability: Account & Identity

## What It Does
Makes signing in a first-class flow separate from granting mailbox access, and gives every user a
visible, revocable set of sessions, a manageable list of connected mailboxes, and account deletion —
so the already-multi-tenant data model has a real product around it.

## What "enterprise grade" means here — scoped honestly

**In Phase 8 (built and tested):**
1. Sign-in as its own OAuth intent requesting `openid email profile` only; mailbox access is a
   separate, later consent the user approves individually.
2. Revocable server-side sessions — a visible device list, per-session revoke, and sign-out-everywhere.
3. Mailbox ownership is globally unique: a mailbox already connected to one account cannot be
   connected to a second, and the second user is told why.
4. Session hardening: `secure` cookie on https, token rotation on sign-in, `AGENT_SECRET_KEY`
   required (the `"insecure-dev-key"` fallback is deleted), no raw IP or raw user-agent stored.
5. Account lifecycle: disconnect a mailbox (revoke at Google + delete the ciphertext), delete the
   account (an explicit, metadata-driven, strictly user-scoped delete of every table carrying a
   `user_id` — **not** an FK cascade, see the business rule below), with the consequence of each
   stated in exact counts before it happens.

**Explicitly deferred — not promised anywhere in the UI, the copy or the README:** SSO/SAML/OIDC
beyond Google, SCIM provisioning, organisations/teams, roles and RBAC, an admin console, audit-log
export, delegated or shared mailboxes, billing, per-tenant rate limits, data residency, password auth
and app-level MFA (MFA is inherited from the Google account and the homepage says so). A long list
half-built is worse than a short list that holds.

## Inputs
| Input | Type | Source | Required |
|-------|------|--------|----------|
| `intent` | `signin` \| `connect` | query param on `/auth/google/start`, round-tripped in the signed state cookie | no (defaults to `connect`) |
| Google OAuth result | identity + optional refresh token | Google | yes |
| session cookie | signed `{uid, sid}` | browser | yes on `/api/*` |
| `confirm_email` | string | account-deletion request body | yes for deletion |

## Outputs
| Output | Type | Destination |
|--------|------|-------------|
| `users` row | DB | created on first sign-in of either intent |
| `user_sessions` row | DB | created on every sign-in; revoked on sign-out |
| `channel_accounts` row | DB | created on `connect` only |
| `GET /api/account` payload | envelope | screen 22 |
| `mailbox_already_connected` error | envelope, 409 | the second user attempting the same mailbox |

## External Calls
| System | Operation | On Failure |
|--------|-----------|------------|
| Google OAuth token endpoint | code exchange | `provider_error` 502; nothing persisted |
| Google OAuth revoke endpoint | revoke on disconnect | **best effort** — logged at WARNING; the local ciphertext is deleted regardless, so a Google outage can never leave a token we cannot remove |
| Gmail API | **none** — disconnect and delete perform zero mailbox operations | n/a |

## Business Rules
- Identity is the Google account. One Google account = one Zero Inbox user; two humans sharing that
  login share the account, and screen 22 says so plainly.
- `intent=signin` writes **no** `channel_accounts` row, requires **no** refresh token, and triggers
  **no** triage run. `intent=connect` keeps the existing behaviour byte for byte, including the
  auto-triage background task.
- The `intent` is read only from the signed state cookie, never from an unsigned callback parameter.
- A revoked or unknown `sid` is `401`. A legacy `uid`-only cookie stays valid and is upgraded in place
  on its next request — no existing user is signed out by this phase.
- `require_user_id` remains the single user-scope chokepoint. Phase 8 adds revocation **inside** it
  and introduces no second authentication path, no bearer token and no API key.
- Every new route is user-scoped and returns `404`, not `403`, for another user's row — matching the
  existing cross-user isolation tests.
- `refresh_token_enc`, raw IPs and raw user-agent strings are never returned by any route and never
  logged.
- **Account deletion does not rely on FK cascade.** SQLite does not enforce `ON DELETE CASCADE`
  unless `PRAGMA foreign_keys=ON` is set per connection, so a cascade-only delete would orphan every
  child row on SQLite while looking correct on Postgres. The route instead deletes every table
  carrying a `user_id` explicitly, derived from the ORM metadata (children first) so a table added
  in a later phase cannot be silently missed, with `WHERE user_id = :user_id` on **every** statement
  — deletion is strictly user-scoped and can never remove or orphan another user's rows. See
  [data.md](../data.md#deletion-semantics). Do not replace this with a cascade.
- **`secure` cookie detection does not rely on `request.url.scheme` alone.** An ASGI scope with no
  `server` and no `Host` header yields a URL with the scheme dropped, which would silently ship the
  session cookie without `secure` over https; the check falls back to the raw ASGI `scope["scheme"]`.
  See [api.md](../api.md#session-hardening-behaviour-change-no-new-route).
- Deleting an account or disconnecting a mailbox performs **no Gmail mutation**. Archived mail stays
  archived and labelled; this is stated in the confirm dialog rather than implied.
- Nothing in this capability touches the triage graph, the reviewer, `apply_decision`, the mutator or
  the taxonomy. The hard safety invariants are untouched by construction.

## Success Criteria
- [ ] `GET /auth/google/start?intent=signin` produces a consent URL whose `scope` contains exactly
      `openid email profile` and **no** `gmail.*` scope; `intent=connect` (and no intent at all)
      produces the existing Gmail scope set unchanged.
- [ ] Completing a `signin` flow creates a `users` row and a session, creates **zero**
      `channel_accounts` rows and starts **zero** triage runs.
- [ ] A `connect` callback for an address owned by another user returns `409
      mailbox_already_connected` and writes zero rows; re-connecting your own mailbox still succeeds
      idempotently.
- [ ] `POST /auth/logout` sets `revoked_at`; a subsequent request replaying the same cookie gets
      `401`.
- [ ] `POST /api/account/sessions/revoke-all` revokes every session for the user, and a request
      bearing any of them then gets `401`.
- [ ] `GET /api/account` for user A never contains a row belonging to user B; `DELETE
      /api/account/sessions/{id}` and `DELETE /api/account/connections/{id}` return `404` for another
      user's id.
- [ ] `DELETE /api/account` with a mismatched `confirm_email` returns `422` and deletes nothing; with
      the correct email it removes every user-scoped row (asserted table by table, on SQLite with
      `PRAGMA foreign_keys` **off** — the default — so the assertion proves the explicit delete and
      not a cascade) and makes zero Gmail calls.
- [ ] Deleting user A's account leaves **every** row belonging to user B intact — asserted table by
      table, with no orphan left behind in any table carrying a `user_id`.
- [ ] A cookie carrying only `uid` (pre-Phase-8) authenticates successfully and is re-issued with a
      `sid`, and the resulting session appears in `GET /api/account`.
- [ ] No response body from any Phase 8 route contains `refresh_token_enc`, a raw IP or a raw
      user-agent string.
- [ ] The app refuses to start when `AGENT_SECRET_KEY` is unset, and no code path signs a cookie with
      `"insecure-dev-key"`.

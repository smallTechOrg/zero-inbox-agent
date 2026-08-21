# Capability: Google Sign-in & Gmail Connection

## What It Does
A user signs in with their real Google account and, in the same consent, grants Gmail access; tokens are stored encrypted per user and a revoked token always surfaces as a "Reconnect Gmail" prompt.

## Inputs
| Input | Type | Source | Required |
|---|---|---|---|
| OAuth consent code | string | Google redirect | yes |

## Outputs
| Output | Type | Destination |
|---|---|---|
| Session cookie | signed cookie | browser |
| user + gmail_account rows | records | [data.md](../data.md) |
| Connection status | `connected`/`needs_reconnect`/`none` | dashboard header |

## External Calls
| System | Operation | On Failure |
|---|---|---|
| Google OAuth | code exchange / token refresh | user-facing "Reconnect Gmail" state; never a traceback |

## Business Rules
- Scopes: `openid email profile` + `gmail.modify` only.
- Refresh tokens encrypted at rest; never logged or returned by any API.
- Other users are added as OAuth test users on the existing client (`AGENT_GOOGLE_CLIENT_ID/SECRET/REDIRECT_URI`).
- Any Google 401/invalid_grant anywhere in the app flips status to `needs_reconnect` and every affected surface shows the reconnect banner.

## Success Criteria
- [ ] A fresh OAuth test user completes sign-in → dashboard in one consent flow.
- [ ] Revoking access in Google account settings then triggering a run yields the reconnect banner, no error page.
- [ ] No user can read another user's data (integration test with two seeded users).

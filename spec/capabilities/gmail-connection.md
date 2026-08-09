# Capability: Gmail Connection

## What It Does
Connects a user's own Gmail account through the full Google OAuth web flow and stores their refresh
token per user so the agent can read (and later modify) that mailbox on their behalf.

## Inputs
| Input | Type | Source | Required |
|-------|------|--------|----------|
| OAuth consent | user interaction | Google consent screen | Yes |
| Authorization code | string | OAuth callback | Yes |
| CSRF state | string | session | Yes |

## Outputs
| Output | Type | Destination |
|--------|------|-------------|
| User record | entity | `users` |
| Connected mailbox | entity | `channel_accounts` (refresh token encrypted at rest) |
| Session | signed cookie | browser |
| Connection status | JSON | `GET /api/me` |

## External Calls
| System | Operation | On Failure |
|--------|-----------|------------|
| Google OAuth | authorize + token exchange | Show the error and a Retry button; nothing is persisted |
| Google OAuth | refresh access token | Retry once; on failure set the connection `reauth_required` and surface a "Reconnect Gmail" button — never silently degrade |
| Gmail API | fetch the profile address | Mark connection `connected` only after this succeeds |

## Business Rules
- Scopes requested: `gmail.readonly`, `gmail.modify`, `gmail.settings.basic`, `gmail.compose` — no more.
- `access_type=offline` and `prompt=consent` so a refresh token is always returned.
- The refresh token is Fernet-encrypted before it touches the database and is never returned by any
  API response, never logged, and never included in an error message.
- Multi-tenant from day one: every downstream query is scoped by `user_id`; one user can never observe
  another user's mailbox, rules, decisions or memory.
- Completing this flow also establishes the dashboard session — there is no separate login.
- Re-connecting the same address updates the existing `channel_accounts` row rather than creating a
  duplicate.

## Success Criteria
- [ ] Clicking "Connect Gmail" reaches the real Google consent screen with exactly the four scopes.
- [ ] After consent, `GET /api/me` returns the real connected Gmail address.
- [ ] `channel_accounts.refresh_token_enc` is not readable as plaintext in the database file.
- [ ] No API response body anywhere contains the string of a refresh or access token.
- [ ] With a deliberately invalidated refresh token, the API returns `reauth_required` (409) and the UI
      shows "Reconnect Gmail" instead of an unhandled error.
- [ ] A second user connecting a different mailbox sees only their own threads.

# Removing the `e2e-actions-test` category from the live account

**This file is a procedure, not a script. There is deliberately no executable
code here and no cleanup script anywhere in this repository.**

Phase 9, slice 8. A category `key='e2e-actions-test'`, `name='E2EActionsTest'`,
`default_action=archive` exists on the user's live account (`6b4ab0f4…`). It was
left behind by a test that ran against production data — the same defect class as
the cached-`get_settings()` incident. Nothing is filed under it, so it is
harmless, but the user sees it the moment he opens the taxonomy editor.

The row is the **symptom**. The **cause** is closed in code by
`tests/isolation.py` + the autouse guard in `tests/conftest.py`, which is what
makes this a one-off operational step rather than a recurring chore.

## Who runs this, and when

- **A human**, by hand, against the already-running server on
  **http://localhost:8001** — the supervised process the user is testing on.
- **After the Phase 9 gate is green**, never during a build.
- **No agent performs this cleanup.** No generator, no auditor, no orchestrator.

## Hard prohibitions

- **Never open, query or write `zero_inbox.db` directly.** It holds ~12,500 real
  decisions for 2 real accounts. `get_settings()` is cached and does **not**
  honour an env override from a standalone script — that is precisely how the
  real database was polluted before. Removal goes through the normal taxonomy
  HTTP path or it does not happen.
- **Never write an ad-hoc cleanup script**, in this directory or any other.
- **Do not restart, kill or duplicate the server on `:8001`.**
- **Nothing is deleted "because it looked empty."** Step 1 is not optional.
- **No Gmail label is touched and no mail is moved.** Deleting the category
  leaves the `ZeroInbox/E2EActionsTest` label and every message exactly as they
  are. This is the taxonomy row only.

## The procedure

### Step 0 — find the category id

Open the taxonomy list for the account (`GET /api/categories`, or the Settings →
Taxonomy screen) and read the `id` of the row whose `key` is `e2e-actions-test`.
Confirm `name` is `E2EActionsTest` and `default_action` is `archive` before going
further. If no such row exists, the cleanup is already done — stop, and report
that.

### Step 1 — verify it is unused (the gate on the whole operation)

Call `GET /api/categories/{id}/usage` for that id.

Proceed to step 2 **only** if all three counts are exactly zero:

| field       | required value |
|-------------|----------------|
| `decisions` | `0`            |
| `rules`     | `0`            |
| `items`     | `0`            |

**Any non-zero count STOPS the operation.** Do not delete, do not retry, do not
"clean up" the referencing rows. Report the actual counts to the user and let
them decide. A category with real decisions behind it is real taxonomy, whatever
its key looks like — deleting it would destroy audit history the user relies on
for undo.

If the endpoint returns an error, that is also a stop: an unverified delete is
not permitted.

### Step 2 — delete through the normal taxonomy path

Call `DELETE /api/categories/{id}` — the same route the user's own taxonomy
editor uses, with the same server-side guards. Nothing bespoke, nothing lower
level.

Expect a success envelope. If the route refuses (for example because it performs
its own usage check and disagrees with step 1), **accept the refusal** and report
it. Do not work around it.

### Step 3 — confirm

Re-call `GET /api/categories` and confirm no row with `key='e2e-actions-test'`
remains. Also confirm the rest of the taxonomy is unchanged — the seed
categories and the user's own categories are all still present with their
`default_action` values intact.

Report to the user: the usage counts observed in step 1, the delete result, and
the confirmed post-state.

## Why this cannot recur

`tests/conftest.py` installs an autouse `before_flush` guard, on the SQLAlchemy
`Session` class itself, that refuses to write a `categories`, `decisions`,
`rules` or `channel_accounts` row when either:

1. the session is not bound to a throwaway `tmp_path` database
   (`assert_isolated_db` — `zero_inbox.db` and anything under `data/` are refused
   by name), or
2. the row names a real account — the `6b4ab0f4…` deny-list, the user's real
   mailbox in any Gmail spelling (dots and `+tags` normalised), or a bare uuid4
   `user_id`, which is the shape this system mints for real users.

It fires **on the write**, so the offending test fails immediately with its own
node id and the offending row in the message. There is no marker and no
environment variable that turns it off; a test that believes it needs a real
account is a spec question, not a local override. `tests/unit/test_isolation_guard.py`
fails if the guard is removed or weakened.

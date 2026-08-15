# Data Model

All tables carry `user_id` and every query is scoped by it — **strict per-user isolation** is a
schema-level invariant, not an application convention. All ids are UUID strings. All timestamps are
timezone-aware UTC.

**Hard rule: no table has a column containing email body text.** Only headers, identifiers, a
redacted ≤200-character snippet, decisions and reasoning are persisted. This is enforced by
`tests/integration/test_no_body_persisted.py`.

---

## Phase 1 Entities

### `users`
| Field | Type | Notes |
|-------|------|-------|
| `id` | str PK | |
| `email` | str unique | from Google profile |
| `display_name` | str | |
| `created_at` | ts | |

### `channel_accounts`
A connected mailbox. One user may connect several.

| Field | Type | Notes |
|-------|------|-------|
| `id` | str PK | |
| `user_id` | str FK→users | |
| `channel` | str | `"gmail"` (only value in v1) |
| `account_email` | str | the connected address |
| `refresh_token_enc` | str | Fernet-encrypted; never logged, never returned by any API |
| `scopes` | json | granted scopes |
| `status` | str | `connected` \| `reauth_required` \| `revoked` |
| `history_id` | str \| null | Gmail incremental-sync cursor |
| `connected_at` | ts | |

Unique on `(user_id, channel, account_email)`.

### `items`
The channel-agnostic thread. Gmail threads normalize into this shape; a future adapter fills the same
columns.

| Field | Type | Notes |
|-------|------|-------|
| `id` | str PK | |
| `user_id` | str FK | |
| `channel_account_id` | str FK | |
| `external_thread_id` | str | Gmail thread id |
| `external_message_ids` | json | message ids in the thread |
| `subject` | str | |
| `from_name` / `from_email` / `from_domain` | str | of the latest message |
| `to_emails` / `cc_emails` | json | |
| `list_id` | str \| null | `List-Id` header — the strongest newsletter signal |
| `unsubscribe_url` | str \| null | from `List-Unsubscribe` |
| `message_count` | int | |
| `has_attachments` | bool | |
| `snippet_redacted` | str(200) | **redacted** snippet — the only content-bearing column |
| `internal_date` | ts | latest message date |
| `is_unread` | bool | |
| `channel_labels` | json | current Gmail label ids |
| `created_at` | ts | |

Unique on `(user_id, external_thread_id)`.

> **Assumed:** a redacted ≤200-char snippet is *not* a body and is permitted storage — it is required
> to render a useful triage queue without re-fetching from Gmail. Full bodies are never stored.

### `sender_profiles`
Per-user, per-sender evidence.

| Field | Type | Notes |
|-------|------|-------|
| `id` PK, `user_id` FK | | |
| `sender_email`, `sender_domain` | str | |
| `received_count`, `opened_count`, `replied_count`, `archived_by_user_count` | int | |
| `ever_replied` | bool | **the never-miss reply-history signal** |
| `last_replied_at`, `last_seen_at` | ts | |
| `importance_score` | float 0–1 | derived; raised by corrections |

Unique on `(user_id, sender_email)`.

### `categories`
Materialises **1:1 as a real Gmail label**.

| Field | Type | Notes |
|-------|------|-------|
| `id` PK, `user_id` FK | | |
| `key` | str | stable slug |
| `name` | str | Newsletters, Notifications, Receipts, Outreach, People, Urgent (defaults) |
| `description` | str | plain English — used verbatim in the classifier prompt |
| `channel_label_name` | str | e.g. `ZeroInbox/Newsletters` |
| `channel_label_id` | str \| null | filled when the label is created in Gmail (Phase 2) |
| `default_action` | str | `keep` \| `archive` \| `digest`. Seeded (from `DEFAULT_TAXONOMY` in `src/tools/rules.py`): `archive` for Newsletters, Notifications, Outreach **and Receipts** (Receipts changed from `keep` in Phase 7); `keep` for People and Urgent. |
| `auto_act_threshold` | float \| null | **Phase 7.** The confidence bar at or above which the agent acts on its own **for this category**. `NULL` inherits `user_settings.auto_act_threshold`. Inert only while `default_action = keep`. Validation `0 < v <= 1`. Seeded: `outreach` and `receipts` = `0.85` — **both live**, since both are `archive` categories; all others NULL. See [drive-to-inbox-zero](capabilities/drive-to-inbox-zero.md#a-the-autonomy-instrument-is-real-and-it-is-per-category) |
| `is_default`, `sort_order` | bool, int | |

Unique on `(user_id, key)`. `key = "urgent"` can never carry `default_action = archive`.

### `rules`
| Field | Type | Notes |
|-------|------|-------|
| `id` PK, `user_id` FK | | |
| `name` | str | |
| `kind` | str | `deterministic` \| `learned` \| `llm_proposed` |
| `source` | str | `seed_pack` \| `mined` \| `chat` \| `user` |
| `matcher` | json | `{from_email?, from_domain?, list_id?, subject_regex?, has_attachment?, older_than_days?}` |
| `action` | json | `{set_category, archive: bool, digest: bool}` |
| `status` | str | `proposed` \| `active` \| `automatic` \| `disabled` |
| `confidence` | float | |
| `match_count` | int | how many items it has matched |
| `channel_filter_id` | str \| null | real Gmail filter id once created (Phase 3) |
| `created_at`, `promoted_at` | ts | |

`status=automatic` is the **only** state in which the agent acts without per-decision approval.

### `triage_runs`
| Field | Type | Notes |
|-------|------|-------|
| `id` PK, `user_id` FK, `channel_account_id` FK | | |
| `kind` | str | `incremental` \| `backlog` |
| `status` | str | `running` \| `completed` \| `failed` \| `cancelled` \| `resumable` |
| `dry_run` | bool | Phase 1: always true |
| `range_start`, `range_end` | ts \| null | backlog chunk bounds |
| `cursor` | str \| null | resume point |
| `items_total`, `items_decided` | int | drives the progress bar |
| `counts` | json | `{by_tier, by_category, needs_your_call}` |
| `tokens_in`, `tokens_out` | int | |
| `cost_usd` | float | |
| `error_message` | str \| null | |
| `started_at`, `finished_at` | ts | |

### `decisions`
| Field | Type | Notes |
|-------|------|-------|
| `id` PK, `user_id` FK, `item_id` FK, `run_id` FK | | |
| `cluster_id` | str FK \| null | |
| `category_id` | str FK \| null | |
| `proposed_action` | str | `keep` \| `archive` \| `digest` |
| `confidence` | float 0–1 | |
| `reasoning` | text | full, expandable in the UI |
| `decided_by` | str | `rule` \| `sender_history` \| `llm` \| `llm_deep` \| `reviewer` \| `error` — **the which-tier-fired indicator** |
| `rule_id` | str FK \| null | set when `decided_by="rule"` |
| `time_sensitive` | bool | forces toward keep |
| `status` | str | `proposed` \| `approved` \| `rejected` \| `applied` \| `undone` \| `needs_your_call` |
| `review_state` | str NOT NULL, default `provisional` | `provisional` \| `reviewed` \| `review_failed` — **the never-miss finality gate** |
| `autonomy_state` | str \| null | **Phase 7.** `auto_act` \| `below_threshold` \| `held_by_never_miss` \| `category_keep` \| `needs_your_call` — **why this thread is or is not leaving the inbox**. Written by `mark_autonomy_state` (see [agent.md](agent.md)); the grouping key of the remainder ledger and the sole basis of `distance_to_zero`. NULL only on rows written before Phase 7, reported as `unclassified` |
| `created_at`, `decided_at` | ts | |

Unique on `(run_id, item_id)` — makes resume idempotent.
Index on `(run_id, review_state)` — the resume query and the apply-eligibility query both use it.
Index on `(run_id, autonomy_state)` — the remainder ledger and the `distance_to_zero` query use it.

`autonomy_state` is orthogonal to both `status` and `review_state`. `status` is the user/action
lifecycle, `review_state` is the never-miss lifecycle, `autonomy_state` is the **autonomy** lifecycle:
whether the agent was permitted to act on this thread by itself, and if not, which rule stopped it.
Exactly one value per decision; the five values partition the run.

```
distance_to_zero = count(decisions WHERE run_id = :id
                                     AND autonomy_state = 'auto_act'
                                     AND status != 'applied')
```

`review_state` is orthogonal to `status`. `status` is the *user/action* lifecycle; `review_state` is
the *never-miss* lifecycle. A row is written `provisional` the instant its tier decides it and is
upgraded to `reviewed` by the second-pass reviewer + never-miss floor
(see [durable-resumable-runs](capabilities/durable-resumable-runs.md)). **Only `reviewed` rows are
eligible for apply/approve**, checked independently of `status` and not bypassable by `force=True`.

### `clusters`
| Field | Type | Notes |
|-------|------|-------|
| `id` PK, `user_id` FK, `run_id` FK | | |
| `kind` | str | `list` \| `sender` \| `domain` \| `category` |
| `label` | str | e.g. "Substack newsletters" |
| `item_count` | int | |
| `suggested_action` | str | |
| `min_confidence`, `avg_confidence` | float | |

### `llm_calls`
| Field | Type | Notes |
|-------|------|-------|
| `id` PK, `user_id` FK, `run_id` FK \| null | | |
| `purpose` | str | `classify` \| `deep_read` \| `review` \| `chat` \| `rule_proposal` \| `digest` \| `draft` |
| `model` | str | |
| `items_in_batch` | int | |
| `tokens_in`, `tokens_out` | int | |
| `cost_usd` | float | |
| `latency_ms` | int | |
| `created_at` | ts | |

### `user_settings`
| Field | Type | Default |
|-------|------|---------|
| `user_id` PK FK | | |
| `auto_act_threshold` | float | **`0.80`** (Phase 7; was `0.95`) — the **global** confidence bar at or above which the agent acts on its own. Overridden per category by `categories.auto_act_threshold`. Enforced from Phase 7 onward; before Phase 7 this column was persisted and rendered but read by no decision or apply path. Calibration in [drive-to-inbox-zero](capabilities/drive-to-inbox-zero.md#b-calibration--why-080-justified-against-the-measured-distribution) |
| `confidence_floor` | float | `0.75` — below this the agent never archives; a hard lower bound on the effective autonomy threshold |
| `dry_run` | bool | `true` (Phase 1: forced true) |
| `llm_model` | str | `nvidia/nemotron-3-nano-30b-a3b` |
| `digest_hour_local` | int | `8` |
| `timezone` | str | `UTC` |

---

## Phase 2 Entities

### `action_logs` — every mailbox mutation
| Field | Type | Notes |
|-------|------|-------|
| `id` PK, `user_id` FK, `decision_id` FK \| null | | |
| `operation` | str | `archive` \| `add_label` \| `remove_label` \| `create_filter` \| `create_draft` |
| `request_params` | json | exact parameters sent |
| `response` | json | provider response ids |
| `undo_token` | json | the inverse operation, sufficient to fully reverse this action |
| `undone_at` | ts \| null | |
| `created_at` | ts | |

Never contains a delete/trash operation — those are not valid values.

### `corrections` — every user correction, as a training signal
| Field | Type | Notes |
|-------|------|-------|
| `id` PK, `user_id` FK, `item_id` FK, `decision_id` FK \| null | | |
| `from_action`, `to_action` | str | |
| `source` | str | `dashboard` \| `observed_unarchive` |
| `note` | str \| null | |
| `created_at` | ts | |

### `vip_entries`
`id`, `user_id`, `kind` (`email` \| `domain` \| `keyword`), `value`, `created_at`.
Unique on `(user_id, kind, value)`. A match here can **never** be archived automatically.

### `priority_profiles`
`user_id` PK, `text` (the plain-English profile the user writes once), `updated_at`.

---

## Phase 3 Entities

### `chat_messages` — conversation memory for chat-to-rules
`id`, `user_id`, `role` (`user` \| `assistant`), `content`, `rule_id` (nullable — the rule this turn
produced or amended), `created_at`. Loaded in full per user on every chat turn.

### `digests`
`id`, `user_id`, `date`, `summary_markdown`, `hidden_count`, `counts` json, `created_at`.

### `rule_pack_templates` — shared starter packs (the only non-user-scoped table)
`id`, `key` (`founder` \| `engineer` \| `recruiter`), `name`, `description`, `rules` json.
Adopting a pack **copies** its rules into the user's `rules` table; the template is never referenced
at runtime, so per-user edits never affect other users.

---

## Phase 6 migration

Alembic revision `0005_decision_review_state` (single migration, required):

1. `ALTER TABLE decisions ADD COLUMN review_state VARCHAR NOT NULL DEFAULT 'provisional'`.
2. **Backfill:** every pre-existing row belongs to a run that already completed its reviewer pass, so
   set `review_state = 'reviewed'` for all existing rows — otherwise historical decisions would
   become un-appliable and un-undoable. New rows default to `provisional`.
3. `CREATE INDEX ix_decisions_run_review ON decisions (run_id, review_state)`.
4. No change is needed for `triage_runs.status` (a free-text/enum-by-convention column); the new
   `resumable` value is additive.

Downgrade drops the index and the column.

---

## Phase 7 migration

Alembic revision `0006_autonomy_policy` (single migration, required). **It touches real user rows —
`zero_inbox.db` holds 11,449 real decisions for 2 real accounts — so every statement is spelled out.**

1. `ALTER TABLE categories ADD COLUMN auto_act_threshold FLOAT NULL`.
2. **Seed the per-category overrides for existing users** (new users get them from `db.seed`):
   `UPDATE categories SET auto_act_threshold = 0.85 WHERE key IN ('outreach', 'receipts')`.
   All other categories stay NULL and inherit the global value.
2b. **Flip Receipts to `archive` for existing users** (new users get it from `DEFAULT_TAXONOMY` in
   `src/tools/rules.py`): `UPDATE categories SET default_action = 'archive' WHERE key = 'receipts' AND
   default_action = 'keep'`. Only the untouched seeded value is migrated — the `AND default_action =
   'keep'` guard means a user who already changed it is never overwritten. Reasoning and safety argument
   in [drive-to-inbox-zero](capabilities/drive-to-inbox-zero.md#decision--receipts-is-archive-was-an-open-question-now-decided).
   The downgrade reverses it (`'archive'` → `'keep'` for `key = 'receipts'`).
3. `ALTER TABLE decisions ADD COLUMN autonomy_state VARCHAR NULL`.
   **No backfill.** A pre-Phase-7 decision was made under a policy that did not exist; inventing an
   `autonomy_state` for it would fabricate history. Every such row is reported by the remainder ledger
   under the explicit `unclassified` bucket, never folded into a healthy one.
4. `CREATE INDEX ix_decisions_run_autonomy ON decisions (run_id, autonomy_state)`.
5. **Migrate the persisted global threshold** — this is the load-bearing statement:
   `UPDATE user_settings SET auto_act_threshold = 0.80 WHERE auto_act_threshold > 0.90`.
   - Rationale: a value above 0.90 was never read by any decision or apply code path, so it never
     expressed a real user preference — it was the unused shipped default (`0.95`), and it sits above
     the model's entire measured output range (ceiling ~0.94). Resetting it cannot regress behaviour,
     because no behaviour was ever derived from it. Leaving it would mean the fix silently does nothing
     for the very account that reported the problem (that account holds `0.95`).
   - Values at or below 0.90 are **left exactly as they are**. The second real account holds `0.75`;
     that is a deliberate "act on everything above the floor" setting and it survives untouched (the
     effective bar is then `max(0.75, confidence_floor) = 0.75`).
   - After this statement, **no `user_settings` row may hold a value above 0.90** — asserted by the
     migration test.
6. `ALTER COLUMN user_settings.auto_act_threshold SET DEFAULT 0.80` (and the ORM default in
   `src/db/models.py`, plus the hardcoded `0.95` fallbacks at `src/api/session.py:143` and `:187`, and
   `DEFAULT_AUTO_ACT_THRESHOLD` in `src/graph/persistence.py:139`).

Downgrade drops `ix_decisions_run_autonomy`, `decisions.autonomy_state` and
`categories.auto_act_threshold`, and restores the `0.95` column default. It does **not** restore
per-row `auto_act_threshold` values — step 5 is a deliberate one-way data migration and the downgrade
says so in a comment rather than guessing at the prior value.

---

## Phase 8 entities

### `user_sessions` — a session you can see is a session you can revoke

| Column | Type | Notes |
|--------|------|-------|
| `id` | text PK | uuid; carried in the session cookie as `sid` |
| `user_id` | text FK → `users.id` `ON DELETE CASCADE` | indexed |
| `created_at` | timestamptz | |
| `last_seen_at` | timestamptz | updated at most once per 60 s per session |
| `revoked_at` | timestamptz NULL | non-null ⇒ every request bearing this `sid` is `401` |
| `user_agent_summary` | text | **derived**, e.g. `"Chrome on macOS"` — the raw UA string is never stored and never returned |
| `ip_hash` | text NULL | HMAC-SHA256 of the client IP keyed by `AGENT_SECRET_KEY`, for "this looks like a new device" only. **The raw IP is never stored, never logged and never returned by any route.** |

Index: `ix_user_sessions_user_active (user_id, revoked_at)`.

No other table is added. The audit trail (`action_logs`), the per-user isolation, and every existing
user-scoped table are unchanged — the multi-tenant data model already exists and Phase 8 builds the
product around it rather than replacing it.

## Phase 8 migration

Alembic revision **`0007_sessions_and_mailbox_ownership`** (the next free revision — existing heads are
`0001`, `0002`, `0005`, `0006`). It touches a live database holding **~12,500 real decisions for 2 real
accounts**, so every statement is spelled out and every one is additive or guarded.

1. `CREATE TABLE user_sessions (...)` as above, plus `ix_user_sessions_user_active`. No backfill —
   existing cookies keep working via the legacy `uid`-only path (see
   [api.md](api.md#session-hardening-behaviour-change-no-new-route)) and gain a row on next use.
2. **Guarded ownership constraint.** Before creating it, the migration runs
   `SELECT channel, account_email, COUNT(DISTINCT user_id) FROM channel_accounts GROUP BY 1,2 HAVING COUNT(DISTINCT user_id) > 1`.
   - Zero rows → `CREATE UNIQUE INDEX uq_channel_account_global ON channel_accounts (channel, account_email)`.
   - Any rows → the migration **raises with the offending addresses named** and changes nothing. It
     must never resolve the conflict by deleting or reassigning a row: which human owns a mailbox is
     not a decision a migration gets to make.
   > **Assumed:** the live database has no such duplicate (2 accounts, 2 distinct addresses), so the
   > guard passes. The guard exists so that assumption is verified at migration time rather than
   > trusted.
3. `ALTER TABLE channel_accounts ADD COLUMN last_synced_at TIMESTAMP NULL` — surfaced on screen 22.
   No backfill; a NULL renders as *"not synced yet"*, never as a fabricated date.

Downgrade drops `last_synced_at`, `uq_channel_account_global`, `ix_user_sessions_user_active` and
`user_sessions`. It is fully reversible; there is no one-way data migration in this revision.

**Deletion semantics.** `DELETE /api/account` **does not rely on `ON DELETE CASCADE`.** The
`ON DELETE CASCADE` clauses above are declared, but **SQLite does not enforce them unless
`PRAGMA foreign_keys=ON` is set on every connection** — which this app does not do. Relying on the
FKs alone would therefore leave every child row orphaned on SQLite while appearing correct on
Postgres, and the account would read as deleted while its decisions, action logs, sessions and
connections survived. *Do not "simplify" this back to a cascade.*

Instead the route deletes explicitly, driven off the SQLAlchemy metadata rather than a hand-written
list (`src/api/account.py:_user_scoped_tables`): every table in `Base.metadata` that carries a
`user_id` column, in reverse `sorted_tables` order (children first), is deleted with
`WHERE user_id = :user_id`, then the `users` row itself. Two properties are load-bearing:

- **Metadata-driven, so it cannot miss a table** added in a later phase — a hand-written loop is
  exactly the thing that silently skips a new table two phases later.
- **Strictly user-scoped**, so it can never delete or orphan another user's rows: every statement
  carries the `user_id` predicate and no statement is unfiltered.

It performs **zero** Gmail calls: deleting the account does not un-archive, un-label or delete a
single message.

---

## Lifecycle

```
Item ingested ──▶ Decision(review_state=provisional, proposed | needs_your_call)   ← durable immediately
                      │
    align_to_category_default ──▶ keep→archive where the category says so          ← Phase 7; the ONLY
                      │            (only above the effective autonomy threshold)      keep→archive stage,
                      │                                                                and it runs FIRST
    second-pass reviewer + never-miss floor ──▶ Decision(review_state=reviewed)     ← now final
                      │
    mark_autonomy_state ──▶ Decision(autonomy_state ∈ auto_act | below_threshold |  ← Phase 7; why this
                      │      held_by_never_miss | category_keep | needs_your_call)     thread does/doesn't
                      │                                                                leave the inbox
                      │  (reviewer unavailable ──▶ review_state=review_failed, never applied)
                      ▼
                  Decision(proposed | needs_your_call)
                      │
      user approves ──┼──▶ Decision(approved) ──▶ [dry_run off] ActionLog(+undo_token) ──▶ Decision(applied)
      user rejects  ──┘                                    │                                        │
                      └──▶ Correction ──▶ SenderProfile.importance_score ↑                          undo
                              │  (Phase 2: signal only — no Rule row written here)                    │
                              └── read later by Phase 3 rule-mining ──▶ candidate Rule(proposed)      ▼
                                                                        Decision(undone), ActionLog.undone_at set
```

`rules` (line above, under Phase 1 Entities) is schema-present from Phase 1 for forward-compatibility but
is only ever **written to** starting Phase 3 (`rule-mining`/`chat-to-rules`); no Phase 1 or Phase 2 code
path inserts a `rules` row.

Retention: `items` and `decisions` are retained indefinitely (they carry no body). `llm_calls` are
retained for cost reporting. Nothing is ever hard-deleted by the agent.

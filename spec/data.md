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
| `default_action` | str | `keep` \| `archive` \| `digest` |
| `is_default`, `sort_order` | bool, int | |

Unique on `(user_id, key)`.

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
| `status` | str | `running` \| `completed` \| `failed` \| `cancelled` |
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
| `created_at`, `decided_at` | ts | |

Unique on `(run_id, item_id)` — makes resume idempotent.

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
| `auto_act_threshold` | float | `0.95` |
| `confidence_floor` | float | `0.75` — below this the agent never archives |
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

## Lifecycle

```
Item ingested ──▶ Decision(proposed | needs_your_call)
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

# Data Model

SQLite (SQLAlchemy 2.x), Postgres-compatible types only. **Every table except
`users` carries `user_id` (FK, indexed); all queries filter by it.** No email body
is stored anywhere; the largest stored text fragment is the ~90-char Gmail snippet.

## Entities

### users
`id` (uuid pk), `email`, `name`, `picture_url`, `created_at`.

### gmail_accounts
`id`, `user_id` (unique), `google_email`, `refresh_token_encrypted`,
`status` (`connected` | `needs_reconnect`), `connected_at`.
Lifecycle: a revoked/failed refresh flips status to `needs_reconnect`; reconnecting
overwrites the token and restores `connected`. Disconnect deletes the row.

### categories
`id`, `user_id`, `name`, `description` (guides the classifier), `rule`
(`label_only` | `label_and_archive`), `gmail_label_id` (nullable, created lazily),
`is_needs_review` (bool — the reserved "Needs review" category, not deletable),
`position`, `created_at`.
Seed defaults per user: Finance, Newsletters, Notifications, Personal, Shopping,
Travel, Needs review. Rename/merge/delete cascade rules: merge repoints
`thread_decisions.category_id` and relabels in Gmail via a normal audited run-less
mutation set that is itself undoable (Phase 2 depth; Phase 1 supports
rename/add/delete-if-unused and rule edit).

### runs
`id`, `user_id`, `status` (`running` | `completed` | `interrupted` | `undone`),
`trigger` (`clean_chunk`), `chunk_limit`, `started_at`, `finished_at`,
`threads_decided`, `counts_json` (per-category), `llm_calls`, `tokens_in`,
`tokens_out`, `est_cost_usd`, `fallback_events`, `interrupt_reason` (nullable),
`undone_at` (nullable).

### thread_decisions  — the per-thread decision index (never-redo + ledger core)
`id`, `user_id`, `run_id`, `gmail_thread_id` (unique per user), `sender`,
`subject`, `snippet`, `category_id`, `confidence`, `reason` (one line),
`needs_review` (bool), `source` (`llm` | `profile`), `undone` (bool),
`decided_at`.
Lifecycle: written before mutations; on run undo, `undone=true` and the row no
longer blocks re-decision (undone threads may be re-triaged by a later run).

### mutations — the audit trail
`id`, `user_id`, `run_id`, `gmail_thread_id`, `action`
(`add_label` | `remove_label` | `remove_inbox` | `restore_inbox`),
`label_name` (nullable), `reason`, `applied_at`, `undone_at` (nullable).
Undo = for each non-undone row of the run, newest first, apply the inverse and set
`undone_at`. Inverses: add_label↔remove_label, remove_inbox↔restore_inbox.

### run_events — persisted feed
`id`, `user_id`, `run_id`, `seq`, `type` (`chunk_loaded` | `decision` | `action` |
`fallback` | `cost_tick` | `run_interrupted` | `run_finished` | `undo_*`),
`sentence` (plain English), `detail_json` (reasoning, tokens, etc.), `created_at`.
Enables feed replay on reconnect and the run-card drill-down.

### llm_calls — cost ledger
`id`, `user_id`, `run_id`, `provider` (`nvidia` | `gemini`), `model`, `tokens_in`,
`tokens_out`, `latency_ms`, `est_cost_usd`, `was_fallback` (bool), `error`
(nullable), `created_at`. Per-user cumulative totals are aggregates over this table.

### sender_profiles (Phase 2)
`id`, `user_id`, `sender_address`, `category_id`, `hit_count`, `created_from`
(`auto` — N consistent decisions | `manual`), `created_at`.
Auto-created after 3 consistent high-confidence decisions for a sender.
> **Assumed:** threshold 3, editable later; profile deleted if its category is
> deleted.

### inbox_snapshots (mini-audit)
`id`, `user_id`, `total_inbox_threads`, `unread`, `oldest_days`,
`top_senders_json` (top 10: address + count), `category_tab_counts_json`,
`created_at`.

## Relationships

users 1—1 gmail_accounts; users 1—N categories/runs/thread_decisions/mutations/
run_events/llm_calls/sender_profiles/inbox_snapshots; runs 1—N thread_decisions,
mutations, run_events, llm_calls; categories 1—N thread_decisions, sender_profiles.

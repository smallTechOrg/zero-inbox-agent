"""initial zero-inbox schema

Multi-tenant triage schema. No table carries email body text — the only
content-bearing column is ``items.snippet_redacted`` (redacted, <= 200 chars).

Revision ID: 0001
Revises:
Create Date: 2026-01-01 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TS = sa.TIMESTAMP(timezone=True)


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "user_settings",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("auto_act_threshold", sa.Float(), nullable=False),
        sa.Column("confidence_floor", sa.Float(), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("llm_model", sa.Text(), nullable=False),
        sa.Column("digest_hour_local", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )

    op.create_table(
        "channel_accounts",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("channel", sa.Text(), nullable=False),
        sa.Column("account_email", sa.Text(), nullable=False),
        sa.Column("refresh_token_enc", sa.Text(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("history_id", sa.Text(), nullable=True),
        sa.Column("connected_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "channel", "account_email", name="uq_channel_account"),
    )
    op.create_index("ix_channel_accounts_user_id", "channel_accounts", ["user_id"])

    op.create_table(
        "items",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("channel_account_id", sa.Text(), nullable=False),
        sa.Column("external_thread_id", sa.Text(), nullable=False),
        sa.Column("external_message_ids", sa.JSON(), nullable=True),
        sa.Column("subject", sa.Text(), nullable=False),
        sa.Column("from_name", sa.Text(), nullable=False),
        sa.Column("from_email", sa.Text(), nullable=False),
        sa.Column("from_domain", sa.Text(), nullable=False),
        sa.Column("to_emails", sa.JSON(), nullable=True),
        sa.Column("cc_emails", sa.JSON(), nullable=True),
        sa.Column("list_id", sa.Text(), nullable=True),
        sa.Column("unsubscribe_url", sa.Text(), nullable=True),
        sa.Column("message_count", sa.Integer(), nullable=False),
        sa.Column("has_attachments", sa.Boolean(), nullable=False),
        # The ONLY content-bearing column in the schema: redacted, <= 200 chars.
        sa.Column("snippet_redacted", sa.Text(), nullable=False),
        sa.Column("internal_date", TS, nullable=True),
        sa.Column("is_unread", sa.Boolean(), nullable=False),
        sa.Column("channel_labels", sa.JSON(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["channel_account_id"], ["channel_accounts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "external_thread_id", name="uq_item_user_thread"),
    )
    op.create_index("ix_items_user_id", "items", ["user_id"])
    op.create_index("ix_items_channel_account_id", "items", ["channel_account_id"])
    op.create_index("ix_items_external_thread_id", "items", ["external_thread_id"])
    op.create_index("ix_items_from_email", "items", ["from_email"])
    op.create_index("ix_items_from_domain", "items", ["from_domain"])
    op.create_index("ix_items_list_id", "items", ["list_id"])

    op.create_table(
        "sender_profiles",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("sender_email", sa.Text(), nullable=False),
        sa.Column("sender_domain", sa.Text(), nullable=False),
        sa.Column("received_count", sa.Integer(), nullable=False),
        sa.Column("opened_count", sa.Integer(), nullable=False),
        sa.Column("replied_count", sa.Integer(), nullable=False),
        sa.Column("archived_by_user_count", sa.Integer(), nullable=False),
        sa.Column("ever_replied", sa.Boolean(), nullable=False),
        sa.Column("last_replied_at", TS, nullable=True),
        sa.Column("last_seen_at", TS, nullable=True),
        sa.Column("importance_score", sa.Float(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "sender_email", name="uq_sender_profile"),
    )
    op.create_index("ix_sender_profiles_user_id", "sender_profiles", ["user_id"])
    op.create_index("ix_sender_profiles_sender_domain", "sender_profiles", ["sender_domain"])

    op.create_table(
        "categories",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("channel_label_name", sa.Text(), nullable=False),
        sa.Column("channel_label_id", sa.Text(), nullable=True),
        sa.Column("default_action", sa.Text(), nullable=False),
        sa.Column("is_default", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "key", name="uq_category_user_key"),
    )
    op.create_index("ix_categories_user_id", "categories", ["user_id"])

    op.create_table(
        "rules",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("matcher", sa.JSON(), nullable=False),
        sa.Column("action", sa.JSON(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("match_count", sa.Integer(), nullable=False),
        sa.Column("channel_filter_id", sa.Text(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("promoted_at", TS, nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_rules_user_id", "rules", ["user_id"])
    op.create_index("ix_rules_status", "rules", ["status"])

    op.create_table(
        "triage_runs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("channel_account_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("range_start", TS, nullable=True),
        sa.Column("range_end", TS, nullable=True),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("items_total", sa.Integer(), nullable=False),
        sa.Column("items_decided", sa.Integer(), nullable=False),
        sa.Column("counts", sa.JSON(), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", TS, nullable=False),
        sa.Column("finished_at", TS, nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["channel_account_id"], ["channel_accounts.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_triage_runs_user_id", "triage_runs", ["user_id"])
    op.create_index("ix_triage_runs_channel_account_id", "triage_runs", ["channel_account_id"])
    op.create_index("ix_triage_runs_status", "triage_runs", ["status"])

    op.create_table(
        "clusters",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("item_count", sa.Integer(), nullable=False),
        sa.Column("suggested_action", sa.Text(), nullable=False),
        sa.Column("min_confidence", sa.Float(), nullable=False),
        sa.Column("avg_confidence", sa.Float(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["triage_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_clusters_user_id", "clusters", ["user_id"])
    op.create_index("ix_clusters_run_id", "clusters", ["run_id"])

    op.create_table(
        "decisions",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("item_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("cluster_id", sa.Text(), nullable=True),
        sa.Column("category_id", sa.Text(), nullable=True),
        sa.Column("proposed_action", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("reasoning", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=False),
        sa.Column("rule_id", sa.Text(), nullable=True),
        sa.Column("time_sensitive", sa.Boolean(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("decided_at", TS, nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["triage_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["cluster_id"], ["clusters.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["category_id"], ["categories.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["rule_id"], ["rules.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "item_id", name="uq_decision_run_item"),
    )
    op.create_index("ix_decisions_user_id", "decisions", ["user_id"])
    op.create_index("ix_decisions_item_id", "decisions", ["item_id"])
    op.create_index("ix_decisions_run_id", "decisions", ["run_id"])
    op.create_index("ix_decisions_cluster_id", "decisions", ["cluster_id"])
    op.create_index("ix_decisions_decided_by", "decisions", ["decided_by"])
    op.create_index("ix_decisions_status", "decisions", ["status"])

    op.create_table(
        "llm_calls",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=True),
        sa.Column("purpose", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("items_in_batch", sa.Integer(), nullable=False),
        sa.Column("tokens_in", sa.Integer(), nullable=False),
        sa.Column("tokens_out", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["triage_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_llm_calls_user_id", "llm_calls", ["user_id"])
    op.create_index("ix_llm_calls_run_id", "llm_calls", ["run_id"])

    op.create_table(
        "action_logs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("decision_id", sa.Text(), nullable=True),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("request_params", sa.JSON(), nullable=False),
        sa.Column("response", sa.JSON(), nullable=True),
        sa.Column("undo_token", sa.JSON(), nullable=True),
        sa.Column("undone_at", TS, nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["decision_id"], ["decisions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_action_logs_user_id", "action_logs", ["user_id"])
    op.create_index("ix_action_logs_decision_id", "action_logs", ["decision_id"])

    op.create_table(
        "corrections",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("item_id", sa.Text(), nullable=False),
        sa.Column("decision_id", sa.Text(), nullable=True),
        sa.Column("from_action", sa.Text(), nullable=False),
        sa.Column("to_action", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["decision_id"], ["decisions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_corrections_user_id", "corrections", ["user_id"])
    op.create_index("ix_corrections_item_id", "corrections", ["item_id"])


def downgrade() -> None:
    for table in (
        "corrections",
        "action_logs",
        "llm_calls",
        "decisions",
        "clusters",
        "triage_runs",
        "rules",
        "categories",
        "sender_profiles",
        "items",
        "channel_accounts",
        "user_settings",
        "users",
    ):
        op.drop_table(table)

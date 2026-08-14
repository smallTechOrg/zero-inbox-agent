"""SQLAlchemy 2.0 declarative schema for Zero Inbox Agent.

Multi-tenant by construction: every user-scoped table carries ``user_id`` and is
indexed on it, so every query can be isolated per user.

HARD INVARIANT — **no email body text is ever persisted**. The only
content-bearing column in the whole schema is ``items.snippet_redacted``
(<= 200 chars, already passed through ``src/tools/redact.py``), plus
model-generated ``decisions.reasoning``. Enforced by
``tests/unit/db/test_no_body_columns.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import (
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Text,
    TIMESTAMP,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# --- Redaction / privacy constants -------------------------------------------------

SNIPPET_MAX_CHARS = 200
"""Maximum length of the single content-bearing column in the schema."""

#: Substrings that may never appear in a column name. A column whose name
#: matches one of these would imply body text is being persisted.
FORBIDDEN_COLUMN_SUBSTRINGS: tuple[str, ...] = (
    "body",
    "body_text",
    "body_html",
    "html",
    "plain_text",
    "raw_message",
    "raw_email",
    "full_text",
    "message_body",
    "payload",
)


def _uuid() -> str:
    return str(uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(**kw):
    return mapped_column(TIMESTAMP(timezone=True), **kw)


class Base(DeclarativeBase):
    pass


# --- Identity -----------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(Text, nullable=False, unique=True, index=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = _ts(nullable=False, default=_now)


class UserSettings(Base):
    __tablename__ = "user_settings"

    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    #: Phase 7: the **global** confidence bar at or above which the agent acts on
    #: its own — finally read by a real code path (``graph.autonomy``). The
    #: default dropped from 0.95 to 0.80 because 0.95 sits above the model's
    #: entire measured output range (~0.94 ceiling); see spec/data.md.
    auto_act_threshold: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.80, server_default="0.8"
    )
    confidence_floor: Mapped[float] = mapped_column(Float, nullable=False, default=0.75)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    llm_model: Mapped[str] = mapped_column(Text, nullable=False, default="nvidia/nemotron-3-nano-30b-a3b")
    digest_hour_local: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    timezone: Mapped[str] = mapped_column(Text, nullable=False, default="UTC")
    updated_at: Mapped[datetime] = _ts(nullable=False, default=_now, onupdate=_now)


class ChannelAccount(Base):
    """A connected mailbox. Channel-agnostic; ``gmail`` is the only v1 value."""

    __tablename__ = "channel_accounts"
    __table_args__ = (
        UniqueConstraint("user_id", "channel", "account_email", name="uq_channel_account"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel: Mapped[str] = mapped_column(Text, nullable=False, default="gmail")
    account_email: Mapped[str] = mapped_column(Text, nullable=False)
    # Fernet ciphertext. Never logged, never returned by any API route.
    refresh_token_enc: Mapped[str] = mapped_column(Text, nullable=False)
    scopes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="connected")
    history_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    connected_at: Mapped[datetime] = _ts(nullable=False, default=_now)


# --- Channel-agnostic content -------------------------------------------------------


class Item(Base):
    """A channel-agnostic thread. Headers + ids + a redacted snippet only."""

    __tablename__ = "items"
    __table_args__ = (
        UniqueConstraint("user_id", "external_thread_id", name="uq_item_user_thread"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel_account_id: Mapped[str] = mapped_column(
        Text, ForeignKey("channel_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    external_thread_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    external_message_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    subject: Mapped[str] = mapped_column(Text, nullable=False, default="")
    from_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    from_email: Mapped[str] = mapped_column(Text, nullable=False, default="", index=True)
    from_domain: Mapped[str] = mapped_column(Text, nullable=False, default="", index=True)
    to_emails: Mapped[list | None] = mapped_column(JSON, nullable=True)
    cc_emails: Mapped[list | None] = mapped_column(JSON, nullable=True)
    list_id: Mapped[str | None] = mapped_column(Text, nullable=True, index=True)
    unsubscribe_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    has_attachments: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    #: The ONLY content-bearing column in the schema: <= 200 chars, redacted.
    snippet_redacted: Mapped[str] = mapped_column(Text, nullable=False, default="")
    internal_date: Mapped[datetime | None] = _ts(nullable=True)
    is_unread: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    channel_labels: Mapped[list | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = _ts(nullable=False, default=_now)


class SenderProfile(Base):
    """Per-user, per-sender evidence — carries the never-miss reply-history signal."""

    __tablename__ = "sender_profiles"
    __table_args__ = (
        UniqueConstraint("user_id", "sender_email", name="uq_sender_profile"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sender_email: Mapped[str] = mapped_column(Text, nullable=False)
    sender_domain: Mapped[str] = mapped_column(Text, nullable=False, default="", index=True)
    received_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    opened_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    replied_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    archived_by_user_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ever_replied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    last_replied_at: Mapped[datetime | None] = _ts(nullable=True)
    last_seen_at: Mapped[datetime | None] = _ts(nullable=True)
    importance_score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)


# --- Taxonomy & rules ---------------------------------------------------------------


class Category(Base):
    """Materialises 1:1 as a real channel label (Gmail label in v1)."""

    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_category_user_key"),)

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    channel_label_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    channel_label_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    default_action: Mapped[str] = mapped_column(Text, nullable=False, default="keep")
    #: Phase 7: per-category autonomy bar. NULL inherits
    #: ``user_settings.auto_act_threshold``, which is what keeps the global
    #: slider load-bearing. Inert while ``default_action = keep``.
    auto_act_threshold: Mapped[float | None] = mapped_column(Float, nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="deterministic")
    source: Mapped[str] = mapped_column(Text, nullable=False, default="seed_pack")
    matcher: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    action: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="proposed", index=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    match_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    channel_filter_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts(nullable=False, default=_now)
    promoted_at: Mapped[datetime | None] = _ts(nullable=True)


# --- Runs, clusters, decisions ------------------------------------------------------


class TriageRun(Base):
    __tablename__ = "triage_runs"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    channel_account_id: Mapped[str] = mapped_column(
        Text, ForeignKey("channel_accounts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="incremental")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="running", index=True)
    dry_run: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    range_start: Mapped[datetime | None] = _ts(nullable=True)
    range_end: Mapped[datetime | None] = _ts(nullable=True)
    cursor: Mapped[str | None] = mapped_column(Text, nullable=True)
    items_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    items_decided: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    counts: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = _ts(nullable=False, default=_now)
    finished_at: Mapped[datetime | None] = _ts(nullable=True)


class Cluster(Base):
    __tablename__ = "clusters"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("triage_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="sender")
    label: Mapped[str] = mapped_column(Text, nullable=False, default="")
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    suggested_action: Mapped[str] = mapped_column(Text, nullable=False, default="keep")
    min_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    avg_confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = _ts(nullable=False, default=_now)


class Decision(Base):
    """The audit record: category + confidence + reasoning + which tier fired."""

    __tablename__ = "decisions"
    __table_args__ = (
        UniqueConstraint("run_id", "item_id", name="uq_decision_run_item"),
        Index("ix_decisions_run_review", "run_id", "review_state"),
        Index("ix_decisions_run_autonomy", "run_id", "autonomy_state"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id: Mapped[str] = mapped_column(
        Text, ForeignKey("items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[str] = mapped_column(
        Text, ForeignKey("triage_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cluster_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("clusters.id", ondelete="SET NULL"), nullable=True, index=True
    )
    category_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("categories.id", ondelete="SET NULL"), nullable=True
    )
    proposed_action: Mapped[str] = mapped_column(Text, nullable=False, default="keep")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: Human-readable justification produced by the deciding tier. Never body text.
    reasoning: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: which-tier-fired: rule | sender_history | llm | llm_deep | reviewer | error
    decided_by: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    rule_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("rules.id", ondelete="SET NULL"), nullable=True
    )
    time_sensitive: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="proposed", index=True)
    #: The never-miss finality gate: provisional | reviewed | review_failed.
    #: A row is durable the instant its tier decides it (``provisional``) and only
    #: becomes appliable once the second-pass reviewer has upgraded it to
    #: ``reviewed``. Orthogonal to ``status`` — see spec/data.md.
    review_state: Mapped[str] = mapped_column(
        Text, nullable=False, default="provisional", server_default="provisional"
    )
    #: Phase 7: the **autonomy** lifecycle — why this thread is or is not leaving
    #: the inbox. One of graph.autonomy.AUTONOMY_STATES. Orthogonal to both
    #: ``status`` and ``review_state``. NULL only on rows written before Phase 7,
    #: reported by the remainder ledger as ``unclassified`` — never backfilled,
    #: because inventing a state for a decision made under a policy that did not
    #: exist would fabricate history.
    autonomy_state: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts(nullable=False, default=_now)
    decided_at: Mapped[datetime | None] = _ts(nullable=True)


class LLMCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    run_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("triage_runs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    purpose: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    items_in_batch: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _ts(nullable=False, default=_now)


# --- Audit trail --------------------------------------------------------------------


class ActionLog(Base):
    """Every mailbox mutation, with the inverse operation needed to undo it.

    ``operation`` never takes a destructive value — delete/trash/spam are not
    valid operations anywhere in this system.
    """

    __tablename__ = "action_logs"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    decision_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("decisions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    operation: Mapped[str] = mapped_column(Text, nullable=False)
    request_params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    undo_token: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    undone_at: Mapped[datetime | None] = _ts(nullable=True)
    created_at: Mapped[datetime] = _ts(nullable=False, default=_now)


ALLOWED_ACTION_OPERATIONS: frozenset[str] = frozenset(
    {"archive", "add_label", "remove_label", "create_filter", "create_draft"}
)
"""Whitelist of mailbox mutations. Nothing destructive is representable."""


class Correction(Base):
    """A user correction — the training signal for sender importance and rules."""

    __tablename__ = "corrections"

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    item_id: Mapped[str] = mapped_column(
        Text, ForeignKey("items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    decision_id: Mapped[str | None] = mapped_column(
        Text, ForeignKey("decisions.id", ondelete="SET NULL"), nullable=True
    )
    from_action: Mapped[str] = mapped_column(Text, nullable=False)
    to_action: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False, default="dashboard")
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts(nullable=False, default=_now)


class VipEntry(Base):
    """The never-hide list. A match here can never be archived automatically."""

    __tablename__ = "vip_entries"
    __table_args__ = (
        UniqueConstraint("user_id", "kind", "value", name="uq_vip_entry"),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # email | domain | keyword
    value: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _ts(nullable=False, default=_now)


class PriorityProfile(Base):
    """The plain-English priorities profile, written once by the user and injected
    verbatim into the classifier and reviewer prompts. Never rewritten by the agent."""

    __tablename__ = "priority_profiles"

    user_id: Mapped[str] = mapped_column(
        Text, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = _ts(nullable=False, default=_now, onupdate=_now)


#: Tables that are NOT scoped to a single user. Empty in Phase 1 — shared
#: rule-pack templates (Phase 3) will be the only entry.
NON_USER_SCOPED_TABLES: frozenset[str] = frozenset()


__all__ = [
    "Base",
    "User",
    "UserSettings",
    "ChannelAccount",
    "Item",
    "SenderProfile",
    "Category",
    "Rule",
    "TriageRun",
    "Cluster",
    "Decision",
    "LLMCall",
    "ActionLog",
    "Correction",
    "VipEntry",
    "PriorityProfile",
    "ALLOWED_ACTION_OPERATIONS",
    "FORBIDDEN_COLUMN_SUBSTRINGS",
    "NON_USER_SCOPED_TABLES",
    "SNIPPET_MAX_CHARS",
]

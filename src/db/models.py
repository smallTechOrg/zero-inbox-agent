"""SQLAlchemy 2.0 models — the full Zero Inbox schema (spec/data.md).

Multi-user isolation is structural: **every table except `users` carries an
indexed `user_id` FK**, and every query filters by it. No email body is stored
anywhere — the largest text fragment is the ~90-char Gmail snippet. Only
Postgres-compatible types are used (String/Text/Integer/Float/Boolean/DateTime/
JSON) so the documented SQLite→Postgres migration is mechanical.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _uuid() -> str:
    return str(uuid.uuid4())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(320), unique=True, nullable=False, index=True)
    name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    picture_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class GmailAccount(Base):
    """1—1 with users. Disconnect deletes the row; a failed refresh flips
    status to `needs_reconnect`; reconnect overwrites the token."""

    __tablename__ = "gmail_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), unique=True, nullable=False, index=True
    )
    google_email: Mapped[str] = mapped_column(String(320), nullable=False)
    refresh_token_encrypted: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="connected")
    connected_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class Category(Base):
    __tablename__ = "categories"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_categories_user_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    rule: Mapped[str] = mapped_column(String(32), nullable=False, default="label_only")
    gmail_label_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # The reserved "Needs review" category — not deletable (409 at the API).
    is_needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class Run(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="running")
    trigger: Mapped[str] = mapped_column(String(32), nullable=False, default="clean_chunk")
    chunk_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    threads_decided: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    counts_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    llm_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    est_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    fallback_events: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    interrupt_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ThreadDecision(Base):
    """The per-thread decision index — never-redo + ledger core.

    `gmail_thread_id` is unique **per user**: re-triaging an undone thread
    UPDATES this row (repointing run_id, clearing `undone`) rather than
    inserting a second one.
    """

    __tablename__ = "thread_decisions"
    __table_args__ = (
        UniqueConstraint("user_id", "gmail_thread_id", name="uq_thread_decisions_user_thread"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, index=True
    )
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.id"), nullable=False, index=True
    )
    gmail_thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    sender: Mapped[str] = mapped_column(String(500), nullable=False)
    subject: Mapped[str] = mapped_column(String(1000), nullable=False, default="")
    snippet: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    category_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("categories.id"), nullable=False, index=True
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="llm")
    undone: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class Mutation(Base):
    """The audit trail. Written BEFORE the Gmail call it describes.

    Undo = for each non-undone row of the run, newest first, apply the inverse
    (see domain.enums.MUTATION_INVERSE) and stamp `undone_at`.
    """

    __tablename__ = "mutations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, index=True
    )
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.id"), nullable=False, index=True
    )
    gmail_thread_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    label_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    applied_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RunEvent(Base):
    """Persisted feed events — enable SSE replay on reconnect (`?after_seq`)."""

    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_run_events_run_seq"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, index=True
    )
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.id"), nullable=False, index=True
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    type: Mapped[str] = mapped_column(String(32), nullable=False)
    sentence: Mapped[str] = mapped_column(Text, nullable=False)
    detail_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class LlmCall(Base):
    """Cost ledger — one row per provider call; cumulative totals are aggregates."""

    __tablename__ = "llm_calls"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, index=True
    )
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("runs.id"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(16), nullable=False)
    model: Mapped[str] = mapped_column(String(120), nullable=False)
    tokens_in: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    est_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    was_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class SenderProfile(Base):
    """Phase 2 — repeat-sender LLM bypass. Schema lands day one so the never-redo
    index and Phase-2 code share one migration-free database."""

    __tablename__ = "sender_profiles"
    __table_args__ = (
        UniqueConstraint("user_id", "sender_address", name="uq_sender_profiles_user_sender"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, index=True
    )
    sender_address: Mapped[str] = mapped_column(String(320), nullable=False)
    category_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("categories.id"), nullable=False, index=True
    )
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_from: Mapped[str] = mapped_column(String(16), nullable=False, default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)


class InboxSnapshot(Base):
    """The mini-audit result (read-only inbox stats)."""

    __tablename__ = "inbox_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id"), nullable=False, index=True
    )
    total_inbox_threads: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unread: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    oldest_days: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    top_senders_json: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    category_tab_counts_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, nullable=False)

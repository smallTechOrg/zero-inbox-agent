"""The channel-agnostic adapter interface.

The triage core never sees a Gmail type. It sees :class:`ChannelItem` — a
thread-level, body-free shape whose fields are exactly the content columns of
the ``items`` table (see spec/data.md), so persistence is a straight
``Item(**channel_item.model_dump())``.

Gmail is the only implementation shipped in v1; a second adapter fills the same
shape.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

SNIPPET_MAX_CHARS = 200


class ChannelError(RuntimeError):
    """Base class for every error raised by a channel adapter."""


class DryRunViolation(ChannelError):
    """A mutation was attempted while the agent is in dry-run mode.

    Phase 1 is dry-run for the whole application: every mutation method on
    every adapter raises this.
    """


class ReauthRequired(ChannelError):
    """The stored OAuth credentials are missing/invalid — the user must reconnect."""


class RateLimited(ChannelError):
    """The channel rejected the request for rate reasons after all retries."""


class ChannelItem(BaseModel):
    """One thread, normalized. Never carries body text."""

    external_thread_id: str
    external_message_ids: list[str] = Field(default_factory=list)
    subject: str = ""
    from_name: str = ""
    from_email: str = ""
    from_domain: str = ""
    to_emails: list[str] = Field(default_factory=list)
    cc_emails: list[str] = Field(default_factory=list)
    list_id: str | None = None
    unsubscribe_url: str | None = None
    message_count: int = 1
    has_attachments: bool = False
    snippet_redacted: str = ""
    internal_date: datetime
    is_unread: bool = False
    channel_labels: list[str] = Field(default_factory=list)

    @field_validator("snippet_redacted")
    @classmethod
    def _cap_snippet(cls, value: str) -> str:
        return (value or "")[:SNIPPET_MAX_CHARS]


class SenderSignal(BaseModel):
    """Per-sender evidence harvested from the channel (the never-miss signal)."""

    sender_email: str
    sender_domain: str = ""
    replied_count: int = 0
    ever_replied: bool = False
    last_replied_at: datetime | None = None


class ChannelAdapter(ABC):
    """Read + mutate surface every channel must provide.

    Read methods are live in Phase 1. Mutation methods exist so the interface is
    complete and callers are type-safe, but every implementation raises
    :class:`DryRunViolation` until dry-run is turned off in Phase 2.
    """

    channel: str = "unknown"

    # --- read ---------------------------------------------------------
    @abstractmethod
    def account_email(self) -> str:
        """The connected mailbox address, confirmed against the provider."""

    @abstractmethod
    def list_threads(self, *, limit: int = 200, query: str | None = None) -> list[ChannelItem]:
        """Most recent inbox threads, newest first, headers/snippet only."""

    @abstractmethod
    def fetch_thread_body(self, external_thread_id: str) -> str:
        """Full thread text, **in memory only** — never persisted."""

    @abstractmethod
    def sender_history(self, *, limit: int = 500) -> dict[str, SenderSignal]:
        """Addresses the user has replied to, keyed by lowercased email."""

    @abstractmethod
    def list_labels(self) -> list[dict]:
        """`[{"id": ..., "name": ...}]` for the account's labels/folders."""

    # --- mutate (Phase 2+; Phase 1 raises DryRunViolation) -------------
    @abstractmethod
    def archive_thread(self, external_thread_id: str) -> dict: ...

    @abstractmethod
    def add_labels(self, external_thread_id: str, label_ids: list[str]) -> dict: ...

    @abstractmethod
    def remove_labels(self, external_thread_id: str, label_ids: list[str]) -> dict: ...

    @abstractmethod
    def create_label(self, name: str) -> dict: ...

    @abstractmethod
    def create_filter(self, criteria: dict, action: dict) -> dict: ...

    @abstractmethod
    def create_draft(self, external_thread_id: str, body: str) -> dict: ...

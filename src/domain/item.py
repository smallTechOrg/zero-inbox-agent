"""The channel-agnostic thread the whole triage core operates on.

Carries headers, identifiers and a redacted snippet — never body text.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from domain.enums import Channel

SNIPPET_MAX_CHARS = 200


class Item(BaseModel):
    """A normalized thread. Gmail threads map onto this; a future adapter fills the same fields."""

    model_config = ConfigDict(from_attributes=True)

    id: str | None = None
    user_id: str
    channel_account_id: str
    channel: Channel = Channel.GMAIL
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

    #: Already passed through ``src/tools/redact.py``; truncated to 200 chars.
    snippet_redacted: str = ""
    internal_date: datetime | None = None
    is_unread: bool = False
    channel_labels: list[str] = Field(default_factory=list)

    @field_validator("snippet_redacted")
    @classmethod
    def _cap_snippet(cls, v: str) -> str:
        """Hard-cap the only content-bearing field. Never store more than a snippet."""
        return (v or "")[:SNIPPET_MAX_CHARS]

    @field_validator("from_domain")
    @classmethod
    def _normalize_domain(cls, v: str) -> str:
        return (v or "").strip().lower().lstrip("@")

    @classmethod
    def domain_of(cls, email: str) -> str:
        """Extract the lowercase domain from an address; '' when unparseable."""
        if not email or "@" not in email:
            return ""
        return email.rsplit("@", 1)[1].strip().lower()

    @property
    def is_mailing_list(self) -> bool:
        """``List-Id`` is the strongest newsletter signal."""
        return bool(self.list_id)


__all__ = ["Item", "SNIPPET_MAX_CHARS"]

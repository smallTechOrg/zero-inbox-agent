"""ClassifierView — the ONLY shape that can reach an LLM prompt.

Privacy boundary (spec/architecture.md "Privacy Boundary"): the only fields ever
serialized into an LLM prompt are sender address + display name, subject,
`List-Unsubscribe` presence, `Reply-To`, Gmail category tab, thread message count,
has-user-replied flag, and the ~90-char Gmail snippet. This dataclass holds exactly
those fields (plus the thread id used to key results). ``frozen=True`` +
``slots=True`` means an email body is *unrepresentable*: there is no attribute to
put it in, no ``__dict__`` to smuggle it through, and ``classify_batch()`` refuses
anything that is not a ``ClassifierView``.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

__all__ = ["ClassifierView", "SNIPPET_MAX_CHARS", "ALLOWED_PROMPT_FIELDS"]

SNIPPET_MAX_CHARS = 90

#: The complete, closed set of fields allowed into a prompt. A unit test asserts
#: the dataclass exposes exactly this set — adding a field is a deliberate,
#: reviewed act, never an accident.
ALLOWED_PROMPT_FIELDS = frozenset(
    {
        "thread_id",
        "sender_address",
        "sender_name",
        "subject",
        "has_list_unsubscribe",
        "reply_to",
        "gmail_category",
        "thread_message_count",
        "has_user_replied",
        "snippet",
    }
)


@dataclass(frozen=True, slots=True)
class ClassifierView:
    """Privacy-bounded view of one Gmail thread, for classification only."""

    thread_id: str
    sender_address: str
    sender_name: str = ""
    subject: str = ""
    has_list_unsubscribe: bool = False
    reply_to: str = ""
    gmail_category: str = ""
    thread_message_count: int = 1
    has_user_replied: bool = False
    snippet: str = ""

    def __post_init__(self) -> None:
        if not (self.thread_id or "").strip():
            raise ValueError("ClassifierView.thread_id must be a non-empty string")
        # Enforce the ~90-char snippet bound at construction: even a caller that
        # pastes a whole body into `snippet` cannot ship more than 90 chars.
        if len(self.snippet) > SNIPPET_MAX_CHARS:
            object.__setattr__(self, "snippet", self.snippet[:SNIPPET_MAX_CHARS])

    def to_prompt_dict(self) -> dict[str, Any]:
        """The exact JSON object serialized into the classification prompt."""
        return {f.name: getattr(self, f.name) for f in fields(self)}

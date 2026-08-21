"""ClassifierView — the ONLY shape that may reach an LLM prompt.

Privacy boundary (spec/architecture.md): the email body is unrepresentable in
this type. `slots=True` + `frozen=True` means no extra attribute (e.g. `body`)
can ever be attached to an instance, and the field list below is the exhaustive
set of what a prompt builder can serialize.
"""

from dataclasses import dataclass, fields

#: ~90 chars is what Gmail returns; enforce a hard ceiling so a full body can
#: never be smuggled through the snippet field.
SNIPPET_MAX_CHARS = 200


@dataclass(frozen=True, slots=True)
class ClassifierView:
    gmail_thread_id: str
    sender_address: str
    sender_name: str
    subject: str
    list_unsubscribe_present: bool
    reply_to: str | None
    category_tab: str | None          # Gmail tab: primary/social/promotions/updates/forums
    thread_message_count: int
    has_user_replied: bool
    snippet: str                      # the ~90-char Gmail snippet, truncated hard

    def __post_init__(self) -> None:
        if len(self.snippet) > SNIPPET_MAX_CHARS:
            object.__setattr__(self, "snippet", self.snippet[:SNIPPET_MAX_CHARS])


#: The exhaustive, ordered field list — asserted by the privacy unit test.
CLASSIFIER_VIEW_FIELDS: tuple[str, ...] = tuple(f.name for f in fields(ClassifierView))

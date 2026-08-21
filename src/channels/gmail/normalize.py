"""Gmail thread payload → `ChannelItem`.

Pure functions, no I/O. One `Item` per **thread**, never per message: the latest
message supplies the displayed sender/subject/date and `message_count` covers
the whole thread. Body data is never read here.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timezone
from email.utils import getaddresses, parseaddr

from channels.base import SNIPPET_MAX_CHARS, ChannelError, ChannelItem

Redactor = Callable[[str], str]

WANTED_HEADERS = [
    "From",
    "To",
    "Cc",
    "Subject",
    "Date",
    "List-Id",
    "List-Unsubscribe",
]

_LIST_ID_RE = re.compile(r"<([^>]+)>")
_HTTP_URL_RE = re.compile(r"<(https?://[^>]+)>")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def default_redactor() -> Redactor:
    """The project redactor if the tools slice has landed, else identity.

    `src/tools/redact.py` is owned by the triage-graph slice. Ingestion callers
    normally pass `redactor=` explicitly; this keeps the adapter usable either way.
    """
    try:
        from tools.redact import redact  # type: ignore

        return redact
    except Exception:
        return lambda text: text


def headers_of(message: dict) -> dict[str, str]:
    raw = (message.get("payload") or {}).get("headers") or []
    return {h.get("name", "").lower(): h.get("value", "") for h in raw}


def addresses_of(value: str) -> list[str]:
    return [addr.lower() for _, addr in getaddresses([value or ""]) if addr]


def internal_date_of(message: dict) -> datetime:
    raw = message.get("internalDate")
    try:
        return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
    except (TypeError, ValueError):
        return _EPOCH


def _has_attachment(message: dict) -> bool:
    def walk(part: dict) -> bool:
        if (part.get("filename") or "").strip():
            return True
        return any(walk(child) for child in part.get("parts") or [])

    return walk(message.get("payload") or {})


def _list_id(raw: str) -> str | None:
    if not raw:
        return None
    match = _LIST_ID_RE.search(raw)
    return (match.group(1) if match else raw).strip() or None


def _unsubscribe_url(raw: str) -> str | None:
    if not raw:
        return None
    match = _HTTP_URL_RE.search(raw)
    return match.group(1) if match else None


def normalize_thread(thread: dict, *, redactor: Redactor | None = None) -> ChannelItem:
    """Normalize one Gmail thread (``format=metadata``) into a `ChannelItem`."""
    thread_id = (thread or {}).get("id")
    if not thread_id:
        raise ChannelError("Gmail thread payload has no id")
    messages = (thread or {}).get("messages") or []
    if not messages:
        raise ChannelError(f"Gmail thread {thread_id} contains no messages")

    ordered = sorted(messages, key=internal_date_of)
    latest = ordered[-1]
    headers = headers_of(latest)

    from_name, from_email = parseaddr(headers.get("from", ""))
    from_email = from_email.lower()
    from_domain = from_email.split("@")[-1] if "@" in from_email else ""

    labels: list[str] = []
    for message in ordered:
        for label in message.get("labelIds") or []:
            if label not in labels:
                labels.append(label)

    redact = redactor or default_redactor()
    # Redact first, then truncate — truncating first could split a secret and
    # leave a readable fragment.
    snippet = redact(latest.get("snippet") or "")[:SNIPPET_MAX_CHARS]

    return ChannelItem(
        external_thread_id=thread_id,
        external_message_ids=[m.get("id", "") for m in ordered],
        subject=headers.get("subject", ""),
        from_name=from_name,
        from_email=from_email,
        from_domain=from_domain,
        to_emails=addresses_of(headers.get("to", "")),
        cc_emails=addresses_of(headers.get("cc", "")),
        list_id=_list_id(headers.get("list-id", "")),
        unsubscribe_url=_unsubscribe_url(headers.get("list-unsubscribe", "")),
        message_count=len(ordered),
        has_attachments=any(_has_attachment(m) for m in ordered),
        snippet_redacted=snippet,
        internal_date=internal_date_of(latest),
        is_unread="UNREAD" in labels,
        channel_labels=labels,
    )

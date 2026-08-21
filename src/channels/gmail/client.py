"""Gmail read-layer entry point (auth-gmail contract, spec/roadmap.md Phase 1).

``list_inbox_threads(user_id, limit=…)`` is THE seam the triage graph reads the
inbox through (``graph/runner._fetch_inbox_threads``) and the seam integration
tests re-point at a synthetic inbox (``tests/fixtures/fake_gmail.py``). It
returns newest-first INBOX threads as privacy-bounded mappings whose keys match
``llm.views.ALLOWED_PROMPT_FIELDS`` — never a message body.
"""

from __future__ import annotations

from typing import Any

_GMAIL_CATEGORY_PREFIX = "CATEGORY_"


def _view_mapping(item) -> dict[str, Any]:
    """One ChannelItem → the ClassifierView mapping (closed prompt-field set)."""
    labels = list(getattr(item, "channel_labels", None) or [])
    category = next(
        (
            label[len(_GMAIL_CATEGORY_PREFIX):].lower()
            for label in labels
            if label.startswith(_GMAIL_CATEGORY_PREFIX)
        ),
        "",
    )
    return {
        "thread_id": item.external_thread_id,
        "sender_address": item.from_email or "",
        "sender_name": item.from_name or "",
        "subject": item.subject or "",
        "has_list_unsubscribe": bool(item.unsubscribe_url or item.list_id),
        "reply_to": "",
        "gmail_category": category,
        "thread_message_count": int(item.message_count or 1),
        "has_user_replied": False,
        "snippet": item.snippet_redacted or "",
    }


def list_inbox_threads(user_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """Newest-first INBOX threads for one user, metadata-format only.

    Raises ``ReauthRequired`` (via the connection store / adapter) when the
    stored token is absent or revoked — callers map it to ``gmail_reconnect``.
    """
    from google.auth.exceptions import RefreshError

    from channels.base import ReauthRequired
    from channels.gmail import oauth
    from channels.gmail.store import adapter_for_user

    try:
        # Validates the stored token is loadable — the seam force_refresh_failure
        # patches; a revoked grant surfaces as ReauthRequired, never a traceback.
        oauth.get_credentials(user_id)
    except RefreshError as exc:
        raise ReauthRequired(
            "Gmail rejected the stored credentials — reconnect Gmail"
        ) from exc

    adapter = adapter_for_user(user_id=user_id)
    items = adapter.list_threads(limit=limit)
    return [_view_mapping(item) for item in items]

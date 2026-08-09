"""Atomic Gmail thread mutation — archive + label in a single ``modify()`` call.

spec/capabilities/gmail-actions-and-undo.md is the source of truth:

- **Archive means exactly one thing**: removing the ``INBOX`` label. It is applied in
  the *same* atomic ``threads().modify()`` call as adding the matching category label —
  a thread is never left labelled-but-still-in-inbox or archived-but-uncategorized by a
  partial write.
- **Never delete.** This module — and the entire mutation code path built on it — has
  no ``trash()``, ``delete()``, or ``report_spam()`` method anywhere. Only
  :meth:`GmailMutator.archive_and_label` and its exact inverse
  :meth:`GmailMutator.undo_archive_and_label` exist. This is a structural guarantee: the
  methods to perform a destructive operation do not exist to be called, not merely
  "we don't call them".

Talks to a built ``googleapiclient`` Gmail service directly, mirroring
``channels.gmail.labels.GmailLabelManager`` and ``channels.gmail.adapter.GmailAdapter``'s
transport conventions (3x retry with backoff, 401/403 -> ``ReauthRequired``,
429/5xx -> backoff-then-``RateLimited``).
"""

from __future__ import annotations

import time
from collections.abc import Callable

from googleapiclient.errors import HttpError

from channels.base import ChannelError, RateLimited, ReauthRequired

INBOX_LABEL_ID = "INBOX"
MAX_ATTEMPTS = 3


class GmailMutator:
    """Atomic add/remove-label mutations against one Gmail thread.

    No delete/trash/spam surface exists on this class, full stop.
    """

    def __init__(
        self,
        service,
        *,
        backoff_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._service = service
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep

    # --- transport ------------------------------------------------------
    def _execute(self, request):
        last: HttpError | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                return request.execute()
            except HttpError as exc:
                status = getattr(exc.resp, "status", None)
                if status in (401, 403):
                    raise ReauthRequired(
                        "Gmail rejected the stored credentials — reconnect Gmail"
                    ) from exc
                if status in (429, 500, 502, 503, 504):
                    last = exc
                    self._sleep(self._backoff_seconds * (2**attempt))
                    continue
                raise ChannelError(f"Gmail request failed with HTTP {status}") from exc
        raise RateLimited("Gmail is rate limiting; retries exhausted") from last

    def _modify(self, thread_id: str, *, add_label_ids: list[str], remove_label_ids: list[str]) -> dict:
        result = self._execute(
            self._service.users()
            .threads()
            .modify(
                userId="me",
                id=thread_id,
                body={"addLabelIds": add_label_ids, "removeLabelIds": remove_label_ids},
            )
        )
        return {
            "thread_id": thread_id,
            "label_ids": list((result or {}).get("labelIds") or []),
        }

    # --- the only two mutations that exist ------------------------------
    def archive_and_label(self, thread_id: str, *, category_label_id: str) -> dict:
        """One atomic ``modify()``: add the category label, remove ``INBOX``.

        Archive *is* the removal of ``INBOX`` — there is no separate archive call.
        """
        return self._modify(
            thread_id,
            add_label_ids=[category_label_id],
            remove_label_ids=[INBOX_LABEL_ID],
        )

    def undo_archive_and_label(self, thread_id: str, *, category_label_id: str) -> dict:
        """The exact inverse, also atomic: re-add ``INBOX``, remove the category label."""
        return self._modify(
            thread_id,
            add_label_ids=[INBOX_LABEL_ID],
            remove_label_ids=[category_label_id],
        )

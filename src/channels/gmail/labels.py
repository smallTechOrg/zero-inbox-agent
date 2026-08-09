"""Gmail label management for the taxonomy — categories map 1:1 to real labels.

Every category is materialised as a label named ``ZeroInbox/<Category>``. Gmail's
label sidebar is the master list of what is archived and how it is categorized
(spec/capabilities/taxonomy-management.md) — there is no separate archive table.

This module talks to a built ``googleapiclient`` Gmail service directly (the same
transport shape ``channels.gmail.adapter.GmailAdapter`` uses) so it can be reused
by both the taxonomy sync endpoint and ``channels/gmail/mutations.py``'s
label-id lookups, without depending on adapter internals.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from googleapiclient.errors import HttpError

from channels.base import ChannelError, RateLimited, ReauthRequired

LABEL_PREFIX = "ZeroInbox/"
MAX_ATTEMPTS = 3


def label_name_for(category_name: str) -> str:
    """The canonical Gmail label name for a category, e.g. ``ZeroInbox/Newsletters``."""
    return f"{LABEL_PREFIX}{category_name}"


class GmailLabelManager:
    """Idempotent create/lookup/rename of ``ZeroInbox/*`` Gmail labels."""

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

    # --- read -------------------------------------------------------------
    def list_labels(self) -> list[dict]:
        listing = self._execute(self._service.users().labels().list(userId="me"))
        return [
            {"id": label.get("id"), "name": label.get("name")}
            for label in (listing or {}).get("labels") or []
        ]

    def list_zero_inbox_labels(self) -> list[dict]:
        return [
            label for label in self.list_labels() if (label["name"] or "").startswith(LABEL_PREFIX)
        ]

    def find_label_by_name(self, name: str) -> dict | None:
        for label in self.list_labels():
            if label["name"] == name:
                return label
        return None

    def label_count(self, label_id: str) -> int:
        """Live thread count for one label — a single cheap Gmail call, no listing."""
        label = self._execute(
            self._service.users().labels().get(userId="me", id=label_id)
        )
        return int((label or {}).get("threadsTotal") or 0)

    # --- write --------------------------------------------------------
    def ensure_label(self, name: str) -> dict:
        """Idempotent: returns the existing label if present, else creates it."""
        existing = self.find_label_by_name(name)
        if existing is not None:
            return existing
        created = self._execute(
            self._service.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": name,
                    "labelListVisibility": "labelShow",
                    "messageListVisibility": "show",
                },
            )
        )
        return {"id": created.get("id"), "name": created.get("name")}

    def rename_label(self, label_id: str, new_name: str) -> dict:
        updated = self._execute(
            self._service.users()
            .labels()
            .patch(userId="me", id=label_id, body={"name": new_name})
        )
        return {"id": updated.get("id"), "name": updated.get("name")}

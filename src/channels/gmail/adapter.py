"""The Gmail `ChannelAdapter` — READ ONLY in Phase 1.

Every mutation method exists (so the interface is complete and callers are
type-safe) and raises `DryRunViolation`. There is deliberately **no** delete,
trash or spam operation anywhere in this class — that is permanently out of
scope, not deferred.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime

from googleapiclient.errors import HttpError

from channels.base import (
    ChannelAdapter,
    ChannelError,
    ChannelItem,
    DryRunViolation,
    RateLimited,
    ReauthRequired,
    SenderSignal,
)
from channels.gmail.normalize import WANTED_HEADERS, Redactor, normalize_thread

MAX_ATTEMPTS = 3
GMAIL_PAGE_SIZE = 100
DRY_RUN_MESSAGE = (
    "Phase 1 is dry-run: the Gmail adapter performs no mutations. "
    "Real mutations ship in Phase 2 behind explicit approval + undo."
)


class GmailAdapter(ChannelAdapter):
    """Wraps a built googleapiclient Gmail service for one user's mailbox."""

    channel = "gmail"

    def __init__(
        self,
        service,
        *,
        user_id: str,
        account_email: str = "",
        redactor: Redactor | None = None,
        backoff_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._service = service
        self.user_id = user_id
        self._account_email = account_email
        self._redactor = redactor
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep

    # --- factory ------------------------------------------------------
    @classmethod
    def for_refresh_token(
        cls, refresh_token: str, *, user_id: str, account_email: str = "", **kwargs
    ) -> "GmailAdapter":
        """Build an adapter from a decrypted refresh token (auto-refreshes access)."""
        from googleapiclient.discovery import build

        from channels.gmail.oauth import credentials_from_refresh_token, google_oauth_config

        config = google_oauth_config()
        credentials = credentials_from_refresh_token(config, refresh_token)
        service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
        return cls(service, user_id=user_id, account_email=account_email, **kwargs)

    # --- transport ----------------------------------------------------
    def _execute(self, request):
        """Run a Gmail request with retries, 401→reauth and 429→backoff."""
        last: HttpError | None = None
        for attempt in range(MAX_ATTEMPTS):
            try:
                return request.execute()
            except HttpError as exc:
                status = getattr(exc.resp, "status", None)
                if status in (401, 403) and self._is_auth_error(status, exc):
                    raise ReauthRequired(
                        "Gmail rejected the stored credentials — reconnect Gmail"
                    ) from exc
                if status in (429, 500, 502, 503, 504):
                    last = exc
                    self._sleep(self._backoff_seconds * (2**attempt))
                    continue
                raise ChannelError(f"Gmail request failed with HTTP {status}") from exc
        raise RateLimited("Gmail is rate limiting; retries exhausted") from last

    @staticmethod
    def _is_auth_error(status: int, exc: HttpError) -> bool:
        if status == 401:
            return True
        return "insufficientPermissions" in str(exc) or "ACCESS_TOKEN" in str(exc)

    # --- read ---------------------------------------------------------
    def account_email(self) -> str:
        profile = self._execute(self._service.users().getProfile(userId="me"))
        email = (profile or {}).get("emailAddress", "")
        self._account_email = email or self._account_email
        return self._account_email

    def list_threads(
        self,
        *,
        limit: int = 200,
        query: str | None = None,
        cancel_check: Callable[[], bool] | None = None,
        after: datetime | None = None,
    ) -> list[ChannelItem]:
        """Most recent inbox threads, newest first.

        ``after`` (optional) stops paging as soon as a thread's own
        ``internal_date`` is at or before that cutoff. Gmail's thread listing is
        ordered by each thread's most recent message, so once we cross the
        cutoff every remaining thread is at least as old — the scan can stop
        rather than re-fetching metadata for threads already seen on a prior
        run. A thread that received a genuinely new reply since ``after``
        legitimately reappears (its internal_date moved forward), which is the
        correct "what's new" behaviour, not a bug.
        """
        if limit < 0:
            raise ChannelError("limit must be >= 0")

        items: list[ChannelItem] = []
        page_token: str | None = None
        while len(items) < limit:
            if cancel_check is not None and cancel_check():
                break
            page = self._execute(
                self._service.users()
                .threads()
                .list(
                    userId="me",
                    labelIds=["INBOX"],
                    maxResults=min(GMAIL_PAGE_SIZE, limit - len(items)),
                    pageToken=page_token,
                    q=query,
                )
            )
            batch = (page or {}).get("threads") or []
            if not batch:
                break
            for entry in batch:
                if cancel_check is not None and cancel_check():
                    return items
                thread_id = entry.get("id")
                if not thread_id:
                    continue
                try:
                    thread = self._execute(
                        self._service.users()
                        .threads()
                        .get(
                            userId="me",
                            id=thread_id,
                            format="metadata",
                            metadataHeaders=WANTED_HEADERS,
                        )
                    )
                except ChannelError:
                    # One unreadable thread must not fail the whole listing.
                    continue
                try:
                    item = normalize_thread(thread, redactor=self._redactor)
                except ChannelError:
                    continue
                if after is not None and item.internal_date <= after:
                    return items
                items.append(item)
                if len(items) >= limit:
                    break
            page_token = (page or {}).get("nextPageToken")
            if not page_token:
                break
        return items[:limit]

    def fetch_thread_body(self, external_thread_id: str) -> str:
        """Full thread text, in memory only — the caller must never persist it."""
        thread = self._execute(
            self._service.users()
            .threads()
            .get(userId="me", id=external_thread_id, format="full")
        )
        return _extract_text(thread)

    def sender_history(self, *, limit: int = 500) -> dict[str, SenderSignal]:
        if limit < 0:
            raise ChannelError("limit must be >= 0")
        listing = self._execute(
            self._service.users()
            .messages()
            .list(userId="me", labelIds=["SENT"], maxResults=min(GMAIL_PAGE_SIZE, limit))
        )
        signals: dict[str, SenderSignal] = {}
        for stub in ((listing or {}).get("messages") or [])[:limit]:
            try:
                message = self._execute(
                    self._service.users()
                    .messages()
                    .get(
                        userId="me",
                        id=stub["id"],
                        format="metadata",
                        metadataHeaders=["To", "Cc", "Date"],
                    )
                )
            except ChannelError:
                continue
            _accumulate_recipients(message, signals)
        return signals

    def list_labels(self) -> list[dict]:
        listing = self._execute(self._service.users().labels().list(userId="me"))
        return [
            {"id": label.get("id"), "name": label.get("name")}
            for label in (listing or {}).get("labels") or []
        ]

    # --- mutate -------------------------------------------------------
    def archive_and_label(
        self, thread_id: str, add_label_ids: list[str], *, remove_inbox: bool = True
    ) -> dict:
        from channels.gmail.mutations import GmailMutator

        mutator = GmailMutator(self._service)
        if remove_inbox:
            return mutator._modify(thread_id, add_label_ids=add_label_ids, remove_label_ids=["INBOX"])
        return mutator._modify(thread_id, add_label_ids=add_label_ids, remove_label_ids=[])

    def undo_archive_and_label(
        self, thread_id: str, add_label_ids: list[str], *, remove_inbox: bool = True
    ) -> dict:
        from channels.gmail.mutations import GmailMutator

        mutator = GmailMutator(self._service)
        return mutator._modify(thread_id, add_label_ids=["INBOX"], remove_label_ids=add_label_ids)

    def create_label(self, name: str) -> dict:
        raise DryRunViolation(DRY_RUN_MESSAGE)

    def create_filter(self, criteria: dict, action: dict) -> dict:
        raise DryRunViolation(DRY_RUN_MESSAGE)

    def create_draft(self, external_thread_id: str, body: str) -> dict:
        raise DryRunViolation(DRY_RUN_MESSAGE)


def _accumulate_recipients(message: dict, signals: dict[str, SenderSignal]) -> None:
    from channels.gmail.normalize import addresses_of, headers_of, internal_date_of

    headers = headers_of(message)
    sent_at: datetime = internal_date_of(message)
    recipients = addresses_of(headers.get("to", "")) + addresses_of(headers.get("cc", ""))
    for email in dict.fromkeys(recipients):
        signal = signals.get(email)
        if signal is None:
            signal = SenderSignal(
                sender_email=email,
                sender_domain=email.split("@")[-1] if "@" in email else "",
            )
            signals[email] = signal
        signal.replied_count += 1
        signal.ever_replied = True
        if signal.last_replied_at is None or sent_at > signal.last_replied_at:
            signal.last_replied_at = sent_at


def _extract_text(thread: dict) -> str:
    """Decode text parts of a `format=full` thread. In-memory use only."""
    import base64

    chunks: list[str] = []

    def walk(part: dict) -> None:
        body = part.get("body") or {}
        data = body.get("data")
        if data and (part.get("mimeType") or "").startswith("text/"):
            chunks.append(
                base64.urlsafe_b64decode(data.encode("ascii")).decode("utf-8", "replace")
            )
        for child in part.get("parts") or []:
            walk(child)

    for message in thread.get("messages") or []:
        walk(message.get("payload") or {})
    return "\n".join(chunks)

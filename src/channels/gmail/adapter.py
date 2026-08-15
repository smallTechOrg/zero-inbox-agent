"""The Gmail `ChannelAdapter` — READ ONLY in Phase 1.

Every mutation method exists (so the interface is complete and callers are
type-safe) and raises `DryRunViolation`. There is deliberately **no** delete,
trash or spam operation anywhere in this class — that is permanently out of
scope, not deferred.
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import structlog

log = structlog.get_logger(__name__)
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
#: Socket timeout (seconds) for every worker's own httplib2 connection.
GMAIL_HTTP_TIMEOUT = 60
#: Concurrent per-thread metadata fetches. Each worker owns its own TLS socket.
GMAIL_MAX_WORKERS = 10
DRY_RUN_MESSAGE = (
    "Phase 1 is dry-run: the Gmail adapter performs no mutations. "
    "Real mutations ship in Phase 2 behind explicit approval + undo."
)


class GmailAdapter(ChannelAdapter):
    """Wraps a built googleapiclient Gmail service for one user's mailbox.

    **Thread safety.** A googleapiclient service owns exactly one ``httplib2.Http``,
    and httplib2 caches one ``HTTPSConnection`` per host — its own docstring says
    it is "not thread-safe, requires external synchronization". Sharing one service
    across a ``ThreadPoolExecutor`` therefore interleaves several workers'
    ``putrequest``/``getresponse`` calls on a single TLS socket; one worker's read
    timeout closes the socket underneath the others, producing a burst of read
    timeouts, an OpenSSL ``RECORD_LAYER_FAILURE`` and finally a native abort with no
    Python traceback.

    So every pooled worker gets its **own** service, built by ``service_factory``
    and cached in a :class:`threading.local`. ``service_factory`` also mints fresh
    credentials per thread, so concurrent access-token refreshes cannot race on one
    shared ``Credentials`` object either.
    """

    channel = "gmail"

    # Class-level defaults so a partially-constructed adapter (tests build one
    # with ``object.__new__`` to inject a fake transport) still has a coherent,
    # single-service, serial configuration.
    _service_factory: Callable[[], object] | None = None
    _max_workers: int = GMAIL_MAX_WORKERS
    # Safe despite being a mutable class-level default: `list_threads` REBINDS
    # `self.last_fetch_failed_ids = []` (instance attribute) before anything
    # extends it, so this shared list is only ever read, never mutated. Keep
    # that rebind if you touch list_threads.
    last_fetch_failed_ids: list[str] = []

    def __init__(
        self,
        service=None,
        *,
        user_id: str,
        account_email: str = "",
        redactor: Redactor | None = None,
        backoff_seconds: float = 0.5,
        sleep: Callable[[float], None] = time.sleep,
        service_factory: Callable[[], object] | None = None,
        max_workers: int = GMAIL_MAX_WORKERS,
    ) -> None:
        if service is None and service_factory is None:
            raise ChannelError("GmailAdapter requires a service or a service_factory")
        self._service_factory = service_factory
        # The serial (non-pooled) service. Built once; only ever touched by the
        # calling thread.
        self._service = service if service is not None else service_factory()  # type: ignore[misc]
        self._local = threading.local()
        self._max_workers = max_workers
        self.user_id = user_id
        self._account_email = account_email
        self._redactor = redactor
        self._backoff_seconds = backoff_seconds
        self._sleep = sleep
        #: External thread ids that could not be fetched on the last
        #: :meth:`list_threads` call, even after a retry on a fresh connection.
        self.last_fetch_failed_ids: list[str] = []

    # --- factory ------------------------------------------------------
    @classmethod
    def for_refresh_token(
        cls, refresh_token: str, *, user_id: str, account_email: str = "", **kwargs
    ) -> "GmailAdapter":
        """Build an adapter from a decrypted refresh token (auto-refreshes access)."""
        from channels.gmail.oauth import google_oauth_config

        config = google_oauth_config()
        factory = _service_factory_for_refresh_token(config, refresh_token)
        return cls(
            None,
            service_factory=factory,
            user_id=user_id,
            account_email=account_email,
            **kwargs,
        )

    # --- per-thread transport ----------------------------------------
    def _worker_service(self, *, fresh: bool = False):
        """The calling thread's own Gmail service (never shared across threads)."""
        if self._service_factory is None:
            # Injected service (tests / callers that supply their own). Serial use
            # only — there is nothing to build a second connection from.
            return self._service
        existing = getattr(self._local, "service", None)
        if fresh or existing is None:
            self._local.service = self._service_factory()
        return self._local.service

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
                    delay = self._backoff_seconds * (2**attempt)
                    # Retries used to be entirely silent: Gmail could rate-limit
                    # us repeatedly and the UI showed nothing at all. Every
                    # backoff is now an event the user can see.
                    log.warning(
                        "gmail.retry",
                        status=status,
                        attempt=attempt + 1,
                        max_attempts=MAX_ATTEMPTS,
                        backoff_seconds=round(delay, 2),
                    )
                    self._sleep(delay)
                    continue
                raise ChannelError(f"Gmail request failed with HTTP {status}") from exc
        raise RateLimited("Gmail is rate limiting; retries exhausted") from last

    @staticmethod
    def _is_auth_error(status: int, exc: HttpError) -> bool:
        if status == 401:
            return True
        return "insufficientPermissions" in str(exc) or "ACCESS_TOKEN" in str(exc)

    # --- helpers ------------------------------------------------------
    def _thread_meta_request(self, service, thread_id: str):
        return service.users().threads().get(
            userId="me", id=thread_id, format="metadata",
            metadataHeaders=WANTED_HEADERS,
        )

    def _fetch_once(self, service, thread_id: str) -> dict:
        """One metadata fetch on the given service. An empty body is a failure.

        Gmail never legitimately answers ``threads().get`` with an empty body, so
        treating it as success would drop the thread from the run without ever
        counting it as failed.
        """
        raw = self._execute(self._thread_meta_request(service, thread_id))
        if not raw:
            raise ChannelError(f"Gmail returned an empty body for thread {thread_id}")
        return raw

    def _fetch_thread_meta_checked(self, thread_id: str) -> tuple[dict | None, str | None]:
        """Fetch one thread's metadata on this thread's own connection.

        Returns ``(raw, error)``. A first failure is retried **once on a brand new
        connection** — a dropped/poisoned socket is the common transient here and a
        fresh `Http` recovers it. Only an unrecoverable failure returns an error,
        and the caller must surface it: silently dropping threads makes a partial
        run look complete.
        """
        try:
            return self._fetch_once(self._worker_service(), thread_id), None
        except Exception as first:
            log.warning(
                "gmail.thread_fetch_retry", thread_id=thread_id, error=str(first)
            )
        try:
            return self._fetch_once(self._worker_service(fresh=True), thread_id), None
        except Exception as exc:
            log.warning("gmail.thread_fetch_failed", thread_id=thread_id, error=str(exc))
            return None, str(exc)

    def _fetch_thread_meta(self, thread_id: str) -> dict | None:
        """Fetch a single thread's metadata — safe to call from a thread pool."""
        return self._fetch_thread_meta_checked(thread_id)[0]

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
        on_page: Callable[[int, int], None] | None = None,
        on_fetch_failed: Callable[[list[str]], None] | None = None,
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

        ``on_page`` (optional) is called with ``(page_num, fetched_so_far)``
        after each page's metadata is fully fetched and normalised — useful for
        emitting SSE progress events during long inbox scans.

        ``on_fetch_failed`` (optional) is called with the list of external thread
        ids that could not be fetched on a page even after a retry on a fresh
        connection. Those threads are missing from the result, so the caller must
        report them rather than present an under-counted run as complete. The same
        ids accumulate on :attr:`last_fetch_failed_ids`.
        """
        if limit < 0:
            raise ChannelError("limit must be >= 0")

        redactor = self._redactor
        self.last_fetch_failed_ids = []
        items: list[ChannelItem] = []
        page_token: str | None = None
        page_num = 0
        while len(items) < limit:
            if cancel_check is not None and cancel_check():
                break
            # Use q="in:inbox" instead of labelIds=["INBOX"] so that threads
            # in Gmail's tabbed-inbox categories (Social, Updates, Promotions,
            # Forums) are included. Those threads carry INBOX + CATEGORY_* labels
            # and are silently excluded when labelIds=["INBOX"] is used.
            effective_q = f"in:inbox {query}".strip() if query else "in:inbox"
            page = self._execute(
                self._service.users()
                .threads()
                .list(
                    userId="me",
                    # labelIds removed — "in:inbox" query covers all tabs including Social/Updates/Promotions
                    maxResults=min(GMAIL_PAGE_SIZE, limit - len(items)),
                    pageToken=page_token,
                    q=effective_q,
                )
            )
            batch = (page or {}).get("threads") or []
            page_num += 1
            if not batch:
                break
            log.info("gmail.fetch_page", page=page_num, fetched_so_far=len(items), page_size=len(batch))

            # Parallelise the per-thread metadata requests. Each pool worker
            # builds and reuses its OWN Gmail service (see `_worker_service`) —
            # they must never share one httplib2 connection.
            page_items: list[ChannelItem] = []
            page_failed: list[str] = []
            # An adapter built with an injected `service` (no factory) has exactly
            # ONE service and `_worker_service` hands that same object to every
            # worker — which is precisely the shared-httplib2 crash this class
            # was rewritten to eliminate. Such an adapter must run serially, or
            # it is the original bug again with a new caller.
            workers = self._max_workers if self._service_factory is not None else 1
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(self._fetch_thread_meta_checked, entry["id"]): entry["id"]
                    for entry in batch if entry.get("id")
                }
                for future in as_completed(futures):
                    raw, error = future.result()
                    if raw is None:
                        if error is not None:
                            page_failed.append(futures[future])
                        continue
                    try:
                        item = normalize_thread(raw, redactor=redactor)
                    except ChannelError:
                        continue
                    if item is not None:
                        page_items.append(item)

            if page_failed:
                self.last_fetch_failed_ids.extend(page_failed)
                log.warning(
                    "gmail.page_fetch_incomplete",
                    page=page_num,
                    failed=len(page_failed),
                    page_size=len(batch),
                )
                if on_fetch_failed is not None:
                    try:
                        on_fetch_failed(list(page_failed))
                    except Exception:  # pragma: no cover - reporting never fails a fetch
                        pass

            # Apply the after-cutoff filter: if any thread is older than the
            # watermark the scan is done (Gmail returns newest-first; once we
            # cross the cutoff all subsequent pages are older still).
            hit_cutoff = False
            if after is not None:
                filtered: list[ChannelItem] = []
                for item in page_items:
                    if item.internal_date <= after:
                        hit_cutoff = True
                    else:
                        filtered.append(item)
                page_items = filtered

            # Honour cancel between pages (not mid-page, as the pool is already done).
            if cancel_check is not None and cancel_check():
                items.extend(page_items)
                break

            items.extend(page_items)

            # Notify the caller (e.g. to emit SSE progress events).
            if on_page is not None:
                on_page(page_num, len(items))

            if hit_cutoff or len(items) >= limit:
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

    def send_as_aliases(self) -> list[str]:
        """Every address this mailbox can send as. Best-effort, never fatal.

        Used only to recognise the user's own address in his own ``SENT`` mail.
        On any failure we fall back to the account address alone and log at
        WARNING — an alias lookup must never take a triage run down.
        """
        cached = getattr(self, "_send_as_cache", None)
        if cached is not None:
            return cached
        aliases: list[str] = []
        try:
            listing = self._execute(
                self._service.users().settings().sendAs().list(userId="me")
            )
            aliases = [
                (entry.get("sendAsEmail") or "").lower()
                for entry in ((listing or {}).get("sendAs") or [])
                if entry.get("sendAsEmail")
            ]
        except Exception as exc:  # noqa: BLE001 — best-effort by contract
            log.warning("gmail.send_as_failed", error=str(exc))
            aliases = []
        self._send_as_cache = aliases
        return aliases

    def sender_history(self, *, limit: int = 500) -> dict[str, SenderSignal]:
        if limit < 0:
            raise ChannelError("limit must be >= 0")
        # Who "me" is, so the user is never harvested as his own correspondent.
        account = self._account_email
        if not account:
            try:
                account = self.account_email()
            except Exception as exc:  # noqa: BLE001 — best-effort by contract
                log.warning("gmail.account_email_failed", error=str(exc))
                account = ""
        aliases = self.send_as_aliases()
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
            _accumulate_recipients(
                message, signals, account_email=account, aliases=aliases
            )
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


def _service_factory_for_refresh_token(config, refresh_token: str) -> Callable[[], object]:
    """A zero-arg builder of a *completely independent* Gmail service.

    Independent means: its own ``httplib2.Http`` (hence its own TLS socket) **and**
    its own ``Credentials`` object, so neither the connection nor the access-token
    refresh is shared with any other thread.
    """

    def _build():
        import google_auth_httplib2
        import httplib2
        from googleapiclient.discovery import build

        from channels.gmail.oauth import credentials_from_refresh_token

        credentials = credentials_from_refresh_token(config, refresh_token)
        authed_http = google_auth_httplib2.AuthorizedHttp(
            credentials, http=httplib2.Http(timeout=GMAIL_HTTP_TIMEOUT)
        )
        return build("gmail", "v1", http=authed_http, cache_discovery=False)

    return _build


def _accumulate_recipients(
    message: dict,
    signals: dict[str, SenderSignal],
    *,
    account_email: str = "",
    aliases: list[str] | None = None,
) -> None:
    """Harvest reply evidence from one ``SENT`` message.

    The user's own addresses are skipped **entirely** — no ``SenderSignal``, no
    ``replied_count``, no ``ever_replied``. Mailing yourself, or being on the
    Cc of your own thread, is not a correspondence.
    """
    from channels.gmail.normalize import addresses_of, headers_of, internal_date_of

    from tools.correspondents import is_self_address

    headers = headers_of(message)
    sent_at: datetime = internal_date_of(message)
    recipients = addresses_of(headers.get("to", "")) + addresses_of(headers.get("cc", ""))
    for email in dict.fromkeys(recipients):
        if is_self_address(email, account_email=account_email, aliases=aliases or []):
            continue
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

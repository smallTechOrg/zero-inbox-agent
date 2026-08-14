"""Regression tests for the crash root cause: one shared Gmail service across a thread pool.

A googleapiclient service owns a single ``httplib2.Http``, which caches one TLS
connection per host and is explicitly documented as not thread-safe. The old
adapter submitted 10 concurrent ``threads().get()`` calls that all reached
through ``self._service`` — one socket, ten interleaved HTTP state machines.
The observable end state was a burst of read timeouts, an OpenSSL
``RECORD_LAYER_FAILURE`` and a native abort with no Python traceback.

The pre-existing concurrency tests could not see this defect: they pass one
in-memory fake service, which is trivially thread-safe. These tests assert the
structural property instead — **each worker thread uses its own service object** —
plus the failure accounting that stops a partial fetch being reported as whole.
"""

from __future__ import annotations

import threading

import pytest

from tests.unit.channels.fake_gmail import FakeGmailService, http_error


def _msg(mid):
    return {
        "id": mid,
        "snippet": "snippet text",
        "internalDate": "1767225600000",
        "labelIds": ["INBOX"],
        "payload": {
            "headers": [
                {"name": "From", "value": "Ada <ada@example.com>"},
                {"name": "To", "value": "me@mine.com"},
                {"name": "Subject", "value": "Hi"},
            ]
        },
    }


def _thread(tid):
    return {"id": tid, "messages": [_msg(f"m-{tid}")]}


class _TrackingService(FakeGmailService):
    """A fake service that records which OS threads used it, and can be made to fail."""

    def __init__(self, threads, *, tracker, fail_ids=(), slow=0.0):
        super().__init__(threads=dict(threads))
        self.used_by_threads: set[int] = set()
        self._tracker = tracker
        self._slow = slow
        for tid in fail_ids:
            self.thread_errors[tid] = http_error(500, "boom")

    def users(self):
        self.used_by_threads.add(threading.get_ident())
        self._tracker.record(self)
        if self._slow:
            import time

            time.sleep(self._slow)
        return super().users()


class _Tracker:
    """Records (service, thread) pairs seen across the pool."""

    def __init__(self):
        self._lock = threading.Lock()
        self.pairs: list[tuple[int, int]] = []
        self.built: list[_TrackingService] = []

    def record(self, service):
        with self._lock:
            self.pairs.append((id(service), threading.get_ident()))

    @property
    def services_per_thread(self) -> dict[int, set[int]]:
        mapping: dict[int, set[int]] = {}
        for service_id, thread_id in self.pairs:
            mapping.setdefault(thread_id, set()).add(service_id)
        return mapping

    @property
    def threads_per_service(self) -> dict[int, set[int]]:
        mapping: dict[int, set[int]] = {}
        for service_id, thread_id in self.pairs:
            mapping.setdefault(service_id, set()).add(thread_id)
        return mapping


def _factory_adapter(threads, *, tracker, fail_ids=(), slow=0.0, **kwargs):
    from channels.gmail.adapter import GmailAdapter

    def _build():
        service = _TrackingService(threads, tracker=tracker, fail_ids=fail_ids, slow=slow)
        tracker.built.append(service)
        return service

    return GmailAdapter(
        None,
        service_factory=_build,
        user_id="user-1",
        account_email="user@example.com",
        backoff_seconds=0,
        **kwargs,
    )


# --- F1: no service is ever shared between two threads ---------------------


def test_each_pool_worker_uses_its_own_service_object():
    """THE regression test: no two worker threads may touch the same service.

    Two threads on one httplib2 connection is the identical bug as ten, so the
    assertion is "shared by nobody", not "shared by few".
    """
    tracker = _Tracker()
    threads = {f"t{i}": _thread(f"t{i}") for i in range(40)}
    # `slow` keeps workers overlapping so the pool really does run concurrently.
    adapter = _factory_adapter(threads, tracker=tracker, slow=0.01, max_workers=8)

    items = adapter.list_threads(limit=40)

    assert len(items) == 40
    # Concurrency actually happened (more than one worker thread was involved).
    worker_threads = set(t for _, t in tracker.pairs)
    assert len(worker_threads) > 1, "pool did not run concurrently; test proves nothing"
    # And every service object was used by exactly one thread.
    for service_id, thread_ids in tracker.threads_per_service.items():
        assert len(thread_ids) == 1, f"service {service_id} was shared across {thread_ids}"


def test_number_of_services_matches_number_of_worker_threads():
    """One service per worker thread — not one per request (no churn), not one shared."""
    tracker = _Tracker()
    threads = {f"t{i}": _thread(f"t{i}") for i in range(30)}
    adapter = _factory_adapter(threads, tracker=tracker, slow=0.01, max_workers=5)

    adapter.list_threads(limit=30)

    worker_threads = set(t for _, t in tracker.pairs)
    distinct_services = set(s for s, _ in tracker.pairs)
    assert len(distinct_services) == len(worker_threads)
    # Each thread reused one service for all of its requests.
    for thread_id, service_ids in tracker.services_per_thread.items():
        assert len(service_ids) == 1


def test_worker_service_is_thread_local_and_stable_within_a_thread():
    tracker = _Tracker()
    adapter = _factory_adapter({}, tracker=tracker)

    seen: list[int] = []
    barrier = threading.Barrier(3)

    def _grab():
        barrier.wait(timeout=5)
        first = adapter._worker_service()
        second = adapter._worker_service()
        assert first is second  # stable within one thread
        seen.append(id(first))

    workers = [threading.Thread(target=_grab) for _ in range(3)]
    for w in workers:
        w.start()
    for w in workers:
        w.join(timeout=10)

    assert len(seen) == 3
    assert len(set(seen)) == 3  # distinct across threads


def test_fresh_flag_replaces_this_threads_service():
    tracker = _Tracker()
    adapter = _factory_adapter({}, tracker=tracker)

    first = adapter._worker_service()
    replaced = adapter._worker_service(fresh=True)
    again = adapter._worker_service()

    assert replaced is not first
    assert again is replaced


def test_injected_service_without_factory_still_works():
    """The existing constructor contract is unchanged for callers that pass a service."""
    from channels.gmail.adapter import GmailAdapter

    service = FakeGmailService(threads={"t1": _thread("t1")})
    adapter = GmailAdapter(service, user_id="u", account_email="u@e.com")

    assert adapter._worker_service() is service
    assert len(adapter.list_threads(limit=5)) == 1


def test_adapter_requires_a_service_or_a_factory():
    from channels.base import ChannelError
    from channels.gmail.adapter import GmailAdapter

    with pytest.raises(ChannelError):
        GmailAdapter(None, user_id="u")


def test_refresh_token_factory_builds_independent_http_and_credentials(monkeypatch):
    """`for_refresh_token`'s factory must mint a new Http *and* new Credentials each call.

    Sharing one `Credentials` across threads races on access-token refresh
    (`google_auth_httplib2.AuthorizedHttp.request` takes no lock).
    """
    import channels.gmail.adapter as adapter_module

    built: list[tuple[int, int]] = []

    class _FakeAuthorizedHttp:
        def __init__(self, credentials, http=None):
            self.credentials = credentials
            self.http = http

    def _fake_build(name, version, http=None, cache_discovery=None):
        built.append((id(http.credentials), id(http.http)))
        return object()

    monkeypatch.setattr(
        adapter_module, "_service_factory_for_refresh_token",
        adapter_module._service_factory_for_refresh_token,
    )
    import google_auth_httplib2
    import googleapiclient.discovery

    monkeypatch.setattr(google_auth_httplib2, "AuthorizedHttp", _FakeAuthorizedHttp)
    monkeypatch.setattr(googleapiclient.discovery, "build", _fake_build)

    from channels.gmail.oauth import GoogleOAuthConfig

    config = GoogleOAuthConfig(client_id="id", client_secret="secret")
    factory = adapter_module._service_factory_for_refresh_token(config, "refresh-token")

    factory()
    factory()

    assert len(built) == 2
    (creds_a, http_a), (creds_b, http_b) = built
    assert creds_a != creds_b, "credentials were shared between services"
    assert http_a != http_b, "the httplib2 connection was shared between services"


# --- F2: a partial fetch is never reported as complete ---------------------


def test_transient_failure_is_retried_on_a_fresh_connection():
    """A poisoned socket is exactly the F1 symptom — retry once on a new service."""
    from channels.gmail.adapter import GmailAdapter

    good = FakeGmailService(threads={"t1": _thread("t1")})
    bad = FakeGmailService(threads={"t1": _thread("t1")})
    bad.thread_errors["t1"] = http_error(500, "connection reset")

    services = [bad, good]
    adapter = GmailAdapter(
        None,
        service_factory=lambda: services.pop(0),
        user_id="u",
        backoff_seconds=0,
    )

    raw, error = adapter._fetch_thread_meta_checked("t1")

    assert error is None
    assert raw["id"] == "t1"
    assert services == []  # a second, fresh service was built for the retry


def test_unrecoverable_failures_are_reported_not_silently_dropped():
    from channels.gmail.adapter import GmailAdapter

    def _build():
        service = FakeGmailService(
            threads={"t1": _thread("t1"), "t2": _thread("t2")}
        )
        service.thread_errors["t2"] = http_error(500, "boom")
        return service

    adapter = GmailAdapter(
        None, service_factory=_build, user_id="u", backoff_seconds=0
    )

    reported: list[list[str]] = []
    items = adapter.list_threads(limit=10, on_fetch_failed=reported.append)

    assert [i.external_thread_id for i in items] == ["t1"]
    assert reported == [["t2"]], "a dropped thread must be reported to the caller"
    assert adapter.last_fetch_failed_ids == ["t2"]


def test_last_fetch_failed_ids_resets_between_calls():
    from channels.gmail.adapter import GmailAdapter

    def _build():
        service = FakeGmailService(threads={"t1": _thread("t1")})
        service.thread_errors["t1"] = http_error(500, "boom")
        return service

    adapter = GmailAdapter(None, service_factory=_build, user_id="u", backoff_seconds=0)
    adapter.list_threads(limit=5)
    assert adapter.last_fetch_failed_ids == ["t1"]
    adapter.list_threads(limit=5)
    assert adapter.last_fetch_failed_ids == ["t1"], "stale failures must not accumulate"


def test_incomplete_fetch_writes_an_honest_error_message_on_the_run():
    """items_total under-counting must reach the user via TriageRun.error_message."""
    from datetime import datetime, timezone
    from uuid import uuid4

    from db.models import ChannelAccount, TriageRun, User
    from db.session import create_db_session
    from graph.nodes import _record_fetch_failures

    run_id = str(uuid4())
    with create_db_session() as session:
        user = User(id=str(uuid4()), email="a@b.com", display_name="A",
                    created_at=datetime.now(timezone.utc))
        session.add(user)
        session.flush()
        account = ChannelAccount(id=str(uuid4()), user_id=user.id, channel="gmail",
                                 account_email="a@b.com", refresh_token_enc="enc",
                                 connected_at=datetime.now(timezone.utc))
        session.add(account)
        session.flush()
        session.add(TriageRun(id=run_id, user_id=user.id, channel_account_id=account.id,
                              status="running", started_at=datetime.now(timezone.utc)))

    _record_fetch_failures(run_id, fetched=2847, failed=53)

    with create_db_session() as session:
        row = session.get(TriageRun, run_id)
        assert row.error_message is not None
        assert "2847" in row.error_message and "2900" in row.error_message
        assert "53" in row.error_message


def test_record_fetch_failures_is_a_no_op_when_nothing_failed():
    from graph.nodes import _record_fetch_failures

    # No run id / no failures must never touch the DB or raise.
    _record_fetch_failures(None, fetched=10, failed=5)
    _record_fetch_failures("does-not-exist", fetched=10, failed=0)


# --- gate follow-ups: the loaded gun and the double-fetch ------------------


def test_an_injected_service_adapter_never_runs_a_concurrent_pool():
    """An adapter built with an injected `service` has exactly ONE service, and
    `_worker_service` hands that same object to every worker — the original
    shared-httplib2 crash. Such an adapter must degrade to a single-worker pool,
    or the bug is simply re-armed for the next caller that passes a service
    directly. Asserted on the pool's actual width, which is the fix itself."""
    import channels.gmail.adapter as adapter_mod
    from channels.gmail.adapter import GmailAdapter

    widths: list[int] = []
    real_pool = adapter_mod.ThreadPoolExecutor

    def _spy(max_workers=None, **kw):
        widths.append(max_workers)
        return real_pool(max_workers=max_workers, **kw)

    threads = {f"t{i}": _thread(f"t{i}") for i in range(12)}

    adapter_mod.ThreadPoolExecutor = _spy
    try:
        injected = GmailAdapter(
            FakeGmailService(threads=threads),
            user_id="u",
            account_email="u@e.com",
            max_workers=10,
        )
        assert len(injected.list_threads(limit=12)) == 12
        # One service -> pool forced to a single worker, never 10.
        assert widths == [1]

        # A factory-backed adapter keeps its real concurrency.
        widths.clear()
        tracker = _Tracker()
        _factory_adapter(threads, tracker=tracker, max_workers=10).list_threads(limit=12)
        assert widths == [10]
    finally:
        adapter_mod.ThreadPoolExecutor = real_pool


def test_fetch_items_does_not_refetch_the_whole_inbox_on_an_internal_typeerror():
    """Regression: `try: list_threads(on_fetch_failed=...) except TypeError:
    list_threads(...)` swallowed ANY TypeError from inside a 2,900-thread fetch
    and silently re-ran the entire fetch — double Gmail quota and runtime, with
    the failure callback lost. Support is now decided by signature inspection,
    so an internal TypeError propagates instead."""
    from graph import nodes

    calls = {"n": 0}

    class _Boom:
        def list_threads(self, *, limit=200, cancel_check=None, after=None,
                         on_page=None, on_fetch_failed=None):
            calls["n"] += 1
            raise TypeError("something inside the fetch, not a bad signature")

        def sender_history(self, *, limit=500):
            return {}

    import channels

    original = channels.get_adapter
    channels.get_adapter = lambda **kw: _Boom()
    try:
        out = nodes.fetch_items(
            {
                "run_id": None,
                "user_id": "u",
                "channel_account_id": "acct",
                "items": [],
                "limit": 5,
            }
        )
    finally:
        channels.get_adapter = original

    # Surfaced as an error, and the inbox was fetched exactly ONCE.
    assert out.get("error")
    assert calls["n"] == 1

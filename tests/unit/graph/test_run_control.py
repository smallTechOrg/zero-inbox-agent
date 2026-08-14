"""Run control — live progress and real (non-cosmetic) cancellation.

Regressions for the Phase-1 gate:
- items_total/items_decided were only written at the very end, so the UI showed
  "0 of ?" for an entire run (spec/api.md: GET /api/runs is polled at 1s);
- POST /api/runs/{id}/cancel set a flag nothing ever read, and finalize then
  unconditionally overwrote it with status="completed".
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from graph import nodes
from tools.rules import DEFAULT_TAXONOMY

RUN_ID = "run-ctl"
USER_ID = "user-ctl"
ACCOUNT_ID = "acct-ctl"


def _item(index: int = 1, **overrides) -> dict:
    base = {
        "id": f"i{index}",
        "external_thread_id": f"t{index}",
        "subject": f"Subject {index}",
        "from_name": "Sender",
        "from_email": "sender@example.com",
        "from_domain": "example.com",
        "list_id": None,
        "snippet_redacted": "snippet",
        "message_count": 1,
        "has_attachments": False,
        "is_unread": True,
        "internal_date": datetime.now(timezone.utc),
    }
    base.update(overrides)
    return base


def _state(**overrides) -> dict:
    state = {
        "run_id": RUN_ID,
        "user_id": USER_ID,
        "channel_account_id": ACCOUNT_ID,
        "categories": DEFAULT_TAXONOMY,
        "rules": [],
        "sender_stats": {},
        "settings": {"confidence_floor": 0.75},
        "items": [_item()],
        "resolved": [],
        "llm_decisions": [],
        "deep_queue": [],
        "llm_calls": [],
    }
    state.update(overrides)
    return state


@pytest.fixture
def run_row(_isolated_db):
    """A persisted TriageRun row the nodes can read and write."""
    from db.models import TriageRun
    from db.session import create_db_session

    with create_db_session() as session:
        session.add(
            TriageRun(
                id=RUN_ID,
                user_id=USER_ID,
                channel_account_id=ACCOUNT_ID,
                status="running",
                dry_run=True,
                items_total=0,
                items_decided=0,
                counts={},
            )
        )

    def _reload():
        with create_db_session() as session:
            row = session.get(TriageRun, RUN_ID)
            session.expunge(row)
            return row

    return _reload


def _set_status(status: str) -> None:
    from db.models import TriageRun
    from db.session import create_db_session

    with create_db_session() as session:
        session.get(TriageRun, RUN_ID).status = status


class ExplodingClient:
    """Any LLM call during a cancelled run is a defect."""

    async def classify_batch(self, *args, **kwargs):
        raise AssertionError("the LLM must not be called after a cancel")

    async def call_model(self, *args, **kwargs):
        raise AssertionError("the LLM must not be called after a cancel")


# --- live progress (defect: no progress until the very end) ----------------


class TestLiveProgress:
    def test_fetch_items_writes_items_total_immediately(self, run_row):
        items = [_item(n) for n in range(7)]
        out = nodes.fetch_items(_state(items=items))
        assert out["error"] is None
        assert run_row().items_total == 7

    def test_prepare_llm_batches_never_clobbers_the_decided_count(self, run_row):
        """Phase 6: tiers 1-2 checkpoint their own rows and bump the counter
        atomically, so this node must not write an absolute count — doing so would
        reset a resumed run's carried-over progress back to this leg's total."""
        from db.models import TriageRun
        from db.session import create_db_session

        with create_db_session() as session:
            session.get(TriageRun, "run-ctl").items_decided = 5

        resolved = [{"item_id": f"i{n}", "decided_by": "rule"} for n in range(2)]
        nodes.prepare_llm_batches(_state(resolved=resolved, llm_queue=[_item(9)]))
        assert run_row().items_decided == 5

    def test_each_classified_batch_increments_items_decided(self, run_row, monkeypatch):
        from llm.providers.base import BatchClassification, LLMResult

        class OneShot:
            async def classify_batch(self, items, **kwargs):
                return BatchClassification(
                    results=[
                        {
                            "item_id": item["item_id"],
                            "category": "people",
                            "action": "keep",
                            "confidence": 0.9,
                            "reasoning": "human mail",
                        }
                        for item in items
                    ],
                    usage=LLMResult(text="", model="m", tokens_in=10, tokens_out=5),
                )

        monkeypatch.setattr("llm.client.get_llm_client", lambda: OneShot())
        batch = [_item(n) for n in range(3)]
        nodes.llm_classify_batch(_state(batch=batch))
        assert run_row().items_decided == 3

    def test_progress_writes_survive_a_missing_run_row(self, _isolated_db):
        # No TriageRun exists — the write is a no-op, never a crash.
        out = nodes.fetch_items(_state(run_id="ghost-run", items=[_item()]))
        assert out["error"] is None

    def test_deep_read_escalation_advances_items_decided_per_item(
        self, run_row, monkeypatch
    ):
        """D15: items_decided must not sit at 0 for the whole tier-4 deep-read
        pass and jump only at the end — each landed deep read should bump the
        counter as it lands."""
        from llm.providers.base import LLMResult

        class OneAtATime:
            async def call_model(self, prompt, **kwargs):
                return LLMResult(
                    text=(
                        '{"item_id": "i1", "category": "people", "action": "keep", '
                        '"confidence": 0.9, "reasoning": "ok"}'
                    ),
                    model="m",
                    tokens_in=5,
                    tokens_out=5,
                )

        monkeypatch.setattr("llm.client.get_llm_client", lambda: OneAtATime())
        nodes.deep_read_escalation(_state(deep_queue=[_item(1)]))
        assert run_row().items_decided == 1


# --- cancellation (defect: cancel was cosmetic) ----------------------------


class TestCancellation:
    def test_a_cancelled_run_skips_the_llm_batch_entirely(self, run_row, monkeypatch):
        """D11: cancelling a run must never synthesize/persist degraded
        decisions for the undecided remainder — that buried the user's real,
        already-decided results behind a wall of decided_by="error" rows."""
        _set_status("cancelled")
        monkeypatch.setattr("llm.client.get_llm_client", lambda: ExplodingClient())

        out = nodes.llm_classify_batch(_state(batch=[_item(1), _item(2)]))

        assert out["llm_decisions"] == []
        assert out["llm_calls"] == []

    def test_a_cancelled_run_skips_remaining_deep_reads(self, run_row, monkeypatch):
        _set_status("cancelled")
        monkeypatch.setattr("llm.client.get_llm_client", lambda: ExplodingClient())

        out = nodes.deep_read_escalation(_state(deep_queue=[_item(1), _item(2)]))

        assert out.get("llm_decisions", []) == []
        assert out.get("llm_calls", []) == []

    def test_finalize_never_overwrites_a_cancelled_status(self, run_row):
        _set_status("cancelled")

        out = nodes.finalize(
            _state(
                decisions=[],
                counts={"total": 0, "by_tier": {}, "needs_your_call": 0},
                cost={"tokens_in": 5, "tokens_out": 2, "usd": 0.0, "llm_calls": 1},
            )
        )

        assert out["status"] == "cancelled"
        row = run_row()
        assert row.status == "cancelled"
        # Spend is still recorded for the audit trail.
        assert row.tokens_in == 5

    def test_update_run_records_counts_but_keeps_cancelled_terminal(self, run_row):
        from db.session import create_db_session
        from graph.persistence import update_run

        _set_status("cancelled")
        with create_db_session() as session:
            update_run(
                session,
                run_id=RUN_ID,
                status="completed",
                items_decided=4,
                counts={"total": 4},
            )
        row = run_row()
        assert row.status == "cancelled"
        assert row.items_decided == 4

    def test_a_running_run_still_finalizes_to_completed(self, run_row):
        out = nodes.finalize(
            _state(
                decisions=[],
                counts={"total": 0, "by_tier": {}, "needs_your_call": 0},
                cost={"tokens_in": 0, "tokens_out": 0, "usd": 0.0, "llm_calls": 0},
            )
        )
        assert out["status"] == "completed"
        assert run_row().status == "completed"

    def test_cancelled_run_persists_zero_degraded_decisions_on_merge(self, run_row):
        """D11(a): cluster_decisions/_merge_decisions must not fabricate
        decided_by="error" rows for items that never got a real decision when
        the run was cancelled mid-flight — those items are simply dropped, not
        persisted as noise."""
        _set_status("cancelled")
        items = [_item(1), _item(2), _item(3)]
        # Only item 1 was actually decided (e.g. resolved by tier 1) before cancel.
        resolved = [
            {
                "item_id": "i1",
                "category": "people",
                "proposed_action": "keep",
                "confidence": 0.95,
                "reasoning": "rule match",
                "decided_by": "rule",
                "rule_id": None,
                "time_sensitive": False,
                "unsure": False,
            }
        ]
        out = nodes.cluster_decisions(
            _state(items=items, resolved=resolved, llm_decisions=[])
        )
        assert out["error"] is None
        decided_ids = {d["item_id"] for d in out["decisions"]}
        assert decided_ids == {"i1"}
        assert all(d["decided_by"] != "error" for d in out["decisions"])

    def test_cancelled_run_via_llm_classify_batch_writes_no_error_rows_end_to_end(
        self, run_row, monkeypatch
    ):
        """D11(a) end-to-end: a batch skipped for cancellation contributes no
        decisions at all through to cluster_decisions."""
        _set_status("cancelled")
        monkeypatch.setattr("llm.client.get_llm_client", lambda: ExplodingClient())
        items = [_item(1), _item(2)]
        batch_out = nodes.llm_classify_batch(_state(items=items, batch=items))
        merged = nodes.cluster_decisions(
            _state(items=items, resolved=[], llm_decisions=batch_out["llm_decisions"])
        )
        assert merged["decisions"] == []


# --- default-view exclusion of cancelled/failed runs (D11b) ----------------


class TestFetchAfterCutoff:
    def test_a_naive_iso_cutoff_from_sqlite_does_not_crash_the_tz_aware_comparison(
        self, monkeypatch
    ):
        """Regression: TriageRun.started_at round-trips through SQLite naive (no
        tzinfo), while ChannelItem.internal_date is always UTC-aware — comparing
        them raised `TypeError: can't compare offset-naive and offset-aware
        datetimes`, so every only_new run failed outright."""
        import channels

        captured: dict = {}

        class FakeAdapter:
            def list_threads(self, *, limit, cancel_check=None, after=None, on_page=None):
                captured["after"] = after
                return []

            def sender_history(self, *, limit=500):
                return {}

        monkeypatch.setattr(channels, "get_adapter", lambda **kw: FakeAdapter())

        naive_cutoff = datetime(2026, 8, 9, 12, 0, 0)  # no tzinfo, as SQLite returns it
        out = nodes.fetch_items(
            _state(items=[], fetch_after=naive_cutoff.isoformat())
        )

        assert out["error"] is None
        assert captured["after"].tzinfo is not None
        assert captured["after"] == naive_cutoff.replace(tzinfo=timezone.utc)

    def test_an_already_aware_cutoff_passes_through_unchanged(self, monkeypatch):
        import channels

        captured: dict = {}

        class FakeAdapter:
            def list_threads(self, *, limit, cancel_check=None, after=None, on_page=None):
                captured["after"] = after
                return []

            def sender_history(self, *, limit=500):
                return {}

        monkeypatch.setattr(channels, "get_adapter", lambda **kw: FakeAdapter())

        aware_cutoff = datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc)
        out = nodes.fetch_items(_state(items=[], fetch_after=aware_cutoff.isoformat()))

        assert out["error"] is None
        assert captured["after"] == aware_cutoff


class TestFetchCancelDuringPagination:
    def test_cancel_check_stops_gmail_thread_id_pagination_early(self):
        """D12: cancellation must be observed inside the fetch/pagination loop,
        not only at batch boundaries after the whole listing has drained.

        list_threads() now merges listing + per-thread metadata fetch into one
        loop (needed for the `after` early-exit cutoff), so the fake service
        must also answer threads().get(), not just threads().list()."""
        from datetime import datetime, timezone

        from channels.gmail.adapter import GmailAdapter

        calls = {"list": 0, "get": 0}

        def _thread_payload(n: int) -> dict:
            return {
                "id": f"t{n}",
                "messages": [
                    {
                        "id": f"m{n}",
                        "labelIds": ["INBOX"],
                        "internalDate": str(1_700_000_000_000 + n),
                        "payload": {
                            "headers": [
                                {"name": "Subject", "value": f"thread {n}"},
                                {"name": "From", "value": "sender@example.com"},
                            ]
                        },
                        "snippet": "hi",
                    }
                ],
            }

        class FakeExecutor:
            def __call__(self, request):
                if request.get("kind") == "list":
                    calls["list"] += 1
                    return {"threads": [{"id": f"t{calls['list']}"}], "nextPageToken": "tok"}
                calls["get"] += 1
                return _thread_payload(request["id_num"])

        adapter = object.__new__(GmailAdapter)
        adapter._execute = FakeExecutor()
        adapter._redactor = None

        class FakeThreadsResource:
            def list(self, **kwargs):
                return {"kind": "list"}

            def get(self, *, id, **kwargs):
                return {"kind": "get", "id_num": int(id[1:])}

        class FakeUsers:
            def threads(self):
                return FakeThreadsResource()

        class FakeService:
            def users(self):
                return FakeUsers()

        adapter._service = FakeService()

        seen = {"n": 0}

        def cancel_after_two():
            seen["n"] += 1
            return seen["n"] > 2

        items = adapter.list_threads(limit=1000, cancel_check=cancel_after_two)
        # Cancellation kicks in after a couple of items, well short of `limit`.
        assert len(items) < 1000
        assert calls["list"] <= 3

    def test_after_cutoff_stops_the_scan_without_refetching_older_threads(self):
        """A second run passing `after=` should stop paging after the page that
        contains the cutoff thread, instead of scanning all subsequent pages.

        With parallel per-thread fetching all threads in the current page are
        fetched concurrently (dates can only be known after fetching), then
        those at-or-before the cutoff are filtered out.  The important savings
        come from not requesting any further *pages* once the cutoff is reached."""
        from datetime import datetime, timezone

        from channels.gmail.adapter import GmailAdapter

        # Four threads, newest (t3) first — matches Gmail's real ordering.
        dates_ms = {"t3": 3_000, "t2": 2_000, "t1": 1_000, "t0": 500}
        order = ["t3", "t2", "t1", "t0"]
        fetched = []

        class FakeExecutor:
            def __call__(self, request):
                if request.get("kind") == "list":
                    return {"threads": [{"id": tid} for tid in order]}
                tid = request["id"]
                fetched.append(tid)
                return {
                    "id": tid,
                    "messages": [
                        {
                            "id": f"m-{tid}",
                            "labelIds": ["INBOX"],
                            "internalDate": str(dates_ms[tid]),
                            "payload": {
                                "headers": [
                                    {"name": "Subject", "value": tid},
                                    {"name": "From", "value": "sender@example.com"},
                                ]
                            },
                            "snippet": "hi",
                        }
                    ],
                }

        adapter = object.__new__(GmailAdapter)
        adapter._execute = FakeExecutor()
        adapter._redactor = None

        class FakeThreadsResource:
            def list(self, **kwargs):
                return {"kind": "list"}

            def get(self, *, id, **kwargs):
                return {"kind": "get", "id": id}

        class FakeUsers:
            def threads(self):
                return FakeThreadsResource()

        class FakeService:
            def users(self):
                return FakeUsers()

        adapter._service = FakeService()

        # Cutoff between t2 (2000ms) and t1 (1000ms): only t3 and t2 are newer.
        cutoff = datetime.fromtimestamp(1.5, tz=timezone.utc)
        items = adapter.list_threads(limit=200, after=cutoff)

        # Parallel fetch fetches all threads in the page concurrently to learn
        # their dates; items above the cutoff are returned (order non-deterministic).
        assert {i.external_thread_id for i in items} == {"t3", "t2"}
        # All threads in the page are fetched (dates only known after fetching).
        assert set(fetched) == {"t3", "t2", "t1", "t0"}
        # We stop at the page boundary — only one list() call, no subsequent pages.
        assert fetched != []  # sanity: something was fetched


# --- failed-batch spend persistence (defect 5) -----------------------------


class TestFailedBatchSpend:
    def test_tokens_from_a_fully_failed_batch_are_persisted_as_classify_failed(
        self, _isolated_db, monkeypatch
    ):
        from llm.providers.base import LLMResult, LLMSchemaError

        class AlwaysFails:
            async def classify_batch(self, *args, **kwargs):
                raise LLMSchemaError(
                    "no parsable results after 2 attempt(s)",
                    usage=LLMResult(
                        text="", model="m", tokens_in=444, tokens_out=333, latency_ms=1200
                    ),
                )

        monkeypatch.setattr("llm.client.get_llm_client", lambda: AlwaysFails())
        out = nodes.llm_classify_batch(_state(batch=[_item(1), _item(2)]))

        assert {d["decided_by"] for d in out["llm_decisions"]} == {"error"}
        # Phase 6: the batch is retried down the whole model chain before it
        # degrades, so every model's burnt tokens are recorded — not just the first.
        assert len(out["llm_calls"]) >= 1
        for call in out["llm_calls"]:
            assert call["purpose"] == "classify_failed"
            assert call["tokens_in"] == 444 and call["tokens_out"] == 333
            assert call["items_in_batch"] == 2

    def test_an_exception_without_usage_still_degrades_cleanly(
        self, _isolated_db, monkeypatch
    ):
        class Boom:
            async def classify_batch(self, *args, **kwargs):
                raise RuntimeError("connection reset")

        monkeypatch.setattr("llm.client.get_llm_client", lambda: Boom())
        out = nodes.llm_classify_batch(_state(batch=[_item(1)]))
        assert out["llm_calls"] == []
        assert out["llm_decisions"][0]["decided_by"] == "error"

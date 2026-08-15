"""Re-organise everything — over a real 10,000-decision mailbox (Phase 9, slice 5).

The user's instruction was *"Re-organise everything."* The failure mode that
would betray it is not a crash; it is a **silent sample** — a job that does the
first few hundred threads, reports a tidy ledger and lets the user find out from
Gmail. Ten thousand rows is the smallest fixture at which a sampled run and a
full run are observably different, so every assertion here is about the
arithmetic being complete, not about the happy path working.

What this file proves, end to end, against the production SQLAlchemy models and
the real ``tools.actions`` mutation path:

* **every row is accounted for** — ``done + sum(skipped.values()) == 10000``,
  with the exact expected count in each named bucket;
* **already-archived threads are relabelled and stay archived** — ``INBOX`` is
  never re-added, so re-filing cannot resurrect a cleared inbox;
* **resume is real** — killed at ~40 %, the job finishes with **zero** duplicate
  mutations and hands strictly fewer threads to the re-classifier;
* **bulk undo is one operation and idempotent** — every pre-job label set comes
  back, and a second call makes zero Gmail calls;
* **``dry_run`` is absolute** — zero mutations, complete ledger;
* **an unreviewed row is never mutated** — counted ``not_reviewed``, and
  ``NotReviewedError`` fires **before the mutator** even with the ledger's own
  pre-check taken out of the way;
* **nothing destructive is representable** — every ``ActionLog.operation`` is in
  ``{archive, add_label, remove_label}`` and every one carries an undo token.

Every test runs on the ``_isolated_db`` fixture. No test here starts a triage
run, touches the live account or opens a Gmail connection.
"""

from __future__ import annotations

import pytest

from db.session import create_db_session
from jobs import reorganise
from tests.fixtures.phase9 import decisions_10k

pytestmark = pytest.mark.usefixtures("_isolated_db")

USER = decisions_10k.USER_ID


class RecordingMutator:
    """A Gmail stand-in that records every call and can refuse named threads.

    It is deliberately *not* a mock of ``tools.actions``: the real actions module
    runs, with its real gate, its real ``ActionLog`` writes and its real undo
    tokens. Only the transport is replaced — which is the seam the ``_no_real_gmail``
    guard exists to keep out of production.
    """

    def __init__(self, fail_for: set[str] | None = None) -> None:
        self.fail_for = fail_for or set()
        self.calls: list[tuple[str, str]] = []
        self.labels: dict[str, list[str]] = {}
        self.on_mutation = None

    def _mutate(self, thread_id: str, op: str, *, add=(), remove=()):
        self.calls.append((op, thread_id))
        if thread_id in self.fail_for:
            from channels.base import ChannelError

            raise ChannelError(f"Gmail refused {thread_id}")
        current = set(self.labels.get(thread_id, []))
        current.difference_update(remove)
        current.update(add)
        self.labels[thread_id] = sorted(current)
        if self.on_mutation is not None:
            self.on_mutation(self)
        return {"thread_id": thread_id, "label_ids": sorted(current)}

    def get_thread_labels(self, thread_id: str) -> list[str]:
        return list(self.labels.get(thread_id, []))

    def archive_and_label(self, thread_id: str, *, category_label_id: str) -> dict:
        return self._mutate(
            thread_id, "archive_and_label", add=[category_label_id], remove=["INBOX"]
        )

    def undo_archive_and_label(self, thread_id: str, *, category_label_id: str) -> dict:
        return self._mutate(
            thread_id, "undo_archive_and_label", add=["INBOX"], remove=[category_label_id]
        )

    def restore_labels(self, thread_id: str, *, add_label_ids, remove_label_ids) -> dict:
        return self._mutate(
            thread_id, "restore_labels", add=add_label_ids, remove=remove_label_ids
        )

    @property
    def mutations(self) -> list[tuple[str, str]]:
        return list(self.calls)


class FakeLabels:
    """``ensure_label`` over the fixture's label ids. Idempotent, like Gmail's."""

    def __init__(self, mapping: dict[str, str]) -> None:
        self.mapping = mapping

    def ensure_label(self, name: str) -> dict:
        key = name.rsplit("/", 1)[-1].lower()
        return {"id": self.mapping.get(key, f"Label_{key}"), "name": name}


@pytest.fixture
def seeded():
    """The 10,000-decision fixture, seeded once per test, and its expected ledger."""
    with create_db_session() as session:
        meta = decisions_10k.seed(session)
        session.commit()
    mutator = RecordingMutator(fail_for=set(meta["error_thread_ids"]))
    # Gmail's own view of each thread's labels, so `get_thread_labels` returns the
    # truth the undo token must later restore.
    with create_db_session() as session:
        from db.models import Item
        from sqlalchemy import select

        for item in session.execute(select(Item).where(Item.user_id == USER)).scalars():
            mutator.labels[item.external_thread_id] = list(item.channel_labels or [])
    labels = FakeLabels({key.lower(): value for key, value in meta["label_ids"].items()})
    return meta, mutator, labels


@pytest.fixture
def reclassify_spy(monkeypatch):
    """Proves the long-tail seam is CALLED, and with which threads.

    The re-classification implementation itself is exercised against the real
    NVIDIA NIM endpoint by
    ``test_the_long_tail_really_goes_through_the_existing_graph`` below. Here we
    assert the wiring — which is the failure this codebase actually produces:
    a seam that exists, is never called, and whose tests pass anyway.
    """
    calls: list[list[str]] = []

    def _spy(*, user_id, channel_account_id, run_id, items):
        calls.append([str(i["id"]) for i in items])
        return {}

    monkeypatch.setattr(reorganise, "reclassify_via_graph", _spy)
    return calls


def _run(job_id: str, mutator, labels, meta) -> dict:
    return reorganise.execute(
        job_id,
        user_id=USER,
        mutator=mutator,
        label_lookup=labels,
        channel_account_id=meta["account_id"],
    )


def _run_cancelling_after_batches(job_id, mutator, labels, meta, *, batches: int) -> dict:
    """Run the job, flipping the real cancel flag after ``batches`` batches.

    ``_is_cancelled`` is the exact predicate the worker consults between batches,
    so replacing its *answer* still exercises the whole cancellation path —
    cursor write, ledger balance, terminal status. Writing ``status='cancelled'``
    from another connection mid-mutation instead would just deadlock SQLite
    against the worker's own transaction, and would be testing the test.
    """
    original = reorganise._is_cancelled
    seen = {"n": 0}

    def _flag(session_factory, jid):
        if jid != job_id:
            return original(session_factory, jid)
        seen["n"] += 1
        return seen["n"] > batches

    reorganise._is_cancelled = _flag
    try:
        return _run(job_id, mutator, labels, meta)
    finally:
        reorganise._is_cancelled = original


# --- the whole mailbox, accounted for -------------------------------------------


def test_every_one_of_ten_thousand_rows_is_accounted_for(seeded, reclassify_spy):
    meta, mutator, labels = seeded

    with create_db_session() as session:
        job_id = reorganise.start(session, user_id=USER, dry_run=False)
        assert session.get(
            __import__("db.models", fromlist=["ReorgJob"]).ReorgJob, job_id
        ).total == 10_000, "the scope is counted, never estimated and never capped"
        session.commit()

    payload = _run(job_id, mutator, labels, meta)

    assert payload["total"] == 10_000
    assert payload["done"] == meta["expected_done"]
    assert payload["skipped"] == meta["expected_skipped"]
    # The invariant. A sampled run cannot satisfy this, which is why the fixture
    # is ten thousand rows and not ten.
    assert payload["done"] + sum(payload["skipped"].values()) == 10_000
    # Anything was skipped, so the job is `partial` and says why — in words.
    assert payload["status"] == "partial"
    assert "5000" in payload["error_message"] or "5,000" in payload["error_message"] or (
        "already filed" in payload["error_message"]
    )


def test_the_long_tail_is_actually_handed_to_the_re_classifier(seeded, reclassify_spy):
    """The seam is wired, not merely present.

    Cut it — stop passing the unresolved items — and this fails, which is the
    only way to know the ~400 threads tier 1 cannot resolve are not silently
    dropped.
    """
    meta, mutator, labels = seeded
    with create_db_session() as session:
        job_id = reorganise.start(session, user_id=USER, dry_run=False)
        session.commit()

    _run(job_id, mutator, labels, meta)

    handed_over = [item_id for batch in reclassify_spy for item_id in batch]
    assert len(handed_over) == 400, "every tier-1-unresolved thread must be re-classified"
    assert len(set(handed_over)) == 400, "and none of them twice"


def test_already_archived_threads_are_relabelled_and_never_return_to_the_inbox(
    seeded, reclassify_spy
):
    """Re-filing must not resurrect an inbox the user has already cleared."""
    meta, mutator, labels = seeded
    with create_db_session() as session:
        job_id = reorganise.start(session, user_id=USER, dry_run=False)
        session.commit()

    _run(job_id, mutator, labels, meta)

    archived_ids = set(meta["buckets"]["relabel_archived"])
    with create_db_session() as session:
        from sqlalchemy import select
        from db.models import Decision, Item

        rows = [
            (thread_id, from_email, list(channel_labels or []), category_id)
            for thread_id, from_email, channel_labels, category_id in session.execute(
                select(
                    Item.external_thread_id,
                    Item.from_email,
                    Item.channel_labels,
                    Decision.category_id,
                )
                .join(Item, Decision.item_id == Item.id)
                .where(Decision.id.in_(sorted(archived_ids)))
            )
        ]

    assert len(rows) == 3_000
    for thread_id, from_email, channel_labels, category_id in rows:
        labels_now = set(channel_labels)
        assert "INBOX" not in labels_now, (
            f"{thread_id} was pushed back into the inbox by a relabel"
        )
        assert category_id == meta["categories"][_target_key(from_email)]
        assert set(mutator.labels[thread_id]) == labels_now


def _target_key(from_email: str) -> str:
    for email, _domain, key in decisions_10k.CONCENTRATED_SENDERS:
        if email == from_email:
            return key
    raise AssertionError(f"no target category for {from_email}")


def test_every_mutation_is_reversible_and_nothing_destructive_is_representable(
    seeded, reclassify_spy
):
    meta, mutator, labels = seeded
    with create_db_session() as session:
        job_id = reorganise.start(session, user_id=USER, dry_run=False)
        session.commit()

    payload = _run(job_id, mutator, labels, meta)

    with create_db_session() as session:
        from sqlalchemy import select
        from db.models import ActionLog

        logs = list(
            session.execute(
                select(ActionLog.operation, ActionLog.undo_token).where(
                    ActionLog.reorg_job_id == job_id
                )
            )
        )

    assert len(logs) == payload["done"] == 4_000
    assert {operation for operation, _token in logs} <= {
        "archive",
        "add_label",
        "remove_label",
    }
    assert all(token for _operation, token in logs), (
        "a mutation we cannot reverse is not an acceptable outcome"
    )
    assert all(
        token.get("original_label_ids") is not None for _operation, token in logs
    )


# --- the unreviewed rows --------------------------------------------------------


def test_an_unreviewed_row_is_counted_and_never_mutated(seeded, reclassify_spy):
    meta, mutator, labels = seeded
    with create_db_session() as session:
        job_id = reorganise.start(session, user_id=USER, dry_run=False)
        session.commit()

    payload = _run(job_id, mutator, labels, meta)

    assert payload["skipped"]["not_reviewed"] == 500

    unreviewed = set(meta["buckets"]["not_reviewed"])
    with create_db_session() as session:
        from sqlalchemy import select
        from db.models import ActionLog, Decision

        touched = session.execute(
            select(ActionLog.id).where(ActionLog.decision_id.in_(sorted(unreviewed)))
        ).scalars().all()
        states = set(
            session.execute(
                select(Decision.review_state).where(Decision.id.in_(sorted(unreviewed)))
            ).scalars()
        )

    assert touched == [], "an unreviewed thread must never reach the mutator"
    # And the job never wrote review_state to get past the gate.
    assert states == {"provisional", "review_failed"}


def test_not_reviewed_error_fires_before_the_mutator_even_without_the_ledger_check(
    seeded,
):
    """The ledger's pre-check is the explanation. This is the fence.

    ``mutate_one`` counts an unreviewed row before calling anything — but if that
    count were deleted tomorrow, ``tools.actions`` must still refuse. Asserted by
    calling the mutation path directly on a ``provisional`` row and proving the
    mutator was never touched.
    """
    from tools.actions import NotReviewedError

    meta, mutator, labels = seeded
    provisional_id = sorted(meta["buckets"]["not_reviewed"])[0]

    with create_db_session() as session:
        from db.models import Decision

        decision = session.get(Decision, provisional_id)
        decision.review_state = "provisional"
        decision.status = "approved"
        session.flush()

        before = len(mutator.calls)
        with pytest.raises(NotReviewedError):
            from tools import actions

            actions.relabel_decision(
                session,
                USER,
                provisional_id,
                mutator=mutator,
                label_lookup=labels,
                dry_run=False,
                keep_archived=True,
            )
        assert len(mutator.calls) == before


# --- dry run --------------------------------------------------------------------


def test_dry_run_mutates_nothing_and_still_produces_the_complete_ledger(
    seeded, reclassify_spy
):
    meta, mutator, labels = seeded
    with create_db_session() as session:
        job_id = reorganise.start(session, user_id=USER, dry_run=True)
        session.commit()

    payload = _run(job_id, mutator, labels, meta)

    assert mutator.calls == [], "dry_run is absolute — zero Gmail calls"
    assert payload["total"] == 10_000
    assert payload["done"] == 0
    # Everything that WOULD have been re-filed is named under `dry_run`, so the
    # preview is a full answer rather than a blank. It includes the 100 threads a
    # live run would have lost to a Gmail error: a dry run cannot know Gmail will
    # refuse, and must not pretend it does.
    assert payload["skipped"]["dry_run"] == meta["expected_done"] + 100
    assert payload["skipped"]["already_correct"] == 5_000
    assert payload["done"] + sum(payload["skipped"].values()) == 10_000

    with create_db_session() as session:
        from sqlalchemy import func, select
        from db.models import ActionLog

        assert (
            session.execute(select(func.count(ActionLog.id))).scalar_one() == 0
        ), "a dry run wrote an action log"


# --- resume ---------------------------------------------------------------------


def test_killed_at_forty_percent_it_resumes_with_zero_duplicate_mutations(
    seeded, reclassify_spy
):
    meta, mutator, labels = seeded
    from db.models import ReorgJob

    with create_db_session() as session:
        job_id = reorganise.start(session, user_id=USER, dry_run=False)
        session.commit()

    # The kill: ~40 % of the way through, the flag the worker consults between
    # batches goes true — the real cancellation seam, so what is being tested is
    # the code path a process death actually leaves behind: cursor written, work
    # durable, nothing half-applied.
    first = _run_cancelling_after_batches(job_id, mutator, labels, meta, batches=20)
    assert first["status"] == "cancelled"
    assert 0 < first["done"] < meta["expected_done"]
    first_pass_calls = len(mutator.calls)
    first_pass_ids = {item_id for batch in reclassify_spy for item_id in batch}

    # Resume: the same row, put back to running. No new job, no re-done thread.
    with create_db_session() as session:
        job = session.get(ReorgJob, job_id)
        job.status = "running"
        job.finished_at = None
        job.error_message = None
        session.commit()

    reclassify_spy.clear()
    second = _run(job_id, mutator, labels, meta)
    resume_ids = {item_id for batch in reclassify_spy for item_id in batch}

    assert second["done"] == meta["expected_done"]
    assert second["skipped"] == meta["expected_skipped"]
    assert second["done"] + sum(second["skipped"].values()) == 10_000

    # Zero duplicate mutations: across BOTH passes, every thread Gmail was asked
    # about was asked about exactly once — the 4,000 that succeeded plus the 100
    # Gmail refused. A resume that re-did its predecessor's work would double
    # these numbers, and a resume that skipped work would fall short of them.
    attempted = [c for c in mutator.calls if c[0] != "get_thread_labels"]
    expected_attempts = meta["expected_done"] + 100
    assert len(attempted) == expected_attempts
    assert len({thread for _op, thread in attempted}) == expected_attempts
    assert first_pass_calls < len(mutator.calls)

    # Strictly fewer threads re-classified on the resume than a from-zero run
    # would have handed over, and between them they cover the tail exactly once.
    assert len(resume_ids) < 400, "the resume re-asked the model the whole tail"
    assert len(first_pass_ids | resume_ids) == 400

    # The only overlap possible is the single batch that was in flight when the
    # job stopped, and it is bounded by one batch — never the whole tail. Those
    # threads cost nothing the second time: the re-classification runs on the
    # JOB'S OWN run id, so the graph's ``already_decided_item_ids`` drops them
    # before a token is spent. That is why the re-classifier is given a run id at
    # all rather than a fresh one per batch.
    overlap = first_pass_ids & resume_ids
    assert len(overlap) <= reorganise.BATCH_SIZE


def test_a_cancelled_job_names_what_it_never_reached_rather_than_dropping_it(
    seeded, reclassify_spy
):
    """A job that stops early still balances. `cancelled` is a count, not a shrug."""
    meta, mutator, labels = seeded

    with create_db_session() as session:
        job_id = reorganise.start(session, user_id=USER, dry_run=False)
        session.commit()

    payload = _run_cancelling_after_batches(job_id, mutator, labels, meta, batches=3)

    assert payload["status"] == "cancelled"
    assert payload["skipped"]["cancelled"] > 0
    assert payload["done"] + sum(payload["skipped"].values()) == 10_000


# --- bulk undo ------------------------------------------------------------------


def test_bulk_undo_restores_every_pre_job_label_set_in_one_operation(
    seeded, reclassify_spy
):
    meta, mutator, labels = seeded
    before = {thread: list(lbls) for thread, lbls in mutator.labels.items()}

    with create_db_session() as session:
        job_id = reorganise.start(session, user_id=USER, dry_run=False)
        session.commit()
    payload = _run(job_id, mutator, labels, meta)
    assert payload["done"] == 4_000
    assert any(
        set(mutator.labels[t]) != set(before[t]) for t in before
    ), "the job must actually have changed something before undo means anything"

    with create_db_session() as session:
        result = reorganise.undo(session, job_id=job_id, user_id=USER, mutator=mutator)
        session.commit()

    assert result["reversed"] == 4_000
    assert result["failed"] == []
    for thread, original in before.items():
        assert set(mutator.labels[thread]) == set(original), (
            f"{thread} did not come back to its exact pre-job label set"
        )

    calls_after_undo = len(mutator.calls)
    with create_db_session() as session:
        second = reorganise.undo(session, job_id=job_id, user_id=USER, mutator=mutator)
        session.commit()

    assert second == {"reversed": 0, "already_undone": 4_000, "failed": []}
    assert len(mutator.calls) == calls_after_undo, "a second undo made a Gmail call"

    with create_db_session() as session:
        assert reorganise.ledger(session, job_id=job_id)["undoable"] is False


# --- the long tail, for real ----------------------------------------------------


@pytest.mark.usefixtures("_require_llm_key")
def test_the_long_tail_really_goes_through_the_existing_graph(seeded):
    """The three threads tier 1 cannot resolve go through the **real** graph.

    Small on purpose — three threads, one real NVIDIA NIM call chain — because
    the point is the seam, not the volume: ``reclassify_via_graph`` must invoke
    ``graph.runner.execute_triage`` on the job's own run id, with ``dry_run=True``
    so the graph's auto-apply never fires, and come back with categories the
    re-organiser can file under. The 10,000-row tests spy on this seam; this one
    executes it.
    """
    meta, _mutator, _labels = seeded

    items = [
        {
            "id": f"test-tail-{n}",
            "external_thread_id": f"thread-tail-{n}",
            "subject": subject,
            "from_email": "a.human@partner.example.com",
            "from_domain": "partner.example.com",
            "snippet_redacted": snippet,
            "channel_labels": ["INBOX"],
        }
        for n, (subject, snippet) in enumerate(
            [
                ("Your invoice for August", "Attached is your monthly invoice."),
                ("Newsletter: this week in theatre", "Our weekly round-up of shows."),
                ("Can we move our call to Thursday?", "Checking if Thursday works."),
            ]
        )
    ]

    targets = reorganise.reclassify_via_graph(
        user_id=USER,
        channel_account_id=meta["account_id"],
        run_id="test-reorg-tail-run",
        items=items,
    )

    assert targets, "the long tail resolved nothing — the graph seam is not wired"
    assert set(targets) <= {i["id"] for i in items}
    assert set(targets.values()) <= set(decisions_10k.CATEGORIES)

    with create_db_session() as session:
        from sqlalchemy import select
        from db.models import Decision

        statuses = list(
            session.execute(
                select(Decision.status).where(Decision.run_id == "test-reorg-tail-run")
            ).scalars()
        )
    assert statuses, "the re-classification must persist onto the JOB's run id"
    # dry_run=True on the graph invocation: the re-organiser owns the mutation
    # pass, so the graph's own auto-apply must not have archived anything.
    assert "applied" not in statuses

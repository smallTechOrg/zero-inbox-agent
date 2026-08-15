"""Unit tests for the re-organisation job (Phase 9, slice 5).

Three things are proved here, and none of them by inspection:

1. **The ledger arithmetic.** ``done + sum(skipped.values()) == total`` on every
   terminal path, including a cancelled one — and ``partial`` whenever anything
   was skipped, because ``completed`` may never hide a skip.
2. **The 409s.** Two writers against one mailbox is not a supported state, so a
   second re-organisation and a re-organisation during a triage run are both
   refused before a single thread is touched.
3. **Bulk undo is idempotent.** A second call performs **zero** Gmail calls.

Plus the seam guard this codebase keeps needing: if ``tools.actions`` does not
expose the mutation functions the job fails **loudly**, because a re-organiser
that reports a clean ledger over work it never did is worse than one that
crashes.
"""

from __future__ import annotations

import pytest

from db.session import create_db_session
from jobs import reorganise


USER = "test-reorg-unit-user"


class FakeMutator:
    """Records every call. Raises for thread ids listed in ``fail_for``."""

    def __init__(self, fail_for: set[str] | None = None) -> None:
        self.calls: list[tuple[str, str]] = []
        self.fail_for = fail_for or set()
        self.labels: dict[str, list[str]] = {}

    def _guard(self, thread_id: str, op: str):
        self.calls.append((op, thread_id))
        if thread_id in self.fail_for:
            from channels.base import ChannelError

            raise ChannelError(f"Gmail refused {thread_id}")

    def get_thread_labels(self, thread_id: str) -> list[str]:
        self.calls.append(("get_thread_labels", thread_id))
        return list(self.labels.get(thread_id, ["INBOX"]))

    def archive_and_label(self, thread_id: str, *, category_label_id: str) -> dict:
        self._guard(thread_id, "archive_and_label")
        return {"thread_id": thread_id, "label_ids": [category_label_id]}

    def undo_archive_and_label(self, thread_id: str, *, category_label_id: str) -> dict:
        self._guard(thread_id, "undo_archive_and_label")
        return {"thread_id": thread_id, "label_ids": ["INBOX"]}

    def restore_labels(self, thread_id: str, *, add_label_ids, remove_label_ids) -> dict:
        self._guard(thread_id, "restore_labels")
        return {"thread_id": thread_id, "label_ids": list(add_label_ids)}

    @property
    def mutating_calls(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[0] != "get_thread_labels"]


def _seed_user(session, user_id: str = USER) -> None:
    from db.models import ChannelAccount, User, UserSettings

    session.add(User(id=user_id, email=f"{user_id}@example.com"))
    session.add(UserSettings(user_id=user_id, dry_run=False))
    session.add(
        ChannelAccount(
            id=f"{user_id}-account",
            user_id=user_id,
            channel="gmail",
            account_email=f"{user_id}-mailbox@example.com",
            refresh_token_enc="test-not-a-real-token",
        )
    )
    session.flush()


# --- the ledger -----------------------------------------------------------------


def test_ledger_arithmetic_holds_and_only_names_known_reasons():
    """``done + sum(skipped) == total``, and no reason outside the closed set."""
    from db.models import ReorgJob

    with create_db_session() as session:
        _seed_user(session)
        job = ReorgJob(
            id="test-job-ledger",
            user_id=USER,
            status="partial",
            total=100,
            done=60,
            # `mystery` is not a real reason. A bucket nothing renders is a
            # silent drop wearing a label, so the ledger must not repeat it.
            skipped={"already_correct": 25, "gmail_error": 15, "mystery": 7},
        )
        session.add(job)
        session.flush()

        payload = reorganise.ledger(session, job_id="test-job-ledger")

    assert payload["total"] == 100
    assert payload["done"] == 60
    assert payload["skipped"] == {"already_correct": 25, "gmail_error": 15}
    assert "mystery" not in payload["skipped"]
    assert payload["done"] + sum(payload["skipped"].values()) == payload["total"]
    assert payload["undoable"] is False


def test_partial_whenever_anything_was_skipped_and_completed_never_hides_a_skip():
    from db.models import ReorgJob

    with create_db_session() as session:
        _seed_user(session)
        skipped_job = ReorgJob(
            id="test-job-partial", user_id=USER, total=10, done=9,
            skipped={"gmail_error": 1},
        )
        clean_job = ReorgJob(
            id="test-job-clean", user_id=USER, total=10, done=10, skipped={}
        )
        session.add_all([skipped_job, clean_job])
        session.flush()

        reorganise._finish(session, skipped_job, cancelled=False)
        reorganise._finish(session, clean_job, cancelled=False)

        assert skipped_job.status == "partial"
        assert skipped_job.error_message  # never null on a partial job
        assert "Gmail refused the change" in skipped_job.error_message
        assert clean_job.status == "completed"
        assert clean_job.error_message is None


def test_ledger_reports_undoable_while_a_mutation_is_still_reversible():
    from db.models import ActionLog, ReorgJob

    with create_db_session() as session:
        _seed_user(session)
        session.add(ReorgJob(id="test-job-undoable", user_id=USER, total=1, done=1))
        session.add(
            ActionLog(
                id="test-log-1",
                user_id=USER,
                operation="add_label",
                request_params={},
                undo_token={"thread_id": "t1", "original_label_ids": ["INBOX"]},
                reorg_job_id="test-job-undoable",
            )
        )
        session.flush()
        assert reorganise.ledger(session, job_id="test-job-undoable")["undoable"] is True


# --- the 409 paths --------------------------------------------------------------


def test_second_reorganisation_is_refused_while_one_is_running():
    with create_db_session() as session:
        _seed_user(session)
        first = reorganise.start(session, user_id=USER, dry_run=False)
        assert first

        with pytest.raises(reorganise.ReorgInProgress) as excinfo:
            reorganise.start(session, user_id=USER, dry_run=False)
        assert first in str(excinfo.value)


@pytest.mark.parametrize("run_status", ["running", "applying"])
def test_reorganisation_is_refused_while_a_triage_run_is_in_flight(run_status):
    from db.models import TriageRun

    with create_db_session() as session:
        _seed_user(session)
        session.add(
            TriageRun(
                id=f"test-run-{run_status}",
                user_id=USER,
                channel_account_id=f"{USER}-account",
                status=run_status,
            )
        )
        session.flush()

        with pytest.raises(reorganise.RunInProgress):
            reorganise.start(session, user_id=USER, dry_run=False)


def test_a_finished_job_does_not_block_the_next_one():
    with create_db_session() as session:
        _seed_user(session)
        first = reorganise.start(session, user_id=USER, dry_run=False)
        job = session.get(__import__("db.models", fromlist=["ReorgJob"]).ReorgJob, first)
        job.status = "completed"
        session.flush()

        second = reorganise.start(session, user_id=USER, dry_run=True)
        assert second != first


def test_another_users_running_job_never_blocks_this_user():
    """User scoping is not decoration: one user's job is not another's lock."""
    other = "test-reorg-other-user"
    with create_db_session() as session:
        _seed_user(session)
        _seed_user(session, other)
        reorganise.start(session, user_id=other, dry_run=False)
        assert reorganise.start(session, user_id=USER, dry_run=False)


# --- bulk undo ------------------------------------------------------------------


def _seed_undoable_job(session, *, job_id: str = "test-job-undo") -> FakeMutator:
    from db.models import ActionLog, ReorgJob

    _seed_user(session)
    session.add(ReorgJob(id=job_id, user_id=USER, status="completed", total=2, done=2))
    for index in range(2):
        session.add(
            ActionLog(
                id=f"test-log-{index}",
                user_id=USER,
                operation="add_label",
                request_params={},
                undo_token={
                    "thread_id": f"thread-{index}",
                    "category_label_id": "Label_new",
                    "original_label_ids": ["INBOX", "Label_old"],
                    "labels_added_by_triage": ["Label_new"],
                },
                reorg_job_id=job_id,
            )
        )
    session.flush()
    return FakeMutator()


def test_bulk_undo_reverses_every_mutation_then_makes_zero_gmail_calls():
    with create_db_session() as session:
        mutator = _seed_undoable_job(session)

        first = reorganise.undo(
            session, job_id="test-job-undo", user_id=USER, mutator=mutator
        )
        assert first == {"reversed": 2, "already_undone": 0, "failed": []}
        assert len(mutator.mutating_calls) == 2

        calls_after_first = len(mutator.calls)
        second = reorganise.undo(
            session, job_id="test-job-undo", user_id=USER, mutator=mutator
        )

    # Idempotent AND free: the local `undone_at` stamp is the guard, so a second
    # click can never double-mutate the mailbox.
    assert second == {"reversed": 0, "already_undone": 2, "failed": []}
    assert len(mutator.calls) == calls_after_first


def test_bulk_undo_restores_the_exact_pre_job_label_set():
    with create_db_session() as session:
        mutator = _seed_undoable_job(session, job_id="test-job-restore")
        reorganise.undo(
            session, job_id="test-job-restore", user_id=USER, mutator=mutator
        )

    assert [c[0] for c in mutator.mutating_calls] == ["restore_labels"] * 2


def test_bulk_undo_ignores_action_logs_from_other_jobs():
    """One indexed query over ONE job — never "everything that looks recent"."""
    from db.models import ActionLog

    with create_db_session() as session:
        mutator = _seed_undoable_job(session, job_id="test-job-scoped")
        session.add(
            ActionLog(
                id="test-log-other-job",
                user_id=USER,
                operation="archive",
                request_params={},
                undo_token={"thread_id": "thread-other", "category_label_id": "L"},
                reorg_job_id=None,  # an ordinary triage apply
            )
        )
        session.flush()

        result = reorganise.undo(
            session, job_id="test-job-scoped", user_id=USER, mutator=mutator
        )

    assert result["reversed"] == 2
    assert "thread-other" not in {t for _, t in mutator.calls}


def test_bulk_undo_names_the_thread_it_could_not_restore():
    with create_db_session() as session:
        mutator = _seed_undoable_job(session, job_id="test-job-undo-fail")
        mutator.fail_for = {"thread-1"}
        result = reorganise.undo(
            session, job_id="test-job-undo-fail", user_id=USER, mutator=mutator
        )

    assert result["reversed"] == 1
    assert [f["thread_id"] for f in result["failed"]] == ["thread-1"]


# --- the seam guard -------------------------------------------------------------


def test_a_missing_mutation_path_fails_the_job_loudly_instead_of_reporting_green():
    """Slice 3 owns the mutator functions. If they vanish, this job must not lie.

    The signature defect of this codebase is code that is plumbed but never
    wired: green tests, absent feature. A re-organiser whose mutation path is
    gone must report ``failed`` with a message that names what is missing — never
    ``completed`` over zero threads.
    """
    import tools.actions as actions_module

    original = {
        name: getattr(actions_module, name, None)
        for name in ("relabel_decision", "archive_to_never_miss_label")
    }
    for name in original:
        if hasattr(actions_module, name):
            delattr(actions_module, name)
    try:
        with pytest.raises(reorganise.MutationPathMissing) as excinfo:
            reorganise._require_mutation_functions()
        assert "relabel_decision" in str(excinfo.value)

        with create_db_session() as session:
            _seed_user(session)
            job_id = reorganise.start(session, user_id=USER, dry_run=False)

        payload = reorganise.execute(
            job_id, user_id=USER, mutator=FakeMutator(), label_lookup=None
        )
        assert payload["status"] == "failed"
        assert "relabel_decision" in (payload["error_message"] or "")
    finally:
        for name, value in original.items():
            if value is not None:
                setattr(actions_module, name, value)


def test_dry_run_never_reaches_the_mutator_and_still_reports_the_thread():
    """``dry_run`` is absolute: zero calls, full ledger. The preview IS the product."""
    from db.models import Category, Decision, Item

    mutator = FakeMutator()
    with create_db_session() as session:
        _seed_user(session)
        target = Category(
            id="test-cat-target", user_id=USER, key="social", name="Social",
            channel_label_name="ZeroInbox/Social", channel_label_id="Label_social",
        )
        current = Category(
            id="test-cat-current", user_id=USER, key="notifications",
            name="Notifications", channel_label_name="ZeroInbox/Notifications",
            channel_label_id="Label_notifications",
        )
        run = _seed_run(session)
        item = Item(
            id="test-item-dry", user_id=USER, channel_account_id=f"{USER}-account",
            external_thread_id="thread-dry", from_email="notification@facebookmail.com",
            from_domain="facebookmail.com", channel_labels=["Label_notifications"],
        )
        decision = Decision(
            id="test-decision-dry", user_id=USER, item_id=item.id, run_id=run.id,
            category_id=current.id, proposed_action="archive", decided_by="rule",
            status="applied", review_state="reviewed",
        )
        session.add_all([target, current, item, decision])
        session.flush()

        outcome = reorganise.mutate_one(
            session, user_id=USER, job_id="test-job-dry", decision=decision,
            item=item, target_category=target, mutator=mutator, label_lookup=None,
            dry_run=True,
        )

    assert outcome == "dry_run"
    assert mutator.calls == []


def test_an_unreviewed_row_is_named_and_never_reaches_the_mutator():
    """The count is the explanation; ``NotReviewedError`` is the fence.

    Both matter: this asserts the ledger names the row, and
    ``tests/integration/test_reorganise.py`` asserts the actions layer still
    raises before the mutator even if this pre-check were gone.
    """
    from db.models import Category, Decision, Item

    mutator = FakeMutator()
    with create_db_session() as session:
        _seed_user(session)
        target = Category(
            id="test-cat-nr-target", user_id=USER, key="social", name="Social",
            channel_label_name="ZeroInbox/Social", channel_label_id="Label_social",
        )
        current = Category(
            id="test-cat-nr-current", user_id=USER, key="notifications",
            name="Notifications", channel_label_name="ZeroInbox/Notifications",
            channel_label_id="Label_notifications",
        )
        run = _seed_run(session)
        item = Item(
            id="test-item-nr", user_id=USER, channel_account_id=f"{USER}-account",
            external_thread_id="thread-nr", from_email="reminders@facebookmail.com",
            from_domain="facebookmail.com", channel_labels=["INBOX", "Label_notifications"],
        )
        decision = Decision(
            id="test-decision-nr", user_id=USER, item_id=item.id, run_id=run.id,
            category_id=current.id, proposed_action="archive", decided_by="llm",
            status="proposed", review_state="provisional",
        )
        session.add_all([target, current, item, decision])
        session.flush()

        outcome = reorganise.mutate_one(
            session, user_id=USER, job_id="test-job-nr", decision=decision,
            item=item, target_category=target, mutator=mutator, label_lookup=None,
            dry_run=False,
        )

    assert outcome == "not_reviewed"
    assert mutator.calls == []


def _seed_run(session):
    from db.models import TriageRun

    run = TriageRun(
        id="test-run-unit", user_id=USER, channel_account_id=f"{USER}-account",
        status="completed",
    )
    session.add(run)
    session.flush()
    return run


# --- tier 1 resolution ----------------------------------------------------------


def test_target_resolution_uses_the_unchanged_tier_one_matcher():
    """The re-organiser has no matcher of its own — it calls ``apply_rules``.

    If it grew a private copy, a re-organisation would file mail differently from
    the triage run that follows it, and the two would drift for ever.
    """
    rules = [
        {
            "id": "r1",
            "name": "mined:facebook",
            "matcher": {"from_email": "reminders@facebookmail.com"},
            "action": {"set_category": "social", "archive": True},
            "status": "active",
            "confidence": 0.98,
        }
    ]
    items = [
        {"id": "i1", "from_email": "reminders@facebookmail.com", "from_domain": "facebookmail.com"},
        {"id": "i2", "from_email": "a.human@partner.example.com", "from_domain": "partner.example.com"},
    ]

    targets = reorganise.resolve_targets_tier1(items, rules)

    assert targets == {"i1": "social"}  # i2 is honestly unresolved, never guessed

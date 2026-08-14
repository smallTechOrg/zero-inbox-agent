"""``apply_run_decisions`` — the load-bearing apply pass (Phase 7, slice 2).

Run ``fbeed060`` applied **zero** of its 615 archive proposals and still reported
itself ``completed``: the old ``_auto_apply_decisions`` had two bare ``return``
statements that produced no ledger and no signal. These tests pin the replacement:

* the Phase 6 review gate binds and is never bypassed (``force`` is always ``False``,
  ``review_state`` is never written by this path);
* neither silent-abort path can ever again be silent;
* ``dry_run`` performs zero mutations;
* one thread's Gmail failure never blocks the rest.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from sqlalchemy import event


class FakeMutator:
    """Records every mutation. Optionally fails for specific thread ids."""

    def __init__(self, fail_threads: set[str] | None = None):
        self.archived: list[str] = []
        self._fail = fail_threads or set()

    def archive_and_label(self, thread_id: str, *, category_label_id: str) -> dict:
        if thread_id in self._fail:
            from channels.base import ChannelError

            raise ChannelError(f"gmail refused {thread_id}")
        self.archived.append(thread_id)
        return {"id": thread_id}

    def get_thread_labels(self, thread_id: str) -> list[str]:
        return ["INBOX"]


class FakeLabelLookup:
    def ensure_label(self, name: str) -> dict:
        return {"id": "Label_1", "name": name}


def _seed(
    *,
    rows: list[dict],
    run_id: str = "run7",
    user_id: str = "u7",
    dry_run: bool = False,
) -> None:
    """Seed one run with one decision per entry in ``rows``."""
    from db.models import (
        Category,
        ChannelAccount,
        Decision,
        Item,
        TriageRun,
        User,
        UserSettings,
    )
    from db.session import create_db_session

    with create_db_session() as session:
        session.add(User(id=user_id, email=f"{user_id}@example.com", display_name="U"))
        session.add(UserSettings(user_id=user_id))
        session.add(
            ChannelAccount(
                id="acct7",
                user_id=user_id,
                channel="gmail",
                account_email=f"{user_id}@gmail.com",
                refresh_token_enc="ENC",
                scopes=["gmail.readonly"],
                status="connected",
            )
        )
        session.add(
            Category(
                id="cat7",
                user_id=user_id,
                key="newsletters",
                name="Newsletters",
                description="",
                channel_label_name="ZeroInbox/Newsletters",
                default_action="archive",
                is_default=True,
                sort_order=1,
            )
        )
        session.add(
            TriageRun(
                id=run_id,
                user_id=user_id,
                channel_account_id="acct7",
                status="completed",
                dry_run=dry_run,
                items_total=len(rows),
                items_decided=len(rows),
            )
        )
        for index, spec in enumerate(rows):
            session.add(
                Item(
                    id=f"item{index}",
                    user_id=user_id,
                    channel_account_id="acct7",
                    external_thread_id=f"thread{index}",
                    external_message_ids=[f"msg{index}"],
                    subject=f"Subject {index}",
                    from_name="Sender",
                    from_email="s@example.com",
                    from_domain="example.com",
                    message_count=1,
                    snippet_redacted="x",
                    internal_date=datetime.now(timezone.utc),
                    is_unread=False,
                    channel_labels=["INBOX"],
                )
            )
            session.add(
                Decision(
                    id=f"dec{index}",
                    user_id=user_id,
                    item_id=f"item{index}",
                    run_id=run_id,
                    category_id="cat7",
                    proposed_action=spec.get("proposed_action", "archive"),
                    confidence=spec.get("confidence", 0.83),
                    reasoning="r",
                    decided_by=spec.get("decided_by", "llm"),
                    time_sensitive=False,
                    status=spec.get("status", "proposed"),
                    review_state=spec.get("review_state", "reviewed"),
                    autonomy_state=spec.get("autonomy_state", "auto_act"),
                )
            )
        session.commit()


@pytest.fixture
def review_state_writes(_isolated_db):
    """Every write to ``Decision.review_state`` anywhere in the process, recorded."""
    from db.models import Decision

    writes: list[tuple[str, str]] = []

    def _listener(target, value, oldvalue, initiator):
        writes.append((getattr(target, "id", "?"), value))
        return value

    event.listen(Decision.review_state, "set", _listener, retval=True)
    yield writes
    event.remove(Decision.review_state, "set", _listener)


@pytest.fixture
def apply_spy(monkeypatch):
    """Spy on ``tools.actions.apply_decision`` recording every call's kwargs."""
    import tools.actions as actions_module

    calls: list[dict] = []
    real = actions_module.apply_decision

    def _spy(session, user_id, decision_id, **kwargs):
        calls.append({"decision_id": decision_id, **kwargs})
        return real(session, user_id, decision_id, **kwargs)

    monkeypatch.setattr(actions_module, "apply_decision", _spy)
    return calls


def _patch_mutator(monkeypatch, mutator):
    from graph import nodes

    monkeypatch.setattr(
        nodes,
        "_build_mutator_for_user",
        lambda user_id, channel_account_id, session: (mutator, FakeLabelLookup()),
    )


def _apply(run_id: str = "run7", user_id: str = "u7", dry_run: bool = False) -> dict:
    from graph.nodes import apply_run_decisions

    return apply_run_decisions(
        run_id=run_id, user_id=user_id, channel_account_id="acct7", dry_run=dry_run
    )


# --------------------------------------------------------------- the review gate


def test_review_gate_binds_and_the_mutator_is_never_touched(
    _isolated_db, monkeypatch, review_state_writes
):
    """A provisional and a review_failed row are counted, never applied."""
    _seed(
        rows=[
            {"review_state": "provisional"},
            {"review_state": "review_failed"},
        ]
    )
    review_state_writes.clear()  # ignore the fixture's own seeding writes
    built = []

    def _never(*args, **kwargs):
        built.append(args)
        raise AssertionError("the mutator must never be built for un-reviewed rows")

    from graph import nodes

    monkeypatch.setattr(nodes, "_build_mutator_for_user", _never)

    ledger = _apply()

    assert ledger["not_reviewed"] == 2
    assert ledger["applied"] == 0
    assert built == []
    # Rule D3 — the apply path never upgrades review_state to get past the gate.
    assert review_state_writes == []


def test_force_is_never_true_and_review_state_is_never_written(
    _isolated_db, monkeypatch, apply_spy, review_state_writes
):
    _seed(rows=[{}, {}, {}])
    review_state_writes.clear()  # ignore the fixture's own seeding writes
    mutator = FakeMutator()
    _patch_mutator(monkeypatch, mutator)

    ledger = _apply()

    assert ledger["applied"] == 3
    assert len(apply_spy) == 3
    assert all(call["force"] is False for call in apply_spy), apply_spy
    assert all(call["dry_run"] is False for call in apply_spy)
    assert review_state_writes == []


def test_keep_proposals_are_never_archived(_isolated_db, monkeypatch, apply_spy):
    _seed(
        rows=[
            {"proposed_action": "keep", "autonomy_state": "category_keep"},
            {"proposed_action": "archive"},
        ]
    )
    mutator = FakeMutator()
    _patch_mutator(monkeypatch, mutator)

    ledger = _apply()

    assert ledger["applied"] == 1
    assert ledger["kept"] == 1
    assert mutator.archived == ["thread1"]


def test_only_auto_act_rows_are_applied(_isolated_db, monkeypatch):
    _seed(
        rows=[
            {"autonomy_state": "auto_act"},
            {"autonomy_state": "below_threshold"},
            {"autonomy_state": "held_by_never_miss"},
            {"autonomy_state": None},
            {"autonomy_state": "needs_your_call", "status": "needs_your_call"},
        ]
    )
    mutator = FakeMutator()
    _patch_mutator(monkeypatch, mutator)

    ledger = _apply()

    assert ledger["applied"] == 1
    assert ledger["below_threshold"] == 1
    assert ledger["needs_your_call"] == 1
    # held_by_never_miss and the NULL (unclassified) row both stay in the inbox.
    assert ledger["kept"] == 2
    assert ledger["distance_to_zero"] == 0


# ------------------------------------------------- the two silent-abort paths


def test_mutator_build_failure_populates_apply_failed_reason(_isolated_db, monkeypatch):
    _seed(rows=[{}, {}])
    from graph import nodes

    def _raise(*args, **kwargs):
        raise RuntimeError("refresh token rejected")

    monkeypatch.setattr(nodes, "_build_mutator_for_user", _raise)

    ledger = _apply()

    assert ledger["apply_failed_reason"] == "RuntimeError: refresh token rejected"
    assert ledger["applied"] == 0
    # The whole point: the number of threads still stuck in the inbox is reported.
    assert ledger["distance_to_zero"] == 2


def test_outer_failure_populates_apply_failed_reason(_isolated_db, monkeypatch):
    """The DB session factory raising inside apply_run_decisions is never silent."""
    _seed(rows=[{}, {}])
    from graph import nodes

    def _raise():
        raise RuntimeError("no database connection")

    monkeypatch.setattr(nodes, "_apply_session", _raise)

    ledger = _apply()

    assert ledger["apply_failed_reason"] == "RuntimeError: no database connection"
    assert ledger["applied"] == 0
    # distance_to_zero is re-read on its own session, so the failure path still
    # reports the true remaining count rather than a comforting zero.
    assert ledger["distance_to_zero"] == 2


def test_ledger_shape_is_always_complete(_isolated_db, monkeypatch):
    _seed(rows=[{}])
    from graph import nodes

    monkeypatch.setattr(
        nodes, "_apply_session", lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    ledger = _apply()

    assert set(ledger) == {
        "applied",
        "already_applied",
        "not_reviewed",
        "kept",
        "needs_your_call",
        "below_threshold",
        "failed",
        "failures",
        "distance_to_zero",
        "apply_failed_reason",
        "dry_run",
    }


# --------------------------------------------------------------------- dry run


def test_dry_run_performs_zero_mutations(_isolated_db, monkeypatch):
    _seed(rows=[{}, {}], dry_run=True)
    mutator = FakeMutator()
    built: list[str] = []

    from graph import nodes

    def _build(user_id, channel_account_id, session):
        built.append(user_id)
        return mutator, FakeLabelLookup()

    monkeypatch.setattr(nodes, "_build_mutator_for_user", _build)

    ledger = _apply(dry_run=True)

    assert ledger["dry_run"] is True
    assert ledger["applied"] == 0
    assert mutator.archived == []
    # Not even a credential is built while dry_run is on.
    assert built == []
    assert ledger["distance_to_zero"] == 2


# ------------------------------------------------------------ failure isolation


def test_one_gmail_failure_does_not_block_the_rest(_isolated_db, monkeypatch):
    _seed(rows=[{}, {}, {}])
    mutator = FakeMutator(fail_threads={"thread1"})
    _patch_mutator(monkeypatch, mutator)

    ledger = _apply()

    assert ledger["applied"] == 2
    assert ledger["failed"] == 1
    assert len(ledger["failures"]) == 1
    assert ledger["failures"][0]["decision_id"] == "dec1"
    assert "gmail refused thread1" in ledger["failures"][0]["error"]
    assert sorted(mutator.archived) == ["thread0", "thread2"]
    # The failed one is still in the inbox and still retryable.
    assert ledger["distance_to_zero"] == 1


def test_already_applied_rows_are_idempotent(_isolated_db, monkeypatch, apply_spy):
    _seed(rows=[{"status": "applied"}, {"status": "applied"}])
    mutator = FakeMutator()
    _patch_mutator(monkeypatch, mutator)

    ledger = _apply()

    assert ledger["already_applied"] == 2
    assert ledger["applied"] == 0
    assert apply_spy == []
    assert mutator.archived == []
    assert ledger["distance_to_zero"] == 0


def test_apply_progress_is_emitted(_isolated_db, monkeypatch):
    _seed(rows=[{}, {}])
    _patch_mutator(monkeypatch, FakeMutator())

    events: list[dict] = []
    import events.bus as bus_mod

    monkeypatch.setattr(
        bus_mod,
        "emit_apply_progress",
        lambda uid, **payload: events.append(payload),
    )

    ledger = _apply()

    assert ledger["applied"] == 2
    assert events and events[-1] == {
        "run_id": "run7",
        "applied": 2,
        "total_to_apply": 2,
        "failed": 0,
    }


def test_undo_token_exists_for_every_mutation(_isolated_db, monkeypatch):
    _seed(rows=[{}, {}])
    _patch_mutator(monkeypatch, FakeMutator())

    _apply()

    from db.models import ActionLog
    from db.session import create_db_session

    with create_db_session() as session:
        logs = session.query(ActionLog).all()
        assert len(logs) == 2
        assert all(log.undo_token for log in logs)
        assert {log.operation for log in logs} <= {"archive", "add_label", "remove_label"}


# ----------------------------------- the persisted remainder snapshot (ordering)


def _finalize(run_id: str = "run7", user_id: str = "u7") -> dict:
    from graph.nodes import finalize

    return finalize(
        {
            "run_id": run_id,
            "user_id": user_id,
            "channel_account_id": "acct7",
            "dry_run": False,
            "decisions": [],
            "counts": {"total": 2},
            "cost": {},
        }
    )


def _persisted_counts(run_id: str = "run7") -> dict:
    from db.models import TriageRun
    from db.session import create_db_session

    with create_db_session() as session:
        return dict(session.get(TriageRun, run_id).counts or {})


def test_persisted_remainder_never_reads_clean_after_a_failed_apply(
    _isolated_db, monkeypatch
):
    """Ordering defect: ``finalize`` computed the remainder ledger *before* the
    apply ledger was on the run row, and ``remainder_ledger`` re-reads
    ``run.counts["apply"]`` from the DB. The persisted snapshot therefore always
    carried ``apply_ok: true`` / ``apply_failed_reason: null`` even when the apply
    pass genuinely failed — the exact false-clean shape this phase exists to kill.
    It is exported to API clients via ``run_payload``'s ``"counts"``.
    """
    _seed(rows=[{}, {}])
    from graph import nodes

    def _raise(*args, **kwargs):
        raise RuntimeError("refresh token rejected")

    monkeypatch.setattr(nodes, "_build_mutator_for_user", _raise)

    _finalize()

    counts = _persisted_counts()
    assert counts["apply"]["apply_failed_reason"] == "RuntimeError: refresh token rejected"

    remainder = counts["remainder"]
    assert remainder["apply_ok"] is False
    assert remainder["apply_failed_reason"] == "RuntimeError: refresh token rejected"
    assert remainder["distance_to_zero"] == 2
    assert remainder["applied"] == 0


def test_persisted_remainder_reads_clean_after_a_successful_apply(
    _isolated_db, monkeypatch
):
    """The inverse: a genuinely successful apply must still persist as clean, so
    the fix above cannot be satisfied by hardcoding failure."""
    _seed(rows=[{}, {}])
    _patch_mutator(monkeypatch, FakeMutator())

    _finalize()

    counts = _persisted_counts()
    assert counts["apply"]["apply_failed_reason"] is None
    remainder = counts["remainder"]
    assert remainder["apply_ok"] is True
    assert remainder["apply_failed_reason"] is None
    assert remainder["distance_to_zero"] == 0
    assert remainder["applied"] == 2

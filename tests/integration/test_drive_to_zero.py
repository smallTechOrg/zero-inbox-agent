"""The load-bearing Phase 7 gate: the inbox actually shrinks, and says so honestly.

Two halves:

1. **Calibration, full data.** The measured ``fbeed060`` archive distribution
   (0 / 22 / 522 / 71 across ``>=0.95`` / ``0.90-0.94`` / ``0.80-0.89`` / ``0.75-0.79``,
   615 rows) is replayed through the real autonomy policy. At the new ``0.80`` default
   the answer is exactly **544** ``auto_act`` and **71** ``below_threshold``; at ``0.95``
   it is **0** — today's production behaviour, reproduced. The fixture is deliberately
   large enough that a sampled or short-circuited answer differs.
2. **A real run.** One real NVIDIA NIM run over the 25-thread tiered fixture with
   ``dry_run=False``, applied end to end through the **real** Phase 6 review gate, then
   the silent-abort regressions and the safety invariants.

**Deviation, deliberate:** the Gmail *transport* is a recording double. An automated
gate may not archive a real person's mail on every CI run; the real-Gmail coverage
lives in ``tests/integration/test_gmail_mutations.py`` (real mutations on a throwaway
thread) and ``tests/integration/test_apply_diagnosis.py`` (real credentials, read-only).
Everything above the transport — the review gate, the autonomy policy, ``force=False``,
the ledger, ``ActionLog`` + undo tokens, ``distance_to_zero`` — is real here.
"""

from __future__ import annotations

import time

import pytest
from sqlalchemy import event, select

from graph.runner import execute_triage

from tests.integration._threads_fixture import (
    ACCOUNT_ID,
    REPLIED_SENDERS,
    SMALL_TOTAL,
    USER_ID,
    build_threads_small,
    seed_user,
)

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------- 1. calibration


def _confidence_fixture() -> list[float]:
    """615 archive proposals in the measured ``fbeed060`` bands."""
    bands = [(0.96, 0), (0.92, 22), (0.84, 522), (0.77, 71)]
    values: list[float] = []
    for confidence, count in bands:
        values.extend([confidence] * count)
    assert len(values) == 615
    return values


def _classify_all(confidences: list[float], *, threshold: float) -> dict[str, int]:
    from graph.autonomy import classify_autonomy_state

    category = {
        "key": "newsletters",
        "name": "Newsletters",
        "default_action": "archive",
        "auto_act_threshold": None,
    }
    settings = {"auto_act_threshold": threshold, "confidence_floor": 0.75}
    counts: dict[str, int] = {}
    for confidence in confidences:
        state = classify_autonomy_state(
            {
                "proposed_action": "archive",
                "confidence": confidence,
                "status": "proposed",
                "decided_by": "llm",
                "time_sensitive": False,
            },
            category,
            settings,
            {},
            {},
            item={"from_email": "bulk@news.example"},
        )
        counts[state] = counts.get(state, 0) + 1
    return counts


def test_calibration_at_the_new_default_archives_544_of_615():
    counts = _classify_all(_confidence_fixture(), threshold=0.80)
    assert counts.get("auto_act") == 544
    assert counts.get("below_threshold") == 71
    assert sum(counts.values()) == 615


def test_calibration_at_095_archives_nothing_reproducing_production():
    counts = _classify_all(_confidence_fixture(), threshold=0.95)
    assert counts.get("auto_act", 0) == 0
    assert counts.get("below_threshold") == 615


# ------------------------------------------------------------------ the real run


class RecordingMutator:
    """Records archives. Stands in for the Gmail transport only."""

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


class RecordingLabels:
    def ensure_label(self, name: str) -> dict:
        return {"id": f"Label_{abs(hash(name)) % 1000}", "name": name}


_RUN: dict = {}


@pytest.fixture(scope="module")
def _nim_key():
    from config.settings import get_settings

    if not get_settings().nvidia_api_key.strip():
        pytest.fail(
            "AGENT_NVIDIA_API_KEY is not set in .env — this gate must run against the "
            "real NVIDIA NIM endpoint, never a stub."
        )


@pytest.fixture
def applied_run(monkeypatch, tmp_path_factory, _nim_key):
    """One real 25-thread run with dry_run=False, applied through the real gate."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import db.session as session_module
    from db.models import Base, Decision

    if "engine" not in _RUN:
        path = tmp_path_factory.mktemp("drive_to_zero") / "run.db"
        engine = create_engine(f"sqlite:///{path}")
        Base.metadata.create_all(engine)
        _RUN["engine"] = engine
        _RUN["factory"] = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    monkeypatch.setattr(session_module, "_engine", _RUN["engine"])
    monkeypatch.setattr(session_module, "_SessionLocal", _RUN["factory"])

    if "state" not in _RUN:
        import tools.actions as actions_module
        from db.session import create_db_session
        from graph import nodes

        with create_db_session() as session:
            seed_user(session)

        mutator = RecordingMutator()
        apply_calls: list[dict] = []
        review_writes: list[tuple[str, str]] = []
        real_apply = actions_module.apply_decision

        def _spy(session, user_id, decision_id, **kwargs):
            row = session.get(Decision, decision_id)
            apply_calls.append(
                {
                    "decision_id": decision_id,
                    "force": kwargs.get("force"),
                    "dry_run": kwargs.get("dry_run"),
                    "review_state_at_apply": getattr(row, "review_state", None),
                    "proposed_action_at_apply": getattr(row, "proposed_action", None),
                }
            )
            return real_apply(session, user_id, decision_id, **kwargs)

        # Only writes made INSIDE the apply pass are a violation; the never-miss
        # chain legitimately writes review_state on its way to `reviewed`.
        apply_window = {"active": False}

        def _review_listener(target, value, oldvalue, initiator):
            if apply_window["active"]:
                review_writes.append((getattr(target, "id", "?"), value))
            return value

        real_apply_run = nodes.apply_run_decisions

        def _tracked_apply_run(**kwargs):
            apply_window["active"] = True
            try:
                return real_apply_run(**kwargs)
            finally:
                apply_window["active"] = False

        monkeypatch.setattr(nodes, "apply_run_decisions", _tracked_apply_run)
        monkeypatch.setattr(actions_module, "apply_decision", _spy)
        monkeypatch.setattr(
            nodes,
            "_build_mutator_for_user",
            lambda user_id, channel_account_id, session: (mutator, RecordingLabels()),
        )

        emitted: list[dict] = []
        import events.bus as bus_mod

        real_emit = bus_mod.emit
        monkeypatch.setattr(
            bus_mod, "emit", lambda uid, evt: (emitted.append(evt), real_emit(uid, evt))[0]
        )

        started = time.monotonic()
        # The apply pass is what must not write review_state — listen only across it.
        event.listen(Decision.review_state, "set", _review_listener, retval=True)
        try:
            _RUN["state"] = execute_triage(
                user_id=USER_ID,
                channel_account_id=ACCOUNT_ID,
                items=build_threads_small(),
                dry_run=False,
            )
        finally:
            event.remove(Decision.review_state, "set", _review_listener)
        _RUN["elapsed_s"] = time.monotonic() - started
        _RUN["mutator"] = mutator
        _RUN["apply_calls"] = apply_calls
        _RUN["review_writes"] = review_writes
        _RUN["emitted"] = emitted
    return _RUN["state"]


def _ledger(state) -> dict:
    ledger = (state.get("counts") or {}).get("apply")
    assert ledger is not None, "finalize must always write counts['apply']"
    return ledger


def test_the_run_completes_and_writes_an_apply_ledger(applied_run):
    assert applied_run["status"] == "completed"
    ledger = _ledger(applied_run)
    assert ledger["dry_run"] is False
    assert ledger["apply_failed_reason"] is None
    assert ledger["applied"] > 0, "the whole point of Phase 7: the inbox must shrink"


def test_a_realistic_archive_reaches_applied_end_to_end(applied_run):
    """Gate 2: through the REAL review gate, with an undo token, out of the inbox."""
    from db.models import ActionLog, Decision, Item
    from db.session import create_db_session

    with create_db_session() as session:
        applied = list(
            session.execute(
                select(Decision).where(Decision.status == "applied")
            ).scalars()
        )
        assert applied, "no decision reached `applied` — this is the fbeed060 defect"
        for decision in applied:
            assert decision.review_state == "reviewed"
            assert decision.proposed_action in ("archive", "digest")
            assert decision.autonomy_state == "auto_act"
            logs = list(
                session.execute(
                    select(ActionLog).where(ActionLog.decision_id == decision.id)
                ).scalars()
            )
            assert len(logs) == 1
            assert logs[0].undo_token, "every mutation must stay undoable"
            assert logs[0].operation in ("archive", "add_label", "remove_label")
            item = session.get(Item, decision.item_id)
            labels = set(item.channel_labels or [])
            assert "INBOX" not in labels
            assert labels, "the thread must carry its category label"

    calls = _RUN["apply_calls"]
    assert calls
    assert all(call["review_state_at_apply"] == "reviewed" for call in calls)


def test_no_side_door_force_is_never_true_and_review_state_is_never_written(applied_run):
    """Gate 7 — the invariant that must never erode."""
    calls = _RUN["apply_calls"]
    assert calls
    assert all(call["force"] is False for call in calls), calls
    assert all(call["dry_run"] is False for call in calls)
    assert _RUN["review_writes"] == [], _RUN["review_writes"]


def test_keeps_are_never_force_archived(applied_run):
    """Gate 8 — every mutated decision proposed a leaving action at apply time."""
    calls = _RUN["apply_calls"]
    assert all(call["proposed_action_at_apply"] in ("archive", "digest") for call in calls)


def test_distance_to_zero_is_zero_on_a_healthy_run(applied_run):
    """Gate 3 — the assertion that makes a stalled apply impossible to miss."""
    from db.models import Decision
    from db.session import create_db_session
    from graph.nodes import distance_to_zero

    ledger = _ledger(applied_run)
    with create_db_session() as session:
        auto_act = len(
            list(
                session.execute(
                    select(Decision).where(Decision.autonomy_state == "auto_act")
                ).scalars()
            )
        )
    assert ledger["applied"] == auto_act
    assert ledger["distance_to_zero"] == 0
    assert distance_to_zero(applied_run["run_id"], USER_ID) == 0


def test_never_miss_still_binds_after_realignment(applied_run):
    """Gate 9 — no VIP, ever-replied or time-sensitive thread is ever archived."""
    from db.models import Decision, Item
    from db.session import create_db_session

    with create_db_session() as session:
        rows = list(
            session.execute(
                select(Decision, Item).join(Item, Item.id == Decision.item_id)
            )
        )
        for decision, item in rows:
            if decision.status != "applied":
                continue
            assert item.from_email not in REPLIED_SENDERS, item.from_email
            assert not decision.time_sensitive


def test_urgent_and_people_are_untouchable(applied_run):
    """Gate 10 — a `keep` category is never auto-acted on, at any confidence."""
    from db.models import Category, Decision
    from db.session import create_db_session

    with create_db_session() as session:
        keep_category_ids = {
            row.id
            for row in session.execute(
                select(Category).where(Category.default_action == "keep")
            ).scalars()
        }
        rows = list(session.execute(select(Decision)).scalars())
        for decision in rows:
            if decision.category_id in keep_category_ids:
                assert decision.proposed_action == "keep", decision.reasoning
                assert decision.autonomy_state != "auto_act"
                assert decision.status != "applied"


def test_every_action_log_is_non_destructive(applied_run):
    """Gate 16 — archive/label only, always undoable."""
    from db.models import ActionLog
    from db.session import create_db_session

    with create_db_session() as session:
        logs = list(session.execute(select(ActionLog)).scalars())
        assert logs
        assert {log.operation for log in logs} <= {"archive", "add_label", "remove_label"}
        assert all(log.undo_token for log in logs)


def test_remainder_arithmetic_holds(applied_run):
    """Gate 15 — inbox_remaining == sum(buckets) + distance_to_zero, unclassified 0."""
    from db.session import create_db_session
    from graph.remainder import remainder_ledger

    with create_db_session() as session:
        ledger = remainder_ledger(session, run_id=applied_run["run_id"], user_id=USER_ID)

    assert ledger["inbox_remaining"] == sum(ledger["remainder"].values()) + ledger[
        "distance_to_zero"
    ]
    assert ledger["remainder"]["unclassified"] == 0
    assert ledger["applied"] + ledger["inbox_remaining"] == SMALL_TOTAL


def test_inbox_zero_report_is_emitted(applied_run):
    types = [e.get("type") for e in _RUN["emitted"]]
    assert "auto_apply_complete" in types
    # A healthy run never raises the failure banner (gate 4/5 assert the inverse).
    assert "run_apply_failed" not in types


# ------------------------------------------- the silent-abort regressions (4 + 5)


def _seed_appliable_run(run_id: str, *, user_id: str = "uz", count: int = 3) -> None:
    from datetime import datetime, timezone

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
                id="acctZ",
                user_id=user_id,
                channel="gmail",
                account_email=f"{user_id}@gmail.com",
                refresh_token_enc="ENC",
                scopes=[],
                status="connected",
            )
        )
        session.add(
            Category(
                id="catZ",
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
                channel_account_id="acctZ",
                status="completed",
                dry_run=False,
                items_total=count,
                items_decided=count,
            )
        )
        for index in range(count):
            session.add(
                Item(
                    id=f"z{index}",
                    user_id=user_id,
                    channel_account_id="acctZ",
                    external_thread_id=f"zthread{index}",
                    external_message_ids=[f"zm{index}"],
                    subject=f"Subject {index}",
                    from_name="Sender",
                    from_email="bulk@news.example",
                    from_domain="news.example",
                    message_count=1,
                    snippet_redacted="x",
                    internal_date=datetime.now(timezone.utc),
                    is_unread=False,
                    channel_labels=["INBOX"],
                )
            )
            session.add(
                Decision(
                    id=f"zdec{index}",
                    user_id=user_id,
                    item_id=f"z{index}",
                    run_id=run_id,
                    category_id="catZ",
                    proposed_action="archive",
                    confidence=0.83,
                    reasoning="bulk",
                    decided_by="llm",
                    time_sensitive=False,
                    status="proposed",
                    review_state="reviewed",
                    autonomy_state="auto_act",
                )
            )
        session.commit()


def _finalize_with(monkeypatch, run_id: str, user_id: str = "uz") -> tuple[dict, list]:
    """Run `finalize` for a seeded completed run, capturing emitted events."""
    import events.bus as bus_mod
    from graph import nodes

    emitted: list[dict] = []
    monkeypatch.setattr(bus_mod, "emit", lambda uid, evt: emitted.append(evt))

    state = {
        "run_id": run_id,
        "user_id": user_id,
        "channel_account_id": "acctZ",
        "dry_run": False,
        "counts": {"total": 3},
        "cost": {"tokens_in": 0, "tokens_out": 0, "usd": 0.0, "llm_calls": 0},
        "decisions": [],
    }
    result = nodes.finalize(state)
    return result, emitted


def test_silent_abort_mutator_path_is_loud(_isolated_db, monkeypatch):
    """Gate 4 — a failed mutator build can never look like a successful run."""
    from db.models import Decision, TriageRun
    from db.session import create_db_session
    from graph import nodes

    _seed_appliable_run("runZ1")
    monkeypatch.setattr(
        nodes,
        "_build_mutator_for_user",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("refresh token rejected")),
    )

    result, emitted = _finalize_with(monkeypatch, "runZ1")
    ledger = result["counts"]["apply"]

    assert "RuntimeError: refresh token rejected" == ledger["apply_failed_reason"]
    assert ledger["applied"] == 0

    with create_db_session() as session:
        run = session.get(TriageRun, "runZ1")
        auto_act = len(
            list(
                session.execute(
                    select(Decision).where(
                        Decision.run_id == "runZ1", Decision.autonomy_state == "auto_act"
                    )
                ).scalars()
            )
        )
        assert run.error_message and "still in your inbox" in run.error_message
    assert ledger["distance_to_zero"] == auto_act == 3
    assert "run_apply_failed" in [e.get("type") for e in emitted]


def test_silent_abort_outer_path_is_loud(_isolated_db, monkeypatch):
    """Gate 5 — the same, with the apply session factory raising."""
    from db.models import TriageRun
    from db.session import create_db_session
    from graph import nodes

    _seed_appliable_run("runZ2")
    monkeypatch.setattr(
        nodes, "_apply_session", lambda: (_ for _ in ()).throw(RuntimeError("db gone"))
    )

    result, emitted = _finalize_with(monkeypatch, "runZ2")
    ledger = result["counts"]["apply"]

    assert ledger["apply_failed_reason"] == "RuntimeError: db gone"
    assert ledger["applied"] == 0
    assert ledger["distance_to_zero"] == 3
    with create_db_session() as session:
        run = session.get(TriageRun, "runZ2")
        assert run.error_message and "db gone" in run.error_message
    assert "run_apply_failed" in [e.get("type") for e in emitted]


def test_review_gate_is_not_weakened(_isolated_db, monkeypatch):
    """Gate 6 — a run whose reviewer pass failed applies exactly nothing."""
    from db.models import Decision
    from db.session import create_db_session
    from graph import nodes

    _seed_appliable_run("runZ3")
    with create_db_session() as session:
        for row in session.execute(
            select(Decision).where(Decision.run_id == "runZ3")
        ).scalars():
            row.review_state = "review_failed"
        session.commit()

    mutator = RecordingMutator()
    monkeypatch.setattr(
        nodes,
        "_build_mutator_for_user",
        lambda *a, **k: (mutator, RecordingLabels()),
    )
    ledger = nodes.apply_run_decisions(
        run_id="runZ3", user_id="uz", channel_account_id="acctZ", dry_run=False
    )

    assert ledger["not_reviewed"] == 3
    assert ledger["applied"] == 0
    assert mutator.archived == []
    assert ledger["distance_to_zero"] == 3


def test_dry_run_is_absolute(_isolated_db, monkeypatch):
    """Gate 12 — zero mutations, no failure banner, ledger.dry_run true."""
    from graph import nodes

    _seed_appliable_run("runZ4")
    mutator = RecordingMutator()
    monkeypatch.setattr(
        nodes, "_build_mutator_for_user", lambda *a, **k: (mutator, RecordingLabels())
    )

    import events.bus as bus_mod

    emitted: list[dict] = []
    monkeypatch.setattr(bus_mod, "emit", lambda uid, evt: emitted.append(evt))

    result = nodes.finalize(
        {
            "run_id": "runZ4",
            "user_id": "uz",
            "channel_account_id": "acctZ",
            "dry_run": True,
            "counts": {"total": 3},
            "cost": {"tokens_in": 0, "tokens_out": 0, "usd": 0.0, "llm_calls": 0},
            "decisions": [],
        }
    )
    ledger = result["counts"]["apply"]

    assert ledger["dry_run"] is True
    assert ledger["applied"] == 0
    assert mutator.archived == []
    assert "run_apply_failed" not in [e.get("type") for e in emitted]


def test_retry_apply_is_idempotent(_isolated_db, monkeypatch):
    """Gate 14 — a second apply pass mutates nothing and calls Gmail zero times."""
    from graph import nodes

    _seed_appliable_run("runZ5")
    first = RecordingMutator()
    monkeypatch.setattr(
        nodes, "_build_mutator_for_user", lambda *a, **k: (first, RecordingLabels())
    )
    ledger_one = nodes.apply_run_decisions(
        run_id="runZ5", user_id="uz", channel_account_id="acctZ", dry_run=False
    )
    assert ledger_one["applied"] == 3

    second = RecordingMutator()
    monkeypatch.setattr(
        nodes, "_build_mutator_for_user", lambda *a, **k: (second, RecordingLabels())
    )
    ledger_two = nodes.apply_run_decisions(
        run_id="runZ5", user_id="uz", channel_account_id="acctZ", dry_run=False
    )

    assert ledger_two["already_applied"] == 3
    assert ledger_two["applied"] == 0
    assert second.archived == []
    assert ledger_two["distance_to_zero"] == 0

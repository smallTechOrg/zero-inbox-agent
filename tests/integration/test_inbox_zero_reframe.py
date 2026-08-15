"""THE load-bearing Phase 9 gate: the inbox reaches actual zero, and nothing is lost.

`spec/roadmap.md` § Phase 9 → *Gate*, all fourteen numbered assertions.

The measured state this phase eliminates is **227 `held_by_never_miss` · 76
`category_keep` · 46 `below_threshold` · 16 `needs_your_call` · 0 `unclassified`
= 365**. Target: **0**. `tests/fixtures/phase9/remainder_365.py` replays that
distribution as **causes**, in full — 365 rows every time, never a sample. 227 is
not reachable from a sample, and neither is its 186/23/18 breakdown.

## Two legs, and why

1. **The reframe leg (365 rows).** The full remainder fixture is driven through
   the **real** never-miss chain, the **real** autonomy policy, the **real**
   review gate, the **real** persistence and the **real** apply pass. The
   reviewer's LLM call is a *deterministic double* here, for one reason: several
   of the fourteen assertions are about the reviewer behaving in a specific way
   (auditing a strict subset; failing outright), which is not something you can
   ask a real model for. Everything the phase actually builds is real.
2. **The real-model leg (25 threads).** One end-to-end `execute_triage` against
   the **real NVIDIA NIM endpoint** via `.env`, with the never-miss chain, the
   reviewer and the reframe all live. This follows the documented Phase 7
   precedent in `test_drive_to_zero.py`: the Gmail *transport* is a recording
   double, because an automated gate may not archive a real person's mail on
   every run; real-Gmail coverage lives in `test_gmail_mutations.py`.

Nothing in either leg passes `force=True`, and nothing writes `review_state`
outside `finalise_review` / `upgrade_review_state`.
"""

from __future__ import annotations

import pytest
from sqlalchemy import event, select

from tests.fixtures.phase9.remainder_365 import (
    ACCOUNT_EMAIL,
    ACCOUNT_ID,
    HELD_BY_DECIDED_BY,
    MEASURED_DISTRIBUTION,
    NO_REPLY_SENDERS,
    SETTINGS,
    TOTAL,
    USER_ID,
    build_remainder,
    classify_measured_distribution,
    fixed_sender_stats,
    legacy_sender_stats,
    phase9_decisions,
    pre_review,
    seed_remainder_items,
    seed_remainder_user,
)

pytestmark = pytest.mark.integration

RUN_ID = "run-reframe-365"
NEVER_MISS_LABELS = {"ZeroInbox/People", "ZeroInbox/Urgent", "ZeroInbox/Important"}


# --------------------------------------------------------------------------
# Doubles — the Gmail transport, and the reviewer's LLM call
# --------------------------------------------------------------------------


class RecordingMutator:
    """Stands in for the Gmail transport. Has no destructive verb, by design."""

    def __init__(self, fail: bool = False):
        self.archived: list[tuple[str, str]] = []
        self.restored: list[tuple] = []
        self.labels: dict[str, list[str]] = {}
        self.fail = fail

    def get_thread_labels(self, thread_id: str) -> list[str]:
        return list(self.labels.get(thread_id, ["INBOX"]))

    def archive_and_label(self, thread_id: str, *, category_label_id: str) -> dict:
        if self.fail:
            from channels.base import ChannelError

            raise ChannelError(f"gmail refused {thread_id}")
        self.archived.append((thread_id, category_label_id))
        self.labels[thread_id] = [category_label_id]
        return {"id": thread_id}

    def restore_labels(self, thread_id, *, add_label_ids, remove_label_ids) -> dict:
        self.restored.append((thread_id, tuple(add_label_ids), tuple(remove_label_ids)))
        self.labels[thread_id] = sorted(add_label_ids)
        return {"id": thread_id}

    def undo_archive_and_label(self, thread_id: str, *, category_label_id: str) -> dict:
        self.labels[thread_id] = ["INBOX"]
        return {"id": thread_id}


class RecordingLabels:
    def __init__(self) -> None:
        self.names: dict[str, str] = {}

    def ensure_label(self, name: str) -> dict:
        label_id = f"Label_{name.replace('/', '_')}"
        self.names[label_id] = name
        return {"id": label_id, "name": name}


def _reviewer_double(flip_ids: set[str], *, fail: bool = False, audit_only=None):
    """A deterministic stand-in for one reviewer batch.

    ``audit_only`` restricts what the reviewer will even look at, so the gate can
    assert that a strict subset of audited rows produces a strict subset of
    ``reviewed`` rows — assertion 6's second half, which cannot be asked of a
    real model.
    """

    def _review(decisions, items_by_id, **kwargs):
        if fail:
            return {}, [], True
        if audit_only is not None and not any(
            d["item_id"] in audit_only for d in decisions
        ):
            return {}, [], True
        return (
            {
                d["item_id"]: "The user would be upset to miss this."
                for d in decisions
                if d["item_id"] in flip_ids
            },
            [],
            False,
        )

    return _review


# --------------------------------------------------------------------------
# The reframe leg — one run over the full 365
# --------------------------------------------------------------------------


def _seed(session, items, run_id: str = RUN_ID):
    from db.models import TriageRun

    seed_remainder_user(session)
    seed_remainder_items(session, items)
    session.add(
        TriageRun(
            id=run_id, user_id=USER_ID, channel_account_id=ACCOUNT_ID,
            status="running", dry_run=False, items_total=TOTAL, items_decided=0,
        )
    )
    session.commit()


def _categories_for_state(session) -> list[dict]:
    from db.models import Category

    return [
        {
            "key": c.key,
            "name": c.name,
            "channel_label_name": c.channel_label_name,
            "default_action": c.default_action,
            "auto_act_threshold": c.auto_act_threshold,
        }
        for c in session.execute(
            select(Category).where(Category.user_id == USER_ID)
        ).scalars()
    ]


def _drive_the_chain(monkeypatch, *, dry_run=False, mutator=None, reviewer=None,
                     delete_category: str | None = None, run_id: str = RUN_ID):
    """Run the full Phase 9 pipeline over the 365, returning everything observed."""
    from db.models import Decision
    from db.session import create_db_session
    from graph import nodes, nodes_autonomy, nodes_review
    from graph.persistence import insert_provisional_decisions

    items, measured = build_remainder()
    decisions = phase9_decisions(items, measured)
    pre, flip_ids = pre_review(decisions)

    with create_db_session() as session:
        _seed(session, items, run_id)
        if delete_category:
            from db.models import Category

            session.delete(
                session.execute(
                    select(Category).where(
                        Category.user_id == USER_ID, Category.key == delete_category
                    )
                ).scalar_one()
            )
            session.commit()
        insert_provisional_decisions(
            session, run_id=run_id, user_id=USER_ID,
            channel_account_id=ACCOUNT_ID, items=items, decisions=pre,
        )
        session.commit()
        categories = _categories_for_state(session)

    monkeypatch.setattr(
        nodes_review, "review_archive_batch", reviewer or _reviewer_double(flip_ids)
    )

    state = {
        "run_id": run_id,
        "user_id": USER_ID,
        "channel_account_id": ACCOUNT_ID,
        "dry_run": dry_run,
        "items": items,
        "decisions": pre,
        "categories": categories,
        "settings": SETTINGS,
        "sender_stats": fixed_sender_stats(),
        "vip": {},
        "counts": {"total": TOTAL},
        "cost": {"tokens_in": 0, "tokens_out": 0, "usd": 0.0, "llm_calls": 0},
    }

    # force / review_state spies — assertion 12, watching the WHOLE run.
    import tools.actions as actions_module

    force_flags: list[object] = []
    review_writes: list[tuple] = []
    real_apply = actions_module.apply_decision
    real_never_miss = actions_module.archive_to_never_miss_label

    def _spy_apply(session, user_id, decision_id, **kwargs):
        force_flags.append(kwargs.get("force"))
        return real_apply(session, user_id, decision_id, **kwargs)

    def _spy_never_miss(session, user_id, decision_id, **kwargs):
        assert "force" not in kwargs
        return real_never_miss(session, user_id, decision_id, **kwargs)

    monkeypatch.setattr(actions_module, "apply_decision", _spy_apply)
    monkeypatch.setattr(actions_module, "archive_to_never_miss_label", _spy_never_miss)

    apply_window = {"active": False}

    def _review_listener(target, value, oldvalue, initiator):
        if apply_window["active"]:
            review_writes.append((getattr(target, "id", "?"), value))
        return value

    mutator = mutator or RecordingMutator()
    labels = RecordingLabels()
    monkeypatch.setattr(
        nodes, "_build_mutator_for_user", lambda *a, **k: (mutator, labels)
    )

    emitted: list[dict] = []
    import events.bus as bus_mod

    real_emit = bus_mod.emit
    monkeypatch.setattr(
        bus_mod, "emit", lambda uid, evt: (emitted.append(evt), real_emit(uid, evt))[0]
    )

    state.update(nodes_review.second_pass_reviewer(state))
    state.update(nodes_review.apply_never_miss_floor(state))
    state.update(nodes_autonomy.mark_autonomy_state(state))
    state.update(nodes.persist_decisions(state))

    event.listen(Decision.review_state, "set", _review_listener, retval=True)
    apply_window["active"] = True
    try:
        ledger = nodes.apply_run_decisions(
            run_id=run_id, user_id=USER_ID,
            channel_account_id=ACCOUNT_ID, dry_run=dry_run,
        )
    finally:
        apply_window["active"] = False
        event.remove(Decision.review_state, "set", _review_listener)

    return {
        "items": items,
        "measured": measured,
        "pre": pre,
        "flip_ids": flip_ids,
        "state": state,
        "ledger": ledger,
        "mutator": mutator,
        "labels": labels,
        "force_flags": force_flags,
        "review_writes": review_writes,
        "emitted": emitted,
    }


@pytest.fixture
def reframe(_isolated_db, monkeypatch):
    return _drive_the_chain(monkeypatch)


def _ledger(session, run_id: str = RUN_ID) -> dict:
    from graph.remainder import remainder_ledger

    return remainder_ledger(session, run_id=run_id, user_id=USER_ID)


# --------------------------------------------------------------------------
# 0. the fixture is the measured floor, reproduced by the real policy
# --------------------------------------------------------------------------


def test_the_fixture_replays_the_measured_365_through_the_real_policy():
    """227 / 76 / 46 / 16 / 0. Not asserted into existence — derived."""
    items, measured = build_remainder()
    assert len(items) == TOTAL == 365

    distribution = classify_measured_distribution(
        items, measured, sender_stats=legacy_sender_stats()
    )
    assert distribution == MEASURED_DISTRIBUTION, distribution
    assert sum(MEASURED_DISTRIBUTION.values()) == 365

    by_decided = {}
    for decision in measured:
        if decision.get("time_sensitive"):
            by_decided["llm"] = by_decided.get("llm", 0) + 1
        elif decision.get("decided_by") == "reviewer":
            by_decided["reviewer"] = by_decided.get("reviewer", 0) + 1
    by_decided["sender_history"] = sum(
        1 for i in items if i["from_email"] == ACCOUNT_EMAIL
    )
    assert by_decided == HELD_BY_DECIDED_BY


# --------------------------------------------------------------------------
# 1. Zero is real
# --------------------------------------------------------------------------


def test_1_the_run_reaches_actual_zero_with_no_human_intervention(reframe):
    from db.session import create_db_session

    with create_db_session() as session:
        ledger = _ledger(session)

    assert ledger["inbox_remaining"] == 0, ledger["remainder"]
    assert ledger["distance_to_zero"] == 0
    assert ledger["applied"] == TOTAL
    assert ledger["apply_ok"] is True
    assert reframe["ledger"]["failed"] == 0, reframe["ledger"]["failures"][:3]
    # No approval step, no manual sweep: the only calls made were the run's own.
    assert all(flag is False for flag in reframe["force_flags"])


# --------------------------------------------------------------------------
# 2. All 227 are labelled, not lost
# --------------------------------------------------------------------------


def test_2_every_never_miss_thread_is_applied_labelled_and_undoable(reframe):
    from db.models import ActionLog, Decision, Item
    from db.session import create_db_session

    labels = reframe["labels"].names

    with create_db_session() as session:
        held = list(
            session.execute(
                select(Decision).where(
                    Decision.run_id == RUN_ID,
                    Decision.autonomy_state == "held_by_never_miss",
                )
            ).scalars()
        )
        assert len(held) == 209, (
            "186 time-sensitive + 23 reviewer holds. The other 18 of the measured "
            "227 were the self-address bug and are no longer held at all."
        )
        for decision in held:
            assert decision.status == "applied", decision.reasoning
            log = session.execute(
                select(ActionLog).where(ActionLog.decision_id == decision.id)
            ).scalar_one()
            assert log.undo_token is not None
            assert log.operation == "archive"

            item = session.get(Item, decision.item_id)
            item_labels = set(item.channel_labels or [])
            assert "INBOX" not in item_labels
            assert {labels[label_id] for label_id in item_labels} <= NEVER_MISS_LABELS

        # Trust invariants, over the WHOLE run.
        logs = list(
            session.execute(select(ActionLog).where(ActionLog.user_id == USER_ID)).scalars()
        )
        assert len(logs) == TOTAL
        assert {log.operation for log in logs} <= {"archive", "add_label", "remove_label"}
        assert all(log.undo_token for log in logs)

    assert not any("TRASH" in str(label) for label in labels.values())
    for verb in ("trash", "delete", "spam", "report_spam"):
        assert not hasattr(reframe["mutator"], verb)


# --------------------------------------------------------------------------
# 3. Nothing is archived unlabelled
# --------------------------------------------------------------------------


def test_3_a_never_miss_thread_with_no_label_stays_in_the_inbox(_isolated_db, monkeypatch):
    """The user deleted `Important`. The 23 reviewer holds are NOT archived."""
    from db.models import Decision
    from db.session import create_db_session

    result = _drive_the_chain(monkeypatch, delete_category="important",
                              run_id="run-no-label")

    with create_db_session() as session:
        ledger = _ledger(session, "run-no-label")
        orphan_actions = [
            row.proposed_action
            for row in session.execute(
                select(Decision).where(
                    Decision.run_id == "run-no-label",
                    Decision.autonomy_state == "held_by_never_miss",
                    Decision.status != "applied",
                )
            ).scalars()
        ]

    assert ledger["remainder"]["no_never_miss_label"] == 23, ledger["remainder"]
    assert orphan_actions == ["keep"] * 23, (
        "with no never-miss label the thread must stay a keep — a bare archive "
        "of a never-miss thread is not a permitted operation"
    )
    assert ledger["inbox_remaining"] == 23
    assert result["ledger"]["failed"] == 0, "nothing was ATTEMPTED and failed"


# --------------------------------------------------------------------------
# 4 + 5. Correspondent truth
# --------------------------------------------------------------------------


def test_4_the_self_address_bug_is_dead(reframe):
    """18 threads from the account's own address produce zero reply-history holds."""
    from db.models import Decision, Item, SenderProfile
    from db.session import create_db_session
    from tools.never_miss import apply_reply_history_guard

    with create_db_session() as session:
        profile = session.execute(
            select(SenderProfile).where(
                SenderProfile.user_id == USER_ID,
                SenderProfile.sender_email == ACCOUNT_EMAIL,
            )
        ).scalar_one()
        assert profile.ever_replied is False
        assert profile.replied_count == 0

        self_items = [
            i for i in session.execute(
                select(Item).where(Item.from_email == ACCOUNT_EMAIL)
            ).scalars()
        ]
        assert len(self_items) == 18
        for item in self_items:
            decision = session.execute(
                select(Decision).where(
                    Decision.run_id == RUN_ID, Decision.item_id == item.id
                )
            ).scalar_one()
            assert decision.autonomy_state != "held_by_never_miss"
            assert decision.decided_by != "sender_history"
            assert decision.status == "applied"

    # Defence in depth: even a STALE profile row asserting `ever_replied` for the
    # account's own address must not hold the thread.
    guarded = apply_reply_history_guard(
        [{"item_id": "x", "proposed_action": "archive", "reasoning": ""}],
        {ACCOUNT_EMAIL: {"ever_replied": True}},
        [{"id": "x", "from_email": ACCOUNT_EMAIL}],
        account_email=ACCOUNT_EMAIL,
        aliases=[],
    )
    assert guarded[0]["proposed_action"] == "archive"


def test_5_no_reply_senders_never_claim_correspondence_but_stay_urgent_eligible(reframe):
    from db.models import Decision, Item, SenderProfile
    from db.session import create_db_session
    from graph.autonomy import URGENT_KEY, never_miss_category_key
    from tools.correspondents import is_no_reply

    labels = reframe["labels"].names

    with create_db_session() as session:
        for sender in NO_REPLY_SENDERS:
            # Three of the five measured senders are `no-reply` spellings; the two
            # facebookmail ones are not, and must NOT be forced to match — a
            # pattern set wide enough to catch them would catch real people.
            assert is_no_reply(sender) == (
                sender not in ("reminders@facebookmail.com", "security@facebookmail.com")
            ), sender
            profile = session.execute(
                select(SenderProfile).where(SenderProfile.sender_email == sender)
            ).scalar_one()
            assert profile.ever_replied is False

            item = session.execute(
                select(Item).where(Item.from_email == sender).limit(1)
            ).scalar_one()
            decision = session.execute(
                select(Decision).where(
                    Decision.run_id == RUN_ID, Decision.item_id == item.id
                )
            ).scalar_one()
            assert decision.autonomy_state == "held_by_never_miss"
            assert decision.status == "applied"
            item_labels = {labels[i] for i in (item.channel_labels or [])}
            assert item_labels == {"ZeroInbox/Urgent"}, (
                "a no-reply security alert is still time-sensitive; correspondent "
                "truth removes a false reply claim, never the mail's urgency"
            )

    assert (
        never_miss_category_key(
            {"time_sensitive": True},
            {"from_email": NO_REPLY_SENDERS[0]},
            vip={},
            sender_stats={},
        )
        == URGENT_KEY
    )


# --------------------------------------------------------------------------
# 6 + 7. The review gate is not vacuous
# --------------------------------------------------------------------------


def test_6_a_failing_reviewer_produces_zero_mutations(_isolated_db, monkeypatch):
    from db.models import Decision
    from db.session import create_db_session

    result = _drive_the_chain(
        monkeypatch, reviewer=_reviewer_double(set(), fail=True), run_id="run-fail"
    )

    assert result["mutator"].archived == [], "a failed review must mutate nothing"
    with create_db_session() as session:
        rows = list(
            session.execute(
                select(Decision).where(Decision.run_id == "run-fail")
            ).scalars()
        )
        assert len(rows) == TOTAL
        assert {row.review_state for row in rows} == {"review_failed"}
        assert not any(row.status == "applied" for row in rows)
    assert result["ledger"]["not_reviewed"] == TOTAL
    assert result["ledger"]["applied"] == 0


def test_6b_not_reviewed_raises_before_the_mutator_even_with_force(_isolated_db, monkeypatch):
    from db.models import Decision
    from db.session import create_db_session
    from tools.actions import NotReviewedError, apply_decision

    _drive_the_chain(
        monkeypatch, reviewer=_reviewer_double(set(), fail=True), run_id="run-fail2"
    )
    spy = RecordingMutator()
    with create_db_session() as session:
        row = session.execute(
            select(Decision).where(Decision.run_id == "run-fail2").limit(1)
        ).scalar_one()
        row.status = "approved"
        session.flush()
        with pytest.raises(NotReviewedError):
            apply_decision(
                session, USER_ID, row.id, mutator=spy,
                label_lookup=RecordingLabels(), dry_run=False, force=True,
            )
    assert spy.archived == [], "force=True has never been a way past the review gate"


def test_6c_only_the_audited_rows_become_reviewed(_isolated_db, monkeypatch):
    """A reviewer that audits a strict subset marks a strict subset `reviewed`."""
    from db.models import Decision
    from db.session import create_db_session

    items, measured = build_remainder()
    audited = {i["id"] for i in items[:50]}

    _drive_the_chain(
        monkeypatch,
        reviewer=_reviewer_double(set(), audit_only=audited),
        run_id="run-subset",
    )

    with create_db_session() as session:
        rows = list(
            session.execute(
                select(Decision).where(Decision.run_id == "run-subset")
            ).scalars()
        )
        reviewed = {r.item_id for r in rows if r.review_state == "reviewed"}
        assert 0 < len(reviewed) < TOTAL, (
            "a run-wide upgrade would make this equal to 365 — that is the vacuous "
            "gate this phase closed"
        )
        assert all(r.status != "applied" for r in rows if r.review_state != "reviewed")


def test_7_every_digest_proposal_is_audited(_isolated_db, monkeypatch):
    """`digest` reaches the same mutation as `archive`, so it must be reviewed."""
    from graph import nodes_review

    seen: list[str] = []

    def _record(decisions, items_by_id, **kwargs):
        seen.extend(d["item_id"] for d in decisions)
        return {}, [], False

    monkeypatch.setattr(nodes_review, "review_archive_batch", _record)
    state = {
        "run_id": "run-digest", "user_id": USER_ID, "settings": {},
        "decisions": [
            {"item_id": "d1", "proposed_action": "digest", "status": "proposed"},
            {"item_id": "d2", "proposed_action": "archive", "status": "proposed"},
            {"item_id": "d3", "proposed_action": "keep", "status": "proposed"},
        ],
        "items": [{"id": f"d{n}"} for n in (1, 2, 3)],
    }
    out = nodes_review.second_pass_reviewer(state)
    assert set(seen) == {"d1", "d2"}
    assert set(out["audited_item_ids"]) == {"d1", "d2"}

    from graph.nodes_review import REVIEWABLE_ACTIONS
    from tools.actions import MUTABLE_ACTIONS

    assert REVIEWABLE_ACTIONS == MUTABLE_ACTIONS


# --------------------------------------------------------------------------
# 8. The floor still binds
# --------------------------------------------------------------------------


def test_8_a_below_floor_thread_is_needs_your_call_and_is_not_archived_by_the_reframe():
    from graph.nodes_autonomy import mark_autonomy_state

    out = mark_autonomy_state(
        {
            "decisions": [
                {
                    "item_id": "low", "category": "notifications",
                    "proposed_action": "keep", "confidence": 0.40,
                    "reasoning": "unsure", "decided_by": "llm",
                    "time_sensitive": True, "status": "needs_your_call",
                }
            ],
            "items": [{"id": "low", "from_email": "x@example.com", "subject": "s"}],
            "categories": [
                {"key": "notifications", "name": "Notifications",
                 "default_action": "archive", "auto_act_threshold": None},
                {"key": "urgent", "name": "Urgent", "default_action": "keep",
                 "channel_label_name": "ZeroInbox/Urgent", "auto_act_threshold": None},
            ],
            "settings": SETTINGS, "sender_stats": {}, "vip": {},
        }
    )
    decision = out["decisions"][0]
    assert decision["autonomy_state"] == "needs_your_call"
    assert decision["proposed_action"] == "keep", (
        "the reframe archives mail the agent was confident MATTERS, never mail it "
        "was not confident about — even when that mail is time-sensitive"
    )
    assert "never_miss_label" not in decision


# --------------------------------------------------------------------------
# 9. NEVER_ARCHIVE_KEYS still holds — in the same run that archives into them
# --------------------------------------------------------------------------


@pytest.mark.parametrize("key", ["people", "urgent", "legal", "important"])
def test_9_never_archive_keys_still_reject_archive_while_receiving_never_miss_archives(
    key, reframe
):
    from db.models import Category
    from db.session import create_db_session
    from tools.taxonomy import NEVER_ARCHIVE_KEYS, TaxonomyError, create_category, update_category

    assert key in NEVER_ARCHIVE_KEYS

    with create_db_session() as session:
        with pytest.raises(TaxonomyError):
            create_category(session, USER_ID, key=f"{key}", name=key.title(),
                            default_action="archive")
        session.rollback()

        existing = session.execute(
            select(Category).where(Category.user_id == USER_ID, Category.key == key)
        ).scalar_one_or_none()
        if existing is not None:
            with pytest.raises(TaxonomyError):
                update_category(session, USER_ID, existing.id, default_action="archive")
            session.rollback()

    # ...and in the very same run, Urgent/Important received real archives.
    assert reframe["mutator"].archived


# --------------------------------------------------------------------------
# 10. Bulk undo
# --------------------------------------------------------------------------


def test_10_run_level_undo_returns_every_thread_with_its_exact_pre_triage_labels(reframe):
    from db.models import ActionLog, Item
    from db.session import create_db_session
    from tools.actions import undo_action

    mutator = reframe["mutator"]

    with create_db_session() as session:
        log_ids = [
            row.id
            for row in session.execute(
                select(ActionLog).where(ActionLog.user_id == USER_ID)
            ).scalars()
        ]
        assert len(log_ids) == TOTAL

    with create_db_session() as session:
        for log_id in log_ids:
            undo_action(session, USER_ID, log_id, mutator=mutator)
        session.commit()

        for item in session.execute(
            select(Item).where(Item.user_id == USER_ID)
        ).scalars():
            assert item.channel_labels == ["INBOX"], (
                "bulk undo restores the EXACT pre-triage label set"
            )

    calls_before = len(mutator.restored)
    with create_db_session() as session:
        for log_id in log_ids:
            undo_action(session, USER_ID, log_id, mutator=mutator)
        session.commit()
    assert len(mutator.restored) == calls_before, (
        "a second undo is an idempotent no-op and makes ZERO Gmail calls"
    )


# --------------------------------------------------------------------------
# 11. dry_run is absolute
# --------------------------------------------------------------------------


def test_11_dry_run_mutates_nothing_and_still_produces_the_whole_ledger(
    _isolated_db, monkeypatch
):
    from db.models import ActionLog
    from db.session import create_db_session

    result = _drive_the_chain(monkeypatch, dry_run=True, run_id="run-dry")

    assert result["mutator"].archived == []
    assert result["mutator"].restored == []
    assert result["ledger"]["dry_run"] is True
    assert result["ledger"]["applied"] == 0
    assert result["ledger"]["distance_to_zero"] == TOTAL, (
        "a dry run says honestly how far it WOULD have moved the inbox"
    )

    with create_db_session() as session:
        assert (
            session.execute(select(ActionLog).where(ActionLog.user_id == USER_ID))
            .scalars()
            .all()
            == []
        )
        ledger = _ledger(session, "run-dry")
    assert set(ledger["remainder"]) >= {"no_never_miss_label", "held_by_never_miss"}
    assert ledger["inbox_remaining"] == TOTAL


# --------------------------------------------------------------------------
# 12. No side door
# --------------------------------------------------------------------------


def test_12_force_is_never_true_and_review_state_is_never_written_at_apply_time(reframe):
    assert reframe["force_flags"], "the spy must have seen the auto_act applies"
    assert all(flag is False for flag in reframe["force_flags"])
    assert reframe["review_writes"] == [], reframe["review_writes"][:5]


def test_12b_a_run_wide_upgrade_to_reviewed_is_no_longer_expressible(_isolated_db):
    from db.session import create_db_session
    from graph.persistence import ReviewScopeError, upgrade_review_state

    with create_db_session() as session:
        with pytest.raises(ReviewScopeError):
            upgrade_review_state(session, run_id=RUN_ID, state="reviewed")


# --------------------------------------------------------------------------
# 13. Honesty
# --------------------------------------------------------------------------


def test_13_a_failing_mutator_reports_apply_ok_false_and_never_claims_zero(
    _isolated_db, monkeypatch
):
    from db.session import create_db_session
    from graph import nodes

    result = _drive_the_chain(
        monkeypatch, mutator=RecordingMutator(fail=True), run_id="run-broken"
    )
    assert result["ledger"]["applied"] == 0
    assert result["ledger"]["failed"] == TOTAL

    import events.bus as bus_mod

    emitted: list[dict] = []
    monkeypatch.setattr(bus_mod, "emit", lambda uid, evt: emitted.append(evt))
    finalized = nodes.finalize(
        {
            "run_id": "run-broken", "user_id": USER_ID,
            "channel_account_id": ACCOUNT_ID, "dry_run": False,
            "counts": {"total": TOTAL},
            "cost": {"tokens_in": 0, "tokens_out": 0, "usd": 0.0, "llm_calls": 0},
            "decisions": [],
        }
    )

    with create_db_session() as session:
        from db.models import TriageRun

        ledger = _ledger(session, "run-broken")
        error_message = session.get(TriageRun, "run-broken").error_message

    assert ledger["apply_ok"] is False
    assert ledger["inbox_remaining"] > 0
    assert error_message and "still in your inbox" in error_message
    assert "run_apply_failed" in [e.get("type") for e in emitted]
    assert "we reached zero" not in str(ledger).lower()
    assert finalized["counts"]["apply"]["applied"] == 0


# --------------------------------------------------------------------------
# 14. Ledger arithmetic
# --------------------------------------------------------------------------


def test_14_ledger_arithmetic_holds_and_unreviewed_applied_is_stated(reframe):
    from db.models import Decision
    from db.session import create_db_session
    from graph.remainder import REMAINDER_BUCKETS, unreviewed_applied_count

    with create_db_session() as session:
        ledger = _ledger(session)
        assert ledger["inbox_remaining"] == sum(ledger["remainder"].values()) + ledger[
            "distance_to_zero"
        ]
        assert set(ledger["remainder"]) == set(REMAINDER_BUCKETS)
        assert ledger["remainder"]["unclassified"] == 0
        assert ledger["applied"] + ledger["inbox_remaining"] == TOTAL

        # The `0008` figure, read the same way the migration counts it.
        migration_count = len(
            list(
                session.execute(
                    select(Decision).where(
                        Decision.user_id == USER_ID,
                        Decision.status == "applied",
                        Decision.review_state != "reviewed",
                    )
                ).scalars()
            )
        )
        assert ledger["unreviewed_applied"] == migration_count
        assert unreviewed_applied_count(session, user_id=USER_ID) == migration_count
        assert ledger["unreviewed_applied"] == 0, (
            "this run applied nothing unreviewed — the figure exists to report "
            "the 44 historic rows, not to excuse new ones"
        )


# --------------------------------------------------------------------------
# The real-model leg — the reframe end to end against NVIDIA NIM
# --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _nim_key():
    from config.settings import get_settings

    if not get_settings().nvidia_api_key.strip():
        pytest.fail(
            "AGENT_NVIDIA_API_KEY is not set in .env — this gate must run against "
            "the real NVIDIA NIM endpoint, never a stub."
        )


def test_the_reframe_holds_end_to_end_against_the_real_model(
    _isolated_db, monkeypatch, _nim_key
):
    """One real NIM run: never-miss holds leave the inbox under a never-miss label."""
    from db.models import ActionLog, Category, Decision, Item
    from db.session import create_db_session
    from graph import nodes
    from graph.runner import execute_triage
    from tests.integration._threads_fixture import (
        ACCOUNT_ID as FIX_ACCOUNT,
        USER_ID as FIX_USER,
        build_threads_small,
        seed_user,
    )

    with create_db_session() as session:
        seed_user(session)
        session.commit()

    mutator, labels = RecordingMutator(), RecordingLabels()
    monkeypatch.setattr(
        nodes, "_build_mutator_for_user", lambda *a, **k: (mutator, labels)
    )

    state = execute_triage(
        user_id=FIX_USER, channel_account_id=FIX_ACCOUNT,
        items=build_threads_small(), dry_run=False,
    )
    assert state["status"] == "completed", state.get("error")

    with create_db_session() as session:
        never_miss_ids = {
            row.id
            for row in session.execute(
                select(Category).where(
                    Category.user_id == FIX_USER,
                    Category.key.in_(["people", "urgent", "important"]),
                )
            ).scalars()
        }
        held = list(
            session.execute(
                select(Decision).where(
                    Decision.user_id == FIX_USER,
                    Decision.autonomy_state == "held_by_never_miss",
                )
            ).scalars()
        )
        assert held, "the 25-thread fixture always produces never-miss holds"

        labelled = [d for d in held if d.status == "applied"]
        assert labelled, (
            "PHASE 9's WHOLE POINT: a never-miss hold must leave the inbox under "
            "its label. Zero applied here is the pre-Phase-9 behaviour."
        )
        for decision in labelled:
            assert decision.category_id in never_miss_ids, (
                "a never-miss archive is always filed under a never-miss category"
            )
            log = session.execute(
                select(ActionLog).where(ActionLog.decision_id == decision.id)
            ).scalar_one()
            assert log.undo_token
            assert log.operation == "archive"
            item = session.get(Item, decision.item_id)
            item_labels = set(item.channel_labels or [])
            assert "INBOX" not in item_labels
            zero_inbox = {
                labels.names[i] for i in item_labels if i in labels.names
            }
            assert zero_inbox and zero_inbox <= NEVER_MISS_LABELS, zero_inbox

        logs = list(
            session.execute(select(ActionLog).where(ActionLog.user_id == FIX_USER)).scalars()
        )
        assert {log.operation for log in logs} <= {"archive", "add_label", "remove_label"}
        assert all(log.undo_token for log in logs)

        # Every applied row was genuinely reviewed — no vacuous gate.
        applied = list(
            session.execute(
                select(Decision).where(
                    Decision.user_id == FIX_USER, Decision.status == "applied"
                )
            ).scalars()
        )
        assert applied
        assert {d.review_state for d in applied} == {"reviewed"}

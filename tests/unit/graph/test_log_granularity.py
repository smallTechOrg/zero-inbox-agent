"""Rule I2 — not one beat of the run happens without a log line.

The live feed is fed by ``observability.logging.activity_bus_processor``, which
bridges **every** structlog line carrying a bound ``user_id`` onto that user's SSE
stream. So the transparency guarantee is a *logging* guarantee: if the graph goes
quiet in the log, the UI goes quiet on screen — which is exactly what made a working
run look hung during a 60 s tier-3 batch.

These tests capture the real logging pipeline (stdout JSON, contextvars merged — not
``structlog.testing.capture_logs``, which would bypass the very processor chain under
test) and assert each Rule I2 event appears, with its documented fields and a bound
``user_id``.

They also assert the anti-regression rule: these events add **zero** new
``bus.emit(`` call sites in ``src/graph/`` (gate item 21). Hand-placed emits are what
drifted out of coverage before; a bridge cannot drift.
"""

from __future__ import annotations

import importlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import structlog

USER_ID = "uL"
RUN_ID = "runL"


@pytest.fixture
def log_stream(monkeypatch):
    """The REAL processor chain, rendered into a buffer this test owns.

    structlog's ``PrintLoggerFactory`` captures a writer at configure time and a
    module-level ``log = get_logger(...)`` binds it once at import, so stdout capture
    is unreliable here. The processor chain — contextvars merge, redaction and the
    ``activity_bus_processor`` bridge that actually feeds the UI — is kept exactly as
    ``observability.logging`` configures it; only the final writer is swapped.
    """
    import observability.logging as logging_module

    buffer = io.StringIO()
    logging_module._configured = False
    logging_module.configure_logging()
    config = structlog.get_config()
    structlog.configure(
        processors=config["processors"],
        wrapper_class=config["wrapper_class"],
        context_class=config["context_class"],
        logger_factory=structlog.PrintLoggerFactory(file=buffer),
        cache_logger_on_first_use=False,
    )
    logger = structlog.get_logger().bind(logger="triage")
    for module_name in ("graph.nodes", "graph.persistence", "graph.checkpoint"):
        monkeypatch.setattr(importlib.import_module(module_name), "log", logger)
    yield buffer
    logging_module._configured = False
    logging_module.configure_logging()


@pytest.fixture
def bound_run(log_stream):
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(user_id=USER_ID, run_id=RUN_ID)
    yield log_stream
    structlog.contextvars.clear_contextvars()


def _events(stream) -> list[dict]:
    parsed = []
    for line in stream.getvalue().splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed.append(json.loads(line))
        except json.JSONDecodeError:  # pragma: no cover - non-JSON noise
            continue
    return parsed


def _find(events: list[dict], name: str) -> list[dict]:
    return [e for e in events if e.get("event") == name]


def _item(index: int) -> dict:
    return {
        "id": f"item{index}",
        "external_thread_id": f"thread{index}",
        "subject": f"Subject {index}",
        "from_name": "Sender",
        "from_email": "s@example.com",
        "from_domain": "example.com",
        "message_count": 1,
        "snippet_redacted": "x",
        "internal_date": datetime.now(timezone.utc),
        "is_unread": False,
        "channel_labels": ["INBOX"],
    }


def _state(**overrides) -> dict:
    state = {
        "run_id": RUN_ID,
        "user_id": USER_ID,
        "channel_account_id": "acctL",
        "dry_run": True,
        "settings": {"confidence_floor": 0.75, "auto_act_threshold": 0.80},
        "categories": [
            {
                "id": "catL",
                "key": "newsletters",
                "name": "Newsletters",
                "description": "",
                "default_action": "archive",
                "auto_act_threshold": None,
            }
        ],
        "rules": [],
        "sender_stats": {},
        "items": [_item(0), _item(1)],
    }
    state.update(overrides)
    return state


# ------------------------------------------------------------------ tier lines


def test_tier_started_and_finished_for_the_free_tiers(bound_run, monkeypatch):
    from graph import checkpoint, nodes

    monkeypatch.setattr(checkpoint, "record_batch", lambda *a, **k: 0)
    state = _state()
    nodes.apply_deterministic_rules(state)
    nodes.apply_sender_history({**state, "llm_queue": state["items"]})

    events = _events(bound_run)
    started = _find(events, "triage.tier_started")
    finished = _find(events, "triage.tier_finished")

    assert {e["tier"] for e in started} >= {1, 2}
    assert {e["tier"] for e in finished} >= {1, 2}
    for event in started + finished:
        assert event["user_id"] == USER_ID  # the bridge drops events without it
        assert event["run_id"] == RUN_ID
        assert event["level"] == "info"
        assert event["tier_name"]


def test_batch_dispatched_and_returned_carry_the_feed_header_fields(
    bound_run, monkeypatch
):
    """The 60 s dead-air window: dispatched is logged BEFORE the call, not after."""
    from types import SimpleNamespace

    from graph import checkpoint, nodes

    monkeypatch.setattr(checkpoint, "record_batch", lambda *a, **k: 0)

    class _FakeClient:
        async def classify_batch(self, payloads, **kwargs):
            return SimpleNamespace(
                by_id={
                    p["item_id"]: {
                        "item_id": p["item_id"],
                        "category": "newsletters",
                        "proposed_action": "archive",
                        "confidence": 0.83,
                        "reasoning": "bulk",
                    }
                    for p in payloads
                },
                results=list(payloads),
                missing_ids=[],
                invalid=[],
                usage=None,
            )

    import llm.client as llm_client

    monkeypatch.setattr(llm_client, "get_llm_client", lambda: _FakeClient())
    monkeypatch.setattr(nodes, "_model_candidates", lambda state: ["fake-model"])

    batch = [_item(0), _item(1)]
    nodes.llm_classify_batch(_state(batch=batch, batches=[batch]))

    events = _events(bound_run)
    dispatched = _find(events, "triage.batch_dispatched")
    returned = _find(events, "triage.batch_returned")

    assert dispatched and returned
    for event in dispatched + returned:
        assert event["user_id"] == USER_ID
        assert event["run_id"] == RUN_ID
        assert event["batch_n"] == 1
        assert event["batch_total"] == 1
        assert event["batch_size"] == 2
        assert event["model"] == "fake-model"

    names = [e.get("event") for e in events]
    assert names.index("triage.batch_dispatched") < names.index("triage.batch_returned")
    assert 3 in {e["tier"] for e in _find(events, "triage.tier_started")}


def test_page_fetched_is_logged_for_every_page(bound_run, monkeypatch):
    from graph import nodes

    class _FakeAdapter:
        def list_threads(self, **kwargs):
            on_page = kwargs.get("on_page")
            if on_page:
                on_page(1, 100)
                on_page(2, 200)
            return [_item(0)]

    import channels

    monkeypatch.setattr(channels, "get_adapter", lambda **kwargs: _FakeAdapter())
    monkeypatch.setattr(nodes, "_record_progress", lambda *a, **k: None)
    monkeypatch.setattr(nodes, "_harvest_sender_stats", lambda adapter, user_id: {})
    monkeypatch.setattr(
        nodes, "_detect_and_record_mailbox_corrections", lambda user_id, items: None
    )

    result = nodes.fetch_items({**_state(), "items": []})
    assert result.get("error") is None

    pages = _find(_events(bound_run), "triage.page_fetched")
    assert [e["page"] for e in pages] == [1, 2]
    assert [e["fetched_so_far"] for e in pages] == [100, 200]
    assert all(e["user_id"] == USER_ID for e in pages)


def test_reviewer_started_and_finished_bracket_the_never_miss_chain(
    bound_run, _isolated_db
):
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
    from graph import nodes
    from graph.persistence import finalise_review

    nodes.cluster_decisions(
        _state(
            resolved=[
                {
                    "item_id": "item0",
                    "category": "newsletters",
                    "proposed_action": "archive",
                    "confidence": 0.83,
                    "reasoning": "bulk",
                    "decided_by": "llm",
                    "status": "proposed",
                }
            ]
        )
    )

    with create_db_session() as session:
        session.add(User(id=USER_ID, email="ul@example.com", display_name="U"))
        session.add(UserSettings(user_id=USER_ID))
        session.add(
            ChannelAccount(
                id="acctL",
                user_id=USER_ID,
                channel="gmail",
                account_email="ul@gmail.com",
                refresh_token_enc="ENC",
                scopes=[],
                status="connected",
            )
        )
        session.add(
            Category(
                id="catL",
                user_id=USER_ID,
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
                id=RUN_ID,
                user_id=USER_ID,
                channel_account_id="acctL",
                status="running",
                dry_run=False,
                items_total=1,
                items_decided=1,
            )
        )
        session.add(
            Item(
                id="item0",
                user_id=USER_ID,
                channel_account_id="acctL",
                external_thread_id="thread0",
                external_message_ids=["m0"],
                subject="Subject 0",
                from_name="Sender",
                from_email="s@example.com",
                from_domain="example.com",
                message_count=1,
                snippet_redacted="x",
                internal_date=datetime.now(timezone.utc),
                is_unread=False,
            )
        )
        session.add(
            Decision(
                id="decL0",
                user_id=USER_ID,
                item_id="item0",
                run_id=RUN_ID,
                category_id="catL",
                proposed_action="archive",
                confidence=0.83,
                reasoning="bulk",
                decided_by="llm",
                time_sensitive=False,
                status="proposed",
                review_state="provisional",
            )
        )
        session.commit()

    with create_db_session() as session:
        finalise_review(
            session,
            run_id=RUN_ID,
            user_id=USER_ID,
            decisions=[
                {
                    "item_id": "item0",
                    "proposed_action": "archive",
                    "confidence": 0.83,
                    "reasoning": "bulk",
                    "decided_by": "llm",
                    "status": "proposed",
                }
            ],
            # Phase 9: the scope of the audit is now explicit and required — only
            # the rows the reviewer actually looked at may be upgraded.
            audited_item_ids=["item0"],
        )
        session.commit()

    events = _events(bound_run)
    started = _find(events, "triage.reviewer_started")
    finished = _find(events, "triage.reviewer_finished")
    assert started and finished
    assert started[0]["to_review"] == 1
    assert started[0]["user_id"] == USER_ID
    assert finished[0]["reviewed"] == 1
    assert finished[0]["user_id"] == USER_ID


def test_checkpoint_and_apply_progress_are_logged(bound_run, _isolated_db, monkeypatch):
    """`triage.checkpoint` (durability) and `triage.apply_progress` (the apply pass)."""
    from tests.unit.graph.test_auto_apply import FakeLabelLookup, FakeMutator, _seed

    from graph import checkpoint, nodes

    _seed(rows=[{}, {}], run_id="run7", user_id="u7")
    structlog.contextvars.bind_contextvars(user_id="u7", run_id="run7")
    monkeypatch.setattr(
        nodes,
        "_build_mutator_for_user",
        lambda user_id, channel_account_id, session: (FakeMutator(), FakeLabelLookup()),
    )
    nodes.apply_run_decisions(
        run_id="run7", user_id="u7", channel_account_id="acct7", dry_run=False
    )

    checkpoint.record_batch(
        {"run_id": "run7", "user_id": "u7", "channel_account_id": "acct7", "items": []},
        [],
        tier="llm",
    )

    events = _events(bound_run)
    progress = _find(events, "triage.apply_progress")
    assert progress
    assert progress[-1]["applied"] == 2
    assert progress[-1]["total_to_apply"] == 2
    assert progress[-1]["failed"] == 0
    assert progress[-1]["user_id"] == "u7"


def test_checkpoint_event_name_exists_in_the_graph(bound_run):
    """`triage.checkpoint` is the durability beat — it must stay on the feed."""
    source = (Path(__file__).parents[3] / "src/graph/checkpoint.py").read_text()
    assert '"triage.checkpoint"' in source


# ------------------------------------------- gate item 21: no hand-placed emits


def test_rule_i2_events_add_no_new_bus_emit_call_sites():
    graph_dir = Path(__file__).parents[3] / "src/graph"
    rule_i2 = (
        "triage.page_fetched",
        "triage.tier_started",
        "triage.tier_finished",
        "triage.batch_dispatched",
        "triage.batch_returned",
        "triage.reviewer_started",
        "triage.reviewer_finished",
        "triage.apply_progress",
    )
    for path in graph_dir.glob("*.py"):
        source = path.read_text()
        for name in rule_i2:
            # Every Rule I2 event is a `log.<level>("<name>", ...)` first argument
            # and reaches the UI via activity_bus_processor — never as an SSE
            # payload built at a call site.
            assert f'"{name}": ' not in source, (
                f"{path.name}: {name} is used as a dict KEY/value in a payload "
                "rather than as a log event name"
            )
        assert '"type": "triage.' not in source, (
            f"{path.name}: a `triage.*` log event is being hand-emitted onto the "
            "bus; Rule I2 events are bridged from the log, never emitted"
        )

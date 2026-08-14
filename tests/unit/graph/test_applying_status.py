"""A run must not read `completed` while its apply pass is still in flight.

Found live on a real 40-thread run: `finalize` set `status="completed"` BEFORE
calling the apply pass, so for the whole duration of the apply a client polling
`GET /api/runs/{id}` saw a terminal run whose decisions were still unapplied —
`apply_ok: false`, `distance_to_zero: 5` — which renders the red "could not
archive" bar. On 40 threads that flashed for seconds; on a 2,000-thread inbox
the apply pass runs for minutes, so every SUCCESSFUL run would show a sustained
false failure exactly when the user is watching for the result.
"""

from __future__ import annotations

import pytest


RUN_ID = "run-applying"
USER_ID = "user-applying"


@pytest.fixture
def run_row(_isolated_db):
    from db.models import TriageRun
    from db.session import create_db_session

    with create_db_session() as session:
        session.add(
            TriageRun(
                id=RUN_ID, user_id=USER_ID, channel_account_id="acct-applying",
                status="running", dry_run=True, items_total=2, items_decided=0, counts={},
            )
        )

    def _status() -> str:
        with create_db_session() as session:
            return session.get(TriageRun, RUN_ID).status

    return _status


def test_status_is_applying_while_the_apply_pass_runs_then_completed(run_row, monkeypatch):
    """The whole point: observe the status from INSIDE the apply pass."""
    from graph import nodes

    seen: list[str] = []

    real_apply = nodes._auto_apply_decisions

    def _spy(state, counts):
        seen.append(run_row())  # what a polling client would see right now
        return real_apply(state, counts)

    monkeypatch.setattr(nodes, "_auto_apply_decisions", _spy)

    nodes.finalize(
        {
            "run_id": RUN_ID,
            "user_id": USER_ID,
            "channel_account_id": "acct-applying",
            "decisions": [],
            "counts": {"total": 2, "by_tier": {}, "needs_your_call": 0},
            "cost": {"tokens_in": 0, "tokens_out": 0, "usd": 0.0, "llm_calls": 0},
        }
    )

    assert seen == ["applying"], (
        f"during the apply pass a client saw {seen!r}; 'completed' there means a "
        "terminal run with unapplied decisions -> a false red failure bar"
    )
    assert run_row() == "completed"


def test_applying_is_not_treated_as_terminal_anywhere():
    """`applying` must stay non-terminal, or the UI stops polling mid-apply and
    the orphan sweep would ignore a killed apply pass."""
    from api.runs import APPLYING_STATUS, TERMINAL_STATUSES

    assert APPLYING_STATUS == "applying"
    assert APPLYING_STATUS not in TERMINAL_STATUSES


def test_an_applying_run_is_still_the_default_dashboard_view():
    """Its clusters already exist — losing the run for the whole apply window
    would blank the dashboard at the moment the user is watching."""
    from api.runs import APPLYING_STATUS
    from api.triage import _DEFAULT_VIEW_STATUSES

    assert APPLYING_STATUS in _DEFAULT_VIEW_STATUSES


def test_an_orphaned_applying_run_is_reconciled_not_stranded(run_row, _isolated_db):
    """A crash during apply must not leave a permanently 'applying' row."""
    from api import _reconcile_orphaned_runs
    from db.models import TriageRun
    from db.session import create_db_session

    with create_db_session() as session:
        session.get(TriageRun, RUN_ID).status = "applying"

    assert _reconcile_orphaned_runs() >= 1
    assert run_row() in ("resumable", "failed")

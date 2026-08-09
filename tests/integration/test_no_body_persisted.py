"""Hard privacy invariant: **no email body text is ever persisted**.

A real triage run is executed against the real NVIDIA NIM endpoint over threads that
each carry a full body and an oversized snippet. Afterwards every column of every row
in every table is scanned for the body marker.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect as sa_inspect, select

from graph.runner import execute_triage

from tests.integration._threads_fixture import ACCOUNT_ID, BODY_MARKER, USER_ID, build_threads, seed_user

SUBSET = 50  # one real LLM batch (<= MAX_BATCH) - still spans every persisted table.
# D13: was 60, which crossed the 50-item MAX_BATCH boundary and forced a second
# real LLM round-trip for no additional coverage — that's the single biggest
# redundant cost center in the default (non-slow) integration gate.


_RUN: dict = {}


@pytest.fixture
def completed_run(monkeypatch, tmp_path_factory):
    """One real run whose database every test in this module then scans."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import db.session as session_module
    from config.settings import get_settings
    from db.models import Base

    if not get_settings().nvidia_api_key.strip():
        pytest.fail("AGENT_NVIDIA_API_KEY is not set in .env — this gate needs the real API.")

    if "engine" not in _RUN:
        path = tmp_path_factory.mktemp("no_body") / "run.db"
        engine = create_engine(f"sqlite:///{path}")
        Base.metadata.create_all(engine)
        _RUN["engine"] = engine
        _RUN["factory"] = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    monkeypatch.setattr(session_module, "_engine", _RUN["engine"])
    monkeypatch.setattr(session_module, "_SessionLocal", _RUN["factory"])

    if "state" not in _RUN:
        from db.session import create_db_session

        with create_db_session() as session:
            seed_user(session)

        threads = build_threads()
        # A slice spanning newsletters, notifications, the LLM tail and secret-bearing mail.
        items = threads[:20] + threads[62:72] + threads[150:160] + threads[202:212]
        assert len(items) == SUBSET
        assert all(BODY_MARKER in item["body"] for item in items)

        state = execute_triage(
            user_id=USER_ID, channel_account_id=ACCOUNT_ID, items=items, dry_run=True
        )
        assert state["status"] == "completed"
        _RUN["state"] = state
    return _RUN["state"]


def _all_rows(session):
    from db.models import Base

    for mapper in Base.registry.mappers:
        cls = mapper.class_
        for row in session.execute(select(cls)).scalars():
            yield cls, row


class TestNoBodyReachesTheDatabase:
    def test_no_column_of_any_row_contains_body_text(self, completed_run):
        from db.session import create_db_session

        offenders = []
        with create_db_session() as session:
            for cls, row in _all_rows(session):
                for attr in sa_inspect(cls).mapper.column_attrs:
                    value = getattr(row, attr.key)
                    if BODY_MARKER in str(value):
                        offenders.append(f"{cls.__tablename__}.{attr.key}")
        assert offenders == []

    def test_no_table_declares_a_body_column(self, completed_run):
        from db.models import Base

        for mapper in Base.registry.mappers:
            names = {attr.key for attr in mapper.mapper.column_attrs}
            assert not {n for n in names if "body" in n.lower()}, mapper.class_.__tablename__

    def test_the_only_content_column_is_a_redacted_snippet_of_at_most_200_chars(
        self, completed_run
    ):
        from db.models import Item
        from db.session import create_db_session

        with create_db_session() as session:
            items = list(session.execute(select(Item)).scalars())
            assert len(items) == SUBSET
            for item in items:
                assert len(item.snippet_redacted) <= 200
                assert BODY_MARKER not in item.snippet_redacted

    def test_secrets_in_the_snippet_are_redacted_before_storage(self, completed_run):
        from db.models import Item
        from db.session import create_db_session

        with create_db_session() as session:
            snippets = " ".join(
                i.snippet_redacted for i in session.execute(select(Item)).scalars()
            )
        assert "sk-abcdefghijklmnopqrstuv" not in snippets
        assert "483920" not in snippets

    def test_reasoning_is_stored_but_never_quotes_the_body(self, completed_run):
        from db.models import Decision
        from db.session import create_db_session

        with create_db_session() as session:
            decisions = list(session.execute(select(Decision)).scalars())
            assert len(decisions) == SUBSET
            assert all(d.reasoning.strip() for d in decisions)
            assert all(BODY_MARKER not in d.reasoning for d in decisions)

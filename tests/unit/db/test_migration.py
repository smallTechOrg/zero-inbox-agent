"""The Alembic migration must reproduce the ORM metadata exactly, from empty."""

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from db.models import FORBIDDEN_COLUMN_SUBSTRINGS, Base

REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def migrated_engine(tmp_path, monkeypatch):
    """Run `alembic upgrade head` against a clean database file."""
    db_path = tmp_path / "migrated.db"
    monkeypatch.setenv("AGENT_DATABASE_URL", f"sqlite:///{db_path}")
    import config.settings as settings_module

    settings_module._settings = None

    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    command.upgrade(cfg, "head")

    engine = create_engine(f"sqlite:///{db_path}")
    yield engine
    engine.dispose()


def test_upgrade_head_creates_every_table_and_stamps_a_revision(migrated_engine):
    inspector = inspect(migrated_engine)
    tables = set(inspector.get_table_names())
    assert "alembic_version" in tables
    missing = {t.name for t in Base.metadata.sorted_tables} - tables
    assert missing == set(), f"migration is missing tables: {missing}"

    with migrated_engine.connect() as conn:
        revisions = [r[0] for r in conn.exec_driver_sql("SELECT version_num FROM alembic_version")]
    assert revisions and revisions[0], "alembic_version must hold a non-empty revision"


def test_migrated_columns_match_the_orm_metadata(migrated_engine):
    inspector = inspect(migrated_engine)
    for table in Base.metadata.sorted_tables:
        expected = {c.name for c in table.columns}
        actual = {c["name"] for c in inspector.get_columns(table.name)}
        assert actual == expected, f"{table.name} drifted: {expected ^ actual}"


def test_migrated_database_has_no_body_bearing_column(migrated_engine):
    inspector = inspect(migrated_engine)
    offenders = [
        f"{name}.{col['name']}"
        for name in inspector.get_table_names()
        for col in inspector.get_columns(name)
        if any(bad in col["name"].lower() for bad in FORBIDDEN_COLUMN_SUBSTRINGS)
    ]
    assert offenders == []
